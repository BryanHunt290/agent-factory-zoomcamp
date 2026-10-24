"""What the API may say about the engine: curated messages and sanitized text.

The domain records keep raw engine detail, which is right for operators: a
run's ``status_reason`` is "<ExceptionType>: <message>", and the message can
contain file paths, sandbox or Docker detail, or command output. None of
that may reach an API client. Two rules apply:

1. **Curated, never copied.** A run's ``status_reason`` is chosen from the
   fixed sentences below, keyed by the run's status and the *name* of the
   exception type (the message part is discarded). So are the summaries of
   blocked milestones and failed-run milestones, whose stored text quotes
   the same raw messages.
2. **Sanitized free text.** Text an agent wrote (milestone summaries,
   approval reasons) is returned with host paths, credential URLs, and known
   secret values replaced, as a second line of defence.

An approval's exact action (tool, category, and parameters) is returned
unchanged only after a disclosure check. A record containing a recognized
credential field, known secret, or host path is refused with a curated error;
redacting an exact action would hide what the person is approving. This is a
defence against recognized patterns, not a detector for every possible secret.
Text a person typed (project names, decision comments) is also returned as
typed: it is the client's own data, not something the system disclosed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from ..domain.approvals import ExactAction
from ..domain.milestones import ACTION_EXECUTION, HUMAN_DECISION, RUN_FINISHED, Milestone, MilestoneStatus
from ..domain.project import Run
from ..state import RunStatus
from .errors import INTERNAL_DETAIL, ApiError, ErrorCode
from .observability import BEARER_CREDENTIAL, JWT_SHAPED

AWAITING_APPROVAL = "Waiting for a decision on a protected action."
AWAITING_RECONCILIATION = (
    "Paused: an approved action may already have happened, and Agent Factory cannot tell. "
    "A person must confirm what happened before the run can continue."
)

BLOCKED_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "SandboxUnavailable": "Paused: the isolated environment for running code is unavailable. "
        "The run resumes once it is available again.",
        "SandboxInfrastructureError": "Paused: the isolated environment for running code reported a problem. "
        "The run resumes once it is fixed.",
        "SandboxCleanupError": "Paused: cleanup of the isolated environment could not be confirmed. "
        "The run resumes once it has been checked.",
        "ExecutionLedgerUnavailable": "Paused: the record of approved actions could not be reached. "
        "Nothing new was carried out; the run resumes once it is available again.",
    }
)
BLOCKED_DEFAULT = "Paused: code could not be run safely. The run resumes once the problem is fixed."

FAILED_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "AgentOutputError": "Stopped: an agent produced output that could not be used.",
        "ProviderError": "Stopped: the model provider reported an error.",
        "ToolError": "Stopped: a tool reported an error.",
        "PermissionDenied": "Stopped: a protected action was refused.",
        "PathViolation": "Stopped: an agent tried to use a file outside the project.",
        "WorkspaceError": "Stopped: the project workspace refused an operation.",
        "RequestRejected": "Stopped: the isolated environment refused an execution request.",
        "InvalidTransitionError": "Stopped: the workflow reached a step it could not continue from.",
        "GraphValidationError": "Stopped: the workflow definition is invalid.",
        "RetryBudgetExhausted": "Stopped: a retry budget ran out.",
    }
)
FAILED_DEFAULT = "Stopped because of an error."
EXHAUSTED_DEFAULT = "Stopped: a retry budget ran out."

_TYPE_NAME = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,99}):")


def _error_type(raw_reason: str) -> str | None:
    match = _TYPE_NAME.match(raw_reason or "")
    return match.group(1) if match else None


def run_status_reason(run: Run) -> str | None:
    """A safe, fixed sentence explaining the run's status; None when nothing needs explaining."""
    kind = _error_type(run.status_reason)
    if run.status is RunStatus.AWAITING_APPROVAL:
        return AWAITING_APPROVAL
    if run.status is RunStatus.AWAITING_RECONCILIATION:
        return AWAITING_RECONCILIATION
    if run.status is RunStatus.BLOCKED:
        return BLOCKED_REASONS.get(kind or "", BLOCKED_DEFAULT)
    if run.status is RunStatus.FAILED:
        return FAILED_REASONS.get(kind or "", FAILED_DEFAULT)
    if run.status is RunStatus.EXHAUSTED:
        return FAILED_REASONS.get(kind or "", EXHAUSTED_DEFAULT)
    return None


# ------------------------------------------------------------ free-text sanitizing

_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (BEARER_CREDENTIAL, "[token]"),
    (JWT_SHAPED, "[token]"),
    # A URL with credentials, or any database URL, in full.
    (re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)://\S+", re.IGNORECASE), "[url]"),
    (re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#@]*@\S*"), "[url]"),
    # Windows paths: UNC (\\server\share\...) and drive-letter (C:\..., C:/...).
    (re.compile(r"\\\\[^\s\\]+\\[^\s\"'<>|]*"), "[path]"),
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]*"), "[path]"),
    # Home-relative and absolute POSIX paths (/home/..., /workspace, /tmp/x), but not
    # fractions or words such as "3/4" and "pass/fail", nor the path part of a URL.
    (re.compile(r"(?<![\w.~:/\\-])~[\\/][^\s\"'<>|]*"), "[path]"),
    (re.compile(r"(?<![\w.~:/\\-])/[\w.@+-]+(?:/[\w.@+-]*)*"), "[path]"),
)

_SECRET_FIELD = re.compile(
    r"(?:password|passwd|api[_-]?key|(?:^|[_-])token(?:$|[_-])|access[_-]?token|refresh[_-]?token|"
    r"secret|credential|authorization|private[_-]?key|database[_-]?url|dsn|cookie)",
    re.IGNORECASE,
)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TextSanitizer:
    """Removes host paths, credential URLs, and known secret values from agent-written text."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secret_values if s}, key=len, reverse=True)

    def check_exact_action(self, action: ExactAction, fingerprint: str) -> None:
        """Refuse unsafe disclosure without editing any fingerprint-bound field."""
        if action.fingerprint != fingerprint or not self._safe_value(action.to_dict()):
            raise ApiError(ErrorCode.INTERNAL_ERROR, INTERNAL_DETAIL)

    def _safe_value(self, value: object) -> bool:
        if isinstance(value, str):
            return not any(secret in value for secret in self._secrets) and not any(
                pattern.search(value) for pattern, _ in _REPLACEMENTS
            )
        if isinstance(value, dict):
            return all(
                self._safe_value(key)
                and (not _SECRET_FIELD.search(key) or key.lower().endswith(("_ref", "_reference")))
                and self._safe_value(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return all(self._safe_value(item) for item in value)
        return True

    def __call__(self, text: str, *, limit: int) -> str:
        for value in self._secrets:
            text = text.replace(value, "[redacted]")
        for pattern, replacement in _REPLACEMENTS:
            text = pattern.sub(replacement, text)
        return clip(text, limit)


_ACTION_SUMMARIES = {
    MilestoneStatus.IN_PROGRESS: "Running an approved action: {tool}.",
    MilestoneStatus.COMPLETED: "An approved action completed: {tool}.",
    MilestoneStatus.FAILED: "An approved action did not take effect: {tool}.",
    MilestoneStatus.BLOCKED: "Action outcome unknown: {tool} was started and Agent Factory cannot tell whether it "
    "happened. A person must check before the run continues.",
}


def _action_summary(m: Milestone, sanitize: TextSanitizer) -> str:
    """Curated from the milestone's status and tool name: never the action's parameters or raw errors."""
    tool = m.metadata.get("tool")
    tool = sanitize(tool, limit=64) if isinstance(tool, str) and tool else "an action"
    template = _ACTION_SUMMARIES.get(m.status, "An approved action: {tool}.")
    return template.format(tool=tool)


def milestone_summary(m: Milestone, sanitize: TextSanitizer) -> str:
    """The summary shown for a milestone: curated where the stored text quotes engine errors."""
    if m.status is MilestoneStatus.BLOCKED and "blocked" in m.metadata:
        return "Blocked. " + BLOCKED_REASONS.get(str(m.metadata.get("blocked")), BLOCKED_DEFAULT)
    if m.type == HUMAN_DECISION and m.status is MilestoneStatus.BLOCKED:
        agent = m.source.agent or "An agent"
        tool = m.metadata.get("tool")
        action = f"wants to run {tool}" if isinstance(tool, str) and tool else "wants to take a protected action"
        return sanitize(f"Approval needed: {agent} {action}", limit=1000)
    if m.type == ACTION_EXECUTION:
        return _action_summary(m, sanitize)
    if m.type == RUN_FINISHED and m.status is MilestoneStatus.FAILED:
        if m.metadata.get("run_status") == RunStatus.EXHAUSTED.value:
            return "The run stopped: a retry budget ran out."
        return "The run stopped because of an error."
    return sanitize(m.summary, limit=1000)

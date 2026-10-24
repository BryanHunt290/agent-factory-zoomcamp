"""Field types shared by the API models, each matching a schema in openapi.yaml.

Patterns use ``[0-9]`` where the contract writes ``\\d``: JSON Schema's ``\\d``
is ASCII-only, while Python and Pydantic would also accept other scripts'
digits. Text checks reuse the domain model's own validators, so the API
accepts exactly what the domain can store.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from ...domain.errors import DomainValidationError
from ...domain.serialization import check_text, check_timestamp

PROJECT_ID_PATTERN = r"^prj-[0-9a-f]{20}$"
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
MILESTONE_ID_PATTERN = r"^ms-[0-9a-f]{20}$"
APPROVAL_ID_PATTERN = r"^apr-[0-9a-f]{16}$"
DECISION_ID_PATTERN = r"^apd-[0-9a-f]{20}$"
EXECUTION_ID_PATTERN = r"^axe-[0-9a-f]{20}$"
WORKER_ID_PATTERN = r"^wkr-[0-9a-f]{20}$"
FINGERPRINT_PATTERN = r"^sha256:[0-9a-f]{64}$"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
MILESTONE_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
CURSOR_PATTERN = r"^[A-Za-z0-9_-]+$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_-]+$"


class ApiModel(BaseModel):
    """Closed, immutable API model: like the contract's schemas, unknown fields are refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestModel(ApiModel):
    """A request body. Strict: no type coercion, so ``1`` is never accepted as ``"1"``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_timestamp(value: str) -> str:
    try:
        return check_timestamp(value, "timestamp")
    except DomainValidationError:
        raise ValueError("not a real UTC date and time") from None


def _single_line(value: str) -> str:
    try:
        return check_text(value, "text", max_len=10_000)
    except DomainValidationError:
        raise ValueError("must contain a visible character and no control characters") from None


def _multi_line(value: str) -> str:
    try:
        return check_text(value, "text", max_len=10_000, allow_empty=True, multiline=True)
    except DomainValidationError:
        raise ValueError("must not contain control characters other than tabs and line breaks") from None


ProjectId = Annotated[str, StringConstraints(pattern=PROJECT_ID_PATTERN)]
RunId = Annotated[str, StringConstraints(pattern=RUN_ID_PATTERN)]
MilestoneId = Annotated[str, StringConstraints(pattern=MILESTONE_ID_PATTERN)]
ApprovalId = Annotated[str, StringConstraints(pattern=APPROVAL_ID_PATTERN)]
DecisionId = Annotated[str, StringConstraints(pattern=DECISION_ID_PATTERN)]
ExecutionId = Annotated[str, StringConstraints(pattern=EXECUTION_ID_PATTERN)]
WorkerId = Annotated[str, StringConstraints(pattern=WORKER_ID_PATTERN)]
ActionFingerprint = Annotated[str, StringConstraints(pattern=FINGERPRINT_PATTERN)]
TemplateName = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
MilestoneType = Annotated[str, StringConstraints(pattern=MILESTONE_TYPE_PATTERN)]
#: A UTC instant; normalised to the canonical six-digit form on the way in.
Timestamp = Annotated[str, StringConstraints(pattern=TIMESTAMP_PATTERN), AfterValidator(_canonical_timestamp)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=CURSOR_PATTERN)]

#: Request text: the contract's length limits first, then the domain's own text rules.
ProjectName = Annotated[str, StringConstraints(min_length=1, max_length=200), AfterValidator(_single_line)]
ProjectDescription = Annotated[str, StringConstraints(max_length=2000), AfterValidator(_multi_line)]
DecisionComment = Annotated[str, StringConstraints(max_length=1000), AfterValidator(_multi_line)]

RunStatus = Literal[
    "running", "awaiting_approval", "awaiting_reconciliation", "blocked", "completed", "failed", "exhausted"
]
ProjectStatus = Literal[
    "not_started", "running", "awaiting_approval", "awaiting_reconciliation", "blocked", "completed", "failed",
    "exhausted",
]
MilestoneStatus = Literal["planned", "in_progress", "completed", "failed", "blocked"]
MilestoneCategory = Literal["workflow", "deliverable", "approval", "lifecycle"]
ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "cancelled"]
DecisionOutcome = Literal["approved", "rejected"]
#: The contract's E5 vocabularies; a test keeps each equal to its domain enum.
ExecutionState = Literal["authorized", "dispatching", "succeeded", "failed", "uncertain", "lapsed"]
ReconciliationChoice = Literal["it_happened", "did_not_happen", "did_not_happen_retry"]
AttentionKind = Literal["decision_required", "reconciliation_required"]
#: Notification status (docs/notifications.md); tests keep both equal to their domain enums.
NotificationState = Literal["queued", "sending", "retrying", "sent", "failed", "skipped"]
NotificationFailure = Literal[
    "timeout", "provider_unavailable", "rate_limited", "outcome_unknown", "invalid_destination",
    "provider_rejected_configuration", "unsupported_destination", "max_attempts", "attention_resolved",
    "destination_disabled", "recipient_not_permitted",
]
TimelineKind = Literal[
    "approval_requested", "approved", "rejected", "expired", "action_dispatching", "retry_dispatched",
    "action_succeeded", "action_failed", "action_not_dispatched", "outcome_uncertain", "reconciliation_required",
    "reconciliation_decided", "retry_authorized", "resolved",
]  # fmt: skip
ActorKind = Literal["unauthenticated", "user", "system"]
#: The contract's Permission enum; a test keeps it equal to domain.principals.Permission.
PermissionName = Literal[
    "projects:read", "projects:create", "runs:read", "milestones:read", "approvals:read", "approvals:decide",
    "actions:reconcile",
]
ActionCategory = Literal[
    "read_workspace",
    "write_workspace",
    "execute_local",
    "execute_sandboxed",
    "destructive_delete",
    "spend_money",
    "production_deploy",
    "credential_change",
    "external_communication",
    "network_access",
    "cloud_read",
]

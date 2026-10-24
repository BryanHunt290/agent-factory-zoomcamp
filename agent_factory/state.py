"""Project state: the structured record that agents read and write.

Agents never see a chat transcript. Each one receives a snapshot of this
state and returns updates to the fields it owns (``plan``, ``implementation``,
``test_report``, ``review``). The executor owns everything else: position in
the graph, retry counters, approvals, errors.

The state is plain JSON-serializable data so it can be checkpointed to disk
and reloaded by a different process.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 3
#: Older schema versions that from_dict() upgrades on load.
_UPGRADABLE = {1, 2}

#: Fields that agents may write via ``AgentResult.updates``.
AGENT_OUTPUT_FIELDS = frozenset({"plan", "implementation", "test_report", "review"})


class StateError(ValueError):
    """A persisted state document could not be loaded."""


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    #: An approved side effect may already have happened, and a person must say whether it did
    #: before the run continues (docs/design/action-execution.md). Not ``blocked``: nothing is
    #: wrong with the infrastructure; human judgment is needed.
    AWAITING_RECONCILIATION = "awaiting_reconciliation"
    #: Execution could not happen safely (sandbox unavailable, unhealthy,
    #: misconfigured, or cleanup unverified). Resumable; consumes no retries.
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.EXHAUSTED)


@dataclass
class TaskSpec:
    """What the project is supposed to achieve."""

    title: str
    goal: str
    requirements: list[str] = field(default_factory=list)


@dataclass
class ProjectState:
    run_id: str
    graph_name: str
    task: TaskSpec
    current_node: str
    status: RunStatus = RunStatus.RUNNING
    step: int = 0

    # Agent-owned outputs.
    plan: dict[str, Any] | None = None
    implementation: dict[str, Any] | None = None
    test_report: dict[str, Any] | None = None
    review: dict[str, Any] | None = None

    # Executor-owned bookkeeping.
    node_visits: dict[str, int] = field(default_factory=dict)
    edge_traversals: dict[str, int] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    #: The uncertain action a person must reconcile (status awaiting_reconciliation).
    pending_reconciliation: dict[str, Any] | None = None
    blocked: dict[str, Any] | None = None
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectState:
        version = data.get("schema_version")
        if version in _UPGRADABLE:
            data = _upgrade(dict(data))
            version = data["schema_version"]
        if version != SCHEMA_VERSION:
            raise StateError(f"unsupported state schema_version {version!r} (expected {SCHEMA_VERSION})")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise StateError(f"unknown state fields: {unknown}")
        data = copy.deepcopy(data)
        try:
            data["task"] = TaskSpec(**data["task"])
            data["status"] = RunStatus(data["status"])
            return cls(**data)
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError(f"invalid state document: {exc}") from exc

    def snapshot(self) -> ProjectState:
        """An independent deep copy, handed to agents as read-only input."""
        return ProjectState.from_dict(self.to_dict())

    def last_history(self, node: str | None = None) -> dict[str, Any] | None:
        for entry in reversed(self.history):
            if node is None or entry["node"] == node:
                return entry
        return None


def _upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """Upgrade an older state document to the current schema."""
    if data.get("schema_version") == 1:
        # v2 added the resumable "blocked" status and its detail field.
        data.setdefault("blocked", None)
        data["schema_version"] = 2
    if data.get("schema_version") == 2:
        # v3 added the awaiting_reconciliation status and its detail field. An older engine
        # refuses v3 checkpoints, so it can never resume a run past an uncertain action.
        data.setdefault("pending_reconciliation", None)
        data["schema_version"] = 3
    return data

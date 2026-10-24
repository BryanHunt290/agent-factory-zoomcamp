"""Projects and runs.

A *project* is the thing a user hands to Agent Factory. It has a stable, opaque
identity and keeps its milestone policy (its own definition of progress). A
*run* is one execution of the workflow for that project; a project can have
many runs.

Run status reuses the engine's ``RunStatus`` and allows exactly the changes
the engine can make.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from ..state import RunStatus
from .errors import DomainValidationError, StatusTransitionError
from .ids import NAME_RE, PROJECT_ID_RE, RUN_ID_RE
from .milestones import MilestonePolicy
from .serialization import (
    RecordMixin,
    check_enum,
    check_fields,
    check_int,
    check_json_object,
    check_pattern,
    check_text,
    check_timestamp,
    open_envelope,
    parse_timestamp,
    seal,
)

#: Mirrors the executor: a running run can pause for approval or for
#: reconciliation, block on isolation, or finish; paused and blocked runs can
#: only go back to running; completed, failed, and exhausted runs are final.
RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = MappingProxyType(
    {
        RunStatus.RUNNING: frozenset(
            {
                RunStatus.AWAITING_APPROVAL,
                RunStatus.AWAITING_RECONCILIATION,
                RunStatus.BLOCKED,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.EXHAUSTED,
            }
        ),
        RunStatus.AWAITING_APPROVAL: frozenset({RunStatus.RUNNING}),
        RunStatus.AWAITING_RECONCILIATION: frozenset({RunStatus.RUNNING}),
        RunStatus.BLOCKED: frozenset({RunStatus.RUNNING}),
        RunStatus.COMPLETED: frozenset(),
        RunStatus.FAILED: frozenset(),
        RunStatus.EXHAUSTED: frozenset(),
    }
)


@dataclass(frozen=True)
class Project(RecordMixin):
    RECORD_TYPE = "project"
    SCHEMA_VERSION = 1

    id: str
    name: str
    created_at: str
    milestone_policy: MilestonePolicy
    template: str | None = None
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_pattern(self.id, PROJECT_ID_RE, "id")
        check_text(self.name, "name", max_len=200)
        object.__setattr__(self, "created_at", check_timestamp(self.created_at, "created_at"))
        if not isinstance(self.milestone_policy, MilestonePolicy):
            raise DomainValidationError("milestone_policy: expected a MilestonePolicy")
        if self.template is not None:
            check_pattern(self.template, NAME_RE, "template")
        check_text(self.description, "description", max_len=2000, allow_empty=True, multiline=True)
        object.__setattr__(self, "metadata", check_json_object(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, Any]:
        return seal(
            self.RECORD_TYPE,
            self.SCHEMA_VERSION,
            {
                "id": self.id,
                "name": self.name,
                "created_at": self.created_at,
                "milestone_policy": self.milestone_policy.to_dict(),
                "template": self.template,
                "description": self.description,
                "metadata": self.metadata,
            },
        )

    @classmethod
    def from_dict(cls, data: Any) -> Project:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(
            body,
            required=("id", "name", "created_at", "milestone_policy", "template", "description", "metadata"),
            where=cls.RECORD_TYPE,
        )
        return cls(**{**body, "milestone_policy": MilestonePolicy.from_dict(body["milestone_policy"])})


@dataclass(frozen=True)
class Run(RecordMixin):
    """One execution of a project's workflow. Identified by ``(project_id, run_id)``."""

    RECORD_TYPE = "run"
    SCHEMA_VERSION = 1

    project_id: str
    run_id: str
    number: int  # 1 for the project's first run, 2 for the second, ...
    graph_name: str
    status: RunStatus
    created_at: str
    updated_at: str
    status_reason: str = ""

    def __post_init__(self) -> None:
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.run_id, RUN_ID_RE, "run_id")
        check_int(self.number, "number", minimum=1)
        check_pattern(self.graph_name, NAME_RE, "graph_name")
        object.__setattr__(self, "status", check_enum(RunStatus, self.status, "status"))
        object.__setattr__(self, "created_at", check_timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", check_timestamp(self.updated_at, "updated_at"))
        if parse_timestamp(self.updated_at) < parse_timestamp(self.created_at):
            raise DomainValidationError("updated_at: earlier than created_at")
        check_text(self.status_reason, "status_reason", max_len=1000, allow_empty=True, multiline=True)

    @property
    def ref(self) -> str:
        """A globally unique reference: run IDs are only unique within a project."""
        return f"{self.project_id}/{self.run_id}"

    def transition(self, status: RunStatus, *, at: str, reason: str = "") -> Run:
        status = check_enum(RunStatus, status, "status")
        if status not in RUN_TRANSITIONS[self.status]:
            raise StatusTransitionError(f"run {self.ref}: {self.status.value} -> {status.value} is not allowed")
        return replace(self, status=status, updated_at=at, status_reason=reason)

    @staticmethod
    def check_successor(old: Run, new: Run) -> None:
        """Raise unless ``new`` is a legal next version of ``old`` (used by repositories)."""
        for name in ("project_id", "run_id", "number", "graph_name", "created_at"):
            if getattr(old, name) != getattr(new, name):
                raise DomainValidationError(f"run {old.ref}: field {name!r} cannot change")
        if new.status != old.status and new.status not in RUN_TRANSITIONS[old.status]:
            raise StatusTransitionError(f"run {old.ref}: {old.status.value} -> {new.status.value} is not allowed")

    def to_dict(self) -> dict[str, Any]:
        return seal(
            self.RECORD_TYPE,
            self.SCHEMA_VERSION,
            {
                "project_id": self.project_id,
                "run_id": self.run_id,
                "number": self.number,
                "graph_name": self.graph_name,
                "status": self.status.value,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "status_reason": self.status_reason,
            },
        )

    @classmethod
    def from_dict(cls, data: Any) -> Run:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(
            body,
            required=("project_id", "run_id", "number", "graph_name", "status", "created_at", "updated_at", "status_reason"),
            where=cls.RECORD_TYPE,
        )
        return cls(**body)

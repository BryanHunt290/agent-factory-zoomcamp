"""Milestones: human-facing progress, with project-defined meaning.

A *milestone* answers "what meaningful progress was made?". It is not a copy
of the execution log, which keeps every low-level step for audit and
debugging.

Project-specific meaning comes from configuration, not core code:

* a :class:`MilestoneCatalog` is the project's vocabulary of milestone types
  (``ingestion_working``, ``api_implemented``, ``evaluation_passed``, ...);
* :class:`NodeMilestoneRule` entries say which workflow node, finishing with
  which outcome, reaches which milestone with which status;
* a :class:`MilestonePolicy` bundles both and is stored with the project.

Two system types always exist because the engine itself produces them:
``human_decision`` (a protected action awaited a decision) and
``run_finished`` (a run reached a final state). A third engine type,
``action_execution`` (what happened when an approved action was carried
out), was added later: new catalogs include it, and catalogs stored before it
existed resolve it from :data:`ENGINE_DEFINITIONS`, so their projects keep
loading unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .errors import DomainValidationError, StatusTransitionError
from .ids import KEY_RE, MILESTONE_ID_RE, PROJECT_ID_RE, RUN_ID_RE, TYPE_RE
from .serialization import (
    RecordMixin,
    check_enum,
    check_fields,
    check_int,
    check_json_object,
    check_optional_text,
    check_pattern,
    check_text,
    check_timestamp,
    open_envelope,
    parse_timestamp,
    seal,
)


class MilestoneStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"

    @property
    def is_terminal(self) -> bool:
        return self in (MilestoneStatus.COMPLETED, MilestoneStatus.FAILED)


#: Legal status changes. Completed and failed are final: a retry is a *new*
#: milestone that can point back at the failure it resolves, so failures stay
#: visible in history. Blocked is temporary and can be unblocked.
MILESTONE_TRANSITIONS: Mapping[MilestoneStatus, frozenset[MilestoneStatus]] = MappingProxyType(
    {
        MilestoneStatus.PLANNED: frozenset(
            {MilestoneStatus.IN_PROGRESS, MilestoneStatus.COMPLETED, MilestoneStatus.FAILED, MilestoneStatus.BLOCKED}
        ),
        MilestoneStatus.IN_PROGRESS: frozenset(
            {MilestoneStatus.COMPLETED, MilestoneStatus.FAILED, MilestoneStatus.BLOCKED}
        ),
        MilestoneStatus.BLOCKED: frozenset(
            {MilestoneStatus.IN_PROGRESS, MilestoneStatus.COMPLETED, MilestoneStatus.FAILED}
        ),
        MilestoneStatus.COMPLETED: frozenset(),
        MilestoneStatus.FAILED: frozenset(),
    }
)


class MilestoneCategory(StrEnum):
    WORKFLOW = "workflow"  # a generic engine stage (plan, implement, test, review)
    DELIVERABLE = "deliverable"  # project-specific progress toward what is being built
    APPROVAL = "approval"  # a human decision point
    LIFECYCLE = "lifecycle"  # the run itself (finished)


HUMAN_DECISION = "human_decision"
RUN_FINISHED = "run_finished"
ACTION_EXECUTION = "action_execution"
RUN_RESUMED = "run_resumed"


# --------------------------------------------------------------- definitions


@dataclass(frozen=True)
class MilestoneDefinition:
    type: str
    title: str
    category: MilestoneCategory
    description: str = ""
    order: int | None = None

    def __post_init__(self) -> None:
        check_pattern(self.type, TYPE_RE, "milestone type")
        check_text(self.title, f"milestone {self.type}: title", max_len=120)
        object.__setattr__(self, "category", check_enum(MilestoneCategory, self.category, f"milestone {self.type}: category"))
        check_text(self.description, f"milestone {self.type}: description", max_len=500, allow_empty=True)
        if self.order is not None:
            check_int(self.order, f"milestone {self.type}: order", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "category": self.category.value,
            "description": self.description,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, data: Any) -> MilestoneDefinition:
        if not isinstance(data, dict):
            raise DomainValidationError("milestone definition: expected an object")
        check_fields(data, required=("type", "title", "category", "description", "order"), where="milestone definition")
        return cls(**data)


@dataclass(frozen=True)
class MilestoneCatalog:
    definitions: tuple[MilestoneDefinition, ...]

    def __post_init__(self) -> None:
        defs = tuple(self.definitions)
        for d in defs:
            if not isinstance(d, MilestoneDefinition):
                raise DomainValidationError(f"milestone catalog: expected MilestoneDefinition, got {type(d).__name__}")
        types = [d.type for d in defs]
        duplicates = sorted({t for t in types if types.count(t) > 1})
        if duplicates:
            raise DomainValidationError(f"milestone catalog: duplicate milestone type(s) {duplicates}")
        object.__setattr__(self, "definitions", defs)

    def __contains__(self, milestone_type: object) -> bool:
        return any(d.type == milestone_type for d in self.definitions)

    def get(self, milestone_type: str) -> MilestoneDefinition:
        for d in self.definitions:
            if d.type == milestone_type:
                return d
        raise DomainValidationError(f"unknown milestone type {milestone_type!r} for this project")

    def to_list(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in self.definitions]

    @classmethod
    def from_list(cls, data: Any) -> MilestoneCatalog:
        if not isinstance(data, list):
            raise DomainValidationError("milestone catalog: expected a list")
        return cls(tuple(MilestoneDefinition.from_dict(d) for d in data))


SYSTEM_DEFINITIONS: tuple[MilestoneDefinition, ...] = (
    MilestoneDefinition(
        HUMAN_DECISION,
        "Human decision",
        MilestoneCategory.APPROVAL,
        "A protected action waited for, and then received, a human decision.",
    ),
    MilestoneDefinition(RUN_FINISHED, "Run finished", MilestoneCategory.LIFECYCLE, "The run reached a final state."),
)

#: Engine types added after projects began storing their catalogs. Not required in a stored
#: catalog (older projects lack them); MilestonePolicy.definition() falls back to these.
ENGINE_DEFINITIONS: tuple[MilestoneDefinition, ...] = (
    MilestoneDefinition(
        ACTION_EXECUTION,
        "Approved action",
        MilestoneCategory.APPROVAL,
        "An approved action was carried out, failed, or needs a person to confirm whether it happened.",
    ),
    MilestoneDefinition(
        RUN_RESUMED,
        "Run resumed",
        MilestoneCategory.LIFECYCLE,
        "A person's decision (an approval, a rejection, an expiry, or a reconciliation) let the run continue.",
    ),
)

STANDARD_WORKFLOW_DEFINITIONS: tuple[MilestoneDefinition, ...] = (
    MilestoneDefinition("plan_ready", "Plan ready", MilestoneCategory.WORKFLOW, "A structured plan was produced.", 1),
    MilestoneDefinition(
        "implementation_ready", "Implementation ready", MilestoneCategory.WORKFLOW, "Code was written for the plan.", 2
    ),
    MilestoneDefinition("tests_passing", "Tests passing", MilestoneCategory.WORKFLOW, "The project's tests pass.", 3),
    MilestoneDefinition(
        "review_approved", "Review approved", MilestoneCategory.WORKFLOW, "A reviewer approved the implementation.", 4
    ),
)


# --------------------------------------------------------------------- rules

_RULE_STATUSES = frozenset({MilestoneStatus.IN_PROGRESS, MilestoneStatus.COMPLETED, MilestoneStatus.FAILED})


@dataclass(frozen=True)
class NodeMilestoneRule:
    """When ``node`` finishes with an outcome listed here, ``milestone_type`` gets that status.

    ``record_blocked``: if the run blocks at this node (isolation unavailable),
    the same milestone is recorded as blocked and later moves to its real
    outcome when the node completes.
    """

    node: str
    milestone_type: str
    outcomes: Mapping[str, MilestoneStatus]
    record_blocked: bool = True

    def __post_init__(self) -> None:
        check_text(self.node, "milestone rule: node", max_len=64)
        check_pattern(self.milestone_type, TYPE_RE, "milestone rule: milestone type")
        if not isinstance(self.outcomes, Mapping) or not self.outcomes:
            raise DomainValidationError(f"milestone rule for node {self.node!r}: at least one outcome is required")
        outcomes: dict[str, MilestoneStatus] = {}
        for outcome, status in self.outcomes.items():
            check_text(outcome, f"milestone rule for node {self.node!r}: outcome", max_len=64)
            status = check_enum(MilestoneStatus, status, f"milestone rule for node {self.node!r}: outcome {outcome!r}")
            if status not in _RULE_STATUSES:
                raise DomainValidationError(
                    f"milestone rule for node {self.node!r}: outcome {outcome!r} must map to in_progress, completed, "
                    f"or failed (not {status.value})"
                )
            outcomes[outcome] = status
        object.__setattr__(self, "outcomes", outcomes)
        if not isinstance(self.record_blocked, bool):
            raise DomainValidationError("milestone rule: record_blocked must be true or false")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "milestone_type": self.milestone_type,
            "outcomes": {k: v.value for k, v in sorted(self.outcomes.items())},
            "record_blocked": self.record_blocked,
        }

    @classmethod
    def from_dict(cls, data: Any) -> NodeMilestoneRule:
        if not isinstance(data, dict):
            raise DomainValidationError("milestone rule: expected an object")
        check_fields(data, required=("node", "milestone_type", "outcomes", "record_blocked"), where="milestone rule")
        return cls(**data)


STANDARD_RULES: tuple[NodeMilestoneRule, ...] = (
    NodeMilestoneRule("planner", "plan_ready", {"planned": MilestoneStatus.COMPLETED}),
    NodeMilestoneRule("developer", "implementation_ready", {"implemented": MilestoneStatus.COMPLETED}),
    NodeMilestoneRule("tester", "tests_passing", {"pass": MilestoneStatus.COMPLETED, "fail": MilestoneStatus.FAILED}),
    NodeMilestoneRule(
        "reviewer", "review_approved", {"approved": MilestoneStatus.COMPLETED, "changes_required": MilestoneStatus.FAILED}
    ),
)


@dataclass(frozen=True)
class MilestonePolicy:
    """A project's milestone vocabulary and the rules that reach each milestone."""

    catalog: MilestoneCatalog
    rules: tuple[NodeMilestoneRule, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, MilestoneCatalog):
            raise DomainValidationError("milestone policy: catalog must be a MilestoneCatalog")
        rules = tuple(self.rules)
        for system in SYSTEM_DEFINITIONS:
            if system.type not in self.catalog:
                raise DomainValidationError(f"milestone policy: system milestone type {system.type!r} is missing")
        nodes: set[str] = set()
        for rule in rules:
            if not isinstance(rule, NodeMilestoneRule):
                raise DomainValidationError(f"milestone policy: expected NodeMilestoneRule, got {type(rule).__name__}")
            if rule.milestone_type not in self.catalog:
                raise DomainValidationError(
                    f"milestone policy: rule for node {rule.node!r} uses milestone type {rule.milestone_type!r}, "
                    "which is not in the catalog"
                )
            if rule.node in nodes:
                raise DomainValidationError(f"milestone policy: more than one rule for node {rule.node!r}")
            nodes.add(rule.node)
        object.__setattr__(self, "rules", rules)

    def rule_for(self, node: str) -> NodeMilestoneRule | None:
        return next((r for r in self.rules if r.node == node), None)

    def definition(self, milestone_type: str) -> MilestoneDefinition:
        """The project's definition of a type; engine types missing from an older catalog use the default."""
        if milestone_type in self.catalog:
            return self.catalog.get(milestone_type)
        for engine in ENGINE_DEFINITIONS:
            if engine.type == milestone_type:
                return engine
        return self.catalog.get(milestone_type)  # raises: unknown for this project

    def to_dict(self) -> dict[str, Any]:
        return {"catalog": self.catalog.to_list(), "rules": [r.to_dict() for r in self.rules]}

    @classmethod
    def from_dict(cls, data: Any) -> MilestonePolicy:
        if not isinstance(data, dict):
            raise DomainValidationError("milestone policy: expected an object")
        check_fields(data, required=("catalog", "rules"), where="milestone policy")
        if not isinstance(data["rules"], list):
            raise DomainValidationError("milestone policy: rules must be a list")
        return cls(MilestoneCatalog.from_list(data["catalog"]), tuple(NodeMilestoneRule.from_dict(r) for r in data["rules"]))


def milestone_policy(
    definitions: Iterable[MilestoneDefinition] = (), rules: Iterable[NodeMilestoneRule] = ()
) -> MilestonePolicy:
    """Build a policy from project-specific definitions; system types are added automatically."""
    definitions = tuple(definitions)
    system = {d.type for d in (*SYSTEM_DEFINITIONS, *ENGINE_DEFINITIONS)}
    reserved = sorted({d.type for d in definitions} & system)
    if reserved:
        raise DomainValidationError(f"milestone policy: {reserved} are reserved system milestone types")
    return MilestonePolicy(MilestoneCatalog((*SYSTEM_DEFINITIONS, *ENGINE_DEFINITIONS, *definitions)), tuple(rules))


def standard_milestone_policy() -> MilestonePolicy:
    """Milestones for the standard planner -> developer -> tester -> reviewer graph."""
    return milestone_policy(STANDARD_WORKFLOW_DEFINITIONS, STANDARD_RULES)


# ----------------------------------------------------------------- instances


@dataclass(frozen=True)
class MilestoneSource:
    """Where a milestone came from in the workflow, when that is known."""

    node: str | None = None
    agent: str | None = None
    step: int | None = None

    def __post_init__(self) -> None:
        check_optional_text(self.node, "source.node", max_len=64)
        check_optional_text(self.agent, "source.agent", max_len=64)
        if self.step is not None:
            check_int(self.step, "source.step", minimum=0)

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "agent": self.agent, "step": self.step}

    @classmethod
    def from_dict(cls, data: Any) -> MilestoneSource:
        if not isinstance(data, dict):
            raise DomainValidationError("source: expected an object")
        check_fields(data, required=("node", "agent", "step"), where="source")
        return cls(**data)


_MILESTONE_FIELDS = (
    "id", "project_id", "run_id", "sequence", "key", "type", "category", "title", "summary", "status",
    "created_at", "occurred_at", "source", "metadata", "resolves_id",
)  # fmt: skip


@dataclass(frozen=True)
class Milestone(RecordMixin):
    """One meaningful progress item in one run.

    ``occurred_at`` is when the current status was reached (time-range queries
    use it). ``resolves_id`` points at an earlier failed milestone that this
    one resolves; that is how "what was repaired?" is answered.
    """

    RECORD_TYPE = "milestone"
    SCHEMA_VERSION = 1

    id: str
    project_id: str
    run_id: str
    sequence: int
    key: str
    type: str
    category: MilestoneCategory
    title: str
    summary: str
    status: MilestoneStatus
    created_at: str
    occurred_at: str
    source: MilestoneSource = field(default_factory=MilestoneSource)
    metadata: dict[str, Any] = field(default_factory=dict)
    resolves_id: str | None = None

    def __post_init__(self) -> None:
        check_pattern(self.id, MILESTONE_ID_RE, "id")
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.run_id, RUN_ID_RE, "run_id")
        check_int(self.sequence, "sequence", minimum=1)
        check_pattern(self.key, KEY_RE, "key")
        check_pattern(self.type, TYPE_RE, "type")
        object.__setattr__(self, "category", check_enum(MilestoneCategory, self.category, "category"))
        check_text(self.title, "title", max_len=120)
        check_text(self.summary, "summary", max_len=1000, allow_empty=True, multiline=True)
        object.__setattr__(self, "status", check_enum(MilestoneStatus, self.status, "status"))
        object.__setattr__(self, "created_at", check_timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "occurred_at", check_timestamp(self.occurred_at, "occurred_at"))
        if parse_timestamp(self.occurred_at) < parse_timestamp(self.created_at):
            raise DomainValidationError("occurred_at: earlier than created_at")
        if not isinstance(self.source, MilestoneSource):
            raise DomainValidationError("source: expected a MilestoneSource")
        object.__setattr__(self, "metadata", check_json_object(self.metadata, "metadata"))
        if self.resolves_id is not None:
            check_pattern(self.resolves_id, MILESTONE_ID_RE, "resolves_id")
            if self.resolves_id == self.id:
                raise DomainValidationError("resolves_id: a milestone cannot resolve itself")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def transition(
        self,
        status: MilestoneStatus,
        *,
        at: str,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        resolves_id: str | None = None,
    ) -> Milestone:
        status = check_enum(MilestoneStatus, status, "status")
        if status not in MILESTONE_TRANSITIONS[self.status]:
            raise StatusTransitionError(f"milestone {self.id}: {self.status.value} -> {status.value} is not allowed")
        return replace(
            self,
            status=status,
            occurred_at=at,
            summary=self.summary if summary is None else summary,
            metadata=self.metadata if metadata is None else metadata,
            resolves_id=self.resolves_id if resolves_id is None else resolves_id,
        )

    @staticmethod
    def check_successor(old: Milestone, new: Milestone) -> None:
        """Raise unless ``new`` is a legal next version of ``old`` (used by repositories)."""
        for name in ("id", "project_id", "run_id", "sequence", "key", "type", "category", "title", "created_at"):
            if getattr(old, name) != getattr(new, name):
                raise DomainValidationError(f"milestone {old.id}: field {name!r} cannot change")
        if old.is_terminal:
            if new != old:
                raise StatusTransitionError(f"milestone {old.id}: {old.status.value} milestones cannot be changed")
            return
        if new.status != old.status and new.status not in MILESTONE_TRANSITIONS[old.status]:
            raise StatusTransitionError(f"milestone {old.id}: {old.status.value} -> {new.status.value} is not allowed")

    def revise(self, *, at: str, summary: str | None = None, metadata: dict[str, Any] | None = None) -> Milestone:
        """Update the description of a milestone that is still open (not completed or failed)."""
        if self.is_terminal:
            raise StatusTransitionError(f"milestone {self.id}: {self.status.value} milestones cannot be changed")
        return replace(
            self,
            occurred_at=at,
            summary=self.summary if summary is None else summary,
            metadata=self.metadata if metadata is None else metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return seal(
            self.RECORD_TYPE,
            self.SCHEMA_VERSION,
            {
                "id": self.id,
                "project_id": self.project_id,
                "run_id": self.run_id,
                "sequence": self.sequence,
                "key": self.key,
                "type": self.type,
                "category": self.category.value,
                "title": self.title,
                "summary": self.summary,
                "status": self.status.value,
                "created_at": self.created_at,
                "occurred_at": self.occurred_at,
                "source": self.source.to_dict(),
                "metadata": self.metadata,
                "resolves_id": self.resolves_id,
            },
        )

    @classmethod
    def from_dict(cls, data: Any) -> Milestone:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(body, required=_MILESTONE_FIELDS, where=cls.RECORD_TYPE)
        return cls(**{**body, "source": MilestoneSource.from_dict(body["source"])})

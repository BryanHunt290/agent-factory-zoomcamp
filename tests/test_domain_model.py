"""Product domain model: identity, records, state transitions, schema validation."""

import json
import math

import pytest

from agent_factory.domain import (
    Actor,
    ActorKind,
    DomainValidationError,
    Milestone,
    MilestoneCatalog,
    MilestoneCategory,
    MilestoneDefinition,
    MilestoneSource,
    MilestoneStatus,
    NodeMilestoneRule,
    Project,
    Run,
    SchemaVersionError,
    StatusTransitionError,
    milestone_id,
    milestone_policy,
    new_project_id,
    standard_milestone_policy,
)
from agent_factory.domain.serialization import check_metadata, open_envelope
from agent_factory.state import RunStatus

T0 = "2026-10-02T01:00:00.000000Z"
T1 = "2026-10-02T02:00:00.000000Z"
PID = "prj-" + "a1" * 10


def _project(**kw):
    base = dict(id=PID, name="Calculator", created_at=T0, milestone_policy=standard_milestone_policy())
    base.update(kw)
    return Project(**base)


def _milestone(status=MilestoneStatus.COMPLETED, **kw):
    base = dict(
        id=milestone_id(PID, "run-1", "step:0"),
        project_id=PID,
        run_id="run-1",
        sequence=1,
        key="step:0",
        type="plan_ready",
        category=MilestoneCategory.WORKFLOW,
        title="Plan ready",
        summary="Plan with 3 steps",
        status=status,
        created_at=T0,
        occurred_at=T0,
        source=MilestoneSource(node="planner", agent="planner", step=0),
        metadata={"visit": 1, "nested": {"files": ["calculator.py"], "ok": True, "ratio": 0.5}},
    )
    base.update(kw)
    return Milestone(**base)


# --------------------------------------------------------------- identity


def test_project_ids_are_opaque_and_never_paths():
    a, b = new_project_id(), new_project_id()
    assert a != b and a.startswith("prj-") and len(a) == 24
    for bad in ["C:/Users/me/workspaces/demo", "workspaces/demo", "demo", "prj-XYZ", ""]:
        with pytest.raises(DomainValidationError):
            _project(id=bad)


def test_milestone_ids_are_deterministic_and_scoped_to_the_project():
    assert milestone_id(PID, "run-1", "step:3") == milestone_id(PID, "run-1", "step:3")
    other = "prj-" + "b2" * 10
    assert milestone_id(PID, "run-1", "step:3") != milestone_id(other, "run-1", "step:3")
    assert milestone_id(PID, "run-1", "step:3") != milestone_id(PID, "run-2", "step:3")


# ------------------------------------------------------------ serialization


@pytest.mark.parametrize(
    "record",
    [
        _project(metadata={"client": {"tier": "gold"}}, template="calculator", description="A demo"),
        Run(project_id=PID, run_id="run-1", number=1, graph_name="software-dev-v1",
            status=RunStatus.BLOCKED, created_at=T0, updated_at=T1, status_reason="docker unavailable"),
        _milestone(resolves_id=milestone_id(PID, "run-1", "step:2")),
    ],
)  # fmt: skip
def test_records_round_trip_through_deterministic_json(record):
    text = record.to_json()
    assert text == record.to_json()  # deterministic
    assert json.loads(text) == json.loads(json.dumps(json.loads(text), sort_keys=True))
    assert type(record).from_json(text) == record
    assert type(record).from_dict(record.to_dict()) == record


def test_serialized_records_carry_type_and_schema_version():
    data = _milestone().to_dict()
    assert data["record_type"] == "milestone" and data["schema_version"] == 1


@pytest.mark.parametrize(
    "mutate, error, message",
    [
        (lambda d: d.update(record_type="project"), DomainValidationError, "record_type"),
        (lambda d: d.update(schema_version=99), SchemaVersionError, "newer"),
        (lambda d: d.pop("schema_version"), SchemaVersionError, "schema_version"),
        (lambda d: d.update(schema_version="1"), SchemaVersionError, "schema_version"),
        (lambda d: d.update(surprise=1), DomainValidationError, "unknown field"),
        (lambda d: d.pop("title"), DomainValidationError, "missing field"),
        (lambda d: d.update(title=7), DomainValidationError, "title"),
        (lambda d: d.update(status="done"), DomainValidationError, "status"),
        (lambda d: d.update(occurred_at="yesterday"), DomainValidationError, "occurred_at"),
        (lambda d: d.update(sequence=0), DomainValidationError, "sequence"),
        (lambda d: d.update(sequence=True), DomainValidationError, "sequence"),
        (lambda d: d.update(id="ms-not-a-hash"), DomainValidationError, r"^id: invalid value"),
    ],
)
def test_strict_schema_validation(mutate, error, message):
    data = _milestone().to_dict()
    mutate(data)
    with pytest.raises(error, match=message):
        Milestone.from_dict(data)


@pytest.mark.parametrize(
    "spelling", ["2026-10-02T01:00:00Z", "2026-10-02T01:00:00.0Z", "2026-10-02T01:00:00.000000Z"]
)
def test_timestamps_have_one_canonical_form(spelling):
    """Equal instants are equal values, so records survive any store unchanged."""
    m = _milestone(created_at=spelling, occurred_at=spelling)
    assert m.created_at == m.occurred_at == "2026-10-02T01:00:00.000000Z"
    assert m == _milestone(created_at=T0, occurred_at=T0)
    assert Milestone.from_json(m.to_json()) == m


def test_a_datetime_without_a_time_zone_is_refused():
    from datetime import datetime

    from agent_factory.domain.serialization import format_timestamp

    with pytest.raises(DomainValidationError, match="time zone"):
        format_timestamp(datetime(2026, 10, 2, 1, 0))


@pytest.mark.parametrize("value", [{"note": "a\x00b"}, {"k\x00": 1}, {"nested": ["ok", {"deep": "\x00"}]}])
def test_metadata_may_not_contain_nul_characters(value):
    """NUL is not representable in PostgreSQL text or jsonb, so both stores refuse it the same way."""
    with pytest.raises(DomainValidationError, match="NUL"):
        check_metadata(value, "metadata")


def test_older_schema_versions_need_an_explicit_upgrade():
    data = _milestone().to_dict()
    data["schema_version"] = 0
    with pytest.raises(SchemaVersionError, match="no upgrade path"):
        Milestone.from_dict(data)  # an older record is never read as if it were current

    v1 = {"record_type": "thing", "schema_version": 1, "name": "a"}
    with pytest.raises(SchemaVersionError, match="no upgrade path"):
        open_envelope(v1, record_type="thing", version=2)
    upgraded = open_envelope(v1, record_type="thing", version=2, upgrades={1: lambda body: {**body, "label": "A"}})
    assert upgraded == {"name": "a", "label": "A"}


@pytest.mark.parametrize(
    "value",
    [
        {"b": b"bytes"},
        {"s": {1, 2}},
        {"t": (1, 2)},
        {"n": math.nan},
        {"i": math.inf},
        {1: "non-string key"},
        {"o": object()},
        {"deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}},
        {"big": "x" * 20000},
    ],
)
def test_metadata_must_be_json_safe_and_bounded(value):
    with pytest.raises(DomainValidationError):
        check_metadata(value, "metadata")
    with pytest.raises(DomainValidationError):
        _milestone(metadata=value)


def test_metadata_is_copied_not_shared():
    meta = {"files": ["a.py"]}
    m = _milestone(metadata=meta)
    meta["files"].append("b.py")
    assert m.metadata == {"files": ["a.py"]}


# ---------------------------------------------------- milestone transitions


@pytest.mark.parametrize(
    "path",
    [
        ["planned", "in_progress", "completed"],
        ["planned", "in_progress", "failed"],
        ["planned", "in_progress", "blocked", "in_progress", "completed"],
        ["planned", "completed"],
        ["blocked", "completed"],
        ["blocked", "failed"],
        ["in_progress", "blocked", "completed"],
    ],
)
def test_valid_milestone_transitions(path):
    m = _milestone(status=MilestoneStatus(path[0]))
    for status in path[1:]:
        m = m.transition(MilestoneStatus(status), at=T1)
        assert m.status is MilestoneStatus(status) and m.occurred_at == T1
    assert m.created_at == T0


@pytest.mark.parametrize(
    "start, target",
    [
        ("completed", "failed"),
        ("completed", "in_progress"),
        ("failed", "in_progress"),
        ("failed", "completed"),
        ("in_progress", "planned"),
        ("blocked", "planned"),
        ("in_progress", "in_progress"),
        ("completed", "completed"),
    ],
)
def test_impossible_milestone_transitions_are_rejected(start, target):
    with pytest.raises(StatusTransitionError):
        _milestone(status=MilestoneStatus(start)).transition(MilestoneStatus(target), at=T1)


def test_terminal_milestones_are_immutable_but_open_ones_can_be_revised():
    with pytest.raises(StatusTransitionError):
        _milestone(status=MilestoneStatus.COMPLETED).revise(at=T1, summary="rewritten")
    blocked = _milestone(status=MilestoneStatus.BLOCKED).revise(at=T1, summary="still blocked: docker down")
    assert blocked.summary == "still blocked: docker down" and blocked.status is MilestoneStatus.BLOCKED


# ----------------------------------------------------------- run transitions


@pytest.mark.parametrize(
    "path",
    [
        ["running", "awaiting_approval", "running", "completed"],
        ["running", "blocked", "running", "failed"],
        ["running", "exhausted"],
    ],
)
def test_valid_run_transitions_match_the_engine(path):
    run = Run(project_id=PID, run_id="r", number=1, graph_name="g", status=RunStatus(path[0]), created_at=T0, updated_at=T0)
    for status in path[1:]:
        run = run.transition(RunStatus(status), at=T1)
    assert run.status is RunStatus(path[-1])


@pytest.mark.parametrize(
    "start, target",
    [("completed", "running"), ("failed", "running"), ("exhausted", "running"),
     ("awaiting_approval", "blocked"), ("blocked", "completed"), ("running", "running")],
)  # fmt: skip
def test_impossible_run_transitions_are_rejected(start, target):
    run = Run(project_id=PID, run_id="r", number=1, graph_name="g", status=RunStatus(start), created_at=T0, updated_at=T0)
    with pytest.raises(StatusTransitionError):
        run.transition(RunStatus(target), at=T1)


# ------------------------------------------- project-defined milestone types

DATA_ENGINEERING = [
    MilestoneDefinition("requirements_accepted", "Requirements accepted", MilestoneCategory.DELIVERABLE, order=1),
    MilestoneDefinition("ingestion_working", "Ingestion working", MilestoneCategory.DELIVERABLE, order=2),
    MilestoneDefinition("transformation_complete", "Transformation complete", MilestoneCategory.DELIVERABLE, order=3),
    MilestoneDefinition("database_load_verified", "Database load verified", MilestoneCategory.DELIVERABLE, order=4),
    MilestoneDefinition("documentation_ready", "Documentation ready", MilestoneCategory.DELIVERABLE, order=5),
]
WEB_APP = [
    MilestoneDefinition("api_implemented", "API implemented", MilestoneCategory.DELIVERABLE, order=1),
    MilestoneDefinition("authentication_implemented", "Authentication implemented", MilestoneCategory.DELIVERABLE, order=2),
    MilestoneDefinition("deployment_candidate_ready", "Deployment candidate ready", MilestoneCategory.DELIVERABLE, order=3),
]
AI_PROJECT = [
    MilestoneDefinition("evaluation_dataset_ready", "Evaluation dataset ready", MilestoneCategory.DELIVERABLE, order=1),
    MilestoneDefinition("evaluation_passed", "Evaluation passed", MilestoneCategory.DELIVERABLE, order=2),
    MilestoneDefinition("safety_review_complete", "Safety review complete", MilestoneCategory.DELIVERABLE, order=3),
]


@pytest.mark.parametrize("definitions", [DATA_ENGINEERING, WEB_APP, AI_PROJECT])
def test_projects_define_their_own_milestone_types(definitions):
    rules = [NodeMilestoneRule("stage-a", definitions[0].type, {"ok": MilestoneStatus.COMPLETED, "error": MilestoneStatus.FAILED})]
    policy = milestone_policy(definitions=definitions, rules=rules)
    for d in definitions:
        assert policy.catalog.get(d.type) == d
    assert policy.catalog.get("human_decision").category is MilestoneCategory.APPROVAL  # system types always present
    assert policy.rule_for("stage-a").milestone_type == definitions[0].type
    assert type(policy).from_dict(policy.to_dict()) == policy


def test_standard_policy_maps_the_standard_graph():
    policy = standard_milestone_policy()
    assert policy.rule_for("tester").outcomes == {"pass": MilestoneStatus.COMPLETED, "fail": MilestoneStatus.FAILED}
    assert policy.rule_for("reviewer").milestone_type == "review_approved"
    assert policy.rule_for("unknown-node") is None
    assert {d.type for d in policy.catalog.definitions} >= {
        "plan_ready", "implementation_ready", "tests_passing", "review_approved", "human_decision", "run_finished"
    }  # fmt: skip


@pytest.mark.parametrize(
    "build, message",
    [
        (lambda: MilestoneCatalog((DATA_ENGINEERING[0], DATA_ENGINEERING[0])), "duplicate"),
        (lambda: MilestoneDefinition("Bad Type", "x", MilestoneCategory.DELIVERABLE), "type"),
        (lambda: milestone_policy(definitions=[MilestoneDefinition("human_decision", "Mine", MilestoneCategory.DELIVERABLE)]), "system"),
        (lambda: milestone_policy(rules=[NodeMilestoneRule("n", "undefined_type", {"ok": MilestoneStatus.COMPLETED})]), "not in the catalog"),
        (lambda: milestone_policy(definitions=WEB_APP, rules=[
            NodeMilestoneRule("n", "api_implemented", {"ok": MilestoneStatus.COMPLETED}),
            NodeMilestoneRule("n", "authentication_implemented", {"ok": MilestoneStatus.COMPLETED})]), "more than one rule"),
        (lambda: NodeMilestoneRule("n", "api_implemented", {"ok": MilestoneStatus.BLOCKED}), "outcome"),
        (lambda: NodeMilestoneRule("n", "api_implemented", {}), "outcome"),
    ],
)  # fmt: skip
def test_invalid_catalogs_and_policies_are_rejected(build, message):
    with pytest.raises(DomainValidationError, match=message):
        build()


def test_unknown_milestone_type_is_rejected():
    with pytest.raises(DomainValidationError, match="unknown milestone type"):
        standard_milestone_policy().catalog.get("ingestion_working")


# -------------------------------------------------------------- actor identity


def test_unauthenticated_actor_placeholder():
    actor = Actor.unauthenticated("bryan")
    assert actor.kind is ActorKind.UNAUTHENTICATED and actor.verified is False and actor.id == "local:bryan"
    with pytest.raises(DomainValidationError):
        Actor(kind=ActorKind.UNAUTHENTICATED, id="local:x", display_name="x", verified=True)


def test_a_user_actor_is_an_authenticated_principal():
    from agent_factory.domain.ids import new_principal_id

    actor = Actor.authenticated(new_principal_id(), "Bryan")
    assert actor.kind is ActorKind.USER and actor.verified and Actor.from_dict(actor.to_dict()) == actor
    for invalid in ({"id": "user-7f3c", "verified": True}, {"id": new_principal_id(), "verified": False}):
        with pytest.raises(DomainValidationError, match="authenticated principal"):
            Actor(kind=ActorKind.USER, display_name="Bryan", **invalid)

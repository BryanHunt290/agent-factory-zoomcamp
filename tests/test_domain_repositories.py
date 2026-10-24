"""The persistence boundary: one contract, two implementations.

Every test here runs against the in-memory store and, when the PostgreSQL
suite is requested (``-m postgres --run-postgres``), against a freshly migrated
PostgreSQL schema as well. Where the semantics should match, they must match.
"""

import pytest

from agent_factory.domain import (
    ApprovalBindingError,
    ApprovalStatus,
    ConflictError,
    DomainValidationError,
    Milestone,
    MilestoneCategory,
    MilestoneSource,
    MilestoneStatus,
    Project,
    RecordNotFoundError,
    Run,
    StatusTransitionError,
    milestone_id,
    standard_milestone_policy,
)
from agent_factory.domain.repositories import (
    ApprovalRepository,
    MilestoneQuery,
    MilestoneRepository,
    ProjectRepository,
    RunRepository,
    UnitOfWork,
    in_memory_store,
)
from agent_factory.state import RunStatus
from test_domain_approvals import PID as APPROVAL_PID
from test_domain_approvals import _request

P1 = "prj-" + "d4" * 10
P2 = "prj-" + "e5" * 10


def ts(hour, minute=0):
    return f"2026-10-02T{hour:02d}:{minute:02d}:00.000000Z"


def _ms(project, run, key, seq, mtype="tests_passing", status=MilestoneStatus.COMPLETED, at=ts(1), resolves=None, mid=None):
    return Milestone(
        id=mid or milestone_id(project, run, key), project_id=project, run_id=run, sequence=seq, key=key, type=mtype,
        category=MilestoneCategory.WORKFLOW, title=mtype.replace("_", " "), summary="", status=status,
        created_at=at, occurred_at=at, source=MilestoneSource(node="tester", step=seq), resolves_id=resolves,
    )  # fmt: skip


def _project(pid, at=ts(0)):
    return Project(id=pid, name="Calc", created_at=at, milestone_policy=standard_milestone_policy())


def _run(pid, run_id, number=1, status=RunStatus.RUNNING, at=ts(0)):
    return Run(project_id=pid, run_id=run_id, number=number, graph_name="g", status=status, created_at=at, updated_at=at)


def seed(store, *runs, projects=()):
    """Add the projects and runs that later records reference (referential integrity)."""
    for pid in projects:
        if store.projects.get(pid) is None:
            store.projects.add(_project(pid))
    for pid, run_id in runs:
        seed(store, projects=[pid])
        if store.runs.get(pid, run_id) is None:
            store.runs.add(_run(pid, run_id, number=len(store.runs.list_for_project(pid)) + 1))


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request):
    if request.param == "memory":
        return in_memory_store()
    return request.getfixturevalue("pg_store")


def test_store_implements_every_interface(store):
    assert isinstance(store.projects, ProjectRepository)
    assert isinstance(store.runs, RunRepository)
    assert isinstance(store.milestones, MilestoneRepository)
    assert isinstance(store.approvals, ApprovalRepository)
    assert isinstance(store.unit_of_work, UnitOfWork)


def test_projects_are_added_once(store):
    p = _project(P1, at=ts(1))
    store.projects.add(p)
    assert store.projects.get(P1) == p and store.projects.list() == [p]
    with pytest.raises(ConflictError):
        store.projects.add(p)


def test_runs_are_scoped_to_their_project(store):
    seed(store, projects=[P1, P2])
    a, b = _run(P1, "demo", at=ts(1)), _run(P2, "demo", at=ts(1))
    store.runs.add(a)
    store.runs.add(b)  # same run_id, different project: no clash
    assert store.runs.get(P1, "demo") == a and store.runs.get(P2, "demo") == b
    assert a.ref != b.ref


def test_run_updates_must_follow_the_state_machine(store):
    seed(store, projects=[P1])
    run = _run(P1, "r1", at=ts(1))
    store.runs.add(run)
    store.runs.update(run.transition(RunStatus.BLOCKED, at=ts(2), reason="docker down"))
    done = Run(project_id=P1, run_id="r1", number=1, graph_name="g", status=RunStatus.COMPLETED, created_at=ts(1), updated_at=ts(3))
    with pytest.raises(StatusTransitionError):
        store.runs.update(done)  # blocked -> completed skips running
    with pytest.raises(DomainValidationError):
        store.runs.update(Run(project_id=P1, run_id="r1", number=9, graph_name="g", status=RunStatus.BLOCKED,
                              created_at=ts(1), updated_at=ts(3)))  # identity (number) changed  # fmt: skip
    with pytest.raises(RecordNotFoundError):
        store.runs.update(_run(P1, "nope", at=ts(1)))
    assert store.runs.get(P1, "r1").status is RunStatus.BLOCKED  # refused updates changed nothing


def test_run_listing_and_latest(store):
    seed(store, projects=[P1])
    for n in (1, 2, 3):
        store.runs.add(_run(P1, f"r{n}", number=n, at=ts(n)))
    assert [r.number for r in store.runs.list_for_project(P1)] == [1, 2, 3]
    assert store.runs.latest_for_project(P1).run_id == "r3"
    assert store.runs.latest_for_project(P2) is None


def test_milestones_reject_duplicate_ids_and_illegal_updates(store):
    seed(store, (P1, "r1"))
    m = _ms(P1, "r1", "step:2", 1, status=MilestoneStatus.BLOCKED)
    store.milestones.add(m)
    with pytest.raises(ConflictError):
        store.milestones.add(m)
    done = m.transition(MilestoneStatus.COMPLETED, at=ts(2))
    store.milestones.update(done)
    with pytest.raises(StatusTransitionError):
        store.milestones.update(m)  # completed -> blocked: terminal records never change
    with pytest.raises(DomainValidationError):
        store.milestones.update(_ms(P1, "r1", "step:2", 7, status=MilestoneStatus.COMPLETED, at=ts(2)))  # sequence changed
    assert store.milestones.get(m.id) == done


def test_milestones_belong_to_their_project_and_run(store):
    seed(store, (P1, "r1"), (P1, "r2"), (P2, "r1"))
    store.milestones.add(_ms(P1, "r1", "step:0", 1))
    store.milestones.add(_ms(P1, "r2", "step:0", 1))
    store.milestones.add(_ms(P2, "r1", "step:0", 1))
    assert {(m.project_id, m.run_id) for m in store.milestones.query(MilestoneQuery(project_id=P1))} == {(P1, "r1"), (P1, "r2")}
    assert [m.run_id for m in store.milestones.query(MilestoneQuery(project_id=P1, run_id="r2"))] == ["r2"]
    assert store.milestones.next_sequence(P1, "r1") == 2 and store.milestones.next_sequence(P2, "r9") == 1


def test_query_ordering_is_deterministic_even_with_equal_timestamps(store):
    seed(store, (P1, "r1"))
    for seq, key in [(3, "c"), (1, "a"), (2, "b")]:
        store.milestones.add(_ms(P1, "r1", key, seq, at=ts(1)))
    first = [m.sequence for m in store.milestones.query(MilestoneQuery(project_id=P1))]
    assert first == [1, 2, 3]
    assert first == [m.sequence for m in store.milestones.query(MilestoneQuery(project_id=P1))]


def test_identifiers_order_by_code_point_not_locale(store):
    """Run IDs differing in case and digits sort the same in every store (PostgreSQL: COLLATE "C")."""
    runs = ["run-a", "Run-B", "run-10", "run-2", "RUN-1"]
    seed(store, *[(P1, r) for r in runs])
    for r in runs:
        store.milestones.add(_ms(P1, r, "step:0", 1, at=ts(1)))
    assert [m.run_id for m in store.milestones.query(MilestoneQuery(project_id=P1))] == sorted(runs)


def test_time_range_query_uses_when_each_status_was_reached(store):
    seed(store, (P1, "r1"))
    early = _ms(P1, "r1", "step:0", 1, mtype="plan_ready", at=ts(1))
    blocked = _ms(P1, "r1", "step:1", 2, status=MilestoneStatus.BLOCKED, at=ts(1, 30))
    store.milestones.add(early)
    store.milestones.add(blocked)
    store.milestones.update(blocked.transition(MilestoneStatus.COMPLETED, at=ts(5)))  # unblocked overnight
    overnight = store.milestones.query(MilestoneQuery(project_id=P1, since=ts(2), until=ts(6)))
    assert [m.key for m in overnight] == ["step:1"]
    assert [m.key for m in store.milestones.query(MilestoneQuery(project_id=P1, until=ts(5)))] == ["step:0"]  # end-exclusive
    assert store.milestones.query(MilestoneQuery(project_id=P1, since=ts(6))) == []
    filtered = MilestoneQuery(project_id=P1, statuses=frozenset({MilestoneStatus.COMPLETED}), types=frozenset({"plan_ready"}))
    assert [m.key for m in store.milestones.query(filtered)] == ["step:0"]
    with pytest.raises(DomainValidationError):
        MilestoneQuery(project_id=P1, since="last night")
    with pytest.raises(DomainValidationError):
        MilestoneQuery(project_id=P1, since=ts(6), until=ts(2))


def test_timestamps_are_stored_in_one_canonical_form(store):
    seed(store, (P1, "r1"))
    m = _ms(P1, "r1", "step:0", 1, at="2026-10-02T01:00:00Z")  # no fraction: same instant as ...00.000000Z
    assert m.occurred_at == "2026-10-02T01:00:00.000000Z"
    store.milestones.add(m)
    assert store.milestones.get(m.id) == m
    assert store.milestones.query(MilestoneQuery(project_id=P1, since="2026-10-02T01:00:00.000000Z")) == [m]


def test_approval_repository_requires_a_request_and_one_decision(store):
    from agent_factory.domain import Actor, ApprovalOutcome, ApprovalService

    seed(store, (APPROVAL_PID, "run-1"))
    svc = ApprovalService(store.approvals)
    req = svc.open_request(_request())
    assert store.approvals.list_requests(req.project_id, status=ApprovalStatus.PENDING) == [req]
    svc.decide(project_id=req.project_id, request_id=req.id, action_fingerprint=req.action_fingerprint,
               outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), at=ts(2))  # fmt: skip
    assert store.approvals.list_requests(req.project_id, status=ApprovalStatus.PENDING) == []
    decision = store.approvals.get_decision(req.project_id, req.id)
    with pytest.raises(ConflictError):
        store.approvals.save_decision(decision, store.approvals.get_request(req.project_id, req.id))


def test_references_must_exist(store):
    """Referential integrity: no record may point at something that is not there."""
    from agent_factory.domain import ApprovalDecision, ApprovalOutcome, decision_id
    from agent_factory.domain.identity import Actor

    with pytest.raises(RecordNotFoundError):
        store.runs.add(_run(P1, "r1"))  # no such project
    seed(store, projects=[P1])
    with pytest.raises(RecordNotFoundError):
        store.milestones.add(_ms(P1, "r1", "step:0", 1))  # no such run
    with pytest.raises(RecordNotFoundError):
        store.approvals.add_request(_request(project_id=P1, run_id="r1"))  # no such run
    seed(store, (P1, "r1"), (P1, "r2"))
    other_run = _ms(P1, "r2", "step:0", 1, status=MilestoneStatus.FAILED)
    store.milestones.add(other_run)
    with pytest.raises(RecordNotFoundError):
        store.milestones.add(_ms(P1, "r1", "step:1", 1, resolves=milestone_id(P1, "r1", "missing")))  # no such milestone
    with pytest.raises(RecordNotFoundError):
        store.milestones.add(_ms(P1, "r1", "step:1", 1, resolves=other_run.id))  # exists, but in another run
    ghost = _request(project_id=P1, run_id="r1")
    decision = ApprovalDecision(id=decision_id(P1, ghost.id), project_id=P1, request_id=ghost.id,
                                action_fingerprint=ghost.action_fingerprint, outcome=ApprovalOutcome.APPROVED,
                                decided_at=ts(2), actor=Actor.unauthenticated("bryan"))  # fmt: skip
    with pytest.raises(RecordNotFoundError):
        store.approvals.save_decision(decision, ghost.transition(ApprovalStatus.APPROVED, at=ts(2)))  # no such request
    assert store.milestones.query(MilestoneQuery(project_id=P1)) == [other_run]
    assert store.approvals.list_requests(P1) == []


def test_natural_keys_are_unique(store):
    seed(store, (P1, "r1"))
    with pytest.raises(ConflictError):
        store.runs.add(_run(P1, "r2", number=1))  # run number 1 is taken in this project
    store.milestones.add(_ms(P1, "r1", "step:0", 1))
    with pytest.raises(ConflictError):
        store.milestones.add(_ms(P1, "r1", "step:1", 1))  # sequence 1 is taken in this run
    with pytest.raises(ConflictError):
        store.milestones.add(_ms(P1, "r1", "step:0", 2, mid="ms-" + "0" * 20))  # key step:0 is taken in this run
    assert len(store.milestones.query(MilestoneQuery(project_id=P1))) == 1


def test_a_decision_must_carry_its_requests_fingerprint(store):
    """Repository-level binding (the database also enforces it with a foreign key)."""
    from agent_factory.domain import ApprovalDecision, ApprovalOutcome, ExactAction, decision_id
    from agent_factory.domain.identity import Actor

    seed(store, (APPROVAL_PID, "run-1"))
    req = _request()
    store.approvals.add_request(req)
    other = ExactAction(tool="delete_file", category="destructive_delete", params={"path": "tests/test_calculator.py"})
    forged = ApprovalDecision(id=decision_id(APPROVAL_PID, req.id), project_id=APPROVAL_PID, request_id=req.id,
                              action_fingerprint=other.fingerprint, outcome=ApprovalOutcome.APPROVED, decided_at=ts(2),
                              actor=Actor.unauthenticated("bryan"))  # fmt: skip
    with pytest.raises(ApprovalBindingError):
        store.approvals.save_decision(forged, req.transition(ApprovalStatus.APPROVED, at=ts(2)))
    assert store.approvals.get_decision(APPROVAL_PID, req.id) is None
    assert store.approvals.get_request(APPROVAL_PID, req.id).status is ApprovalStatus.PENDING


def test_a_transaction_takes_effect_completely_or_not_at_all(store):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with store.transaction():
            store.projects.add(_project(P1))
            store.runs.add(_run(P1, "r1"))
            raise Boom
    assert store.projects.get(P1) is None and store.runs.get(P1, "r1") is None

    with store.transaction():
        store.projects.add(_project(P1))
        try:
            with store.transaction():  # nested: undone on its own, like a savepoint
                store.runs.add(_run(P1, "r1"))
                store.runs.add(_run(P1, "r1"))  # duplicate: fails
        except ConflictError:
            pass
        store.runs.add(_run(P1, "r2"))
    assert store.projects.get(P1) is not None
    assert [r.run_id for r in store.runs.list_for_project(P1)] == ["r2"]


def test_the_model_answers_the_overnight_questions(store):
    """Not a summary generator: proves the questions are answerable from the records."""
    from agent_factory.domain import ApprovalService

    seed(store, projects=[P1])
    store.runs.add(_run(P1, "r1"))
    failed = _ms(P1, "r1", "step:2", 2, status=MilestoneStatus.FAILED, at=ts(2))
    for m in (_ms(P1, "r1", "step:0", 1, mtype="plan_ready", at=ts(1)), failed,
              _ms(P1, "r1", "step:4", 3, at=ts(3), resolves=failed.id),
              _ms(P1, "r1", "step:6", 4, mtype="review_approved", status=MilestoneStatus.BLOCKED, at=ts(4))):  # fmt: skip
        store.milestones.add(m)
    ApprovalService(store.approvals).open_request(_request(project_id=P1, run_id="r1", requested_at=ts(4)))
    store.runs.update(store.runs.get(P1, "r1").transition(RunStatus.AWAITING_APPROVAL, at=ts(4)))

    night = MilestoneQuery(project_id=P1, since=ts(0), until=ts(8))
    happened = store.milestones.query(night)
    succeeded = [m.type for m in happened if m.status is MilestoneStatus.COMPLETED]
    failed_now = [m.type for m in happened if m.status is MilestoneStatus.FAILED]
    repaired = [(m.type, m.resolves_id) for m in happened if m.resolves_id]
    blocked = [m.type for m in store.milestones.query(MilestoneQuery(project_id=P1, statuses=frozenset({MilestoneStatus.BLOCKED})))]
    needs_me = store.approvals.list_requests(P1, status=ApprovalStatus.PENDING)

    assert [m.sequence for m in happened] == [1, 2, 3, 4]
    assert succeeded == ["plan_ready", "tests_passing"]
    assert failed_now == ["tests_passing"]
    assert repaired == [("tests_passing", failed.id)]
    assert blocked == ["review_approved"]
    assert len(needs_me) == 1 and needs_me[0].action.params == {"path": "legacy_calc.py"}
    assert store.runs.latest_for_project(P1).status is RunStatus.AWAITING_APPROVAL

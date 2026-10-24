"""Worker coordination against a real PostgreSQL database: claims, leases, fencing, and races.

    .venv\\Scripts\\python -m pytest -m postgres --run-postgres    (docs/workers.md, docs/testing.md)

Nothing here is mocked: every claim, renewal, release, and fenced write is
SQL against a fresh schema, and the races use real concurrent connections
from threads released together. Process-level tests (real worker processes
that die, stall, and are fenced out) are in tests/test_worker_processes.py.
"""

import time
from datetime import timedelta
from types import SimpleNamespace

import action_fixtures as fx
import pytest
from worker_support import BeforeAgent, NoticeSource, WrappedSource, age_claim, new_calculator_run, race, wait_until
from worker_support import approved_notice_run as approved_notice

from agent_factory.domain import ApprovalService, Project, Run, standard_milestone_policy
from agent_factory.domain.approvals import ApprovalRequest, ExactAction, approval_expiry, risk_for
from agent_factory.domain.claims import Fence, LeasePolicy, OwnershipLost, new_worker_id
from agent_factory.domain.errors import RecordNotFoundError
from agent_factory.domain.executions import ActionExecution, ExecutionLedger, ExecutionSpec, ExecutionState, LedgerKind
from agent_factory.domain.milestones import Milestone, MilestoneCategory, MilestoneSource, MilestoneStatus
from agent_factory.domain.recorder import ProgressRecorder
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.safety.execution import semantics_of
from agent_factory.state import ProjectState, RunStatus, TaskSpec
from agent_factory.util import timestamp, utc_now
from agent_factory.worker import ClaimOwnership, LeaseKeeper, Worker, WorkerEvents, WorkerIdentity, WorkerSettings

pytestmark = pytest.mark.postgres

PROJECT = "prj-" + "c" * 20
POLICY = LeasePolicy()  # 60 s lease, 20 s heartbeat
SHORT = LeasePolicy(lease=timedelta(seconds=2), heartbeat_interval=timedelta(milliseconds=500))


@pytest.fixture
def admin(pg_schema):
    """A plain connection for setting up and inspecting rows (never a worker's)."""
    from agent_factory.persistence.postgres import connect

    conn = connect(pg_schema)
    yield conn
    conn.close()


@pytest.fixture
def open_claims(pg_schema):
    from agent_factory.persistence.claims import open_claim_repository

    opened = []

    def make():
        repo = open_claim_repository(pg_schema)
        opened.append(repo)
        return repo

    yield make
    for repo in opened:
        repo.close()


@pytest.fixture
def open_fenced(pg_schema):
    from agent_factory.persistence.claims import open_fenced_store

    opened = []

    def make(fence):
        store = open_fenced_store(pg_schema, fence)
        opened.append(store)
        return store

    yield make
    for store in opened:
        store.close()


def add_runs(store, *run_ids, status=RunStatus.RUNNING, project=PROJECT):
    now = timestamp(utc_now)
    if store.projects.get(project) is None:
        store.projects.add(Project(id=project, name="claims", created_at=now, milestone_policy=standard_milestone_policy()))
    first = len(store.runs.list_for_project(project)) + 1
    for number, run_id in enumerate(run_ids, start=first):
        store.runs.add(Run(project_id=project, run_id=run_id, number=number, graph_name="g", status=status, created_at=now,
                           updated_at=now, status_reason=""))  # fmt: skip


def claim_row(admin, run_id="run-1", project=PROJECT):
    row = admin.execute(
        "SELECT worker_id, fencing_token, lease_expires_at, last_heartbeat_at, available_at FROM run_claims "
        "WHERE project_id = %s AND run_id = %s",
        (project, run_id),
    ).fetchone()
    return SimpleNamespace(worker_id=row[0], token=row[1], lease=row[2], heartbeat=row[3], available=row[4]) if row else None


# ===================================================================== basics


def test_claim_renew_release_on_postgresql(pg_store, open_claims, admin):
    add_runs(pg_store, "run-1")
    claims = open_claims()
    queued = claims.enqueue(PROJECT, "run-1")
    assert (queued.fencing_token, queued.worker_id, queued.claimed_at) == (0, None, None)
    worker = new_worker_id()
    claim = claims.claim_next(worker, POLICY)
    assert claim.fence == Fence(PROJECT, "run-1", worker, 1) and not claim.taken_over
    row = admin.execute(
        "SELECT lease_expires_at - claimed_at, last_heartbeat_at = claimed_at FROM run_claims WHERE run_id = 'run-1'"
    ).fetchone()
    assert row == (timedelta(seconds=60), True)  # the lease is measured by the database clock alone
    assert claims.claim_next(new_worker_id(), POLICY) is None
    renewed = claims.renew(claim.fence, POLICY)
    assert renewed > claim.lease_expires_at
    assert claims.release(claim.fence, requeue=False)
    released = claim_row(admin)
    assert (released.worker_id, released.lease, released.available, released.token) == (None, None, None, 1)


@pytest.mark.parametrize(
    "plan",
    [{"requeue": False}, {"requeue": True, "at": "2099-01-01T00:00:00.000000Z"}, {"requeue": True, "delay": timedelta(hours=1)},
     {"requeue": None}],
    ids=["not-wanted", "at-expiry", "after-delay", "unchanged"],
)  # fmt: skip
def test_a_wake_up_during_a_claim_survives_the_release_whatever_the_owner_planned(pg_store, open_claims, admin, plan):
    """The decision's wake-up commits while a worker holds the run: its release must not undo it (E5)."""
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    claim = claims.claim_next(new_worker_id(), POLICY)
    with pg_store.transaction():
        pg_store.queue.wake(PROJECT, "run-1")  # as decideApproval does, in its own transaction
    assert claims.release(claim.fence, **plan)
    assert admin.execute("SELECT worker_id, available_at <= now(), wake_seq > claimed_wake_seq FROM run_claims").fetchone() == (
        None, True, True,
    )  # fmt: skip
    again = claims.claim_next(new_worker_id(), POLICY)
    assert again is not None and claims.release(again.fence, requeue=False)
    assert claim_row(admin).available is None  # without a wake-up, the owner's plan stands


def test_a_wake_up_waits_for_a_claim_in_progress_and_is_then_counted(pg_store, open_claims, admin):
    """The counter is written under the claim row's lock, so commit order cannot hide a wake-up from a release."""
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    before = admin.execute("SELECT wake_seq FROM run_claims").fetchone()[0]
    claim = claims.claim_next(new_worker_id(), POLICY)
    assert admin.execute("SELECT claimed_wake_seq FROM run_claims").fetchone()[0] == before
    with pg_store.transaction():
        pg_store.queue.wake(PROJECT, "run-1")
    assert admin.execute("SELECT wake_seq, claimed_wake_seq FROM run_claims").fetchone() == (before + 1, before)
    assert claims.release(claim.fence, requeue=False)


def test_a_claim_needs_a_real_run(pg_store, open_claims):
    add_runs(pg_store, "run-1")
    with pytest.raises(RecordNotFoundError):
        open_claims().enqueue(PROJECT, "run-404")


@pytest.mark.parametrize("statement", [
    # an owner without a lease
    "UPDATE run_claims SET worker_id = 'wkr-" + "a" * 20 + "', claimed_at = now(), last_heartbeat_at = now(), fencing_token = 1",
    # generation 0 with a claim time
    "UPDATE run_claims SET claimed_at = now()",
    # a worker identity that is a hostname
    "UPDATE run_claims SET worker_id = 'my-laptop', claimed_at = now(), last_heartbeat_at = now(), fencing_token = 1, "
    "lease_expires_at = now() + interval '1 minute'",
    # a lease longer than five minutes
    "UPDATE run_claims SET worker_id = 'wkr-" + "a" * 20 + "', claimed_at = now(), last_heartbeat_at = now(), "
    "fencing_token = 1, lease_expires_at = now() + interval '6 minutes'",
    # a token that goes negative
    "UPDATE run_claims SET fencing_token = -1",
])  # fmt: skip
def test_the_database_refuses_impossible_claims(pg_store, open_claims, admin, statement):
    import psycopg

    add_runs(pg_store, "run-1")
    open_claims().enqueue(PROJECT, "run-1")
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(statement + " WHERE run_id = 'run-1'")


def test_a_finished_run_is_never_claimed_even_if_its_claim_says_available(pg_store, open_claims):
    add_runs(pg_store, "run-1", status=RunStatus.RUNNING)
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    run = pg_store.runs.get(PROJECT, "run-1")
    pg_store.runs.update(run.transition(RunStatus.COMPLETED, at=timestamp(utc_now), reason=""))
    assert claims.claim_next(new_worker_id(), POLICY) is None


# ============================================================ the acceptance test


@pytest.fixture
def takeover(pg_store, open_claims, open_fenced, admin):
    """Worker A claims generation 41; A stalls past its lease; worker B claims generation 42."""
    add_runs(pg_store, "run-1")
    admin.execute(
        "INSERT INTO run_claims (project_id, run_id, fencing_token, claimed_at, last_heartbeat_at, available_at, "
        "created_at, updated_at) VALUES (%s, 'run-1', 40, now() - interval '2 hours', now() - interval '2 hours', "
        "now(), now() - interval '3 hours', now())",
        (PROJECT,),
    )  # generations 1..40 came and went
    claims = open_claims()
    a = claims.claim_next(new_worker_id(), POLICY)
    assert a.fence.fencing_token == 41
    a_store = open_fenced(a.fence)
    a_store.unit_of_work.prove_ownership()  # A really owned the run
    age_claim(admin, PROJECT, "run-1")  # A stops heartbeating; its lease runs out (database time)
    b = claims.claim_next(new_worker_id(), POLICY)
    assert b.fence.fencing_token == 42 and b.taken_over and b.previous_worker_id == a.fence.worker_id
    return SimpleNamespace(claims=claims, a=a, b=b, a_store=a_store, b_store=open_fenced(b.fence), plain=pg_store)


def test_generation_41_is_refused_once_42_exists_although_worker_a_still_trusts_its_lease(takeover):
    """THE fencing test (docs/workers.md): A's own view of its lease does not matter; the database decides."""
    t = takeover

    class FarMonotonic:  # A's local clock says its lease has plenty of time left
        def __call__(self):
            return 0.0

    keeper = LeaseKeeper(lambda: t.claims, t.a, POLICY, WorkerEvents("a"), sent_at=0.0, monotonic=FarMonotonic())
    import threading

    ownership = ClaimOwnership(keeper, t.a_store.unit_of_work.prove_ownership, stop=threading.Event())
    assert keeper.trusted() and keeper.lost is None  # A believes it still owns the run
    with pytest.raises(OwnershipLost) as lost:
        ownership.verify()
    assert lost.value.reason == "superseded" and "41" in str(lost.value) and "42" in str(lost.value)


def _execution(project_id: str) -> ActionExecution:
    action = ExactAction(tool="send_notice", category="external_communication", params={"to": "x@example.test", "text": "t"})
    spec = ExecutionSpec(
        project_id=project_id, run_id="run-1", request_id="apr-" + "0" * 16, node="operate", visit=1, agent="operator",
        action=action, semantics=semantics_of(fx.SendNoticeTool(".")), authorized_at=timestamp(utc_now),
    )  # fmt: skip
    return ActionExecution.authorized(spec, at=timestamp(utc_now))


def _stale_operations(t):
    now = timestamp(utc_now)
    run = t.plain.runs.get(PROJECT, "run-1")
    project = t.plain.projects.get(PROJECT)
    state = ProjectState(run_id="run-1", graph_name="g", task=TaskSpec(title="t", goal="g"), current_node="plan",
                         status=RunStatus.COMPLETED, created_at=run.created_at, updated_at=now)  # fmt: skip
    milestone = Milestone(
        id="ms-" + "1" * 20, project_id=PROJECT, run_id="run-1", sequence=1, key="step:0", type="run_finished",
        category=MilestoneCategory.LIFECYCLE, title="t", summary="s", status=MilestoneStatus.COMPLETED, created_at=now,
        occurred_at=now, source=MilestoneSource(), metadata={},
    )  # fmt: skip
    action = ExactAction(tool="send_notice", category="external_communication", params={"to": "x@example.test", "text": "t"})
    request = ApprovalRequest(
        id="apr-" + "0" * 16, project_id=PROJECT, run_id="run-1", node="operate", agent="operator", action=action,
        action_fingerprint=action.fingerprint, reason="r", risk=risk_for(action.category), requested_at=now,
        expires_at=approval_expiry(now),
    )  # fmt: skip
    return {
        "prove ownership": lambda s: s.unit_of_work.prove_ownership(),
        "change the run status": lambda s: s.runs.update(run.transition(RunStatus.FAILED, at=now, reason="stale")),
        "sync product records": lambda s: ProgressRecorder(s, project).sync(state),
        "record a milestone": lambda s: s.milestones.add(milestone),
        "open an approval request": lambda s: ApprovalService(s.approvals).open_request(request),
        "authorize an execution": lambda s: ExecutionLedger(s.executions, kind=LedgerKind.PRODUCT_STORE).authorize(
            ExecutionSpec(project_id=PROJECT, run_id="run-1", request_id=request.id, node="operate", visit=1,
                          agent="operator", action=action, semantics=semantics_of(fx.SendNoticeTool(".")),
                          authorized_at=now)),  # fmt: skip
        "write-ahead a dispatch": lambda s: ExecutionLedger(s.executions, kind=LedgerKind.PRODUCT_STORE).begin(
            _execution(PROJECT), owner=t.a.fence.owner
        ),
        "read the run": lambda s: s.runs.get(PROJECT, "run-1"),  # even reads: a fenced session proves every statement
    }


@pytest.mark.parametrize("operation", [
    "prove ownership", "change the run status", "sync product records", "record a milestone", "open an approval request",
    "authorize an execution", "write-ahead a dispatch", "read the run",
])  # fmt: skip
def test_every_write_of_the_stale_generation_is_refused_and_leaves_nothing(takeover, admin, operation):
    t = takeover
    before = admin.execute(
        "SELECT (SELECT status FROM runs WHERE run_id = 'run-1'), (SELECT count(*) FROM milestones), "
        "(SELECT count(*) FROM approval_requests), (SELECT count(*) FROM action_executions), "
        "(SELECT count(*) FROM action_execution_attempts)"
    ).fetchone()
    with pytest.raises(OwnershipLost) as lost:
        _stale_operations(t)[operation](t.a_store)
    assert lost.value.reason == "superseded"
    after = admin.execute(
        "SELECT (SELECT status FROM runs WHERE run_id = 'run-1'), (SELECT count(*) FROM milestones), "
        "(SELECT count(*) FROM approval_requests), (SELECT count(*) FROM action_executions), "
        "(SELECT count(*) FROM action_execution_attempts)"
    ).fetchone()
    assert after == before == ("running", 0, 0, 0, 0)


def test_the_current_generation_may_write(takeover, admin):
    t = takeover
    ops = _stale_operations(t)
    ops["prove ownership"](t.b_store)
    ops["change the run status"](t.b_store)
    ops["record a milestone"](t.b_store)
    assert admin.execute("SELECT status FROM runs WHERE run_id = 'run-1'").fetchone() == ("failed",)


def test_a_stale_heartbeat_is_refused_and_leaves_the_new_owners_lease_alone(takeover, admin):
    t = takeover
    lease_before = claim_row(admin).lease
    assert t.claims.renew(t.a.fence, POLICY) is None
    row = claim_row(admin)
    assert (row.worker_id, row.token, row.lease) == (t.b.fence.worker_id, 42, lease_before)


def test_a_stale_release_is_refused_and_the_new_owner_keeps_the_run(takeover, admin):
    t = takeover
    available_before = claim_row(admin).available
    assert t.claims.release(t.a.fence, requeue=False) is False
    row = claim_row(admin)
    assert (row.worker_id, row.token, row.available) == (t.b.fence.worker_id, 42, available_before)
    t.claims.verify(t.b.fence)


# ========================================================= more fencing rules


def test_the_same_worker_cannot_write_with_its_previous_generation(pg_store, open_claims, open_fenced, admin):
    """Fencing is by generation, not by worker: a worker that re-claims its own run fences out its old self."""
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    worker = new_worker_id()
    first = claims.claim_next(worker, POLICY)
    old_session = open_fenced(first.fence)
    age_claim(admin, PROJECT, "run-1")
    second = claims.claim_next(worker, POLICY)
    assert second.fence.worker_id == worker and second.fence.fencing_token == 2
    with pytest.raises(OwnershipLost) as lost:
        old_session.runs.get(PROJECT, "run-1")
    assert lost.value.reason == "superseded"
    assert claims.renew(first.fence, POLICY) is None and not claims.release(first.fence, requeue=False)
    open_fenced(second.fence).unit_of_work.prove_ownership()


def test_an_expired_lease_refuses_writes_and_renewal_before_anyone_takes_over(pg_store, open_claims, open_fenced, admin):
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    claim = claims.claim_next(new_worker_id(), POLICY)
    store = open_fenced(claim.fence)
    store.unit_of_work.prove_ownership()
    age_claim(admin, PROJECT, "run-1")  # nobody else has claimed it
    with pytest.raises(OwnershipLost) as lost:
        store.unit_of_work.prove_ownership()
    assert lost.value.reason == "expired"
    assert claims.renew(claim.fence, POLICY) is None  # an expired lease is lost, not renewable
    assert claim_row(admin).worker_id == claim.fence.worker_id  # still recorded, but powerless


def test_the_lease_expiry_instant_belongs_to_the_next_owner(pg_store, open_claims):
    """Clock boundary, in one transaction (one now()): at lease_expires_at = now() the lease has expired."""
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    a = claims.claim_next(new_worker_id(), POLICY)
    db = claims.db
    with db.transaction():  # every statement below sees the same now()
        db.query("UPDATE run_claims SET lease_expires_at = now() WHERE run_id = 'run-1'")
        with pytest.raises(OwnershipLost) as lost:
            claims.verify(a.fence)  # valid only while now() < lease_expires_at
        assert lost.value.reason == "expired"
        assert claims.renew(a.fence, POLICY) is None
        b = claims.claim_next(new_worker_id(), POLICY)  # expired once lease_expires_at <= now()
        assert b is not None and b.fence.fencing_token == 2
        db.query("UPDATE run_claims SET lease_expires_at = now() + interval '1 microsecond' WHERE run_id = 'run-1'")
        claims.verify(b.fence)  # one microsecond before its end, a lease is still valid
        assert claims.claim_next(new_worker_id(), POLICY) is None


def test_fencing_tokens_only_ever_increase_by_one_per_claim(pg_store, open_claims, admin):
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    tokens = []
    for i in range(30):
        claim = claims.claim_next(new_worker_id(), POLICY)
        tokens.append(claim.fence.fencing_token)
        claims.renew(claim.fence, POLICY)  # renewing never changes the generation
        if i % 2:
            claims.release(claim.fence, requeue=True)  # nor does releasing
        else:
            age_claim(admin, PROJECT, "run-1")  # a takeover after expiry
    assert tokens == list(range(1, 31))


def test_a_claimer_skips_a_run_another_transaction_has_locked_instead_of_waiting(pg_store, open_claims, admin):
    add_runs(pg_store, "run-1", "run-2")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    time.sleep(0.01)
    claims.enqueue(PROJECT, "run-2")
    with admin.transaction():
        admin.execute("SELECT 1 FROM run_claims WHERE run_id = 'run-1' FOR UPDATE")  # another claimer, mid-claim
        started = time.monotonic()
        claim = claims.claim_next(new_worker_id(), POLICY)
        assert claim.fence.run_id == "run-2"  # the locked, older run was skipped
        assert claims.claim_next(new_worker_id(), POLICY) is None  # and is not waited for
        assert time.monotonic() - started < 2.0  # lock_timeout is 10 s: no waiting happened
    assert claims.claim_next(new_worker_id(), POLICY).fence.run_id == "run-1"


def test_a_fenced_write_in_progress_holds_off_a_takeover_until_it_ends(pg_store, open_claims, open_fenced, admin):
    """FOR SHARE: a takeover can never commit in the middle of a fenced write."""
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    a = claims.claim_next(new_worker_id(), SHORT)
    a_store = open_fenced(a.fence)
    run = a_store.runs.get(PROJECT, "run-1")
    with a_store.transaction():  # proves the fence and holds FOR SHARE on the claim until it ends
        a_store.runs.update(run.transition(RunStatus.BLOCKED, at=timestamp(utc_now), reason="in progress"))
        wait_until(lambda: claims.now() > a.lease_expires_at, timeout=10)  # the lease runs out meanwhile
        assert claims.claim_next(new_worker_id(), POLICY) is None  # the row is locked: skipped, not taken
    b = claims.claim_next(new_worker_id(), POLICY)  # after the write committed, the takeover happens
    assert b.fence.fencing_token == 2
    assert admin.execute("SELECT status FROM runs WHERE run_id = 'run-1'").fetchone() == ("blocked",)
    with pytest.raises(OwnershipLost):
        a_store.runs.get(PROJECT, "run-1")


def test_a_claim_with_real_time_expiry_is_taken_over_and_the_old_owner_fenced(pg_store, open_claims, open_fenced):
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    a = claims.claim_next(new_worker_id(), SHORT)
    assert claims.claim_next(new_worker_id(), SHORT) is None
    wait_until(lambda: claims.now() >= a.lease_expires_at, timeout=10)
    b = claims.claim_next(new_worker_id(), SHORT)
    assert b.fence.fencing_token == 2 and b.previous_worker_id == a.fence.worker_id
    with pytest.raises(OwnershipLost):
        open_fenced(a.fence).unit_of_work.prove_ownership()


# ===================================================================== races


@pytest.mark.parametrize("workers", [2, 10])
def test_workers_racing_for_one_run_produce_exactly_one_owner(pg_store, open_claims, workers):
    rounds = 15 if workers == 2 else 8
    add_runs(pg_store, *[f"run-{i}" for i in range(rounds)])
    sessions = [open_claims() for _ in range(workers)]
    for i in range(rounds):
        sessions[0].enqueue(PROJECT, f"run-{i}")
        ids = [new_worker_id() for _ in range(workers)]
        results = race([lambda s=s, w=w: s.claim_next(w, POLICY) for s, w in zip(sessions, ids, strict=True)])
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, results
        assert winners[0].fence.run_id == f"run-{i}" and winners[0].fence.fencing_token == 1
        stored = sessions[0].get(PROJECT, f"run-{i}")
        assert (stored.worker_id, stored.fencing_token) == (winners[0].fence.worker_id, 1)


def test_ten_workers_claim_ten_different_runs_each_exactly_once(pg_store, open_claims):
    for round_ in range(4):
        run_ids = [f"r{round_}-{i}" for i in range(10)]
        add_runs(pg_store, *run_ids)
        sessions = [open_claims() for _ in range(10)]
        for run_id in run_ids:
            sessions[0].enqueue(PROJECT, run_id)
        started = time.monotonic()
        results = race([lambda s=s: s.claim_next(new_worker_id(), POLICY) for s in sessions])
        assert time.monotonic() - started < 9  # SKIP LOCKED: nobody waited out a lock
        claimed = sorted(r.fence.run_id for r in results if r is not None)
        assert claimed == sorted(run_ids)  # every run claimed, none twice
        assert len({r.fence.worker_id for r in results}) == 10
        for session in sessions:
            session.close()


def test_a_renewal_racing_a_takeover_never_leaves_two_owners(pg_store, open_claims, admin):
    add_runs(pg_store, "run-1")
    a_session, b_session = open_claims(), open_claims()
    a_session.enqueue(PROJECT, "run-1")
    outcomes = set()
    current = a_session.claim_next(new_worker_id(), POLICY)
    for i in range(40):
        # Put the end of the current owner's lease right around the moment both act.
        offset_ms = (i % 9 - 4) * 2
        age_claim(admin, PROJECT, "run-1", seconds=10)
        admin.execute(
            "UPDATE run_claims SET lease_expires_at = now() + make_interval(secs => %s) WHERE run_id = 'run-1'",
            (offset_ms / 1000 + 0.004,),
        )
        taker = new_worker_id()
        renewed, taken = race([lambda: a_session.renew(current.fence, POLICY), lambda: b_session.claim_next(taker, POLICY)])
        assert not (renewed is not None and taken is not None), f"round {i}: both the old owner and the taker won"
        row = claim_row(admin)
        if taken is not None:
            assert (row.worker_id, row.token) == (taker, current.fence.fencing_token + 1)
            with pytest.raises(OwnershipLost):
                a_session.verify(current.fence)
            current = taken
            outcomes.add("taken over")
        elif renewed is not None:
            assert (row.worker_id, row.token) == (current.fence.worker_id, current.fence.fencing_token)
            outcomes.add("renewed")
        else:
            outcomes.add("neither")  # the lease ended between the two: the next claim takes it
            assert row.worker_id == current.fence.worker_id
    assert {"taken over", "renewed"} <= outcomes  # both orders really happened


# ====================================================== stale worker, through the engine


def _claimed_calculator(pg_store, pg_schema, tmp_path, open_claims):
    from agent_factory.worker.sources import WorkspaceRunSource

    ws = new_calculator_run(pg_store, tmp_path / "workspaces")
    claims = open_claims()
    claims.enqueue(ws.project_id, "run-1")
    return ws, claims, WorkspaceRunSource(pg_schema, tmp_path / "workspaces")


def _worker(pg_schema, source, *, policy=POLICY, events=None, **settings):
    from agent_factory.persistence.claims import open_claim_repository

    identity = WorkerIdentity.new()
    return Worker(
        identity, WorkerSettings(policy=policy, poll_interval=0.05, **settings),
        open_claims=lambda: open_claim_repository(pg_schema), source=source,
        events=WorkerEvents(identity.worker_id, sink=events.append if events is not None else None),
    )  # fmt: skip


def test_a_stale_worker_cannot_write_its_checkpoint_or_run_state(pg_store, pg_schema, tmp_path, open_claims, admin):
    ws, claims, source = _claimed_calculator(pg_store, pg_schema, tmp_path, open_claims)
    checkpoint = CheckpointStore(ws.runs_dir).path("run-1")
    before = checkpoint.read_bytes()
    taker = new_worker_id()

    def take_over():  # during A's first node, A stalls past its lease and B claims the run
        age_claim(admin, ws.project_id, "run-1")
        assert claims.claim_next(taker, POLICY).fence.fencing_token == 2

    def adjust(ex):
        ex.agents["planner"] = BeforeAgent(ex.agents["planner"], take_over)

    got = []
    _worker(pg_schema, WrappedSource(source, adjust), events=got, max_runs=1).run()
    assert checkpoint.read_bytes() == before  # A's node result was never committed
    rejected = [e for e in got if e["event"] == "stale_worker_rejected"]
    assert [(e["operation"], e["reason"], e["fencing_token"]) for e in rejected] == [("advance", "superseded", 1)]
    assert "run_released" not in [e["event"] for e in got]
    assert pg_store.runs.get(ws.project_id, "run-1").status is RunStatus.RUNNING
    assert admin.execute("SELECT count(*) FROM milestones WHERE run_id = 'run-1'").fetchone() == (0,)
    row = claim_row(admin, project=ws.project_id)
    assert (row.worker_id, row.token) == (taker, 2)
    # B's claim here is a bare claim with no worker behind it: once it expires, a real worker
    # (generation 3) finishes the run from the last state A committed while it owned the run.
    age_claim(admin, ws.project_id, "run-1")
    _worker(pg_schema, source, max_runs=1).run()
    assert pg_store.runs.get(ws.project_id, "run-1").status is RunStatus.COMPLETED


@pytest.fixture
def approved_notice_run(tmp_path, pg_store, open_claims):
    """The fixture run (one approved class C notice), approved and queued for a worker."""
    claims = open_claims()
    ws = approved_notice(pg_store, claims, tmp_path / "ws", tmp_path / "oracle")
    return SimpleNamespace(ws=ws, oracle=tmp_path / "oracle", claims=claims)


def test_a_stale_worker_cannot_record_a_dispatch_so_nothing_is_sent(approved_notice_run, pg_schema, admin, monkeypatch):
    r = approved_notice_run
    taker = new_worker_id()
    real_begin = ExecutionLedger.begin

    def begin(self, execution, *, owner):
        if owner.endswith("@1"):  # A is about to write the dispatch ahead: B takes the run over first
            age_claim(admin, r.ws.project_id, "run-1")
            assert r.claims.claim_next(taker, POLICY).fence.fencing_token == 2
        return real_begin(self, execution, owner=owner)

    monkeypatch.setattr(ExecutionLedger, "begin", begin)
    got = []
    _worker(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle), events=got, max_runs=1).run()
    assert fx.read_lines(r.oracle / "notices.jsonl") == []  # NOT dispatched
    assert admin.execute("SELECT count(*) FROM action_execution_attempts").fetchone() == (0,)  # no write-ahead row
    assert [e["reason"] for e in got if e["event"] == "stale_worker_rejected"] == ["superseded"]
    # B, the current owner, sends it exactly once.
    age_claim(admin, r.ws.project_id, "run-1")
    _worker(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle), max_runs=1).run()
    assert len(fx.read_lines(r.oracle / "notices.jsonl")) == 1
    assert admin.execute("SELECT state FROM action_executions").fetchone() == ("succeeded",)


def test_ownership_lost_after_the_write_ahead_record_means_the_tool_never_runs(approved_notice_run, pg_schema, admin, monkeypatch):
    r = approved_notice_run
    taker = new_worker_id()
    real_begin = ExecutionLedger.begin

    def begin(self, execution, *, owner):
        result = real_begin(self, execution, owner=owner)  # the write-ahead commits under A's valid fence...
        if owner.endswith("@1"):
            age_claim(admin, r.ws.project_id, "run-1")  # ...and then A stalls and B takes over
            assert r.claims.claim_next(taker, POLICY).fence.fencing_token == 2
        return result

    monkeypatch.setattr(ExecutionLedger, "begin", begin)
    got = []
    _worker(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle), events=got, max_runs=1).run()
    assert fx.read_lines(r.oracle / "notices.jsonl") == []  # the last check before the tool refused
    attempt = admin.execute("SELECT owner, ended_at FROM action_execution_attempts").fetchone()
    assert attempt[0].endswith("@1") and attempt[1] is None  # the write-ahead row stays open...
    events = [e["event"] for e in fx.executor(r.ws, "notice", r.oracle).log_for("run-1").entries()]
    assert events[-2:] == ["action_dispatching", "action_not_dispatched"]  # ...with the evidence beside it
    monkeypatch.setattr(ExecutionLedger, "begin", real_begin)
    age_claim(admin, r.ws.project_id, "run-1")
    _worker(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle), max_runs=1).run()
    assert len(fx.read_lines(r.oracle / "notices.jsonl")) == 1  # recovered as not sent, then sent once
    results = admin.execute("SELECT owner, result FROM action_execution_attempts ORDER BY attempt").fetchall()
    assert [res for _, res in results] == ["not_dispatched", "succeeded"]
    assert results[0][0].endswith("@1") and results[1][0].endswith("@3")


def test_an_action_in_flight_when_ownership_changes_is_recovered_from_its_evidence_not_resent(approved_notice_run, pg_schema, admin, monkeypatch):
    r = approved_notice_run
    taker = new_worker_id()

    class TakenOverWhileSending(fx.SendNoticeTool):
        def run(self, workspace, *, to, text):
            result = super().run(workspace, to=to, text=text)  # the notice is out...
            age_claim(admin, r.ws.project_id, "run-1")  # ...and the worker loses the run before recording it
            assert r.claims.claim_next(taker, POLICY) is not None
            return result

    monkeypatch.setattr(fx, "tool_for", lambda kind, oracle: TakenOverWhileSending(oracle))
    got = []
    _worker(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle), events=got, max_runs=1).run()
    assert len(fx.read_lines(r.oracle / "notices.jsonl")) == 1
    assert admin.execute("SELECT state FROM action_executions").fetchone() == ("dispatching",)  # outcome not recorded
    monkeypatch.setattr(fx, "tool_for", lambda kind, oracle: fx.SendNoticeTool(oracle))
    age_claim(admin, r.ws.project_id, "run-1")
    _worker(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle), max_runs=1).run()
    assert len(fx.read_lines(r.oracle / "notices.jsonl")) == 1  # never resent: the log held the outcome
    assert admin.execute("SELECT state FROM action_executions").fetchone() == ("succeeded",)


# ============================================== connection drops during heartbeat


def _backend_pid(repo):
    return repo.db.conn.info.backend_pid


def test_a_dropped_heartbeat_connection_is_replaced_while_the_database_is_healthy(pg_store, open_claims, admin):
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    policy = LeasePolicy(lease=timedelta(seconds=3), heartbeat_interval=timedelta(milliseconds=300))
    claim = claims.claim_next(new_worker_id(), policy)
    got = []
    keeper = LeaseKeeper(open_claims, claim, policy, WorkerEvents("a", sink=got.append), sent_at=time.monotonic())
    keeper.start()
    try:
        wait_until(lambda: keeper.renewals >= 1, timeout=5)
        admin.execute("SELECT pg_terminate_backend(%s)", (_backend_pid(keeper._repo),))  # the connection drops
        renewals = keeper.renewals
        wait_until(lambda: keeper.renewals >= renewals + 3, timeout=5)  # a new session renews again
    finally:
        keeper.stop()
    assert keeper.lost is None
    row = claim_row(admin)
    assert (row.worker_id, row.token) == (claim.fence.worker_id, 1)  # still the same owner: no takeover happened


def test_a_worker_that_cannot_reach_the_database_stops_advancing_and_is_taken_over(
    pg_store, pg_schema, tmp_path, open_claims, admin, monkeypatch
):
    """Fail closed: no confirmed heartbeat in time means no more writes, and another worker recovers the run."""
    import agent_factory.worker.loop as loop_module
    from agent_factory.domain.errors import StorageUnavailableError
    from agent_factory.persistence.claims import open_claim_repository

    ws, claims, source = _claimed_calculator(pg_store, pg_schema, tmp_path, open_claims)
    checkpoint = CheckpointStore(ws.runs_dir).path("run-1")
    before = checkpoint.read_bytes()
    policy = LeasePolicy(lease=timedelta(seconds=3), heartbeat_interval=timedelta(milliseconds=500))
    sessions, cut = [], {"off": False}

    def keeper_session():  # the heartbeat's sessions: once cut off, the database cannot be reached
        if cut["off"]:
            raise StorageUnavailableError("database unavailable: connection refused")
        sessions.append(open_claim_repository(pg_schema))
        return sessions[-1]

    real_keeper = loop_module.LeaseKeeper
    monkeypatch.setattr(loop_module, "LeaseKeeper", lambda _open, *a, **kw: real_keeper(keeper_session, *a, **kw))

    def lose_the_database():  # during the first node: the heartbeat connection drops and cannot come back
        cut["off"] = True
        admin.execute("SELECT pg_terminate_backend(%s)", (_backend_pid(sessions[-1]),))
        time.sleep(3.0)  # past the local deadline (lease - margin = 2.25 s)

    def adjust(ex):
        ex.agents["planner"] = BeforeAgent(ex.agents["planner"], lose_the_database)

    got = []
    _worker(pg_schema, WrappedSource(source, adjust), policy=policy, events=got, max_runs=1).run()
    assert checkpoint.read_bytes() == before  # nothing committed without a confirmed lease
    reasons = [(e["operation"], e["reason"]) for e in got if e["event"] == "stale_worker_rejected"]
    assert ("heartbeat", "unconfirmed") in reasons and ("advance", "unconfirmed") in reasons
    assert "run_released" not in [e["event"] for e in got]
    monkeypatch.setattr(loop_module, "LeaseKeeper", real_keeper)
    wait_until(lambda: claims.now() >= claims.get(ws.project_id, "run-1").lease_expires_at, timeout=10)
    _worker(pg_schema, source, max_runs=1).run()  # another worker takes over (generation 2) and finishes
    assert claims.get(ws.project_id, "run-1").fencing_token == 2
    assert pg_store.runs.get(ws.project_id, "run-1").status is RunStatus.COMPLETED


def test_the_claim_query_holds_no_transaction_open_while_a_run_is_advanced(pg_store, pg_schema, tmp_path, open_claims, admin):
    """Claim in a short transaction, execute outside it: no worker session sits idle in a transaction."""
    ws, claims, source = _claimed_calculator(pg_store, pg_schema, tmp_path, open_claims)
    seen = []

    def look():
        seen.extend(admin.execute(
            "SELECT state FROM pg_stat_activity WHERE application_name = 'agent-factory-worker' "
            "AND datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchall())  # fmt: skip

    def adjust(ex):
        ex.agents["developer"] = BeforeAgent(ex.agents["developer"], look)

    _worker(pg_schema, WrappedSource(source, adjust), max_runs=1).run()
    assert seen and all(state != ("idle in transaction",) for state in seen), seen
    assert pg_store.runs.get(ws.project_id, "run-1").status is RunStatus.COMPLETED


def test_two_claims_of_different_workers_never_share_a_generation(pg_store, open_claims, admin):
    add_runs(pg_store, "run-1")
    claims = open_claims()
    claims.enqueue(PROJECT, "run-1")
    seen = {}
    for _ in range(10):
        claim = claims.claim_next(new_worker_id(), POLICY)
        assert claim.fence.fencing_token not in seen
        seen[claim.fence.fencing_token] = claim.fence.worker_id
        age_claim(admin, PROJECT, "run-1")
    import psycopg

    with pytest.raises(psycopg.errors.UniqueViolation):  # one claim row per run: no second, parallel history
        admin.execute(
            "INSERT INTO run_claims (project_id, run_id, fencing_token, created_at, updated_at) "
            "VALUES (%s, 'run-1', 0, now(), now())",
            (PROJECT,),
        )

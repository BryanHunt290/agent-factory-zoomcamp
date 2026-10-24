"""Run claims: the state model, lease arithmetic, fencing comparisons, and configuration (no database).

The in-memory claim repository follows the same rules as PostgreSQL and is
used here to pin those rules down. It proves nothing about coordination
between processes: tests/test_postgres_claims.py does that, against a real
database (docs/workers.md).
"""

from datetime import UTC, datetime, timedelta

import pytest

from agent_factory.domain import Project, Run, in_memory_store, standard_milestone_policy
from agent_factory.domain.claims import WORKER_ID_RE, Claim, Fence, LeasePolicy, OwnershipLost, RunClaim, new_worker_id
from agent_factory.domain.errors import DomainValidationError, RecordNotFoundError
from agent_factory.domain.repositories import InMemoryClaimRepository
from agent_factory.domain.serialization import format_timestamp
from agent_factory.state import RunStatus

PROJECT = "prj-" + "1" * 20
T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)


def ts(seconds: float = 0) -> str:
    return format_timestamp(T0 + timedelta(seconds=seconds))


class Clock:
    """The coordinating clock of the in-memory store (PostgreSQL's now() in production)."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def set(self, moment: str) -> None:
        self.now = datetime.fromisoformat(moment.replace("Z", "+00:00"))


@pytest.fixture
def clock():
    return Clock()


def _store_with_runs(*run_ids, status=RunStatus.RUNNING):
    store = in_memory_store()
    store.projects.add(Project(id=PROJECT, name="p", created_at=ts(), milestone_policy=standard_milestone_policy()))
    for number, run_id in enumerate(run_ids, start=1):
        store.runs.add(
            Run(project_id=PROJECT, run_id=run_id, number=number, graph_name="g", status=status, created_at=ts(),
                updated_at=ts(), status_reason="")
        )  # fmt: skip
    return store


@pytest.fixture
def claims(clock):
    store = _store_with_runs("run-1", "run-2", "run-3")
    repo = InMemoryClaimRepository(store.unit_of_work, clock)
    repo.store = store
    return repo


POLICY = LeasePolicy()  # 60 s lease, 20 s heartbeat


# ------------------------------------------------------------ worker identity


def test_worker_ids_are_random_per_process_instance_and_never_a_hostname():
    import socket

    ids = {new_worker_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(WORKER_ID_RE.fullmatch(i) for i in ids)
    assert not any(socket.gethostname().lower() in i for i in ids)


def test_two_worker_identities_in_one_process_differ():
    from agent_factory.worker import WorkerIdentity

    a, b = WorkerIdentity.new(), WorkerIdentity.new()
    assert a.worker_id != b.worker_id and a.pid == b.pid and a.host == b.host


# ------------------------------------------------------------ lease policy


def test_the_default_lease_policy_is_conservative():
    assert POLICY.lease == timedelta(seconds=60)
    assert POLICY.heartbeat_interval == timedelta(seconds=20)
    assert POLICY.margin == timedelta(seconds=15)
    assert POLICY.local_validity == timedelta(seconds=45)
    # Two heartbeats can fail and the third still lands inside the local validity.
    assert 2 * POLICY.heartbeat_interval < POLICY.local_validity


@pytest.mark.parametrize("lease,heartbeat", [
    (timedelta(seconds=1.999), timedelta(milliseconds=500)),  # shorter than the minimum
    (timedelta(minutes=5, microseconds=1), timedelta(seconds=20)),  # longer than the maximum
    (timedelta(seconds=60), timedelta(seconds=20, microseconds=1)),  # fewer than three heartbeats per lease
    (timedelta(seconds=60), timedelta(0)),
    (timedelta(seconds=60), timedelta(seconds=-1)),
])  # fmt: skip
def test_the_lease_policy_refuses_unsafe_settings(lease, heartbeat):
    with pytest.raises(DomainValidationError):
        LeasePolicy(lease=lease, heartbeat_interval=heartbeat)


def test_the_lease_policy_accepts_its_bounds():
    LeasePolicy(lease=LeasePolicy.MIN_LEASE, heartbeat_interval=timedelta(milliseconds=600))
    LeasePolicy(lease=LeasePolicy.MAX_LEASE, heartbeat_interval=timedelta(seconds=100))  # exactly three per lease


def test_the_lease_policy_refuses_non_durations():
    with pytest.raises(DomainValidationError):
        LeasePolicy(lease=60)  # type: ignore[arg-type]


def test_the_local_deadline_is_measured_from_the_send_time_and_ends_before_the_lease():
    # Sent at monotonic 100.0: trusted until 145.0, while the database's lease ends 60 s after
    # its own now(), which is no earlier than the send. The local deadline can only be early.
    assert POLICY.local_deadline(100.0) == pytest.approx(145.0)
    assert POLICY.local_deadline(100.0) < 100.0 + POLICY.lease.total_seconds()


# ------------------------------------------------------------ fences


def test_a_fence_needs_a_real_project_run_worker_and_generation():
    Fence(PROJECT, "run-1", new_worker_id(), 1)
    for bad in [
        ("not-a-project", "run-1", new_worker_id(), 1),
        (PROJECT, "bad run", new_worker_id(), 1),
        (PROJECT, "run-1", "host-a:123", 1),  # a hostname is not a worker identity
        (PROJECT, "run-1", new_worker_id(), 0),  # generation 0 means "never claimed"
        (PROJECT, "run-1", new_worker_id(), True),
    ]:
        with pytest.raises(DomainValidationError):
            Fence(*bad)


def test_the_owner_recorded_on_attempts_names_the_worker_and_its_generation():
    worker = new_worker_id()
    assert Fence(PROJECT, "run-1", worker, 42).owner == f"{worker}@42"


def test_ownership_lost_is_not_an_exception_so_broad_handlers_cannot_swallow_it():
    assert issubclass(OwnershipLost, BaseException) and not issubclass(OwnershipLost, Exception)
    with pytest.raises(ValueError):
        OwnershipLost("x", reason="because I said so")
    try:
        try:
            raise OwnershipLost("lost", reason="superseded")
        except Exception:  # noqa: BLE001 - the point of the test
            pytest.fail("an except Exception handler caught OwnershipLost")
    except OwnershipLost as exc:
        assert exc.reason == "superseded"


# ------------------------------------------------------------ the claim record


def owned(worker, token=41, *, claimed=0, expires=60, heartbeat=None, available=0) -> RunClaim:
    return RunClaim(
        project_id=PROJECT, run_id="run-1", fencing_token=token, created_at=ts(-100), updated_at=ts(claimed),
        worker_id=worker, claimed_at=ts(claimed), lease_expires_at=ts(expires),
        last_heartbeat_at=ts(claimed if heartbeat is None else heartbeat), available_at=ts(available),
    )  # fmt: skip


@pytest.mark.parametrize("changes", [
    {"fencing_token": 0},  # generation 0 with a claim time
    {"lease_expires_at": None},  # an owner without a lease
    {"last_heartbeat_at": None},  # an owner without a heartbeat
    {"lease_expires_at": ts(0)},  # a lease that ends when it starts
    {"lease_expires_at": ts(301)},  # longer than five minutes
    {"last_heartbeat_at": ts(-1)},  # a heartbeat before the claim
    {"worker_id": "host-a:1:abcd"},
    {"fencing_token": -1},
])  # fmt: skip
def test_the_claim_record_refuses_impossible_states(changes):
    from dataclasses import asdict

    with pytest.raises(DomainValidationError):
        RunClaim(**{**asdict(owned(new_worker_id())), **changes})


def test_a_free_claim_has_no_lease_and_a_never_claimed_one_has_generation_zero():
    RunClaim(project_id=PROJECT, run_id="run-1", fencing_token=0, created_at=ts(), updated_at=ts(), available_at=ts())
    with pytest.raises(DomainValidationError):
        RunClaim(project_id=PROJECT, run_id="run-1", fencing_token=0, created_at=ts(), updated_at=ts(), worker_id=new_worker_id(),
                 lease_expires_at=ts(60))  # fmt: skip


def test_the_lease_boundary_instant_belongs_to_expiry():
    claim = owned(new_worker_id(), expires=60)
    assert claim.lease_valid(ts(59.999999)) and not claim.lease_expired(ts(59.999999))
    assert not claim.lease_valid(ts(60)) and claim.lease_expired(ts(60))  # lease_expires_at <= now: expired
    assert not claim.lease_valid(ts(61)) and claim.lease_expired(ts(61))


def test_fencing_admits_only_the_current_generation_of_the_current_worker_while_its_lease_is_valid():
    worker = new_worker_id()
    claim = owned(worker, token=42)
    assert claim.admits(Fence(PROJECT, "run-1", worker, 42), ts(10))
    assert not claim.admits(Fence(PROJECT, "run-1", worker, 41), ts(10))  # an older generation of the same worker
    assert not claim.admits(Fence(PROJECT, "run-1", worker, 43), ts(10))  # a generation that does not exist
    assert not claim.admits(Fence(PROJECT, "run-1", new_worker_id(), 42), ts(10))
    assert not claim.admits(Fence(PROJECT, "run-2", worker, 42), ts(10))
    assert not claim.admits(Fence(PROJECT, "run-1", worker, 42), ts(60))  # the lease ended


def test_a_refusal_says_why():
    worker = new_worker_id()
    assert owned(worker, token=42).refusal(Fence(PROJECT, "run-1", worker, 41), ts(10)).reason == "superseded"
    assert owned(worker, token=42).refusal(Fence(PROJECT, "run-1", worker, 42), ts(60)).reason == "expired"
    released = RunClaim(project_id=PROJECT, run_id="run-1", fencing_token=42, created_at=ts(-100), updated_at=ts(5),
                        claimed_at=ts(0), last_heartbeat_at=ts(0))  # fmt: skip
    assert released.refusal(Fence(PROJECT, "run-1", worker, 42), ts(10)).reason == "released"


def test_claimable_means_wanted_now_and_without_a_live_owner():
    worker = new_worker_id()
    assert not owned(worker, expires=60).claimable(ts(30))  # live owner
    assert owned(worker, expires=60).claimable(ts(60))  # expired at the boundary: claimable
    free = RunClaim(project_id=PROJECT, run_id="run-1", fencing_token=3, created_at=ts(-100), updated_at=ts(-100),
                    claimed_at=ts(-100), last_heartbeat_at=ts(-100), available_at=ts(10))  # fmt: skip
    assert not free.claimable(ts(9.999999)) and free.claimable(ts(10))
    from dataclasses import replace

    assert not replace(free, available_at=None).claimable(ts(10_000))  # nobody wants it


def test_a_claim_result_records_whom_it_took_over_from():
    worker, previous = new_worker_id(), new_worker_id()
    fence = Fence(PROJECT, "run-1", worker, 42)
    assert not Claim(fence, ts(), ts(60)).taken_over
    assert Claim(fence, ts(), ts(60), previous, 41, ts(-1)).taken_over
    with pytest.raises(DomainValidationError):
        Claim(fence, ts(), ts(60), previous, None, ts(-1))


# ------------------------------------------------------------ the in-memory repository


def test_enqueue_needs_a_real_run_and_starts_at_generation_zero(claims):
    with pytest.raises(RecordNotFoundError):
        claims.enqueue(PROJECT, "run-9")
    claim = claims.enqueue(PROJECT, "run-1")
    assert (claim.fencing_token, claim.worker_id, claim.available_at) == (0, None, ts())


def test_one_claim_per_run_and_each_claim_adds_exactly_one_to_the_token(claims, clock):
    claims.enqueue(PROJECT, "run-1")
    a, b = new_worker_id(), new_worker_id()
    first = claims.claim_next(a, POLICY)
    assert first.fence == Fence(PROJECT, "run-1", a, 1) and first.lease_expires_at == ts(60)
    assert claims.claim_next(b, POLICY) is None  # a live owner: nothing for b
    clock.advance(60)  # a's lease has just expired
    second = claims.claim_next(b, POLICY)
    assert second.fence.fencing_token == 2 and second.taken_over
    assert (second.previous_worker_id, second.previous_fencing_token, second.previous_lease_expires_at) == (a, 1, ts(60))


def test_a_takeover_needs_the_lease_to_have_expired_by_the_coordinating_clock(claims, clock):
    claims.enqueue(PROJECT, "run-1")
    claims.claim_next(new_worker_id(), POLICY)
    clock.advance(59.999999)
    assert claims.claim_next(new_worker_id(), POLICY) is None
    clock.advance(0.000001)  # exactly lease_expires_at
    assert claims.claim_next(new_worker_id(), POLICY).fence.fencing_token == 2


def test_runs_are_claimed_oldest_available_first(claims, clock):
    for run_id in ("run-3", "run-1", "run-2"):
        claims.enqueue(PROJECT, run_id)
        clock.advance(1)
    got = [claims.claim_next(new_worker_id(), POLICY).fence.run_id for _ in range(3)]
    assert got == ["run-3", "run-1", "run-2"]


def test_a_finished_run_is_never_handed_to_a_worker(clock):
    store = _store_with_runs("run-1", status=RunStatus.COMPLETED)
    repo = InMemoryClaimRepository(store.unit_of_work, clock)
    repo.enqueue(PROJECT, "run-1")
    assert repo.claim_next(new_worker_id(), POLICY) is None


def test_renewal_only_for_the_current_generation_and_only_while_the_lease_is_valid(claims, clock):
    claims.enqueue(PROJECT, "run-1")
    a = claims.claim_next(new_worker_id(), POLICY)
    clock.advance(20)
    assert claims.renew(a.fence, POLICY) == ts(80)
    clock.advance(60)  # expired at 80; nobody took over
    assert claims.renew(a.fence, POLICY) is None  # an expired lease is lost, even without a takeover
    assert claims.get(PROJECT, "run-1").lease_expires_at == ts(80)


def test_release_only_for_the_current_generation_and_how_it_requeues(claims, clock):
    claims.enqueue(PROJECT, "run-1")
    a = claims.claim_next(new_worker_id(), POLICY)
    assert not claims.release(Fence(PROJECT, "run-1", a.fence.worker_id, 2), requeue=True)  # not this generation
    assert claims.release(a.fence, requeue=True, delay=timedelta(seconds=30))
    released = claims.get(PROJECT, "run-1")
    assert (released.worker_id, released.lease_expires_at, released.fencing_token, released.available_at) == (None, None, 1, ts(30))
    assert not claims.release(a.fence, requeue=True)  # already released
    b = claims.claim_next(new_worker_id(), POLICY)
    assert b is None  # not before its delay
    clock.advance(30)
    b = claims.claim_next(new_worker_id(), POLICY)
    assert b.fence.fencing_token == 2
    assert claims.release(b.fence, requeue=False)
    assert claims.get(PROJECT, "run-1").available_at is None  # nobody wants it now
    clock.advance(3600)
    assert claims.claim_next(new_worker_id(), POLICY) is None
    with pytest.raises(DomainValidationError):
        claims.release(b.fence, requeue=True, delay=timedelta(seconds=-1))


def test_a_release_that_keeps_the_availability(claims):
    claims.enqueue(PROJECT, "run-1")
    a = claims.claim_next(new_worker_id(), POLICY)
    assert claims.release(a.fence, requeue=None)
    assert claims.get(PROJECT, "run-1").available_at == ts()


@pytest.mark.parametrize(
    "plan",
    [{"requeue": False}, {"requeue": True, "at": ts(7200)}, {"requeue": True, "delay": timedelta(hours=1)}, {"requeue": None}],
    ids=["not-wanted", "at-expiry", "after-delay", "unchanged"],
)
def test_a_wake_up_during_a_claim_survives_the_release_whatever_the_owner_planned(claims, clock, plan):
    """A person decides while a worker holds the run, after it looked: the run must stay wanted now (E5)."""
    from agent_factory.domain.repositories import InMemoryRunQueue

    queue = InMemoryRunQueue(claims.store.unit_of_work, clock)
    claims.enqueue(PROJECT, "run-1")
    claim = claims.claim_next(new_worker_id(), POLICY)
    clock.advance(1)
    queue.wake(PROJECT, "run-1")
    clock.advance(1)
    assert claims.release(claim.fence, **plan)
    released = claims.get(PROJECT, "run-1")
    assert released.available_at == ts(2) and released.woken_while_claimed
    again = claims.claim_next(new_worker_id(), POLICY)
    assert again is not None and not claims.get(PROJECT, "run-1").woken_while_claimed  # the new claim saw it
    assert claims.release(again.fence, requeue=False)
    assert claims.get(PROJECT, "run-1").available_at is None  # without a wake-up, the owner's plan stands


def test_a_dead_owners_run_becomes_claimable_when_its_lease_expires(claims, clock):
    claims.enqueue(PROJECT, "run-1")
    claims.claim_next(new_worker_id(), POLICY)  # the owner dies: it never renews or releases
    clock.advance(60)
    assert claims.claim_next(new_worker_id(), POLICY).taken_over


def test_verify_raises_ownership_lost_with_a_reason(claims, clock):
    with pytest.raises(OwnershipLost) as missing:
        claims.verify(Fence(PROJECT, "run-1", new_worker_id(), 1))
    assert missing.value.reason == "missing"
    claims.enqueue(PROJECT, "run-1")
    a = claims.claim_next(new_worker_id(), POLICY)
    claims.verify(a.fence)
    clock.advance(60)
    with pytest.raises(OwnershipLost) as expired:
        claims.verify(a.fence)
    assert expired.value.reason == "expired"
    b = claims.claim_next(new_worker_id(), POLICY)
    with pytest.raises(OwnershipLost) as superseded:
        claims.verify(a.fence)
    assert superseded.value.reason == "superseded"
    claims.release(b.fence, requeue=False)
    with pytest.raises(OwnershipLost) as released:
        claims.verify(b.fence)
    assert released.value.reason == "released"


def test_generation_41_is_refused_once_42_exists_even_though_41_was_never_told(claims, clock):
    """The fencing example of docs/workers.md, on the in-memory store (the PostgreSQL test is the proof)."""
    claims.enqueue(PROJECT, "run-1")
    for _ in range(40):  # generations 1..40 come and go
        claim = claims.claim_next(new_worker_id(), POLICY)
        claims.release(claim.fence, requeue=True)
    a = claims.claim_next(new_worker_id(), POLICY)
    assert a.fence.fencing_token == 41
    clock.advance(60)
    b = claims.claim_next(new_worker_id(), POLICY)
    assert b.fence.fencing_token == 42
    with pytest.raises(OwnershipLost):
        claims.verify(a.fence)
    assert claims.renew(a.fence, POLICY) is None
    assert not claims.release(a.fence, requeue=False)
    claims.verify(b.fence)  # untouched by a's attempts


def test_claiming_validates_the_worker_id(claims):
    claims.enqueue(PROJECT, "run-1")
    with pytest.raises(DomainValidationError):
        claims.claim_next("my-laptop", POLICY)

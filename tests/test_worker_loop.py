"""The worker loop, lease keeper, and claim ownership on the in-memory store (no database).

These pin down the lifecycle: claim, advance (bounded), heartbeat, release,
graceful shutdown, and what a worker does when it loses its claim or the
database. The in-memory store has no fenced writes; the PostgreSQL tests
(tests/test_postgres_claims.py, tests/test_worker_processes.py) prove the
coordination itself.
"""

import json
from dataclasses import replace
import signal
import threading
from datetime import UTC, datetime, timedelta

import pytest

from agent_factory.agents.base import Agent
from agent_factory.demo import scenario_provider
from agent_factory.domain import in_memory_store
from agent_factory.domain.claims import LeasePolicy, OwnershipLost, new_worker_id
from agent_factory.domain.errors import StorageUnavailableError
from agent_factory.domain.repositories import InMemoryClaimRepository
from agent_factory.domain.serialization import format_timestamp
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.engine.locks import FileLock
from agent_factory.factory import build_executor
from agent_factory.state import RunStatus
from agent_factory.templates import CALCULATOR_TEMPLATE
from agent_factory.worker import (
    ClaimOwnership,
    LeaseKeeper,
    RunUnavailable,
    Worker,
    WorkerEvents,
    WorkerIdentity,
    WorkerSettings,
)

POLICY = LeasePolicy(lease=timedelta(seconds=60), heartbeat_interval=timedelta(seconds=20))
FAST = {"poll_interval": 0.01, "max_backoff": 0.05}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 4, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Monotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Setup:
    """A calculator workspace with created runs, the in-memory store, and its claims."""

    def __init__(self, tmp_path, runs=("run-1",), scenario="repair") -> None:
        self.store = in_memory_store()
        self.ws = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
        self.scenario = scenario
        ex = build_executor(CALCULATOR_TEMPLATE, scenario_provider(scenario), self.ws, product_store=self.store)
        for run_id in runs:
            ex.create(CALCULATOR_TEMPLATE.task, run_id=run_id, metadata={"provider": "fake", "scenario": scenario})
        self.clock = Clock()
        self.claims = InMemoryClaimRepository(self.store.unit_of_work, self.clock)
        for run_id in runs:
            self.claims.enqueue(self.ws.project_id, run_id)
        self.agents: dict[str, Agent] = {}
        self.unavailable: RunUnavailable | None = None
        self.opened = 0

    def open(self, claim):
        self.opened += 1
        return MemoryRun(self, claim)

    def worker(self, events_into=None, **settings) -> Worker:
        identity = WorkerIdentity.new()
        events = WorkerEvents(identity.worker_id, sink=events_into.append if events_into is not None else None)
        return Worker(identity, WorkerSettings(**{**FAST, **settings}), open_claims=lambda: self.claims, source=self, events=events)

    def checkpoint(self, run_id="run-1"):
        return CheckpointStore(self.ws.runs_dir).load(run_id)


class MemoryRun:
    def __init__(self, setup: Setup, claim) -> None:
        self.setup = setup
        self.claim = claim

    def prove(self) -> None:
        self.setup.claims.verify(self.claim.fence)

    def executor(self, ownership):
        if self.setup.unavailable is not None:
            raise self.setup.unavailable
        ex = build_executor(
            CALCULATOR_TEMPLATE, scenario_provider(self.setup.scenario), self.setup.ws, product_store=self.setup.store,
            ownership=ownership,
        )  # fmt: skip
        ex.agents.update(self.setup.agents)
        return ex

    def close(self) -> None:
        pass


def names(records):
    return [r["event"] for r in records]


# ------------------------------------------------------------------ lifecycle


def test_a_worker_claims_a_run_advances_it_and_releases_it_when_it_finishes(tmp_path):
    s = Setup(tmp_path)
    got = []
    report = s.worker(got, max_runs=1).run()
    assert names(got) == ["worker_started", "run_claimed", "run_released", "worker_stopped"]
    released = got[2]
    assert (released["reason"], released["requeued"], released["fencing_token"]) == ("finished", False, 1)
    assert s.checkpoint().status is RunStatus.COMPLETED
    claim = s.claims.get(s.ws.project_id, "run-1")
    assert (claim.worker_id, claim.available_at, claim.fencing_token) == (None, None, 1)
    assert (report.claims, report.released, report.lost, report.stopped_because) == (1, 1, 0, "max_runs")
    assert got[-1]["reason"] == "max_runs"


def test_a_run_waiting_for_a_decision_sleeps_until_its_approval_expires_and_is_not_continued(tmp_path):
    """Without a decision a paused run stays paused; its only wake-up is the approval's expiry."""
    s = Setup(tmp_path, scenario="approval")
    got = []
    s.worker(got, max_runs=1).run()  # runs until the approval pause
    state = s.checkpoint()
    assert state.status is RunStatus.AWAITING_APPROVAL
    released = got[2]
    assert (released["reason"], released["requeued"], released["wake_at"]) == (
        "waiting_for_person", True, state.pending_approval["expires_at"],
    )  # fmt: skip
    claim = s.claims.get(s.ws.project_id, "run-1")
    assert claim.available_at == state.pending_approval["expires_at"]  # not before: nothing polls it
    before = CheckpointStore(s.ws.runs_dir).path("run-1").read_bytes()
    s.claims.enqueue(s.ws.project_id, "run-1")  # even if something queued it early...
    s.claims.db.claims[(s.ws.project_id, "run-1")] = replace(s.claims.get(s.ws.project_id, "run-1"), available_at=claim.created_at)
    again = []
    s.worker(again, max_runs=1).run()
    assert again[2]["reason"] == "waiting_for_person"
    assert CheckpointStore(s.ws.runs_dir).path("run-1").read_bytes() == before  # ...it is not touched


def test_a_step_budget_hands_the_run_back_after_each_bounded_piece_of_work(tmp_path):
    s = Setup(tmp_path)
    got = []
    s.worker(got, step_budget=1, max_runs=3).run()
    released = [r for r in got if r["event"] == "run_released"]
    assert [(r["reason"], r["requeued"], r["fencing_token"]) for r in released] == [("yielded", True, 1), ("yielded", True, 2), ("yielded", True, 3)]
    assert s.checkpoint().step == 3 and s.checkpoint().status is RunStatus.RUNNING
    s.worker(max_runs=1).run()  # without a budget the next claim finishes it
    assert s.checkpoint().status is RunStatus.COMPLETED
    assert s.claims.get(s.ws.project_id, "run-1").fencing_token == 4


class StopDuringNode(Agent):
    """A planner that receives the stop request (a Ctrl+C) while it is working."""

    name = "planner"
    writes = frozenset({"plan"})

    def __init__(self, inner: Agent, worker_ref: list) -> None:
        self.inner = inner
        self.worker_ref = worker_ref

    def run(self, ctx):
        self.worker_ref[0].request_stop()
        return self.inner.run(ctx)


def test_graceful_shutdown_finishes_the_current_node_then_releases_the_run_for_another_worker(tmp_path):
    s = Setup(tmp_path)
    got, ref = [], [None]
    worker = s.worker(got)
    ref[0] = worker
    planner = build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), s.ws).agents["planner"]
    s.agents["planner"] = StopDuringNode(planner, ref)
    report = worker.run()
    state = s.checkpoint()
    assert (state.status, state.step) == (RunStatus.RUNNING, 1)  # the node in progress finished and was committed
    released = [r for r in got if r["event"] == "run_released"]
    assert released == [released[0]] and (released[0]["reason"], released[0]["requeued"]) == ("shutdown", True)
    assert got[-1] == {**got[-1], "event": "worker_stopped", "reason": "stop_requested"}
    assert report.claims == 1  # no new claim after the stop request
    claim = s.claims.get(s.ws.project_id, "run-1")
    assert claim.worker_id is None and claim.available_at is not None  # another worker may continue it
    s.agents.clear()
    s.worker(max_runs=1).run()
    assert s.checkpoint().status is RunStatus.COMPLETED


class TakenOverDuringNode(Agent):
    """While this node runs, the claim's lease expires and another worker takes the run over."""

    name = "planner"
    writes = frozenset({"plan"})

    def __init__(self, inner: Agent, setup: Setup) -> None:
        self.inner = inner
        self.setup = setup

    def run(self, ctx):
        self.setup.clock.advance(61)
        self.setup.taker = self.setup.claims.claim_next(new_worker_id(), POLICY)
        return self.inner.run(ctx)


def test_a_worker_that_loses_its_claim_stops_at_once_and_does_not_release_it(tmp_path):
    s = Setup(tmp_path)
    planner = build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), s.ws).agents["planner"]
    s.agents["planner"] = TakenOverDuringNode(planner, s)
    got = []
    report = s.worker(got, max_runs=1).run()
    rejected = [r for r in got if r["event"] == "stale_worker_rejected"]
    assert [(r["operation"], r["reason"], r["fencing_token"]) for r in rejected] == [("advance", "superseded", 1)]
    assert "run_released" not in names(got) and report.lost == 1
    assert s.checkpoint().step == 0  # the stale worker's node result was never committed
    claim = s.claims.get(s.ws.project_id, "run-1")
    assert (claim.worker_id, claim.fencing_token) == (s.taker.fence.worker_id, 2)  # the new owner keeps it


def test_a_run_held_by_another_process_on_this_host_is_offered_again_shortly(tmp_path):
    s = Setup(tmp_path)
    lock = FileLock(CheckpointStore(s.ws.runs_dir).run_dir("run-1") / "run.lock")  # an operator's command holds it
    assert lock.try_acquire()
    try:
        got = []
        s.worker(got, max_runs=1).run()
    finally:
        lock.release()
    released = got[2]
    assert (released["event"], released["reason"], released["requeued"], released["retry_in_s"]) == ("run_released", "run_busy", True, FAST["poll_interval"])
    assert s.checkpoint().step == 0


@pytest.mark.parametrize("requeue,delay", [(True, 30.0), (False, None)])
def test_a_run_this_worker_cannot_run_is_offered_again_only_if_another_worker_might(tmp_path, requeue, delay):
    s = Setup(tmp_path)
    s.unavailable = RunUnavailable("not here", reason="workspace_unavailable" if requeue else "unsupported_run", requeue=requeue)
    got = []
    s.worker(got, max_runs=1).run()
    assert (got[2]["requeued"], got[2]["retry_in_s"]) == (requeue, delay)
    assert (s.claims.get(s.ws.project_id, "run-1").available_at is not None) is requeue


class FlakyClaims:
    """A claim session that fails ``failures`` times, as when the database is unreachable."""

    def __init__(self, inner, failures: int) -> None:
        self.inner, self.failures, self.closed = inner, failures, 0

    def claim_next(self, worker_id, policy):
        if self.failures > 0:
            self.failures -= 1
            raise StorageUnavailableError("database unavailable (postgresql://***@localhost/x): connection refused")
        return self.inner.claim_next(worker_id, policy)

    def close(self):
        self.closed += 1

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_worker_waits_longer_after_each_database_failure_and_opens_a_new_session(tmp_path):
    s = Setup(tmp_path)
    flaky = FlakyClaims(s.claims, failures=4)
    opened = []

    def open_claims():
        opened.append(1)
        return flaky

    got = []
    identity = WorkerIdentity.new()
    worker = Worker(identity, WorkerSettings(poll_interval=0.01, max_backoff=0.04, max_runs=1), open_claims=open_claims,
                    source=s, events=WorkerEvents(identity.worker_id, sink=got.append))  # fmt: skip
    worker.run()
    waits = [r["retry_in_s"] for r in got if r["event"] == "database_unavailable"]
    assert waits == [0.01, 0.02, 0.04, 0.04]  # doubling, capped
    assert all(r["error_type"] == "StorageUnavailableError" for r in got if r["event"] == "database_unavailable")
    assert len(opened) >= 5 and flaky.closed >= 4  # every failure drops the session; the next attempt opens a new one
    assert s.checkpoint().status is RunStatus.COMPLETED  # once the database answered, the work happened


def test_worker_events_hold_no_paths_urls_or_exception_text(tmp_path):
    s = Setup(tmp_path, runs=("run-1", "run-2"))
    flaky = FlakyClaims(s.claims, failures=1)
    got = []
    identity = WorkerIdentity.new()
    Worker(identity, WorkerSettings(poll_interval=0.01, max_backoff=0.02, max_runs=2), open_claims=lambda: flaky, source=s,
           events=WorkerEvents(identity.worker_id, sink=got.append)).run()  # fmt: skip
    text = json.dumps(got)
    assert str(tmp_path) not in text and str(tmp_path).replace("\\", "/") not in text
    assert "postgresql" not in text and "connection refused" not in text and "Traceback" not in text
    for record in got:
        assert all(isinstance(v, (str, int, float, bool, type(None))) for v in record.values())


def test_events_refuse_unknown_names_reasons_and_complex_values():
    events = WorkerEvents(new_worker_id())
    with pytest.raises(ValueError):
        events.emit("heartbeat_milestone")
    with pytest.raises(ValueError):
        events.emit("run_released", reason="because")
    with pytest.raises(TypeError):
        events.emit("run_claimed", detail={"path": "C:/secret"})


@pytest.mark.parametrize("bad", [
    {"poll_interval": 0}, {"step_budget": 0}, {"retry_delay": timedelta(seconds=-1)},
    {"poll_interval": 5, "max_backoff": 1}, {"max_runs": 0}, {"idle_exit": 0},
])  # fmt: skip
def test_worker_settings_are_validated(bad):
    with pytest.raises(ValueError):
        WorkerSettings(**bad)


def test_an_idle_worker_stops_after_its_idle_limit_and_a_stopped_worker_claims_nothing(tmp_path):
    s = Setup(tmp_path, runs=())
    got = []
    assert s.worker(got, idle_exit=0.05).run().stopped_because == "idle"
    s2 = Setup(tmp_path / "second")
    worker = s2.worker()
    worker.request_stop()
    assert worker.run().claims == 0 and s2.checkpoint().step == 0


# ----------------------------------------------------------------- lease keeper


def claimed(s: Setup, policy=POLICY):
    return s.claims.claim_next(new_worker_id(), policy)


def test_the_keeper_renews_and_moves_its_local_deadline_from_the_send_time(tmp_path):
    s = Setup(tmp_path)
    mono = Monotonic()
    claim = claimed(s)
    got = []
    keeper = LeaseKeeper(lambda: s.claims, claim, POLICY, WorkerEvents("w", sink=got.append), sent_at=mono.t, monotonic=mono)
    keeper.open()
    assert keeper.trusted()
    mono.t += 44.9
    assert keeper.trusted()
    mono.t += 0.1  # 45 s after the claim was sent: the local deadline (lease - margin)
    assert not keeper.trusted()
    s.clock.advance(45)
    assert keeper.beat() and keeper.trusted() and keeper.renewals == 1
    assert s.claims.get(s.ws.project_id, "run-1").lease_expires_at == format_timestamp(s.clock.now + timedelta(seconds=60))
    assert names(got) == ["lease_renewed"]


@pytest.mark.parametrize("how,reason", [("expired", "expired"), ("taken_over", "superseded"), ("released", "released")])
def test_a_refused_renewal_loses_the_claim_for_good(tmp_path, how, reason):
    s = Setup(tmp_path)
    claim = claimed(s)
    if how == "expired":
        s.clock.advance(60)
    elif how == "taken_over":
        s.clock.advance(60)
        claimed(s)
    else:
        s.claims.release(claim.fence, requeue=True)
    got = []
    keeper = LeaseKeeper(lambda: s.claims, claim, POLICY, WorkerEvents("w", sink=got.append), sent_at=0.0, monotonic=Monotonic())
    keeper.open()
    assert keeper.beat() is False and keeper.lost == reason and not keeper.trusted()
    rejected = [r for r in got if r["event"] == "stale_worker_rejected"]
    assert [(r["operation"], r["reason"]) for r in rejected] == [("heartbeat", reason)]
    assert ("lease_expired" in names(got)) is (reason == "expired")


def test_an_unreachable_database_never_extends_the_deadline_and_ends_the_claim_when_it_passes(tmp_path):
    s = Setup(tmp_path)
    mono = Monotonic()
    claim = claimed(s)
    sessions = []

    def open_claims():
        sessions.append(1)
        return FlakyRenewals(s.claims)

    keeper = LeaseKeeper(open_claims, claim, POLICY, WorkerEvents("w"), sent_at=mono.t, monotonic=mono)
    keeper.open()
    mono.t += 20
    assert keeper.beat() is True and keeper.lost is None  # keeps trying...
    mono.t += 20
    assert keeper.beat() is True and keeper.trusted()  # ...on a new session each time
    mono.t += 5  # 45 s: the deadline of the claim itself, never extended
    assert not keeper.trusted()
    assert keeper.beat() is False and keeper.lost == "unconfirmed"
    assert len(sessions) == 3


class FlakyRenewals:
    def __init__(self, inner):
        self.inner = inner

    def renew(self, fence, policy):
        raise StorageUnavailableError("database unavailable")

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_the_keeper_thread_renews_on_its_interval(tmp_path):
    s = Setup(tmp_path)
    fast = LeasePolicy(lease=timedelta(seconds=2), heartbeat_interval=timedelta(milliseconds=50))
    claim = claimed(s, fast)
    keeper = LeaseKeeper(lambda: s.claims, claim, fast, WorkerEvents("w"), sent_at=Monotonic().t)
    keeper.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.4)
    finally:
        keeper.stop()
    assert keeper.renewals >= 3


# ----------------------------------------------------------------- ownership


def ownership_for(s: Setup, *, mono=None, stop=None, budget=None, prove=None):
    mono = mono or Monotonic()
    claim = claimed(s)
    keeper = LeaseKeeper(lambda: s.claims, claim, POLICY, WorkerEvents("w"), sent_at=mono.t, monotonic=mono)
    keeper.open()
    return keeper, ClaimOwnership(keeper, prove or (lambda: s.claims.verify(claim.fence)), stop=stop or threading.Event(), step_budget=budget)


def test_ownership_asks_the_database_and_refuses_once_the_claim_is_doubtful(tmp_path):
    s = Setup(tmp_path)
    mono = Monotonic()
    keeper, ownership = ownership_for(s, mono=mono)
    ownership.verify()
    assert ownership.owner == f"{keeper.fence.worker_id}@1" and ownership.run_id == "run-1"
    mono.t += 45  # no renewal confirmed in time: refused without asking anyone
    with pytest.raises(OwnershipLost) as unconfirmed:
        ownership.verify()
    assert unconfirmed.value.reason == "unconfirmed"


def test_ownership_that_cannot_be_confirmed_is_lost(tmp_path):
    s = Setup(tmp_path)

    def prove():
        raise StorageUnavailableError("database unavailable")

    _, ownership = ownership_for(s, prove=prove)
    with pytest.raises(OwnershipLost) as lost:
        ownership.verify()
    assert lost.value.reason == "unconfirmed" and "StorageUnavailableError" in str(lost.value)


def test_ownership_refuses_at_once_after_the_keeper_lost_the_claim(tmp_path):
    s = Setup(tmp_path)
    keeper, ownership = ownership_for(s)
    s.clock.advance(60)
    claimed(s)  # taken over
    assert keeper.beat() is False
    with pytest.raises(OwnershipLost) as lost:
        ownership.verify()
    assert lost.value.reason == "superseded"


def test_should_yield_on_stop_budget_and_doubt(tmp_path):
    s = Setup(tmp_path)
    stop = threading.Event()
    _, budgeted = ownership_for(s, stop=stop, budget=2)
    assert [budgeted.should_yield() for _ in range(3)] == [False, False, True]
    s2 = Setup(tmp_path / "b")
    _, stopping = ownership_for(s2, stop=stop)
    assert not stopping.should_yield()
    stop.set()
    assert stopping.should_yield()
    s3 = Setup(tmp_path / "c")
    mono = Monotonic()
    _, doubtful = ownership_for(s3, mono=mono)
    mono.t += 45
    assert doubtful.should_yield()


# -------------------------------------------------------------- signals (CLI)


def test_the_first_signal_stops_gracefully_and_the_second_stops_at_once():
    from agent_factory.cli import _stop_on_signals

    class FakeWorker:
        stopping = False

        def request_stop(self):
            self.stopping = True

    worker = FakeWorker()
    before = signal.getsignal(signal.SIGINT)
    restore = _stop_on_signals(worker)
    try:
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert worker.stopping
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is before

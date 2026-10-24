"""Shared fixtures for the E5 tests: one approval-gated fixture run, consumed by real worker loops.

A ``Flow`` holds the action fixture's run (one class C notice; kind
``unknown-notice`` times out after sending the first one), its product store,
its claims, and a run source for ``Worker``. Decisions are recorded exactly as
the API records them (the decision or answer and the run's wake-up, in one
transaction), and workers consume them through ``Executor.advance``.

* ``Flow.memory``: the in-memory store and claims on one movable clock.
* ``Flow.postgres``: real PostgreSQL. Each claimed run gets its own fenced
  session (``open_fenced_store``), as in production.

Controlled fixtures only: every "external" effect is a line in an oracle file.
Used by tests/test_worker_decisions.py, tests/test_postgres_decisions.py, and
the child process tests/decision_child.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import action_fixtures as fx

from agent_factory.domain import in_memory_store
from agent_factory.domain.approvals import ApprovalOutcome
from agent_factory.domain.ids import new_principal_id
from agent_factory.domain.principals import Permission, Principal, PrincipalStatus
from agent_factory.domain.reconciliation import ReconciliationService
from agent_factory.domain.repositories import InMemoryClaimRepository
from agent_factory.domain.services import ApprovalService
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.state import TaskSpec
from agent_factory.util import timestamp, utc_now
from agent_factory.worker import Worker, WorkerEvents, WorkerIdentity, WorkerSettings

ENROLLED = "2026-10-01T00:00:00.000000Z"
DECIDE = {Permission.APPROVALS_READ, Permission.APPROVALS_DECIDE}
RECONCILE = {Permission.APPROVALS_READ, Permission.ACTIONS_RECONCILE}
FAST = {"poll_interval": 0.01, "max_backoff": 0.05}


class MovableClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 5, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    def at(self, moment: str) -> None:
        self.now = datetime.fromisoformat(moment.replace("Z", "+00:00"))


def person(store, name: str, permissions) -> Principal:
    """An enrolled person, as ``auth principal add`` would enroll them (no token involved)."""
    principal = Principal(
        id=new_principal_id(), issuer="https://issuer.example.test", subject=f"sub-{name}-{new_principal_id()[4:]}",
        status=PrincipalStatus.ACTIVE, created_at=ENROLLED, updated_at=ENROLLED, display_name=name,
    )  # fmt: skip
    store.principals.add(principal)
    store.principals.grant(principal.id, permissions, at=ENROLLED)
    return principal


def task() -> TaskSpec:
    return TaskSpec(title="fixture", goal="one approval-gated action")


class _MemoryRun:
    def __init__(self, flow: Flow, claim) -> None:
        self.flow = flow
        self.claim = claim

    def prove(self) -> None:
        self.flow.claims.verify(self.claim.fence)

    def executor(self, ownership):
        ex = self.flow.executor(self.flow.store)
        ex.ownership = ownership
        self.flow.adjust(ex)
        return ex

    def close(self) -> None:
        pass


class _PostgresRun:
    def __init__(self, flow: Flow, claim) -> None:
        from agent_factory.persistence.claims import open_fenced_store

        self.flow = flow
        self.store = open_fenced_store(flow.config, claim.fence)

    def prove(self) -> None:
        self.store.unit_of_work.prove_ownership()

    def executor(self, ownership):
        ex = self.flow.executor(self.store)
        ex.ownership = ownership
        self.flow.adjust(ex)
        return ex

    def close(self) -> None:
        self.store.close()


class Flow:
    """The fixture run, its store and claims, and helpers that act as the API, a person, and workers."""

    def __init__(self, root: Path, *, kind: str, store, clock, open_claims, run_type, config=None, enroll=True) -> None:
        self.root = Path(root)
        self.kind = kind
        self.store = store
        self.clock = clock
        self.open_claims = open_claims
        self.run_type = run_type
        self.config = config
        self.ws = fx.workspace(self.root / "ws")
        self.oracle = self.root / "oracle"
        self.adjusters: list[Any] = []
        self.dana = person(store, "dana", DECIDE | RECONCILE) if enroll else None

    # ------------------------------------------------------------ backends

    @classmethod
    def memory(cls, root: Path, kind: str = "notice") -> Flow:
        clock = MovableClock()
        store = in_memory_store(clock)
        claims = InMemoryClaimRepository(store.unit_of_work, clock)
        flow = cls(root, kind=kind, store=store, clock=clock, open_claims=lambda: claims, run_type=_MemoryRun)
        flow.claims = claims
        return flow

    @classmethod
    def postgres(cls, root: Path, config, store, kind: str = "notice", *, enroll: bool = True) -> Flow:
        """``enroll=False`` attaches to a flow another process set up (tests/decision_child.py)."""
        from agent_factory.persistence.claims import open_claim_repository

        flow = cls(root, kind=kind, store=store, clock=utc_now, open_claims=lambda: open_claim_repository(config),
                   run_type=_PostgresRun, config=config, enroll=enroll)  # fmt: skip
        return flow

    # ------------------------------------------------------------- the run

    @property
    def project_id(self) -> str:
        return self.ws.project_id

    def executor(self, store=None):
        store = store if store is not None else self.store
        return fx.executor(
            fx.workspace(self.ws.root), self.kind, self.oracle, recorder=fx.recorder_for(store, self.ws, self.clock),
            clock=self.clock,
        )  # fmt: skip

    def adjust(self, ex) -> None:
        for adjuster in self.adjusters:
            adjuster(ex)

    def create(self) -> None:
        """Create the run (first checkpoint only) and queue it for a worker, as ``worker enqueue`` does."""
        self.executor().create(task(), run_id="run-1")
        claims = self.open_claims()
        try:
            claims.enqueue(self.project_id, "run-1")
        finally:
            if claims is not getattr(self, "claims", None):
                claims.close()

    def open(self, claim):  # RunSource
        return self.run_type(self, claim)

    def worker(self, events: list | None = None, **settings) -> Worker:
        identity = WorkerIdentity.new()
        sink = events.append if events is not None else None
        merged = {**FAST, "max_runs": 1, "idle_exit": 0.3, **settings}
        return Worker(identity, WorkerSettings(**merged), open_claims=self.open_claims, source=self,
                      events=WorkerEvents(identity.worker_id, sink=sink))  # fmt: skip

    def work(self, **settings) -> list[dict]:
        """One worker: at most one claim, then stop. Its events."""
        events: list[dict] = []
        self.worker(events, **settings).run()
        return events

    def checkpoint(self):
        return CheckpointStore(self.ws.runs_dir).load("run-1")

    def log(self) -> list[dict]:
        return self.executor().log_for("run-1").entries()

    def events(self, name: str) -> list[dict]:
        return [e for e in self.log() if e["event"] == name]

    def notices(self) -> int:
        return len(fx.read_lines(self.oracle / "notices.jsonl"))

    # ------------------------------------------------------ product records

    def request(self):
        [request] = [r for r in self._requests()]
        return request

    def _requests(self):
        state = self.checkpoint()
        ids = set(state.approvals) | ({state.pending_approval["request_id"]} if state.pending_approval else set())
        return [self.store.approvals.get_request(self.project_id, i) for i in sorted(ids)]

    def execution(self):
        [execution] = self.store.executions.list_for_run(self.project_id, "run-1")
        return execution

    def now(self) -> str:
        return timestamp(self.clock)

    # ------------------------------------------------- what the API records

    def decide(self, outcome: str = "approved", actor=None, *, request=None, store=None):
        """decideApproval: the decision and the run's wake-up, in one transaction (on ``store``'s session)."""
        request = request or self.request()
        store = store or self.store
        with store.transaction():
            result = ApprovalService(store.approvals, clock=self.clock).record(
                project_id=self.project_id, request_id=request.id, action_fingerprint=request.action_fingerprint,
                outcome=ApprovalOutcome(outcome), actor=actor or self.dana.actor(),
            )  # fmt: skip
            if result.recorded_now:
                store.queue.wake(self.project_id, request.run_id)
        return result

    def reconcile(self, choice: str, actor=None, *, attempt: int | None = None, store=None):
        """reconcileAction: the answer and the run's wake-up, in one transaction (the service wakes the run)."""
        execution = self.execution()
        store = store or self.store
        with store.transaction():
            return ReconciliationService(store, clock=self.clock).record(
                project_id=self.project_id, run_id="run-1", execution_id=execution.id,
                attempt=attempt or execution.attempts, action_fingerprint=execution.action_fingerprint, choice=choice,
                actor=actor or self.dana.actor(),
            )  # fmt: skip

    # --------------------------------------------------------- shortcuts

    def paused(self) -> list[dict]:
        """Created, queued, and advanced by a worker until it waits for its approval. That worker's events."""
        self.create()
        events = self.work()
        assert self.checkpoint().pending_approval is not None, self.checkpoint().status
        return events

    def uncertain(self) -> None:
        """Approved and sent, with an unknown outcome: the run waits for a person to say whether it happened."""
        self.paused()
        self.decide("approved")
        self.work()
        assert self.checkpoint().pending_reconciliation is not None, self.checkpoint().status
        assert self.notices() == 1


def once(adjust):
    """An executor adjustment for the first executor only (the first worker to open the run)."""
    done = []

    def adjust_once(ex) -> None:
        if not done:
            done.append(True)
            adjust(ex)

    return adjust_once


def released(events: list[dict]) -> dict:
    [found] = [e for e in events if e["event"] == "run_released"]
    return found


def claimed(events: list[dict]) -> dict | None:
    found = [e for e in events if e["event"] == "run_claimed"]
    return found[0] if found else None

"""The worker loop: claim a run, advance it under the claim, give it back.

::

    start -> register identity -> claim an eligible run -> advance it (bounded),
             heartbeating meanwhile -> release it -> repeat -> stop

* **One run at a time** per worker process. To run several workers, start the
  command several times: nothing here starts another process.
* **Claiming** is one short transaction (``ClaimRepository.claim_next``). The
  graph runs outside it.
* **Advancing** means ``Executor.advance``: a stored human decision (an
  approval, a rejection, a due expiry, a reconciliation) is re-checked and
  applied under the claim, then a running run continues until it pauses,
  finishes, or yields. A run still waiting for a person is released with a
  wake-up at its approval's expiry (or none, for a reconciliation); the
  decision itself wakes it earlier. A decision that contradicts the engine's
  exact pending action is never applied (``decision_refused``).
* **Bounded**: with a step budget the executor yields after that many nodes,
  and the run is released for the next claim (by any worker).
* **Ownership lost** (``OwnershipLost``): the worker stops working on the run
  at once and does not release it (it no longer owns it). Whoever claims it
  next recovers it.
* **Database errors**: the worker stops working on the run, tries once to
  release it (the lease expires otherwise), then waits, with a growing
  delay, before claiming again. A later claim is a new generation: work never
  continues under the claim that was in doubt.
* **Graceful shutdown** (``request_stop``): no new claims; the current run
  yields at its next node boundary, after its checkpoint, and is released for
  another worker. An action already being dispatched is never abandoned
  halfway: the executor yields only between nodes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from ..domain.approvals import approval_expiry
from ..domain.claims import Claim, Fence, LeasePolicy, OwnershipLost
from ..domain.errors import DecisionRefused, StorageError
from ..domain.repositories import ClaimRepository
from ..engine.executor import Executor, ExecutorError, RunLocked
from ..engine.ownership import RunOwnership
from ..safety.execution import ExecutionLedgerUnavailable
from ..state import ProjectState, RunStatus
from .events import WorkerEvents
from .identity import WorkerIdentity
from .lease import ClaimOwnership, LeaseKeeper


class RunUnavailable(Exception):
    """This worker cannot work on the claimed run: it is not on this host, or this code cannot run it."""

    def __init__(self, message: str, *, reason: str, requeue: bool) -> None:
        super().__init__(message)
        self.reason = reason
        self.requeue = requeue


class ClaimedRun(Protocol):
    """One claimed run, as a worker reaches it: storage fenced by the claim, and the run's executor."""

    def prove(self) -> None:
        """Raise ``OwnershipLost`` unless the claim is current (on the run's own fenced session)."""

    def executor(self, ownership: RunOwnership) -> Executor:
        """The run's executor, bound to ``ownership``. Raises ``RunUnavailable``."""

    def close(self) -> None: ...


class RunSource(Protocol):
    def open(self, claim: Claim) -> ClaimedRun: ...


@dataclass(frozen=True)
class WorkerSettings:
    """How a worker paces itself (docs/workers.md, "configuration")."""

    policy: LeasePolicy = field(default_factory=LeasePolicy)
    poll_interval: float = 2.0  # seconds between claim attempts while there is no work
    step_budget: int | None = None  # nodes per claim; None: until the run pauses or finishes
    retry_delay: timedelta = timedelta(seconds=30)  # before a run this worker could not start is offered again
    max_backoff: float = 30.0  # the longest wait between attempts while the database is unreachable
    max_runs: int | None = None  # stop after this many claims (local development)
    idle_exit: float | None = None  # stop after this many seconds without work (local development)

    def __post_init__(self) -> None:
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.step_budget is not None and self.step_budget < 1:
            raise ValueError("step_budget must be at least 1")
        if self.retry_delay < timedelta(0):
            raise ValueError("retry_delay must not be negative")
        if self.max_backoff < self.poll_interval:
            raise ValueError("max_backoff must be at least poll_interval")
        if self.max_runs is not None and self.max_runs < 1:
            raise ValueError("max_runs must be at least 1")
        if self.idle_exit is not None and self.idle_exit <= 0:
            raise ValueError("idle_exit must be positive")


@dataclass
class WorkerReport:
    claims: int = 0
    released: int = 0
    lost: int = 0
    stopped_because: str = ""


@dataclass(frozen=True)
class Release:
    """What to do with a claim once its run has been advanced (``ClaimRepository.release``)."""

    requeue: bool | None  # True: wanted again (after ``delay``, or at ``at``); False: not wanted; None: unchanged
    reason: str
    delay: timedelta = timedelta(0)
    at: str | None = None


def release_plan(state: ProjectState, *, stopping: bool = False) -> Release:
    """What a run's status after advancing means for its claim."""
    status = state.status
    if status is RunStatus.RUNNING:
        return Release(True, "shutdown" if stopping else "yielded")
    if status is RunStatus.AWAITING_APPROVAL:
        # Wake at the approval's expiry, so that an undecided request is expired on time; a decision wakes
        # the run earlier (the transactional outbox), and nothing polls it in between.
        pending = state.pending_approval or {}
        expires = pending.get("expires_at") or (approval_expiry(pending["requested_at"]) if pending.get("requested_at") else None)
        return Release(expires is not None, "waiting_for_person", at=expires)
    if status is RunStatus.AWAITING_RECONCILIATION:
        return Release(False, "waiting_for_person")  # no deadline: a reconciliation decision wakes it
    if status is RunStatus.BLOCKED:
        return Release(False, "blocked")
    return Release(False, "finished")

_WAIT_SLICE = 0.2  # seconds: how quickly an idle worker notices a stop request


class Worker:
    """One worker process's loop. ``run`` returns when stopped; ``request_stop`` may be called from anywhere."""

    def __init__(
        self,
        identity: WorkerIdentity,
        settings: WorkerSettings,
        *,
        open_claims: Callable[[], ClaimRepository],
        source: RunSource,
        events: WorkerEvents | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.identity = identity
        self.settings = settings
        self._open_claims = open_claims
        self._source = source
        self.events = events or WorkerEvents(identity.worker_id)
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._claims: ClaimRepository | None = None
        self._failures = 0
        self.report = WorkerReport()

    # ------------------------------------------------------------- control

    def request_stop(self) -> None:
        """Stop claiming; let the current run yield at its next node boundary; then return from ``run``."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ---------------------------------------------------------------- loop

    def run(self) -> WorkerReport:
        policy = self.settings.policy
        self.events.emit(
            "worker_started",
            host=self.identity.host,
            pid=self.identity.pid,
            lease_s=policy.lease.total_seconds(),
            heartbeat_s=policy.heartbeat_interval.total_seconds(),
            poll_s=self.settings.poll_interval,
            step_budget=self.settings.step_budget,
        )
        reason = "stop_requested"
        idle_since = self._monotonic()
        try:
            while not self._stop.is_set():
                if self.settings.max_runs is not None and self.report.claims >= self.settings.max_runs:
                    reason = "max_runs"
                    break
                found = self._claim_next()
                if found is None:
                    if self.settings.idle_exit is not None and self._monotonic() - idle_since >= self.settings.idle_exit:
                        reason = "idle"
                        break
                    self._wait(self._idle_delay())
                    continue
                self.report.claims += 1
                self._work(*found)
                idle_since = self._monotonic()
        except BaseException:
            reason = "error"
            raise
        finally:
            self._close_claims()
            self.report.stopped_because = reason
            self.events.emit(
                "worker_stopped", reason=reason, claims=self.report.claims, released=self.report.released,
                lost=self.report.lost,
            )  # fmt: skip
        return self.report

    # ------------------------------------------------------------ claiming

    def _control(self) -> ClaimRepository:
        if self._claims is None:
            self._claims = self._open_claims()
        return self._claims

    def _close_claims(self) -> None:
        claims, self._claims = self._claims, None
        if claims is not None:
            try:
                claims.close()
            except StorageError:
                pass  # closing a broken session: nothing else to do

    def _database_unavailable(self, exc: StorageError) -> None:
        self._close_claims()  # the session may be broken; the next attempt opens a new one
        self._failures += 1
        self.events.emit("database_unavailable", error_type=type(exc).__name__, retry_in_s=self._idle_delay())

    def _idle_delay(self) -> float:
        if not self._failures:
            return self.settings.poll_interval
        return min(self.settings.max_backoff, self.settings.poll_interval * 2 ** (self._failures - 1))

    def _wait(self, seconds: float) -> None:
        deadline = self._monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(_WAIT_SLICE, remaining))

    def _claim_next(self) -> tuple[Claim, float] | None:
        try:
            claims = self._control()
            sent_at = self._monotonic()  # before the claim is sent: the local deadline can only be early
            claim = claims.claim_next(self.identity.worker_id, self.settings.policy)
        except StorageError as exc:
            self._database_unavailable(exc)
            return None
        self._failures = 0
        return None if claim is None else (claim, sent_at)

    # ------------------------------------------------------------- working

    @staticmethod
    def _where(fence: Fence) -> dict[str, str | int]:
        return {"project_id": fence.project_id, "run_id": fence.run_id, "fencing_token": fence.fencing_token}

    def _work(self, claim: Claim, sent_at: float) -> None:
        fence = claim.fence
        where = self._where(fence)
        if claim.taken_over:
            self.events.emit(
                "lease_expired", project_id=fence.project_id, run_id=fence.run_id,
                fencing_token=claim.previous_fencing_token, owner_worker_id=claim.previous_worker_id,
                lease_expires_at=claim.previous_lease_expires_at, detected_by="takeover",
            )  # fmt: skip
            self.events.emit(
                "run_taken_over", **where, previous_worker_id=claim.previous_worker_id,
                previous_fencing_token=claim.previous_fencing_token,
            )  # fmt: skip
        self.events.emit("run_claimed", **where, lease_expires_at=claim.lease_expires_at)
        keeper = LeaseKeeper(self._open_claims, claim, self.settings.policy, self.events, sent_at=sent_at, monotonic=self._monotonic)
        try:
            try:
                keeper.start()
                plan = self._advance(claim, keeper)
            finally:
                keeper.stop()
        except OwnershipLost as exc:
            # Never release a claim this worker no longer owns: the next owner recovers the run.
            self.report.lost += 1
            self.events.emit("stale_worker_rejected", **where, operation="advance", reason=exc.reason)
            return
        except (StorageError, ExecutionLedgerUnavailable):
            plan = Release(True, "storage_error", self.settings.retry_delay)
        except BaseException:
            self._release(fence, Release(True, "error", self.settings.retry_delay))
            raise
        self._release(fence, plan)

    def _advance(self, claim: Claim, keeper: LeaseKeeper) -> Release:
        run = self._source.open(claim)
        try:
            ownership = ClaimOwnership(keeper, run.prove, stop=self._stop, step_budget=self.settings.step_budget)
            try:
                executor = run.executor(ownership)
                state = executor.advance(claim.fence.run_id)
            except RunUnavailable as exc:
                return Release(exc.requeue, exc.reason, self.settings.retry_delay)
            except RunLocked:
                # A live process on this host holds the run (an operator's command, or an old owner that
                # has not stopped yet). It is offered again shortly.
                return Release(True, "run_busy", timedelta(seconds=self.settings.poll_interval))
            except DecisionRefused:
                # A stored decision contradicts the engine's exact pending action: never applied, and no
                # worker should pick the run up again until a person has looked (the run log says why).
                return Release(False, "decision_refused")
            except ExecutorError:
                return Release(False, "unsupported_run")
            return release_plan(state, stopping=self._stop.is_set())
        finally:
            run.close()

    def _release(self, fence: Fence, plan: Release) -> bool:
        where = self._where(fence)
        try:
            claims = self._control()
            released = claims.release(fence, requeue=plan.requeue, delay=plan.delay, at=plan.at)
        except StorageError as exc:
            self._database_unavailable(exc)  # the lease expires on its own; the next owner recovers the run
            return False
        if not released:
            self.report.lost += 1
            self.events.emit("stale_worker_rejected", **where, operation="release", reason=self._refusal(claims, fence))
            return False
        self.report.released += 1
        self.events.emit(
            "run_released", **where, reason=plan.reason, requeued=bool(plan.requeue),
            retry_in_s=plan.delay.total_seconds() if plan.requeue and plan.at is None else None,
            wake_at=plan.at if plan.requeue else None,
        )  # fmt: skip
        return True

    @staticmethod
    def _refusal(claims: ClaimRepository, fence: Fence) -> str:
        try:
            claims.verify(fence)
        except OwnershipLost as exc:
            return exc.reason
        except StorageError:
            return "unconfirmed"
        return "unconfirmed"


__all__ = ["ClaimedRun", "Release", "RunSource", "RunUnavailable", "Worker", "WorkerReport", "WorkerSettings", "release_plan"]

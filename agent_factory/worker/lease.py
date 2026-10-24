"""Keeping a claim: heartbeats, the local deadline, and the executor's proof of ownership."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..domain.claims import Claim, LeasePolicy, OwnershipLost
from ..domain.errors import StorageError
from ..domain.repositories import ClaimRepository
from .events import WorkerEvents


class LeaseKeeper:
    """Renews one claim's lease from a background thread, on a database session of its own.

    * **Renewal** every ``policy.heartbeat_interval``. The database renews only
      the current generation, and only while its lease is still valid.
    * **Refused** (the lease expired, or another worker took the run over):
      the claim is lost for good. The keeper stops; it never tries to win the
      run back. Only a new claim, with a new token, could.
    * **Database unreachable**: the keeper drops its session and tries again,
      on a new one, at the next interval, but the local deadline is not
      extended. Once it passes, the claim counts as lost (``unconfirmed``):
      the worker does not keep working in the hope that the database comes
      back. (A new session is safe: the database alone decides whether a
      renewal is allowed, whichever connection sends it.)

    The local deadline is measured on the monotonic clock from the moment
    the last confirmed renewal (or the claim) was *sent*: never longer than
    the real lease, which the database measured from a later instant.
    """

    def __init__(
        self,
        open_claims: Callable[[], ClaimRepository],
        claim: Claim,
        policy: LeasePolicy,
        events: WorkerEvents,
        *,
        sent_at: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.claim = claim
        self.fence = claim.fence
        self.policy = policy
        self._open = open_claims
        self._events = events
        self._monotonic = monotonic
        self._deadline = policy.local_deadline(sent_at)
        self._lost: str | None = None
        self._state = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._repo: ClaimRepository | None = None
        self.renewals = 0

    # --------------------------------------------------------- lifecycle

    def open(self) -> None:
        """Open the keeper's own session: a heartbeat never queues behind the run's own writes."""
        if self._repo is None:
            self._repo = self._open()

    def start(self) -> None:
        self.open()
        self._thread = threading.Thread(target=self._loop, name=f"lease-{self.fence.run_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._drop_session()

    def _drop_session(self) -> None:
        repo, self._repo = self._repo, None
        if repo is not None:
            try:
                repo.close()
            except StorageError:
                pass  # closing a broken session: nothing else to do

    # ------------------------------------------------------------- state

    @property
    def lost(self) -> str | None:
        """Why the claim is lost (an ``OwnershipLost`` reason), or None while it is held."""
        with self._state:
            return self._lost

    def trusted(self) -> bool:
        """Held, and the local deadline has not passed."""
        with self._state:
            return self._lost is None and self._monotonic() < self._deadline

    # --------------------------------------------------------- heartbeat

    def _loop(self) -> None:
        interval = self.policy.heartbeat_interval.total_seconds()
        while not self._stop.wait(interval):
            if not self.beat():
                return

    def beat(self) -> bool:
        """One renewal attempt. False once the claim is lost for good."""
        sent = self._monotonic()
        try:
            self.open()
            repo = self._repo
            assert repo is not None
            expires = repo.renew(self.fence, self.policy)
        except StorageError as exc:
            self._drop_session()
            if self._monotonic() >= self._deadline:
                self._lose("unconfirmed", error_type=type(exc).__name__)
                return False
            return True  # try again at the next interval; the deadline stays where it was
        if expires is None:
            self._lose(self._refusal(repo))
            return False
        with self._state:
            if self._lost is not None:
                return False
            self._deadline = self.policy.local_deadline(sent)
            self.renewals += 1
        self._events.emit("lease_renewed", **self._where(), lease_expires_at=expires)
        return True

    def _refusal(self, repo: ClaimRepository) -> str:
        try:
            repo.verify(self.fence)
        except OwnershipLost as exc:
            return exc.reason
        except StorageError:
            return "unconfirmed"
        return "unconfirmed"  # the renewal was refused although the fence now verifies: trust neither

    def _lose(self, reason: str, **extra: str) -> None:
        with self._state:
            if self._lost is not None:
                return
            self._lost = reason
        if reason == "expired":
            self._events.emit("lease_expired", **self._where(), detected_by="owner")
        self._events.emit("stale_worker_rejected", **self._where(), operation="heartbeat", reason=reason, **extra)

    def _where(self) -> dict[str, str | int]:
        return {"project_id": self.fence.project_id, "run_id": self.fence.run_id, "fencing_token": self.fence.fencing_token}


class ClaimOwnership:
    """A claim as the executor sees it (``engine.ownership.RunOwnership``).

    ``verify`` refuses at once if the keeper has lost the claim or no longer
    trusts its lease, and otherwise asks the database (``prove``: one fenced,
    empty transaction on the run's own session). A database error while
    proving is treated as a lost claim: ownership that cannot be confirmed is
    not assumed.

    ``should_yield`` asks the executor to hand the run back at the next node
    boundary when the worker is stopping, the claim is in doubt, or the step
    budget (nodes per claim) is used up.
    """

    def __init__(
        self,
        keeper: LeaseKeeper,
        prove: Callable[[], None],
        *,
        stop: threading.Event,
        step_budget: int | None = None,
    ) -> None:
        self._keeper = keeper
        self._prove = prove
        self._stop = stop
        self._budget = step_budget
        self._boundaries = 0

    @property
    def run_id(self) -> str:
        return self._keeper.fence.run_id

    @property
    def owner(self) -> str:
        return self._keeper.fence.owner

    def verify(self) -> None:
        fence = self._keeper.fence
        where = f"run {fence.project_id}/{fence.run_id}, generation {fence.fencing_token}"
        lost = self._keeper.lost
        if lost is not None:
            raise OwnershipLost(f"{where}: the claim was lost ({lost})", reason=lost)
        if not self._keeper.trusted():
            raise OwnershipLost(f"{where}: no lease renewal was confirmed in time", reason="unconfirmed")
        try:
            self._prove()
        except StorageError as exc:
            raise OwnershipLost(f"{where}: ownership could not be confirmed ({type(exc).__name__})", reason="unconfirmed") from None

    def should_yield(self) -> bool:
        if self._stop.is_set() or not self._keeper.trusted():
            return True
        self._boundaries += 1
        return self._budget is not None and self._boundaries > self._budget


__all__ = ["ClaimOwnership", "LeaseKeeper"]

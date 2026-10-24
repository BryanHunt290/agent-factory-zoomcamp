"""Run claims: which worker process may change a run, until when, and in which generation.

Several worker processes may share one PostgreSQL database (docs/workers.md).
Only one of them may own a run at a time, and a worker that has stopped
owning a run must not change it again, even if it does not know yet. Three
mechanisms together give that guarantee:

* **A claim** names the owning worker. Worker IDs are random per worker
  *process* (``wkr-`` + 20 hex), never a hostname: two processes on one host,
  or one host restarting a worker, are always different owners.
* **A lease** bounds the ownership in time. The owner renews it with
  heartbeats; if it stops (it crashed, hung, or lost the network), the lease
  runs out and another worker may take the run over. Lease validity is
  decided by the database's clock alone: valid while ``now < lease_expires_at``,
  expired once ``lease_expires_at <= now``.
* **A fencing token** numbers the ownership generations of a run: 0 before
  the first claim, then 1, 2, 3, ..., one more for every claim. Every write a
  worker makes to a run carries its :class:`Fence` and is refused unless that
  fence is still the current one, in the same database transaction as the
  write. A lease alone is not enough: a worker that stalls after checking its
  lease, and wakes after a takeover, would otherwise write over the new owner.
  With fencing its write fails (41 is not the current 42), however sure it is
  that its lease is still valid.

This module holds the records and rules. The repositories (claim, renew,
release, verify) are in ``repositories.py`` and ``persistence/claims.py``.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from .errors import DomainValidationError
from .ids import PROJECT_ID_RE, RUN_ID_RE
from .serialization import check_int, check_optional_timestamp, check_pattern, check_timestamp, parse_timestamp

WORKER_ID_RE = re.compile(r"^wkr-[0-9a-f]{20}$")


def new_worker_id() -> str:
    """A new worker identity: random, so unique per worker process instance. Not a secret."""
    return "wkr-" + secrets.token_hex(10)


class OwnershipLost(BaseException):
    """This process can no longer prove that it owns the run: it must stop changing it at once.

    Raised when a write's fence check fails (another worker holds a newer
    generation, the lease expired, or the claim was released), and when
    ownership cannot be confirmed at all, for example because the database is
    unreachable: an unconfirmed owner is treated as a lost one (fail closed).

    It derives from ``BaseException``, as ``asyncio.CancelledError`` does, so
    that ``except Exception`` handlers never swallow it: not an agent's, and
    not the executor's failure path, which would record the run as failed.
    Losing ownership is not a failure of the run, and the stale worker must
    write nothing more to it.
    """

    #: Why ownership was lost, for logs and events (never shown to API callers).
    REASONS = frozenset({"superseded", "expired", "released", "missing", "unconfirmed", "wrong_run"})

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        if reason not in self.REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        self.reason = reason


@dataclass(frozen=True)
class Fence:
    """What a worker presents with every protected write: which run, which worker, which generation."""

    project_id: str
    run_id: str
    worker_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        check_pattern(self.project_id, PROJECT_ID_RE, "fence.project_id")
        check_pattern(self.run_id, RUN_ID_RE, "fence.run_id")
        check_pattern(self.worker_id, WORKER_ID_RE, "fence.worker_id")
        check_int(self.fencing_token, "fence.fencing_token", minimum=1)

    @property
    def owner(self) -> str:
        """How the execution ledger records this owner on an attempt: worker and generation."""
        return f"{self.worker_id}@{self.fencing_token}"


@dataclass(frozen=True)
class RunClaim:
    """The stored claim of one run: a row of ``run_claims``, with the same invariants."""

    project_id: str
    run_id: str
    fencing_token: int
    created_at: str
    updated_at: str
    worker_id: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    last_heartbeat_at: str | None = None
    available_at: str | None = None
    wake_seq: int = 0  # wake-ups so far (decisions recorded, enqueues)
    claimed_wake_seq: int = 0  # wake_seq when the run was last claimed

    def __post_init__(self) -> None:
        check_pattern(self.project_id, PROJECT_ID_RE, "claim.project_id")
        check_pattern(self.run_id, RUN_ID_RE, "claim.run_id")
        if self.worker_id is not None:
            check_pattern(self.worker_id, WORKER_ID_RE, "claim.worker_id")
        check_int(self.fencing_token, "claim.fencing_token", minimum=0)
        check_int(self.wake_seq, "claim.wake_seq", minimum=0)
        check_int(self.claimed_wake_seq, "claim.claimed_wake_seq", minimum=0)
        if self.claimed_wake_seq > self.wake_seq:
            raise DomainValidationError("claim: claimed_wake_seq cannot be ahead of wake_seq")
        for name in ("created_at", "updated_at"):
            object.__setattr__(self, name, check_timestamp(getattr(self, name), f"claim.{name}"))
        for name in ("claimed_at", "lease_expires_at", "last_heartbeat_at", "available_at"):
            object.__setattr__(self, name, check_optional_timestamp(getattr(self, name), f"claim.{name}"))
        if (self.fencing_token == 0) != (self.claimed_at is None):
            raise DomainValidationError("claim: generation 0 exactly when the run has never been claimed")
        if (self.worker_id is None) != (self.lease_expires_at is None):
            raise DomainValidationError("claim: an owner always has a lease, and a run without one has none")
        if self.worker_id is not None and self.last_heartbeat_at is None:
            raise DomainValidationError("claim: an owner always has a last heartbeat")
        if self.lease_expires_at is not None:
            expires = parse_timestamp(self.lease_expires_at)
            heartbeat = parse_timestamp(self.last_heartbeat_at)  # type: ignore[arg-type]
            if expires <= parse_timestamp(self.claimed_at) or expires <= heartbeat:  # type: ignore[arg-type]
                raise DomainValidationError("claim: the lease must end after the claim and the renewal that set it")
            if expires - heartbeat > LeasePolicy.MAX_LEASE:
                raise DomainValidationError(f"claim: a lease is never longer than {LeasePolicy.MAX_LEASE}")
        if self.last_heartbeat_at is not None and (
            self.claimed_at is None or parse_timestamp(self.last_heartbeat_at) < parse_timestamp(self.claimed_at)
        ):
            raise DomainValidationError("claim: the last heartbeat is before the claim")
        if parse_timestamp(self.updated_at) < parse_timestamp(self.created_at):
            raise DomainValidationError("claim: updated_at is before created_at")

    # Every comparison below is against the coordinating clock: the database's now().

    def lease_valid(self, now: str) -> bool:
        """Owned, and the lease has not ended: now < lease_expires_at."""
        return self.lease_expires_at is not None and parse_timestamp(now) < parse_timestamp(self.lease_expires_at)

    def lease_expired(self, now: str) -> bool:
        """Owned, but the lease has ended: lease_expires_at <= now. The boundary instant is expired."""
        return self.lease_expires_at is not None and parse_timestamp(self.lease_expires_at) <= parse_timestamp(now)

    @property
    def woken_while_claimed(self) -> bool:
        """Did a wake-up arrive after the current (or last) claim? Its release must leave the run available."""
        return self.wake_seq > self.claimed_wake_seq

    def claimable(self, now: str) -> bool:
        """The run wants a worker by now, and has no owner with a valid lease."""
        if self.available_at is None or parse_timestamp(self.available_at) > parse_timestamp(now):
            return False
        return self.worker_id is None or self.lease_expired(now)

    def admits(self, fence: Fence, now: str) -> bool:
        """Is ``fence`` the current owner, with a valid lease? Every protected write asks this."""
        return (
            (self.project_id, self.run_id) == (fence.project_id, fence.run_id)
            and self.worker_id == fence.worker_id
            and self.fencing_token == fence.fencing_token
            and self.lease_valid(now)
        )

    def refusal(self, fence: Fence, now: str) -> OwnershipLost:
        """Why ``fence`` is not admitted, as the error the stale worker receives."""
        where = f"run {fence.project_id}/{fence.run_id}"
        if self.fencing_token != fence.fencing_token or (self.worker_id not in (None, fence.worker_id)):
            return OwnershipLost(
                f"{where}: generation {fence.fencing_token} was superseded by generation {self.fencing_token}",
                reason="superseded",
            )
        if self.worker_id is None:
            return OwnershipLost(f"{where}: the claim of generation {fence.fencing_token} was released", reason="released")
        return OwnershipLost(f"{where}: the lease of generation {fence.fencing_token} expired", reason="expired")


@dataclass(frozen=True)
class Claim:
    """A successful claim: the new owner's fence and lease, and the expired owner it replaced, if any."""

    fence: Fence
    claimed_at: str
    lease_expires_at: str
    previous_worker_id: str | None = None
    previous_fencing_token: int | None = None
    previous_lease_expires_at: str | None = None

    def __post_init__(self) -> None:
        check_timestamp(self.claimed_at, "claim.claimed_at")
        check_timestamp(self.lease_expires_at, "claim.lease_expires_at")
        if self.previous_worker_id is not None:
            check_pattern(self.previous_worker_id, WORKER_ID_RE, "claim.previous_worker_id")
            check_int(self.previous_fencing_token, "claim.previous_fencing_token", minimum=1)
            check_timestamp(self.previous_lease_expires_at, "claim.previous_lease_expires_at")

    @property
    def taken_over(self) -> bool:
        """True if another worker still held the claim, with an expired lease."""
        return self.previous_worker_id is not None


@dataclass(frozen=True)
class LeasePolicy:
    """How long a lease lasts, and how often its owner renews it.

    Defaults (docs/workers.md, "leases"): a 60-second lease, renewed every 20
    seconds, so two consecutive heartbeats can fail before the lease runs out.
    A crashed worker's run is taken over about a minute later. The longest
    accepted lease is five minutes; long leases only delay recovery.

    The owner also keeps a **local deadline** on its monotonic clock: if no
    renewal has been confirmed within ``lease - margin`` of *sending* the last
    confirmed one, it stops working on the run by itself. That never decides
    anything for other processes (only the database clock does); it makes a
    worker that cannot reach the database stop early instead of working on
    without a lease it can prove.
    """

    lease: timedelta = timedelta(seconds=60)
    heartbeat_interval: timedelta = timedelta(seconds=20)

    MIN_LEASE: ClassVar[timedelta] = timedelta(seconds=2)
    MAX_LEASE: ClassVar[timedelta] = timedelta(minutes=5)

    def __post_init__(self) -> None:
        for name in ("lease", "heartbeat_interval"):
            if not isinstance(getattr(self, name), timedelta):
                raise DomainValidationError(f"lease policy: {name} must be a timedelta")
        if not self.MIN_LEASE <= self.lease <= self.MAX_LEASE:
            raise DomainValidationError(f"lease policy: the lease must be between {self.MIN_LEASE} and {self.MAX_LEASE}")
        if self.heartbeat_interval <= timedelta(0) or self.heartbeat_interval * 3 > self.lease:
            raise DomainValidationError("lease policy: heartbeats must come at least three times per lease")

    @property
    def margin(self) -> timedelta:
        """How long before the database's expiry the owner gives up on its own: a quarter of the lease."""
        return self.lease / 4

    @property
    def local_validity(self) -> timedelta:
        """How long after sending a confirmed renewal the owner keeps working: lease minus margin."""
        return self.lease - self.margin

    def local_deadline(self, sent_at: float) -> float:
        """The monotonic time until which a renewal sent at ``sent_at`` (monotonic seconds) is trusted.

        The database set the lease to (its now) + lease, and its now was no
        earlier than the moment the renewal was sent, so measuring from the
        send time can only underestimate the real lease.
        """
        return sent_at + self.local_validity.total_seconds()


__all__ = [
    "WORKER_ID_RE",
    "Claim",
    "Fence",
    "LeasePolicy",
    "OwnershipLost",
    "RunClaim",
    "new_worker_id",
]

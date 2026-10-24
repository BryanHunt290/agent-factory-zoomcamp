"""The notification outbox's persistence boundary (docs/notifications.md), and its in-memory implementation.

Two interfaces, split like the run queue and the run claims:

* :class:`NotificationRepository` is part of ``ProductStore``. Its writes
  run in the caller's transaction: the recorder enqueues a notification
  intent in the very transaction that records the attention event (the
  transactional outbox), and the API reads notification status. Enqueueing
  is idempotent: one notification per (attention event, destination), ever.
* :class:`NotificationOutbox` is the dispatcher's own session, never part of
  ``ProductStore``: claiming, attempts, results. Every operation is one short
  transaction; no transaction is ever held open while a provider is called.

Rules every implementation follows (PostgreSQL decides time with ``now()``):

* **Claims.** ``claim_due`` hands each due notification to one dispatcher
  under a lease, adding 1 to its ``claim_token``. Of concurrent claimers,
  exactly one gets it.
* **Fencing.** ``begin_attempt``, ``finish``, ``defer``, and ``skip`` change
  a notification only for its current ``(claimed_by, claim_token)`` while it
  is ``sending``; ``begin_attempt`` also needs the lease to still be valid. A
  stale dispatcher's write changes nothing and returns None/False.
* **Recovery.** ``recover_expired`` takes back notifications whose lease
  expired, as a new generation (claim_token + 1). An attempt left open by the
  lost dispatcher is closed as ``unknown``: the provider may or may not have
  accepted it. The notification is retried (at least once), or fails at the
  attempt limit.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from ..util import Clock, timestamp, utc_now
from .approvals import ApprovalStatus
from .attention import AttentionKind
from .errors import ConflictError, RecordNotFoundError
from .executions import ExecutionState
from .notifications import (
    AttemptOutcome,
    Delivery,
    Destination,
    FailureCategory,
    Notification,
    NotificationAttempt,
    NotificationIntent,
    NotificationStatus,
    RetryPolicy,
    after_attempt,
    later,
)
from .serialization import check_pattern, parse_timestamp
from .notifications import DISPATCHER_ID_RE, PERMISSION_FOR

if TYPE_CHECKING:
    from .repositories import InMemoryDatabase

#: The longest lease a dispatcher may hold on a notification (the database refuses longer ones too).
MAX_LEASE = timedelta(minutes=5)


class NotificationRepository(ABC):
    """Notification intents, their status, and destinations; in the caller's transaction."""

    @abstractmethod
    def enqueue(self, intent: NotificationIntent) -> list[str]:
        """One pending notification per eligible destination that has none for this event yet.

        Eligible: an active destination of an active principal holding
        ``intent.permission``. Returns the IDs created now (empty when every
        one already existed: recording the same event again creates nothing).
        """

    @abstractmethod
    def get(self, notification_id: str) -> Notification | None: ...

    @abstractmethod
    def list_for_approval(self, project_id: str, approval_id: str) -> list[Notification]:
        """Every notification about this approval (its decision and any reconciliation), oldest first."""

    @abstractmethod
    def attempts(self, notification_id: str) -> list[NotificationAttempt]:
        """The notification's attempts, by attempt number."""

    @abstractmethod
    def with_unknown_outcome(self, notification_ids: list[str]) -> set[str]:
        """Those of ``notification_ids`` with at least one attempt whose outcome is unknown."""

    @abstractmethod
    def add_destination(self, destination: Destination) -> None:
        """``ConflictError`` if the principal already has this exact address on this channel."""

    @abstractmethod
    def get_destination(self, destination_id: str) -> Destination | None: ...

    @abstractmethod
    def list_destinations(self, principal_id: str | None = None) -> list[Destination]:
        """Destinations (one principal's, or all), in creation order."""

    @abstractmethod
    def update_destination(self, destination: Destination) -> None:
        """Store a new status. Nothing else about a destination ever changes (``Destination.check_successor``)."""


class NotificationOutbox(ABC):
    """The dispatcher's session: claim, attempt, record. Each call is its own short transaction."""

    @abstractmethod
    def now(self) -> str:
        """The coordinating clock (PostgreSQL: ``now()``)."""

    @abstractmethod
    def recover_expired(self, policy: RetryPolicy, rng: random.Random | None = None, limit: int = 50) -> list[Notification]:
        """Take back notifications whose dispatcher's lease expired (see the module docstring)."""

    @abstractmethod
    def claim_due(self, dispatcher_id: str, lease: timedelta, limit: int = 10) -> list[Notification]:
        """Claim up to ``limit`` pending notifications that are due, oldest due first."""

    @abstractmethod
    def still_needed(self, n: Notification) -> FailureCategory | None:
        """Why ``n`` should not be sent any more (its attention resolved, its destination or recipient
        no longer eligible), or None if it is still needed."""

    @abstractmethod
    def attempts_since(self, destination_id: str, since: str) -> list[str]:
        """Start times of attempts to this destination after ``since``, oldest first (rate limit)."""

    @abstractmethod
    def destination(self, destination_id: str) -> Destination | None: ...

    @abstractmethod
    def begin_attempt(self, n: Notification) -> Notification | None:
        """Record a new open attempt (write-ahead), only for the current claim with a valid lease."""

    @abstractmethod
    def finish(
        self, n: Notification, delivery: Delivery, policy: RetryPolicy, rng: random.Random | None = None
    ) -> Notification | None:
        """Close the open attempt and release the notification (sent, retry later, or failed). Fenced."""

    @abstractmethod
    def defer(self, n: Notification, until: str) -> bool:
        """Release without an attempt, due again at ``until`` (rate limit). Fenced."""

    @abstractmethod
    def skip(self, n: Notification, reason: FailureCategory) -> bool:
        """Release as skipped: no longer needed. Fenced."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources such as a database connection."""


# ------------------------------------------------------------------ in memory


def _owned(stored: Notification | None, n: Notification) -> bool:
    return (
        stored is not None
        and stored.status is NotificationStatus.SENDING
        and (stored.claimed_by, stored.claim_token) == (n.claimed_by, n.claim_token)
    )


class InMemoryNotificationRepository(NotificationRepository):
    def __init__(self, db: InMemoryDatabase) -> None:
        self.db = db

    def _eligible(self, intent: NotificationIntent) -> list[Destination]:
        found = []
        for d in self.db.destinations.values():
            principal = self.db.principals.get(d.principal_id)
            if d.active and principal is not None and principal.active and (d.principal_id, intent.permission) in self.db.grants:
                found.append(d)
        return sorted(found, key=lambda d: (parse_timestamp(d.created_at), d.id))

    def enqueue(self, intent: NotificationIntent) -> list[str]:
        created = []
        with self.db.lock:
            self.db.require_run(intent.item.project_id, intent.item.run_id)
            for destination in self._eligible(intent):
                n = intent.notification_for(destination)
                if n.id not in self.db.notifications:
                    self.db.notifications[n.id] = n
                    created.append(n.id)
        return created

    def get(self, notification_id: str) -> Notification | None:
        with self.db.lock:
            return self.db.notifications.get(notification_id)

    def list_for_approval(self, project_id: str, approval_id: str) -> list[Notification]:
        with self.db.lock:
            found = [n for n in self.db.notifications.values() if (n.project_id, n.approval_id) == (project_id, approval_id)]
        return sorted(found, key=lambda n: (parse_timestamp(n.created_at), n.id))

    def attempts(self, notification_id: str) -> list[NotificationAttempt]:
        with self.db.lock:
            found = [a for (nid, _), a in self.db.notification_attempts.items() if nid == notification_id]
        return sorted(found, key=lambda a: a.attempt)

    def with_unknown_outcome(self, notification_ids: list[str]) -> set[str]:
        wanted = set(notification_ids)
        with self.db.lock:
            return {
                nid for (nid, _), a in self.db.notification_attempts.items() if nid in wanted and a.outcome is AttemptOutcome.UNKNOWN
            }

    def add_destination(self, destination: Destination) -> None:
        with self.db.lock:
            if destination.principal_id not in self.db.principals:
                raise RecordNotFoundError(f"principal {destination.principal_id} does not exist")
            if destination.id in self.db.destinations:
                raise ConflictError(f"destination {destination.id} already exists")
            same = (destination.principal_id, destination.channel, destination.address)
            if any((d.principal_id, d.channel, d.address) == same for d in self.db.destinations.values()):
                raise ConflictError("this principal already has this destination")
            self.db.destinations[destination.id] = destination

    def get_destination(self, destination_id: str) -> Destination | None:
        with self.db.lock:
            return self.db.destinations.get(destination_id)

    def list_destinations(self, principal_id: str | None = None) -> list[Destination]:
        with self.db.lock:
            found = [d for d in self.db.destinations.values() if principal_id is None or d.principal_id == principal_id]
        return sorted(found, key=lambda d: (parse_timestamp(d.created_at), d.id))

    def update_destination(self, destination: Destination) -> None:
        with self.db.lock:
            old = self.db.destinations.get(destination.id)
            if old is None:
                raise RecordNotFoundError(f"destination {destination.id} does not exist")
            Destination.check_successor(old, destination)
            self.db.destinations[destination.id] = destination


class InMemoryNotificationOutbox(NotificationOutbox):
    """The dispatcher's operations for unit tests, on an injectable clock.

    It follows the PostgreSQL rules but proves nothing about coordination
    between processes; the PostgreSQL tests do that.
    """

    def __init__(self, db: InMemoryDatabase, clock: Clock = utc_now) -> None:
        self.db = db
        self._clock = clock

    def now(self) -> str:
        return timestamp(self._clock)

    def recover_expired(self, policy: RetryPolicy, rng: random.Random | None = None, limit: int = 50) -> list[Notification]:
        recovered = []
        with self.db.lock:
            now = self.now()
            expired = sorted(
                (n for n in self.db.notifications.values()
                 if n.status is NotificationStatus.SENDING and parse_timestamp(n.lease_expires_at) <= parse_timestamp(now)),  # type: ignore[arg-type]
                key=lambda n: (parse_timestamp(n.lease_expires_at), n.id),  # type: ignore[arg-type]
            )[:limit]  # fmt: skip
            for n in expired:
                taken = replace(n, claim_token=n.claim_token + 1)  # a new generation: the lost dispatcher is fenced out
                open_attempt = self.db.notification_attempts.get((n.id, n.attempts))
                if open_attempt is not None and open_attempt.open:
                    self.db.notification_attempts[(n.id, n.attempts)] = replace(
                        open_attempt, ended_at=now, outcome=AttemptOutcome.UNKNOWN, failure_category=FailureCategory.OUTCOME_UNKNOWN
                    )
                    new = after_attempt(
                        taken, Delivery(AttemptOutcome.UNKNOWN, FailureCategory.OUTCOME_UNKNOWN), at=now, policy=policy, rng=rng
                    )
                else:  # the dispatcher stopped before it began an attempt: nothing was sent; due at once
                    new = replace(taken, status=NotificationStatus.PENDING, claimed_by=None, lease_expires_at=None,
                                  next_attempt_at=now, updated_at=now)  # fmt: skip
                self.db.notifications[n.id] = new
                recovered.append(new)
        return recovered

    def claim_due(self, dispatcher_id: str, lease: timedelta, limit: int = 10) -> list[Notification]:
        check_pattern(dispatcher_id, DISPATCHER_ID_RE, "dispatcher_id")
        if not timedelta(0) < lease <= MAX_LEASE:
            raise ValueError("lease must be positive and at most five minutes")
        claimed = []
        with self.db.lock:
            now = self.now()
            due = sorted(
                (n for n in self.db.notifications.values()
                 if n.status is NotificationStatus.PENDING and parse_timestamp(n.next_attempt_at) <= parse_timestamp(now)),  # type: ignore[arg-type]
                key=lambda n: (parse_timestamp(n.next_attempt_at), n.id),  # type: ignore[arg-type]
            )[:limit]  # fmt: skip
            for n in due:
                new = replace(n, status=NotificationStatus.SENDING, claimed_by=dispatcher_id, claim_token=n.claim_token + 1,
                              lease_expires_at=later(now, lease), updated_at=now)  # fmt: skip
                self.db.notifications[n.id] = new
                claimed.append(new)
        return claimed

    def still_needed(self, n: Notification) -> FailureCategory | None:
        with self.db.lock:
            destination = self.db.destinations.get(n.destination_id)
            if destination is None or not destination.active:
                return FailureCategory.DESTINATION_DISABLED
            principal = self.db.principals.get(n.principal_id)
            if principal is None or not principal.active or (n.principal_id, PERMISSION_FOR[n.attention_kind]) not in self.db.grants:
                return FailureCategory.RECIPIENT_NOT_PERMITTED
            if n.attention_kind is AttentionKind.DECISION_REQUIRED:
                request = self.db.requests.get((n.project_id, n.approval_id))
                if request is None or request.status is not ApprovalStatus.PENDING or request.is_expired(self.now()):
                    return FailureCategory.ATTENTION_RESOLVED
                return None
            execution = next((e for e in self.db.executions.values() if e.id == n.execution_id), None)
            if (
                execution is None
                or execution.state is not ExecutionState.UNCERTAIN
                or execution.attempts != n.attention_attempt
                or (n.execution_id, n.attention_attempt) in self.db.reconciliation_decisions
            ):
                return FailureCategory.ATTENTION_RESOLVED
            return None

    def attempts_since(self, destination_id: str, since: str) -> list[str]:
        with self.db.lock:
            ids = {nid for nid, n in self.db.notifications.items() if n.destination_id == destination_id}
            starts = [a.started_at for (nid, _), a in self.db.notification_attempts.items()
                      if nid in ids and parse_timestamp(a.started_at) > parse_timestamp(since)]  # fmt: skip
        return sorted(starts, key=parse_timestamp)

    def destination(self, destination_id: str) -> Destination | None:
        with self.db.lock:
            return self.db.destinations.get(destination_id)

    def begin_attempt(self, n: Notification) -> Notification | None:
        with self.db.lock:
            stored = self.db.notifications.get(n.id)
            now = self.now()
            if not _owned(stored, n) or parse_timestamp(stored.lease_expires_at) <= parse_timestamp(now):  # type: ignore[union-attr, arg-type]
                return None
            new = replace(stored, attempts=stored.attempts + 1, last_attempt_at=now, updated_at=now)  # type: ignore[arg-type]
            self.db.notification_attempts[(n.id, new.attempts)] = NotificationAttempt(
                notification_id=n.id, attempt=new.attempts, dispatcher_id=n.claimed_by, claim_token=n.claim_token,  # type: ignore[arg-type]
                started_at=now,
            )  # fmt: skip
            self.db.notifications[n.id] = new
            return new

    def finish(
        self, n: Notification, delivery: Delivery, policy: RetryPolicy, rng: random.Random | None = None
    ) -> Notification | None:
        with self.db.lock:
            stored = self.db.notifications.get(n.id)
            if not _owned(stored, n):
                return None
            now = self.now()
            attempt = self.db.notification_attempts.get((n.id, stored.attempts))  # type: ignore[union-attr]
            if attempt is None or not attempt.open:
                return None
            self.db.notification_attempts[(n.id, attempt.attempt)] = replace(
                attempt, ended_at=now, outcome=delivery.outcome, failure_category=delivery.failure_category,
                provider_message_id=delivery.provider_message_id,
            )  # fmt: skip
            new = after_attempt(stored, delivery, at=now, policy=policy, rng=rng)  # type: ignore[arg-type]
            self.db.notifications[n.id] = new
            return new

    def defer(self, n: Notification, until: str) -> bool:
        with self.db.lock:
            stored = self.db.notifications.get(n.id)
            if not _owned(stored, n):
                return False
            self.db.notifications[n.id] = replace(
                stored, status=NotificationStatus.PENDING, claimed_by=None, lease_expires_at=None, next_attempt_at=until,  # type: ignore[arg-type]
                failure_category=FailureCategory.RATE_LIMITED, updated_at=self.now(),
            )  # fmt: skip
            return True

    def skip(self, n: Notification, reason: FailureCategory) -> bool:
        with self.db.lock:
            stored = self.db.notifications.get(n.id)
            if not _owned(stored, n):
                return False
            self.db.notifications[n.id] = replace(
                stored, status=NotificationStatus.SKIPPED, claimed_by=None, lease_expires_at=None, next_attempt_at=None,  # type: ignore[arg-type]
                failure_category=reason, updated_at=self.now(),
            )  # fmt: skip
            return True


__all__ = [
    "MAX_LEASE",
    "InMemoryNotificationOutbox",
    "InMemoryNotificationRepository",
    "NotificationOutbox",
    "NotificationRepository",
]

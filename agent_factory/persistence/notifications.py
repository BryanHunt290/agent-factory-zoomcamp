"""The notification outbox in PostgreSQL (migration ``0007_notifications.sql``; docs/notifications.md).

* :class:`PostgresNotificationRepository` is part of the product store: its
  writes join the caller's transaction, so the recorder's sync stores an
  attention event and its notification intents together (and, for a claimed
  run, fenced by the run's claim like every other write of the sync).
* :class:`PostgresNotificationOutbox` is a dispatcher's own session. Each
  operation is one short transaction, decided by PostgreSQL's ``now()``, and
  none is ever open while a provider is being called:

  ::

      claim_due      UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED): sending, token + 1, lease
      begin_attempt  fenced, lease still valid: attempts + 1, an open attempt row      COMMIT
      (provider call: no transaction)
      finish         fenced: close the attempt, sent / pending (backoff) / failed      COMMIT

  "Fenced" means the row is still ``sending`` for this dispatcher's
  ``(claimed_by, claim_token)``; a dispatcher whose notification was
  recovered by another (``recover_expired`` adds 1 to the token) changes
  nothing.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg

from ..domain.errors import RecordNotFoundError, StorageUnavailableError
from ..domain.notification_repositories import MAX_LEASE, NotificationOutbox, NotificationRepository
from ..domain.notifications import (
    DISPATCHER_ID_RE,
    PERMISSION_FOR,
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
)
from ..domain.serialization import check_pattern
from .config import DatabaseConfig
from .postgres import PostgresDatabase, _scrub, _text, _ts, connection_kwargs, require_current_schema

#: Dispatcher connections name themselves, so they can be told apart in pg_stat_activity.
DISPATCHER_APPLICATION_NAME = "agent-factory-notifier"

_DESTINATION_COLUMNS = "id, principal_id, channel, address, status, policy, created_at, updated_at"
_NOTIFICATION_COLUMNS = (
    "id, dedup_key, schema_version, project_id, run_id, approval_id, attention_kind, execution_id, attention_attempt, "
    "destination_id, principal_id, channel, severity, title, body, link_path, status, attempts, created_at, available_at, "
    "next_attempt_at, last_attempt_at, sent_at, provider_message_id, failure_category, claimed_by, claim_token, "
    "lease_expires_at, updated_at"
)
_ATTEMPT_COLUMNS = (
    "notification_id, attempt, dispatcher_id, claim_token, started_at, ended_at, outcome, failure_category, provider_message_id"
)
_TIMES = ("created_at", "available_at", "next_attempt_at", "last_attempt_at", "sent_at", "lease_expires_at", "updated_at")


def _destination_from(row: Mapping[str, Any]) -> Destination:
    return Destination(
        id=row["id"], principal_id=row["principal_id"], channel=row["channel"], address=row["address"],
        status=row["status"], policy=row["policy"], created_at=_text(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_text(row["updated_at"]),  # type: ignore[arg-type]
    )  # fmt: skip


def _notification_from(row: Mapping[str, Any]) -> Notification:
    values = {name: row[name] for name in _NOTIFICATION_COLUMNS.split(", ")}
    for name in _TIMES:
        values[name] = _text(values[name])
    return Notification(**values)


def _notification_row(n: Notification) -> dict[str, Any]:
    row: dict[str, Any] = {name: getattr(n, name) for name in _NOTIFICATION_COLUMNS.split(", ")}
    for name in _TIMES:
        row[name] = _ts(row[name])
    for name in ("attention_kind", "channel", "severity", "status", "failure_category"):
        row[name] = row[name].value if row[name] is not None else None
    return row


def _attempt_from(row: Mapping[str, Any]) -> NotificationAttempt:
    return NotificationAttempt(
        notification_id=row["notification_id"], attempt=row["attempt"], dispatcher_id=row["dispatcher_id"],
        claim_token=row["claim_token"], started_at=_text(row["started_at"]), ended_at=_text(row["ended_at"]),  # type: ignore[arg-type]
        outcome=row["outcome"], failure_category=row["failure_category"], provider_message_id=row["provider_message_id"],
    )  # fmt: skip


_INSERT_NOTIFICATION = (
    f"INSERT INTO notifications ({_NOTIFICATION_COLUMNS}) VALUES ("
    + ", ".join(f"%({c})s" for c in _NOTIFICATION_COLUMNS.split(", "))
    + ") ON CONFLICT DO NOTHING RETURNING id"
)
_MUTABLE = ("status", "attempts", "next_attempt_at", "last_attempt_at", "sent_at", "provider_message_id", "failure_category",
            "claimed_by", "claim_token", "lease_expires_at", "updated_at")  # fmt: skip
_UPDATE_NOTIFICATION = (
    "UPDATE notifications SET " + ", ".join(f"{c} = %({c})s" for c in _MUTABLE) + " WHERE id = %(id)s"
)

#: Eligible destinations: active, of an active principal holding the permission to act on the attention.
_ELIGIBLE = f"""
SELECT {", ".join("d." + c for c in _DESTINATION_COLUMNS.split(", "))}
FROM notification_destinations d
JOIN principals p ON p.id = d.principal_id AND p.status = 'active'
JOIN principal_permissions g ON g.principal_id = d.principal_id AND g.permission = %s
WHERE d.status = 'active'
ORDER BY d.created_at, d.id
"""


class PostgresNotificationRepository(NotificationRepository):
    """Notification intents and destinations, in the caller's transaction (part of ``store_for``)."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def enqueue(self, intent: NotificationIntent) -> list[str]:
        destinations = [_destination_from(r) for r in self.db.query(_ELIGIBLE, (intent.permission.value,))]
        created = []
        for destination in destinations:
            # ON CONFLICT DO NOTHING: the (dedup_key, destination_id) row already exists. Recording the same
            # attention event again, or two writers recording it at once, keeps exactly one notification.
            rows = self.db.query(_INSERT_NOTIFICATION, _notification_row(intent.notification_for(destination)))
            created += [r["id"] for r in rows]
        return created

    def get(self, notification_id: str) -> Notification | None:
        rows = self.db.query(f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications WHERE id = %s", (notification_id,))
        return _notification_from(rows[0]) if rows else None

    def list_for_approval(self, project_id: str, approval_id: str) -> list[Notification]:
        rows = self.db.query(
            f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications WHERE project_id = %s AND approval_id = %s "
            "ORDER BY created_at, id",
            (project_id, approval_id),
        )
        return [_notification_from(r) for r in rows]

    def attempts(self, notification_id: str) -> list[NotificationAttempt]:
        rows = self.db.query(
            f"SELECT {_ATTEMPT_COLUMNS} FROM notification_attempts WHERE notification_id = %s ORDER BY attempt",
            (notification_id,),
        )
        return [_attempt_from(r) for r in rows]

    def with_unknown_outcome(self, notification_ids: list[str]) -> set[str]:
        if not notification_ids:
            return set()
        rows = self.db.query(
            "SELECT DISTINCT notification_id FROM notification_attempts WHERE notification_id = ANY(%s) AND outcome = 'unknown'",
            (sorted(set(notification_ids)),),
        )
        return {r["notification_id"] for r in rows}

    def add_destination(self, destination: Destination) -> None:
        self.db.query(
            f"INSERT INTO notification_destinations ({_DESTINATION_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (destination.id, destination.principal_id, destination.channel.value, destination.address,
             destination.status.value, destination.policy.value, _ts(destination.created_at), _ts(destination.updated_at)),
        )  # fmt: skip

    def get_destination(self, destination_id: str) -> Destination | None:
        rows = self.db.query(f"SELECT {_DESTINATION_COLUMNS} FROM notification_destinations WHERE id = %s", (destination_id,))
        return _destination_from(rows[0]) if rows else None

    def list_destinations(self, principal_id: str | None = None) -> list[Destination]:
        where, params = ("WHERE principal_id = %s", (principal_id,)) if principal_id is not None else ("", ())
        rows = self.db.query(f"SELECT {_DESTINATION_COLUMNS} FROM notification_destinations {where} ORDER BY created_at, id", params)
        return [_destination_from(r) for r in rows]

    def update_destination(self, destination: Destination) -> None:
        with self.db.transaction():
            rows = self.db.query(
                f"SELECT {_DESTINATION_COLUMNS} FROM notification_destinations WHERE id = %s FOR UPDATE", (destination.id,)
            )
            if not rows:
                raise RecordNotFoundError(f"destination {destination.id} does not exist")
            Destination.check_successor(_destination_from(rows[0]), destination)
            self.db.query(
                "UPDATE notification_destinations SET status = %s, policy = %s, updated_at = %s WHERE id = %s",
                (destination.status.value, destination.policy.value, _ts(destination.updated_at), destination.id),
            )


#: The claim query: due pending notifications, oldest due first, skipping rows another dispatcher is locking.
CLAIM_DUE = f"""
WITH due AS (
    SELECT id FROM notifications
    WHERE status = 'pending' AND next_attempt_at <= now()
    ORDER BY next_attempt_at, id
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
UPDATE notifications AS n
SET status = 'sending', claimed_by = %(dispatcher_id)s, claim_token = n.claim_token + 1,
    lease_expires_at = now() + %(lease)s, updated_at = now()
FROM due
WHERE n.id = due.id AND n.status = 'pending'
RETURNING {", ".join("n." + c for c in _NOTIFICATION_COLUMNS.split(", "))}
"""

#: Fencing: the row as this dispatcher's claim left it, locked for the rest of the transaction.
_OWNED = f"""
SELECT {_NOTIFICATION_COLUMNS}, now() AS db_now, lease_expires_at > now() AS lease_valid FROM notifications
WHERE id = %(id)s AND status = 'sending' AND claimed_by = %(claimed_by)s AND claim_token = %(claim_token)s
FOR UPDATE
"""

_STILL_NEEDED = """
SELECT
    d.status = 'active' AS destination_active,
    (p.status = 'active' AND EXISTS (
        SELECT 1 FROM principal_permissions g WHERE g.principal_id = n.principal_id AND g.permission = %(permission)s
    )) AS recipient_permitted,
    CASE WHEN n.attention_kind = 'decision_required' THEN EXISTS (
        SELECT 1 FROM approval_requests r
        WHERE r.project_id = n.project_id AND r.id = n.approval_id AND r.status = 'pending' AND r.expires_at > now()
    ) ELSE EXISTS (
        SELECT 1 FROM action_executions e
        WHERE e.id = n.execution_id AND e.state = 'uncertain' AND e.attempts = n.attention_attempt
          AND NOT EXISTS (SELECT 1 FROM reconciliation_decisions x
                          WHERE x.execution_id = n.execution_id AND x.attempt = n.attention_attempt)
    ) END AS attention_open
FROM notifications n
JOIN notification_destinations d ON d.id = n.destination_id
JOIN principals p ON p.id = n.principal_id
WHERE n.id = %(id)s
"""


def _fence(n: Notification) -> dict[str, Any]:
    return {"id": n.id, "claimed_by": n.claimed_by, "claim_token": n.claim_token}


class PostgresNotificationOutbox(NotificationOutbox):
    """A dispatcher's operations on its own connection (never a run's fenced session)."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def close(self) -> None:
        self.db.close()

    def now(self) -> str:
        return _text(self.db.query("SELECT now() AS now")[0]["now"])  # type: ignore[return-value]

    def recover_expired(self, policy: RetryPolicy, rng: random.Random | None = None, limit: int = 50) -> list[Notification]:
        recovered = []
        with self.db.transaction():
            rows = self.db.query(
                f"SELECT {_NOTIFICATION_COLUMNS}, now() AS db_now FROM notifications "
                "WHERE status = 'sending' AND lease_expires_at <= now() "
                "ORDER BY lease_expires_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
                (limit,),
            )
            for row in rows:
                n, now = _notification_from(row), _text(row["db_now"])
                taken = replace(n, claim_token=n.claim_token + 1)  # a new generation: the lost dispatcher is fenced out
                open_rows = self.db.query(
                    f"SELECT {_ATTEMPT_COLUMNS} FROM notification_attempts WHERE notification_id = %s AND ended_at IS NULL FOR UPDATE",
                    (n.id,),
                )
                if open_rows:
                    # The provider may have accepted it before the dispatcher was lost: an honest "unknown", then a retry.
                    self.db.query(
                        "UPDATE notification_attempts SET ended_at = now(), outcome = 'unknown', failure_category = 'outcome_unknown' "
                        "WHERE notification_id = %s AND attempt = %s",
                        (n.id, open_rows[0]["attempt"]),
                    )
                    new = after_attempt(
                        taken, Delivery(AttemptOutcome.UNKNOWN, FailureCategory.OUTCOME_UNKNOWN), at=now, policy=policy, rng=rng  # type: ignore[arg-type]
                    )
                else:  # lost before any attempt began: nothing was sent, so it is due at once
                    new = replace(taken, status=NotificationStatus.PENDING, claimed_by=None, lease_expires_at=None, next_attempt_at=now, updated_at=now)
                self.db.query(_UPDATE_NOTIFICATION, _notification_row(new))
                recovered.append(new)
        return recovered

    def claim_due(self, dispatcher_id: str, lease: timedelta, limit: int = 10) -> list[Notification]:
        check_pattern(dispatcher_id, DISPATCHER_ID_RE, "dispatcher_id")
        if not timedelta(0) < lease <= MAX_LEASE:
            raise ValueError("lease must be positive and at most five minutes")
        with self.db.transaction():
            rows = self.db.query(CLAIM_DUE, {"dispatcher_id": dispatcher_id, "lease": lease, "limit": limit})
        return sorted((_notification_from(r) for r in rows), key=lambda n: (n.next_attempt_at, n.id))

    def still_needed(self, n: Notification) -> FailureCategory | None:
        rows = self.db.query(_STILL_NEEDED, {"id": n.id, "permission": PERMISSION_FOR[n.attention_kind].value})
        if not rows or not rows[0]["destination_active"]:
            return FailureCategory.DESTINATION_DISABLED
        if not rows[0]["recipient_permitted"]:
            return FailureCategory.RECIPIENT_NOT_PERMITTED
        if not rows[0]["attention_open"]:
            return FailureCategory.ATTENTION_RESOLVED
        return None

    def attempts_since(self, destination_id: str, since: str) -> list[str]:
        rows = self.db.query(
            "SELECT a.started_at FROM notification_attempts a JOIN notifications n ON n.id = a.notification_id "
            "WHERE n.destination_id = %s AND a.started_at > %s ORDER BY a.started_at",
            (destination_id, _ts(since)),
        )
        return [_text(r["started_at"]) for r in rows]  # type: ignore[misc]

    def destination(self, destination_id: str) -> Destination | None:
        rows = self.db.query(f"SELECT {_DESTINATION_COLUMNS} FROM notification_destinations WHERE id = %s", (destination_id,))
        return _destination_from(rows[0]) if rows else None

    def begin_attempt(self, n: Notification) -> Notification | None:
        # Write-ahead: the attempt row is committed before the provider is called, so a crash during the call
        # leaves an open attempt that recovery closes as "unknown" instead of pretending nothing was sent.
        with self.db.transaction():
            rows = self.db.query(_OWNED, _fence(n))
            if not rows or not rows[0]["lease_valid"]:
                return None
            stored, now = _notification_from(rows[0]), _text(rows[0]["db_now"])
            new = replace(stored, attempts=stored.attempts + 1, last_attempt_at=now, updated_at=now)  # type: ignore[arg-type]
            self.db.query(_UPDATE_NOTIFICATION, _notification_row(new))
            self.db.query(
                f"INSERT INTO notification_attempts ({_ATTEMPT_COLUMNS}) VALUES (%s, %s, %s, %s, now(), NULL, NULL, NULL, NULL)",
                (n.id, new.attempts, n.claimed_by, n.claim_token),
            )
        return new

    def finish(
        self, n: Notification, delivery: Delivery, policy: RetryPolicy, rng: random.Random | None = None
    ) -> Notification | None:
        with self.db.transaction():
            rows = self.db.query(_OWNED, _fence(n))
            if not rows:
                return None
            stored, now = _notification_from(rows[0]), _text(rows[0]["db_now"])
            closed = self.db.query(
                "UPDATE notification_attempts SET ended_at = now(), outcome = %s, failure_category = %s, provider_message_id = %s "
                "WHERE notification_id = %s AND attempt = %s AND ended_at IS NULL RETURNING attempt",
                (delivery.outcome.value, delivery.failure_category.value if delivery.failure_category else None,
                 delivery.provider_message_id, n.id, stored.attempts),
            )  # fmt: skip
            if not closed:
                return None
            new = after_attempt(stored, delivery, at=now, policy=policy, rng=rng)  # type: ignore[arg-type]
            self.db.query(_UPDATE_NOTIFICATION, _notification_row(new))
        return new

    def defer(self, n: Notification, until: str) -> bool:
        with self.db.transaction():
            rows = self.db.query(
                "UPDATE notifications SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL, "
                "next_attempt_at = %(until)s, failure_category = 'rate_limited', updated_at = now() "
                "WHERE id = %(id)s AND status = 'sending' AND claimed_by = %(claimed_by)s AND claim_token = %(claim_token)s "
                "RETURNING id",
                {**_fence(n), "until": _ts(until)},
            )
        return bool(rows)

    def skip(self, n: Notification, reason: FailureCategory) -> bool:
        with self.db.transaction():
            rows = self.db.query(
                "UPDATE notifications SET status = 'skipped', claimed_by = NULL, lease_expires_at = NULL, "
                "next_attempt_at = NULL, failure_category = %(reason)s, updated_at = now() "
                "WHERE id = %(id)s AND status = 'sending' AND claimed_by = %(claimed_by)s AND claim_token = %(claim_token)s "
                "RETURNING id",
                {**_fence(n), "reason": reason.value},
            )
        return bool(rows)


def _connect(config: DatabaseConfig) -> psycopg.Connection:
    try:
        return psycopg.connect(config.url, **connection_kwargs(config, application_name=DISPATCHER_APPLICATION_NAME))
    except psycopg.Error as exc:
        raise StorageUnavailableError(f"cannot connect to {config.redacted()}: {_scrub(exc, config)}") from None


def open_notification_outbox(config: DatabaseConfig) -> PostgresNotificationOutbox:
    """A dispatcher's outbox on a new connection of its own; ``close()`` it when done. Never migrates."""
    conn = _connect(config)
    try:
        require_current_schema(conn, config)
    except BaseException:
        conn.close()
        raise
    return PostgresNotificationOutbox(PostgresDatabase(conn, config))


__all__ = [
    "CLAIM_DUE",
    "DISPATCHER_APPLICATION_NAME",
    "PostgresNotificationOutbox",
    "PostgresNotificationRepository",
    "open_notification_outbox",
]

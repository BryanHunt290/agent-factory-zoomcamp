"""Run claims in PostgreSQL, and database sessions whose every write is fenced.

Two pieces (docs/workers.md):

* :class:`PostgresClaimRepository` claims, renews, releases, and verifies
  run claims (migration ``0005_run_claims.sql``). Each operation is one short
  transaction, and lease validity is decided by PostgreSQL's ``now()``.
* :class:`FencedPostgresDatabase` is a session bound to one :class:`Fence`.
  Every transaction it opens first proves, under a row lock, that the fence
  is still the run's current owner with a valid lease; a statement issued
  outside a transaction runs inside such a transaction of its own. A stale
  worker's write therefore fails inside the very transaction that would have
  stored it, and nothing it attempted is kept. The repositories built on it
  (``store_for``) are the ordinary ones: the recorder's sync, the execution
  ledger's write-ahead, and every other write are fenced without knowing it.

Why the proof takes ``FOR SHARE`` on the claim row: a takeover locks the row
with ``FOR UPDATE`` (and skips rows it cannot lock), and a renewal or release
updates it. Both conflict with ``FOR SHARE``, so a takeover can never commit
in the middle of a fenced write. Each fenced write is ordered entirely before
or entirely after any change of owner.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import psycopg
from psycopg import pq

from ..domain.claims import WORKER_ID_RE, Claim, Fence, LeasePolicy, OwnershipLost, RunClaim
from ..domain.errors import DomainValidationError, StorageUnavailableError
from ..domain.repositories import ClaimRepository, ProductStore
from ..domain.serialization import check_pattern
from ..state import RunStatus
from .config import DatabaseConfig
from .postgres import PostgresDatabase, _scrub, _text, _ts, connection_kwargs, require_current_schema, store_for

#: Worker connections name themselves, so they can be told apart in pg_stat_activity.
WORKER_APPLICATION_NAME = "agent-factory-worker"
#: A run that has finished is never handed to a worker, whatever its claim says.
FINISHED_STATUSES = sorted(s.value for s in RunStatus if s.is_terminal)

_CLAIM_COLUMNS = (
    "project_id, run_id, worker_id, fencing_token, claimed_at, lease_expires_at, last_heartbeat_at, "
    "available_at, wake_seq, claimed_wake_seq, created_at, updated_at"
)

#: The claim query (docs/workers.md, "claim query"). One statement, one short transaction:
#: discover the oldest available run without a live owner, lock it (skipping rows another
#: claimer holds), assign the worker, add 1 to the fencing token, set the lease. The UPDATE
#: repeats the ownership condition, so it is safe on its own even if the lock were missing.
CLAIM_NEXT = """
WITH candidate AS (
    SELECT c.project_id, c.run_id,
           c.worker_id AS previous_worker_id,
           c.fencing_token AS previous_fencing_token,
           c.lease_expires_at AS previous_lease_expires_at
    FROM run_claims c
    WHERE c.available_at <= now()
      AND (c.worker_id IS NULL OR c.lease_expires_at <= now())
      AND NOT EXISTS (
          SELECT 1 FROM runs r
          WHERE r.project_id = c.project_id AND r.run_id = c.run_id AND r.status = ANY(%(finished)s)
      )
    ORDER BY c.available_at, c.project_id, c.run_id
    LIMIT 1
    FOR UPDATE OF c SKIP LOCKED
)
UPDATE run_claims AS c
SET worker_id = %(worker_id)s,
    fencing_token = c.fencing_token + 1,
    claimed_at = now(),
    lease_expires_at = now() + %(lease)s,
    last_heartbeat_at = now(),
    claimed_wake_seq = c.wake_seq,
    updated_at = now()
FROM candidate
WHERE c.project_id = candidate.project_id AND c.run_id = candidate.run_id
  AND (c.worker_id IS NULL OR c.lease_expires_at <= now())
RETURNING c.project_id, c.run_id, c.fencing_token, c.claimed_at, c.lease_expires_at,
          candidate.previous_worker_id, candidate.previous_fencing_token, candidate.previous_lease_expires_at
"""

#: An operator command claims one specific run (docs/workers.md, "operator commands"): whether or not it
#: wants a worker, and even if it has finished, but never from an owner whose lease is still valid.
#: SKIP LOCKED: a run another claimer is locking right now is reported busy, never waited for.
CLAIM_RUN = """
WITH candidate AS (
    SELECT c.project_id, c.run_id,
           c.worker_id AS previous_worker_id,
           c.fencing_token AS previous_fencing_token,
           c.lease_expires_at AS previous_lease_expires_at
    FROM run_claims c
    WHERE c.project_id = %(project_id)s AND c.run_id = %(run_id)s
      AND (c.worker_id IS NULL OR c.lease_expires_at <= now())
    FOR UPDATE OF c SKIP LOCKED
)
UPDATE run_claims AS c
SET worker_id = %(worker_id)s,
    fencing_token = c.fencing_token + 1,
    claimed_at = now(),
    lease_expires_at = now() + %(lease)s,
    last_heartbeat_at = now(),
    claimed_wake_seq = c.wake_seq,
    updated_at = now()
FROM candidate
WHERE c.project_id = candidate.project_id AND c.run_id = candidate.run_id
  AND (c.worker_id IS NULL OR c.lease_expires_at <= now())
RETURNING c.project_id, c.run_id, c.fencing_token, c.claimed_at, c.lease_expires_at,
          candidate.previous_worker_id, candidate.previous_fencing_token, candidate.previous_lease_expires_at
"""

#: A heartbeat: only the current generation, and only while its lease is still valid.
RENEW = """
UPDATE run_claims
SET lease_expires_at = now() + %(lease)s, last_heartbeat_at = now(), updated_at = now()
WHERE project_id = %(project_id)s AND run_id = %(run_id)s
  AND worker_id = %(worker_id)s AND fencing_token = %(fencing_token)s
  AND lease_expires_at > now()
RETURNING lease_expires_at
"""

#: A release: only the current generation. The token is kept, so the next claim still adds 1.
#: A wake-up that committed after this claim (wake_seq moved on) makes the run available at once,
#: whatever the owner planned: the owner may have looked before the decision was visible.
RELEASE = """
UPDATE run_claims
SET worker_id = NULL, lease_expires_at = NULL, updated_at = now(),
    available_at = CASE WHEN wake_seq > claimed_wake_seq THEN now()
                        WHEN %(keep)s THEN available_at
                        WHEN %(requeue)s THEN COALESCE(%(at)s, now() + %(delay)s) ELSE NULL END
WHERE project_id = %(project_id)s AND run_id = %(run_id)s
  AND worker_id = %(worker_id)s AND fencing_token = %(fencing_token)s
RETURNING run_id
"""

#: The proof every fenced transaction starts with. FOR SHARE holds off a concurrent takeover,
#: renewal, or release of this claim until the transaction ends.
FENCE_CHECK = """
SELECT 1 FROM run_claims
WHERE project_id = %(project_id)s AND run_id = %(run_id)s
  AND worker_id = %(worker_id)s AND fencing_token = %(fencing_token)s
  AND lease_expires_at > now()
FOR SHARE
"""

ENQUEUE = f"""
INSERT INTO run_claims (project_id, run_id, fencing_token, available_at, created_at, updated_at)
VALUES (%(project_id)s, %(run_id)s, 0, now(), now(), now())
ON CONFLICT (project_id, run_id) DO UPDATE
SET available_at = COALESCE(run_claims.available_at, now()), wake_seq = run_claims.wake_seq + 1, updated_at = now()
RETURNING {_CLAIM_COLUMNS}
"""


def _fence_params(fence: Fence) -> dict[str, Any]:
    return {
        "project_id": fence.project_id,
        "run_id": fence.run_id,
        "worker_id": fence.worker_id,
        "fencing_token": fence.fencing_token,
    }


def _claim_from(row: Mapping[str, Any]) -> RunClaim:
    return RunClaim(
        project_id=row["project_id"],
        run_id=row["run_id"],
        worker_id=row["worker_id"],
        fencing_token=row["fencing_token"],
        claimed_at=_text(row["claimed_at"]),
        lease_expires_at=_text(row["lease_expires_at"]),
        last_heartbeat_at=_text(row["last_heartbeat_at"]),
        available_at=_text(row["available_at"]),
        wake_seq=row["wake_seq"],
        claimed_wake_seq=row["claimed_wake_seq"],
        created_at=_text(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_text(row["updated_at"]),  # type: ignore[arg-type]
    )


def _refusal(fence: Fence, query: Any) -> OwnershipLost:
    """Why ``fence`` is not the current owner, read with ``query`` (which must not lock anything)."""
    rows = query(f"SELECT {_CLAIM_COLUMNS}, now() AS db_now FROM run_claims WHERE project_id = %s AND run_id = %s",
                 (fence.project_id, fence.run_id))  # fmt: skip
    if not rows:
        return OwnershipLost(f"run {fence.project_id}/{fence.run_id} has no claim", reason="missing")
    return _claim_from(rows[0]).refusal(fence, _text(rows[0]["db_now"]))  # type: ignore[arg-type]


class PostgresClaimRepository(ClaimRepository):
    """Run claims over one database session (its own connection: never a fenced one)."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def close(self) -> None:
        self.db.close()

    def now(self) -> str:
        return _text(self.db.query("SELECT now() AS now")[0]["now"])  # type: ignore[return-value]

    def enqueue(self, project_id: str, run_id: str) -> RunClaim:
        with self.db.transaction():
            rows = self.db.query(ENQUEUE, {"project_id": project_id, "run_id": run_id})
        return _claim_from(rows[0])

    def claim_next(self, worker_id: str, policy: LeasePolicy) -> Claim | None:
        check_pattern(worker_id, WORKER_ID_RE, "worker_id")
        with self.db.transaction():
            rows = self.db.query(CLAIM_NEXT, {"worker_id": worker_id, "lease": policy.lease, "finished": FINISHED_STATUSES})
        return self._claim(worker_id, rows)

    def claim_run(self, project_id: str, run_id: str, worker_id: str, policy: LeasePolicy) -> Claim | None:
        check_pattern(worker_id, WORKER_ID_RE, "worker_id")
        with self.db.transaction():
            self.db.query(
                "INSERT INTO run_claims (project_id, run_id, fencing_token, created_at, updated_at) "
                "VALUES (%s, %s, 0, now(), now()) ON CONFLICT (project_id, run_id) DO NOTHING",
                (project_id, run_id),
            )
            rows = self.db.query(
                CLAIM_RUN, {"project_id": project_id, "run_id": run_id, "worker_id": worker_id, "lease": policy.lease}
            )
        return self._claim(worker_id, rows)

    @staticmethod
    def _claim(worker_id: str, rows: list[dict[str, Any]]) -> Claim | None:
        if not rows:
            return None
        row = rows[0]
        return Claim(
            fence=Fence(row["project_id"], row["run_id"], worker_id, row["fencing_token"]),
            claimed_at=_text(row["claimed_at"]),  # type: ignore[arg-type]
            lease_expires_at=_text(row["lease_expires_at"]),  # type: ignore[arg-type]
            previous_worker_id=row["previous_worker_id"],
            previous_fencing_token=row["previous_fencing_token"] if row["previous_worker_id"] else None,
            previous_lease_expires_at=_text(row["previous_lease_expires_at"]),
        )

    def renew(self, fence: Fence, policy: LeasePolicy) -> str | None:
        with self.db.transaction():
            rows = self.db.query(RENEW, {**_fence_params(fence), "lease": policy.lease})
        return _text(rows[0]["lease_expires_at"]) if rows else None

    def release(
        self, fence: Fence, *, requeue: bool | None, delay: timedelta = timedelta(0), at: str | None = None
    ) -> bool:
        if delay < timedelta(0):
            raise DomainValidationError("release: the delay must not be negative")
        params = {
            **_fence_params(fence), "keep": requeue is None, "requeue": bool(requeue), "delay": delay,
            "at": _ts(at) if at is not None else None,
        }  # fmt: skip
        with self.db.transaction():
            return bool(self.db.query(RELEASE, params))

    def verify(self, fence: Fence) -> None:
        with self.db.transaction():
            if self.db.query(FENCE_CHECK, _fence_params(fence)):
                return
            raise _refusal(fence, self.db.query)

    def get(self, project_id: str, run_id: str) -> RunClaim | None:
        rows = self.db.query(
            f"SELECT {_CLAIM_COLUMNS} FROM run_claims WHERE project_id = %s AND run_id = %s", (project_id, run_id)
        )
        return _claim_from(rows[0]) if rows else None


class FencedPostgresDatabase(PostgresDatabase):
    """A session in which every transaction first proves that ``fence`` still owns its run.

    The proof is the first statement of every outermost transaction, under a
    ``FOR SHARE`` lock on the claim row held until that transaction ends. A
    statement issued outside any transaction is wrapped in a fenced
    transaction of its own, so no write can bypass the proof. If the proof
    fails, the transaction is rolled back and ``OwnershipLost`` is raised.
    ``snapshot()`` blocks are read-only and are not fenced.
    """

    def __init__(self, conn: psycopg.Connection, config: DatabaseConfig, fence: Fence, *, owns_connection: bool = True) -> None:
        super().__init__(conn, config, owns_connection=owns_connection)
        self.fence = fence

    def _idle(self) -> bool:
        return self.conn.info.transaction_status == pq.TransactionStatus.IDLE

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outermost = self._idle()
            with super().transaction():
                if outermost:
                    self._prove()
                yield

    def query(self, statement: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            if self._idle():
                with self.transaction():
                    return super().query(statement, params)
            return super().query(statement, params)

    def prove_ownership(self) -> None:
        """Raise ``OwnershipLost`` unless the fence is current now (one fenced, empty transaction)."""
        with self.transaction():
            pass

    def _prove(self) -> None:
        base_query = super().query
        if base_query(FENCE_CHECK, _fence_params(self.fence)):
            return
        raise _refusal(self.fence, base_query)


def _connect(config: DatabaseConfig) -> psycopg.Connection:
    try:
        return psycopg.connect(config.url, **connection_kwargs(config, application_name=WORKER_APPLICATION_NAME))
    except psycopg.Error as exc:
        raise StorageUnavailableError(f"cannot connect to {config.redacted()}: {_scrub(exc, config)}") from None


def _opened(config: DatabaseConfig, make: Any) -> Any:
    conn = _connect(config)
    try:
        require_current_schema(conn, config)
        return make(conn)
    except BaseException:
        conn.close()
        raise


def open_claim_repository(config: DatabaseConfig) -> PostgresClaimRepository:
    """A claim repository on a new connection of its own; ``close()`` it when done. Never migrates."""
    return _opened(config, lambda conn: PostgresClaimRepository(PostgresDatabase(conn, config)))


def open_fenced_store(config: DatabaseConfig, fence: Fence) -> ProductStore:
    """The product store for one claimed run, on a new connection, with every write fenced by ``fence``."""
    return _opened(config, lambda conn: store_for(FencedPostgresDatabase(conn, config, fence)))


__all__ = [
    "CLAIM_NEXT",
    "CLAIM_RUN",
    "FENCE_CHECK",
    "FINISHED_STATUSES",
    "RELEASE",
    "RENEW",
    "WORKER_APPLICATION_NAME",
    "FencedPostgresDatabase",
    "PostgresClaimRepository",
    "open_claim_repository",
    "open_fenced_store",
]

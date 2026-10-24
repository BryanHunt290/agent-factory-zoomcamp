"""PostgreSQL implementation of the product repositories.

The mapping between the domain model and the relational schema
(``migrations/0001_initial.sql``) is explicit, in the ``_row``/``_from``
functions below:

* **Validation on read.** Every row becomes a domain object again when read,
  so the domain model's validation also covers data coming out of the
  database.
* **Typed columns vs JSONB.** Everything Agent Factory queries or relates is
  a typed column. JSONB holds only free-form structures: metadata, approval
  context, action parameters, and a project's milestone policy (a
  configuration document that is only ever read as a whole).
* **Timestamps** travel as timezone-aware datetimes and come back normalised
  to UTC. The connection's session time zone is UTC as well.
* **Updates** lock the stored row (``SELECT ... FOR UPDATE``) and check the
  change with the domain model's ``check_successor``, all in one transaction.
* **Pages** are keyset queries, ``WHERE (sort key) > (last seen) ORDER BY
  sort key LIMIT n``, each served by an index (docs/database.md#indexes).
* **Errors.** Driver exceptions never escape. They become the domain's error
  types (``ConflictError``, ``RecordNotFoundError``, ``ApprovalBindingError``,
  ``DomainValidationError``) or ``StorageError``, and messages never contain
  the password.

Business code never imports this module directly; it receives a
``ProductStore`` from :func:`open_postgres_store` (one connection, for the
CLI) or from a ``PostgresStorePool`` (pooled connections, for the API).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg import errors as pg_errors
from psycopg import pq
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..domain.approvals import ApprovalDecision, ApprovalRequest, ApprovalStatus, ExactAction
from ..domain.executions import ActionExecution, ActionReconciliation, ExecutionAttempt, check_replace
from ..domain.errors import (
    ApprovalBindingError,
    ConflictError,
    DomainValidationError,
    RecordNotFoundError,
    StorageError,
    StorageUnavailableError,
)
from ..domain.attention import AttentionItem, AttentionPosition
from ..domain.idempotency import IdempotencyRecord
from ..domain.identity import Actor, ActorKind
from ..domain.milestones import Milestone, MilestonePolicy, MilestoneSource
from ..domain.model_usage import ModelUsageByModel, RunModelUsage
from ..domain.principals import Permission, Principal, check_permissions
from ..domain.project import Project, Run
from ..domain.reconciliation import ReconciliationDecision
from ..domain.repositories import (
    ApprovalPosition,
    ApprovalRepository,
    ExecutionRepository,
    IdempotencyRepository,
    MilestonePosition,
    MilestoneQuery,
    MilestoneRepository,
    ModelUsageRepository,
    PrincipalRepository,
    ProductStore,
    ProjectPosition,
    ProjectRepository,
    ReconciliationRepository,
    RunQueue,
    RunRepository,
    UnitOfWork,
    approval_position,
    attention_position,
    attempt_identity,
    check_limit,
    milestone_position,
    project_position,
    same_authorization,
)
from ..domain.serialization import check_int, check_timestamp, format_timestamp, parse_timestamp
from .config import DatabaseConfig
from .migrator import SchemaNotReadyError, migration_status

CONNECT_TIMEOUT_S = 10
APPLICATION_NAME = "agent-factory"
#: Session settings for every connection: our schema only, UTC, and bounded waits
#: (a stuck lock becomes a ConflictError instead of hanging a run forever).
SESSION_OPTIONS = "-c search_path={schema} -c TimeZone=UTC -c lock_timeout=10s -c statement_timeout=60s"


def connection_kwargs(config: DatabaseConfig, *, application_name: str = APPLICATION_NAME) -> dict[str, Any]:
    """Settings for every connection: autocommit, bounded connect time, our schema, UTC, bounded waits."""
    return {
        "autocommit": True,
        "connect_timeout": CONNECT_TIMEOUT_S,
        "application_name": application_name,
        "options": SESSION_OPTIONS.format(schema=config.schema),
    }


def connect(config: DatabaseConfig) -> psycopg.Connection:
    """Open an autocommit connection bound to the configured schema, in UTC."""
    try:
        return psycopg.connect(config.url, **connection_kwargs(config))
    except psycopg.Error as exc:
        raise StorageUnavailableError(f"cannot connect to {config.redacted()}: {_scrub(exc, config)}") from None


def require_current_schema(conn: psycopg.Connection, config: DatabaseConfig) -> None:
    """Raise ``SchemaNotReadyError`` unless every migration this code has is applied, and nothing newer."""
    with _translated(config):
        status = migration_status(conn, config.schema)
    if not status.up_to_date:
        raise SchemaNotReadyError(f"{config.redacted()}: {status.describe()}. Run: python -m agent_factory db migrate")


def store_for(db: PostgresDatabase) -> ProductStore:
    """The product repositories over one database session."""
    from .notifications import PostgresNotificationRepository  # imports this module: bound late

    return ProductStore(
        projects=PostgresProjectRepository(db),
        runs=PostgresRunRepository(db),
        milestones=PostgresMilestoneRepository(db),
        approvals=PostgresApprovalRepository(db),
        idempotency=PostgresIdempotencyRepository(db),
        executions=PostgresExecutionRepository(db),
        principals=PostgresPrincipalRepository(db),
        reconciliations=PostgresReconciliationRepository(db),
        queue=PostgresRunQueue(db),
        notifications=PostgresNotificationRepository(db),
        model_usage=PostgresModelUsageRepository(db),
        unit_of_work=db,
    )


def open_postgres_store(config: DatabaseConfig, *, check_schema: bool = True) -> ProductStore:
    """Connect, check that the schema is fully migrated, and return the store. Never migrates."""
    conn = connect(config)
    try:
        if check_schema:
            require_current_schema(conn, config)
    except BaseException:
        conn.close()
        raise
    return store_for(PostgresDatabase(conn, config))


class PostgresDatabase(UnitOfWork):
    """One connection, its transactions, and error translation. Calls are serialized by a lock.

    ``owns_connection=False`` is for a connection borrowed from a pool:
    ``close()`` then leaves it open, and the pool takes it back.
    """

    def __init__(self, conn: psycopg.Connection, config: DatabaseConfig, *, owns_connection: bool = True) -> None:
        self.conn = conn
        self.config = config
        self.owns_connection = owns_connection
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # psycopg turns a nested block into a SAVEPOINT, matching UnitOfWork's nesting rule.
        with self._lock, _translated(self.config):
            with self.conn.transaction():
                yield

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """A read-only REPEATABLE READ transaction: every read in it sees the same committed state.

        READ COMMITTED, PostgreSQL's default, gives each statement a fresh
        snapshot, so a page and the counts shown next to it could disagree.
        Inside an enclosing transaction the block simply joins it.
        """
        with self._lock, _translated(self.config):
            if self.conn.info.transaction_status != pq.TransactionStatus.IDLE:
                yield
                return
            with self.conn.transaction():
                self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield

    def query(self, statement: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock, _translated(self.config):
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(statement, params)
                return cur.fetchall() if cur.description else []

    def close(self) -> None:
        with self._lock:
            if self.owns_connection:
                self.conn.close()


# ------------------------------------------------------------ error mapping


def _scrub(exc: BaseException, config: DatabaseConfig) -> str:
    text = " ".join(str(exc).split())
    password = urlsplit(config.url).password
    return text.replace(password, "***") if password else text


def _explain(exc: psycopg.Error) -> str:
    parts = [exc.diag.message_primary or "", exc.diag.message_detail or ""]
    return " ".join(p for p in parts if p).strip() or type(exc).__name__


@contextmanager
def _translated(config: DatabaseConfig) -> Iterator[None]:
    """Turn driver errors into the domain's error vocabulary."""
    try:
        yield
    except pg_errors.UniqueViolation as exc:
        raise ConflictError(f"already recorded ({exc.diag.constraint_name}): {_explain(exc)}") from exc
    except pg_errors.ForeignKeyViolation as exc:
        if exc.diag.constraint_name == "approval_decisions_binding_fkey":
            raise ApprovalBindingError(f"the decision is for a different action than its request: {_explain(exc)}") from exc
        if exc.diag.constraint_name in ("reconciliation_decisions_binding_fkey", "action_reconciliations_binding_fkey"):
            raise ApprovalBindingError(f"the reconciliation is for a different action: {_explain(exc)}") from exc
        if exc.diag.constraint_name == "action_executions_approved_binding_fkey":
            raise ApprovalBindingError(
                f"the execution is not bound to an approved decision for this exact action: {_explain(exc)}"
            ) from exc
        raise RecordNotFoundError(f"reference to a missing record ({exc.diag.constraint_name}): {_explain(exc)}") from exc
    except (pg_errors.CheckViolation, pg_errors.NotNullViolation, pg_errors.DataError) as exc:
        raise DomainValidationError(f"rejected by the database ({exc.diag.constraint_name}): {_explain(exc)}") from exc
    except (pg_errors.SerializationFailure, pg_errors.DeadlockDetected, pg_errors.LockNotAvailable) as exc:
        raise ConflictError(f"concurrent update, retry: {_explain(exc)}") from exc
    except pg_errors.QueryCanceled as exc:
        raise StorageError(f"statement timed out or was cancelled: {_explain(exc)}") from exc
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        raise StorageUnavailableError(f"database unavailable ({config.redacted()}): {_scrub(exc, config)}") from None
    except psycopg.Error as exc:
        raise StorageError(f"database error: {_scrub(exc, config)}") from None


# ------------------------------------------------------------------ mapping


def _ts(value: str | None) -> datetime | None:
    return None if value is None else parse_timestamp(value)


def _text(value: datetime | None) -> str | None:
    return None if value is None else format_timestamp(value)


_PROJECT_COLUMNS = "id, name, created_at, template, description, milestone_policy, metadata"


def _project_row(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "created_at": _ts(p.created_at),
        "template": p.template,
        "description": p.description,
        "milestone_policy": Jsonb(p.milestone_policy.to_dict()),
        "metadata": Jsonb(p.metadata),
    }


def _project_from(row: Mapping[str, Any]) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        created_at=_text(row["created_at"]),
        milestone_policy=MilestonePolicy.from_dict(row["milestone_policy"]),
        template=row["template"],
        description=row["description"],
        metadata=row["metadata"],
    )


_RUN_COLUMNS = "project_id, run_id, number, graph_name, status, created_at, updated_at, status_reason"


def _run_row(r: Run) -> dict[str, Any]:
    return {
        "project_id": r.project_id,
        "run_id": r.run_id,
        "number": r.number,
        "graph_name": r.graph_name,
        "status": r.status.value,
        "created_at": _ts(r.created_at),
        "updated_at": _ts(r.updated_at),
        "status_reason": r.status_reason,
    }


def _run_from(row: Mapping[str, Any]) -> Run:
    return Run(
        project_id=row["project_id"],
        run_id=row["run_id"],
        number=row["number"],
        graph_name=row["graph_name"],
        status=row["status"],
        created_at=_text(row["created_at"]),
        updated_at=_text(row["updated_at"]),
        status_reason=row["status_reason"],
    )


_MILESTONE_COLUMNS = (
    "id, project_id, run_id, sequence, key, type, category, title, summary, status, created_at, occurred_at, "
    "source_node, source_agent, source_step, metadata, resolves_id"
)


def _milestone_row(m: Milestone) -> dict[str, Any]:
    return {
        "id": m.id,
        "project_id": m.project_id,
        "run_id": m.run_id,
        "sequence": m.sequence,
        "key": m.key,
        "type": m.type,
        "category": m.category.value,
        "title": m.title,
        "summary": m.summary,
        "status": m.status.value,
        "created_at": _ts(m.created_at),
        "occurred_at": _ts(m.occurred_at),
        "source_node": m.source.node,
        "source_agent": m.source.agent,
        "source_step": m.source.step,
        "metadata": Jsonb(m.metadata),
        "resolves_id": m.resolves_id,
    }


def _milestone_from(row: Mapping[str, Any]) -> Milestone:
    return Milestone(
        id=row["id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        sequence=row["sequence"],
        key=row["key"],
        type=row["type"],
        category=row["category"],
        title=row["title"],
        summary=row["summary"],
        status=row["status"],
        created_at=_text(row["created_at"]),
        occurred_at=_text(row["occurred_at"]),
        source=MilestoneSource(node=row["source_node"], agent=row["source_agent"], step=row["source_step"]),
        metadata=row["metadata"],
        resolves_id=row["resolves_id"],
    )


_REQUEST_COLUMNS = (
    "project_id, id, run_id, node, agent, action_tool, action_category, action_params, action_fingerprint, "
    "reason, risk, requested_at, status, context, expires_at, updated_at"
)


def _request_row(r: ApprovalRequest) -> dict[str, Any]:
    return {
        "project_id": r.project_id,
        "id": r.id,
        "run_id": r.run_id,
        "node": r.node,
        "agent": r.agent,
        "action_tool": r.action.tool,
        "action_category": r.action.category,
        "action_params": Jsonb(r.action.params),
        "action_fingerprint": r.action_fingerprint,
        "reason": r.reason,
        "risk": r.risk,
        "requested_at": _ts(r.requested_at),
        "status": r.status.value,
        "context": Jsonb(r.context),
        "expires_at": _ts(r.expires_at),
        "updated_at": _ts(r.updated_at),
    }


def _request_from(row: Mapping[str, Any]) -> ApprovalRequest:
    # The constructor recomputes the action's fingerprint and refuses a row whose stored
    # fingerprint does not match its stored action.
    return ApprovalRequest(
        id=row["id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        node=row["node"],
        agent=row["agent"],
        action=ExactAction(tool=row["action_tool"], category=row["action_category"], params=row["action_params"]),
        action_fingerprint=row["action_fingerprint"],
        reason=row["reason"],
        risk=row["risk"],
        requested_at=_text(row["requested_at"]),
        status=row["status"],
        context=row["context"],
        expires_at=_text(row["expires_at"]),
        updated_at=_text(row["updated_at"]),
    )


_DECISION_COLUMNS = (
    "id, project_id, request_id, action_fingerprint, outcome, decided_at, actor_kind, actor_id, actor_display_name, "
    "actor_verified, comment, principal_id"
)


def _decision_row(d: ApprovalDecision) -> dict[str, Any]:
    return {
        "id": d.id,
        "project_id": d.project_id,
        "request_id": d.request_id,
        "action_fingerprint": d.action_fingerprint,
        "outcome": d.outcome.value,
        "decided_at": _ts(d.decided_at),
        "actor_kind": d.actor.kind.value,
        "actor_id": d.actor.id,
        "actor_display_name": d.actor.display_name,
        "actor_verified": d.actor.verified,
        "comment": d.comment,
        # A user decision references its principal (approval_decisions_principal_fkey).
        "principal_id": d.actor.id if d.actor.kind is ActorKind.USER else None,
    }


def _decision_from(row: Mapping[str, Any]) -> ApprovalDecision:
    return ApprovalDecision(
        id=row["id"],
        project_id=row["project_id"],
        request_id=row["request_id"],
        action_fingerprint=row["action_fingerprint"],
        outcome=row["outcome"],
        decided_at=_text(row["decided_at"]),
        actor=Actor(
            kind=row["actor_kind"],
            id=row["actor_id"],
            display_name=row["actor_display_name"],
            verified=row["actor_verified"],
        ),
        comment=row["comment"],
    )


_IDEMPOTENCY_COLUMNS = "scope, key, request_hash, response_status, response_body, created_at, expires_at"


def _idempotency_row(r: IdempotencyRecord) -> dict[str, Any]:
    return {
        "scope": r.scope,
        "key": r.key,
        "request_hash": r.request_hash,
        "response_status": r.response_status,
        "response_body": Jsonb(r.response_body),
        "created_at": _ts(r.created_at),
        "expires_at": _ts(r.expires_at),
    }


def _idempotency_from(row: Mapping[str, Any]) -> IdempotencyRecord:
    return IdempotencyRecord(
        scope=row["scope"],
        key=row["key"],
        request_hash=row["request_hash"],
        response_status=row["response_status"],
        response_body=row["response_body"],
        created_at=_text(row["created_at"]),
        expires_at=_text(row["expires_at"]),
    )


_EXECUTION_COLUMNS = (
    "id, project_id, run_id, request_id, decision_outcome, node, visit, agent, action_tool, action_category, "
    "action_params, action_fingerprint, effect_class, idempotency, max_attempts, key_retention_s, state, version, "
    "authorization_seq, authorized_at, authorized_until, attempts, dispatches, key_generation, open_attempt, owner, "
    "created_at, updated_at, finished_at, result, failure, lapse_reason"
)
#: Columns a compare-and-set may change. Identity columns are never in an UPDATE.
_EXECUTION_MUTABLE = (
    "state", "version", "authorization_seq", "authorized_at", "authorized_until", "attempts", "dispatches",
    "key_generation", "open_attempt", "owner", "updated_at", "finished_at", "result", "failure", "lapse_reason",
)  # fmt: skip


def _json_or_none(value: Mapping[str, Any] | None) -> Jsonb | None:
    return None if value is None else Jsonb(dict(value))


def _execution_row(e: ActionExecution) -> dict[str, Any]:
    return {
        "id": e.id,
        "project_id": e.project_id,
        "run_id": e.run_id,
        "request_id": e.request_id,
        "decision_outcome": "approved" if e.request_id is not None else None,
        "node": e.node,
        "visit": e.visit,
        "agent": e.agent,
        "action_tool": e.action.tool,
        "action_category": e.action.category,
        "action_params": Jsonb(e.action.params),
        "action_fingerprint": e.action_fingerprint,
        "effect_class": e.effect.value,
        "idempotency": e.idempotency.value if e.idempotency else None,
        "max_attempts": e.max_attempts,
        "key_retention_s": e.key_retention_s,
        "state": e.state.value,
        "version": e.version,
        "authorization_seq": e.authorization_seq,
        "authorized_at": _ts(e.authorized_at),
        "authorized_until": _ts(e.authorized_until),
        "attempts": e.attempts,
        "dispatches": e.dispatches,
        "key_generation": e.key_generation,
        "open_attempt": e.open_attempt,
        "owner": e.owner,
        "created_at": _ts(e.created_at),
        "updated_at": _ts(e.updated_at),
        "finished_at": _ts(e.finished_at),
        "result": _json_or_none(e.result),
        "failure": _json_or_none(e.failure),
        "lapse_reason": e.lapse_reason,
    }


def _execution_from(row: Mapping[str, Any]) -> ActionExecution:
    # The constructor recomputes the action's fingerprint and the execution ID from it, so a row
    # whose stored identity does not match its stored action is refused when read.
    return ActionExecution(
        id=row["id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        request_id=row["request_id"],
        node=row["node"],
        visit=row["visit"],
        agent=row["agent"],
        action=ExactAction(tool=row["action_tool"], category=row["action_category"], params=row["action_params"]),
        action_fingerprint=row["action_fingerprint"],
        effect=row["effect_class"],
        idempotency=row["idempotency"],
        max_attempts=row["max_attempts"],
        key_retention_s=row["key_retention_s"],
        state=row["state"],
        version=row["version"],
        authorization_seq=row["authorization_seq"],
        authorized_at=_text(row["authorized_at"]),
        authorized_until=_text(row["authorized_until"]),
        attempts=row["attempts"],
        dispatches=row["dispatches"],
        key_generation=row["key_generation"],
        open_attempt=row["open_attempt"],
        owner=row["owner"],
        created_at=_text(row["created_at"]),
        updated_at=_text(row["updated_at"]),
        finished_at=_text(row["finished_at"]),
        result=row["result"],
        failure=row["failure"],
        lapse_reason=row["lapse_reason"],
    )


_ATTEMPT_COLUMNS = "execution_id, attempt, authorization_seq, owner, idempotency_key, dispatched_at, ended_at, result, detail"


def _attempt_row(a: ExecutionAttempt) -> dict[str, Any]:
    return {
        "execution_id": a.execution_id,
        "attempt": a.attempt,
        "authorization_seq": a.authorization_seq,
        "owner": a.owner,
        "idempotency_key": a.idempotency_key,
        "dispatched_at": _ts(a.dispatched_at),
        "ended_at": _ts(a.ended_at),
        "result": a.result.value if a.result else None,
        "detail": a.detail,
    }


def _attempt_from(row: Mapping[str, Any]) -> ExecutionAttempt:
    return ExecutionAttempt(
        execution_id=row["execution_id"],
        attempt=row["attempt"],
        authorization_seq=row["authorization_seq"],
        owner=row["owner"],
        idempotency_key=row["idempotency_key"],
        dispatched_at=_text(row["dispatched_at"]),
        ended_at=_text(row["ended_at"]),
        result=row["result"],
        detail=row["detail"],
    )


_RECONCILIATION_COLUMNS = (
    "execution_id, attempt, action_fingerprint, choice, decided_at, actor_kind, actor_id, actor_display_name, "
    "actor_verified, comment, retry_authorized_until, principal_id"
)


def _principal_of(actor: Actor) -> str | None:
    """The enrolled principal a user actor names (migration 0004/0006); nothing for anyone else."""
    return actor.id if actor.kind is ActorKind.USER else None


def _reconciliation_row(r: ActionReconciliation) -> dict[str, Any]:
    return {
        "execution_id": r.execution_id,
        "attempt": r.attempt,
        "action_fingerprint": r.action_fingerprint,
        "choice": r.choice.value,
        "decided_at": _ts(r.decided_at),
        "actor_kind": r.actor.kind.value,
        "actor_id": r.actor.id,
        "actor_display_name": r.actor.display_name,
        "actor_verified": r.actor.verified,
        "comment": r.comment,
        "retry_authorized_until": _ts(r.retry_authorized_until),
        "principal_id": _principal_of(r.actor),
    }


_RECONCILIATION_DECISION_COLUMNS = (
    "project_id, run_id, execution_id, attempt, action_fingerprint, choice, decided_at, actor_kind, actor_id, "
    "actor_display_name, actor_verified, principal_id, comment"
)


def _reconciliation_decision_row(d: ReconciliationDecision) -> dict[str, Any]:
    return {
        "project_id": d.project_id,
        "run_id": d.run_id,
        "execution_id": d.execution_id,
        "attempt": d.attempt,
        "action_fingerprint": d.action_fingerprint,
        "choice": d.choice.value,
        "decided_at": _ts(d.decided_at),
        "actor_kind": d.actor.kind.value,
        "actor_id": d.actor.id,
        "actor_display_name": d.actor.display_name,
        "actor_verified": d.actor.verified,
        "principal_id": _principal_of(d.actor),
        "comment": d.comment,
    }


def _reconciliation_decision_from(row: Mapping[str, Any]) -> ReconciliationDecision:
    return ReconciliationDecision(
        project_id=row["project_id"],
        run_id=row["run_id"],
        execution_id=row["execution_id"],
        attempt=row["attempt"],
        action_fingerprint=row["action_fingerprint"],
        choice=row["choice"],
        decided_at=_text(row["decided_at"]),  # type: ignore[arg-type]
        actor=Actor(
            kind=row["actor_kind"], id=row["actor_id"], display_name=row["actor_display_name"], verified=row["actor_verified"]
        ),
        comment=row["comment"],
    )


def _reconciliation_from(row: Mapping[str, Any]) -> ActionReconciliation:
    return ActionReconciliation(
        execution_id=row["execution_id"],
        attempt=row["attempt"],
        action_fingerprint=row["action_fingerprint"],
        choice=row["choice"],
        decided_at=_text(row["decided_at"]),
        actor=Actor(
            kind=row["actor_kind"],
            id=row["actor_id"],
            display_name=row["actor_display_name"],
            verified=row["actor_verified"],
        ),
        comment=row["comment"],
        retry_authorized_until=_text(row["retry_authorized_until"]),
    )


_PRINCIPAL_COLUMNS = "id, issuer, subject, display_name, email, status, created_at, updated_at"


def _principal_row(p: Principal) -> dict[str, Any]:
    return {
        "id": p.id,
        "issuer": p.issuer,
        "subject": p.subject,
        "display_name": p.display_name,
        "email": p.email,
        "status": p.status.value,
        "created_at": _ts(p.created_at),
        "updated_at": _ts(p.updated_at),
    }


def _principal_from(row: Mapping[str, Any]) -> Principal:
    return Principal(
        id=row["id"],
        issuer=row["issuer"],
        subject=row["subject"],
        display_name=row["display_name"],
        email=row["email"],
        status=row["status"],
        created_at=_text(row["created_at"]),
        updated_at=_text(row["updated_at"]),
    )


def _insert(table: str, columns: str) -> str:
    names = [c.strip() for c in columns.split(",")]
    return f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(f'%({n})s' for n in names)})"


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {r["project_id"]: int(r["n"]) for r in rows}


# ------------------------------------------------------------- repositories


class PostgresProjectRepository(ProjectRepository):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def add(self, project: Project) -> None:
        self.db.query(_insert("projects", _PROJECT_COLUMNS), _project_row(project))

    def get(self, project_id: str) -> Project | None:
        rows = self.db.query(f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = %s", (project_id,))
        return _project_from(rows[0]) if rows else None

    def list(self) -> list[Project]:
        return [_project_from(r) for r in self.db.query(f"SELECT {_PROJECT_COLUMNS} FROM projects ORDER BY created_at, id")]

    def page(self, *, limit: int, after: ProjectPosition | None = None) -> list[Project]:
        # Projects are created by people, so the table stays small: no index on created_at
        # (docs/database.md#indexes).
        check_limit(limit)
        position = project_position(after)
        where, params = "", []
        if position is not None:
            where, params = "WHERE (created_at, id) < (%s, %s)", list(position)
        rows = self.db.query(
            f"SELECT {_PROJECT_COLUMNS} FROM projects {where} ORDER BY created_at DESC, id DESC LIMIT %s",
            [*params, limit],
        )
        return [_project_from(r) for r in rows]

    def get_many(self, project_ids: Iterable[str]) -> dict[str, Project]:
        wanted = sorted(set(project_ids))
        if not wanted:
            return {}
        rows = self.db.query(f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ANY(%s)", (wanted,))
        return {r["id"]: _project_from(r) for r in rows}


class PostgresRunRepository(RunRepository):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def add(self, run: Run) -> None:
        self.db.query(_insert("runs", _RUN_COLUMNS), _run_row(run))

    def get(self, project_id: str, run_id: str) -> Run | None:
        rows = self.db.query(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE project_id = %s AND run_id = %s", (project_id, run_id)
        )
        return _run_from(rows[0]) if rows else None

    def update(self, run: Run) -> None:
        with self.db.transaction():
            rows = self.db.query(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE project_id = %s AND run_id = %s FOR UPDATE",
                (run.project_id, run.run_id),
            )
            if not rows:
                raise RecordNotFoundError(f"run {run.ref} does not exist")
            Run.check_successor(_run_from(rows[0]), run)
            self.db.query(
                "UPDATE runs SET status = %(status)s, updated_at = %(updated_at)s, status_reason = %(status_reason)s "
                "WHERE project_id = %(project_id)s AND run_id = %(run_id)s",
                _run_row(run),
            )

    def list_for_project(self, project_id: str) -> list[Run]:
        rows = self.db.query(f"SELECT {_RUN_COLUMNS} FROM runs WHERE project_id = %s ORDER BY number", (project_id,))
        return [_run_from(r) for r in rows]

    def latest_for_project(self, project_id: str) -> Run | None:
        rows = self.db.query(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE project_id = %s ORDER BY number DESC LIMIT 1", (project_id,)
        )
        return _run_from(rows[0]) if rows else None

    def page_for_project(self, project_id: str, *, limit: int, before_number: int | None = None) -> list[Run]:
        # Served backwards by the unique index on (project_id, number).
        check_limit(limit)
        clauses, params = ["project_id = %s"], [project_id]
        if before_number is not None:
            clauses.append("number < %s")
            params.append(check_int(before_number, "before_number", minimum=1))
        rows = self.db.query(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE {' AND '.join(clauses)} ORDER BY number DESC LIMIT %s",
            [*params, limit],
        )
        return [_run_from(r) for r in rows]

    def latest_for_projects(self, project_ids: Iterable[str]) -> dict[str, Run]:
        wanted = sorted(set(project_ids))
        if not wanted:
            return {}
        rows = self.db.query(
            f"SELECT DISTINCT ON (project_id) {_RUN_COLUMNS} FROM runs WHERE project_id = ANY(%s) "
            "ORDER BY project_id, number DESC",
            (wanted,),
        )
        return {r["project_id"]: _run_from(r) for r in rows}


class PostgresMilestoneRepository(MilestoneRepository):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def add(self, milestone: Milestone) -> None:
        self.db.query(_insert("milestones", _MILESTONE_COLUMNS), _milestone_row(milestone))

    def get(self, milestone_id: str) -> Milestone | None:
        rows = self.db.query(f"SELECT {_MILESTONE_COLUMNS} FROM milestones WHERE id = %s", (milestone_id,))
        return _milestone_from(rows[0]) if rows else None

    def update(self, milestone: Milestone) -> None:
        with self.db.transaction():
            rows = self.db.query(f"SELECT {_MILESTONE_COLUMNS} FROM milestones WHERE id = %s FOR UPDATE", (milestone.id,))
            if not rows:
                raise RecordNotFoundError(f"milestone {milestone.id} does not exist")
            Milestone.check_successor(_milestone_from(rows[0]), milestone)
            self.db.query(
                "UPDATE milestones SET summary = %(summary)s, status = %(status)s, occurred_at = %(occurred_at)s, "
                "source_node = %(source_node)s, source_agent = %(source_agent)s, source_step = %(source_step)s, "
                "metadata = %(metadata)s, resolves_id = %(resolves_id)s WHERE id = %(id)s",
                _milestone_row(milestone),
            )

    @staticmethod
    def _where(query: MilestoneQuery) -> tuple[list[str], list[Any]]:
        clauses, params = ["project_id = %s"], [query.project_id]
        if query.run_id is not None:
            clauses.append("run_id = %s")
            params.append(query.run_id)
        if query.since is not None:
            clauses.append("occurred_at >= %s")
            params.append(parse_timestamp(query.since))
        if query.until is not None:
            clauses.append("occurred_at < %s")
            params.append(parse_timestamp(query.until))
        if query.statuses is not None:
            clauses.append("status = ANY(%s)")
            params.append(sorted(s.value for s in query.statuses))
        if query.types is not None:
            clauses.append("type = ANY(%s)")
            params.append(sorted(query.types))
        if query.categories is not None:
            clauses.append("category = ANY(%s)")
            params.append(sorted(c.value for c in query.categories))
        return clauses, params

    def query(self, query: MilestoneQuery) -> list[Milestone]:
        clauses, params = self._where(query)
        rows = self.db.query(
            f"SELECT {_MILESTONE_COLUMNS} FROM milestones WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_at, run_id, sequence, id",
            params,
        )
        return [_milestone_from(r) for r in rows]

    def page(self, query: MilestoneQuery, *, limit: int, after: MilestonePosition | None = None) -> list[Milestone]:
        # milestones_project_occurred_idx is (project_id, occurred_at, run_id, sequence): the
        # position comparison and the ORDER BY are both a range of that index.
        check_limit(limit)
        position = milestone_position(after)
        clauses, params = self._where(query)
        if position is not None:
            clauses.append("(occurred_at, run_id, sequence) > (%s, %s, %s)")
            params.extend(position)
        rows = self.db.query(
            f"SELECT {_MILESTONE_COLUMNS} FROM milestones WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_at, run_id, sequence LIMIT %s",
            [*params, limit],
        )
        return [_milestone_from(r) for r in rows]

    def count_since(self, project_ids: Iterable[str], since: str) -> dict[str, int]:
        wanted = sorted(set(project_ids))
        if not wanted:
            return {}
        rows = self.db.query(
            "SELECT project_id, count(*) AS n FROM milestones WHERE project_id = ANY(%s) AND occurred_at >= %s "
            "GROUP BY project_id",
            (wanted, parse_timestamp(check_timestamp(since, "since"))),
        )
        return _counts(rows)

    def next_sequence(self, project_id: str, run_id: str) -> int:
        rows = self.db.query(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM milestones WHERE project_id = %s AND run_id = %s",
            (project_id, run_id),
        )
        return int(rows[0]["next"])


class PostgresApprovalRepository(ApprovalRepository):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def add_request(self, request: ApprovalRequest) -> None:
        self.db.query(_insert("approval_requests", _REQUEST_COLUMNS), _request_row(request))

    def get_request(self, project_id: str, request_id: str) -> ApprovalRequest | None:
        rows = self.db.query(
            f"SELECT {_REQUEST_COLUMNS} FROM approval_requests WHERE project_id = %s AND id = %s", (project_id, request_id)
        )
        return _request_from(rows[0]) if rows else None

    def update_request(self, request: ApprovalRequest) -> None:
        with self.db.transaction():
            old = self._locked_request(request.project_id, request.id)
            ApprovalRequest.check_successor(old, request)
            self._write_status(request)

    def list_requests(
        self, project_id: str, *, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        clauses, params = ["project_id = %s"], [project_id]
        if run_id is not None:
            clauses.append("run_id = %s")
            params.append(run_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(ApprovalStatus(status).value)
        rows = self.db.query(
            f"SELECT {_REQUEST_COLUMNS} FROM approval_requests WHERE {' AND '.join(clauses)} ORDER BY requested_at, id",
            params,
        )
        return [_request_from(r) for r in rows]

    def save_decision(self, decision: ApprovalDecision, request: ApprovalRequest) -> None:
        if (request.project_id, request.id) != (decision.project_id, decision.request_id):
            raise DomainValidationError("decision and request do not belong together")
        with self.db.transaction():  # decision and request status: both or neither
            old = self._locked_request(decision.project_id, decision.request_id)
            if decision.action_fingerprint != old.action_fingerprint:
                raise ApprovalBindingError(f"approval {decision.request_id}: the decision is for a different action")
            if self.get_decision(decision.project_id, decision.request_id) is not None:
                raise ConflictError(f"approval request {decision.request_id} already has a decision")
            ApprovalRequest.check_successor(old, request)
            self.db.query(_insert("approval_decisions", _DECISION_COLUMNS), _decision_row(decision))
            self._write_status(request)

    def get_decision(self, project_id: str, request_id: str) -> ApprovalDecision | None:
        rows = self.db.query(
            f"SELECT {_DECISION_COLUMNS} FROM approval_decisions WHERE project_id = %s AND request_id = %s",
            (project_id, request_id),
        )
        return _decision_from(rows[0]) if rows else None

    def page_pending(
        self, *, limit: int, project_id: str | None = None, after: ApprovalPosition | None = None, at: str | None = None
    ) -> list[ApprovalRequest]:
        # The literal status = 'pending' matches the predicate of the partial index
        # approval_requests_pending_idx, which therefore holds only pending rows: a paused run
        # has at most one, so the set stays small even across all projects.
        check_limit(limit)
        position = approval_position(after)
        clauses, params = ["status = 'pending'"], []
        if at is not None:
            clauses.append("expires_at > %s")
            params.append(_ts(at))
        if project_id is not None:
            clauses.append("project_id = %s")
            params.append(project_id)
        if position is not None:
            clauses.append("(requested_at, project_id, id) > (%s, %s, %s)")
            params.extend(position)
        rows = self.db.query(
            f"SELECT {_REQUEST_COLUMNS} FROM approval_requests WHERE {' AND '.join(clauses)} "
            "ORDER BY requested_at, project_id, id LIMIT %s",
            [*params, limit],
        )
        return [_request_from(r) for r in rows]

    def page_attention(
        self, *, limit: int, at: str, project_id: str | None = None, after: AttentionPosition | None = None
    ) -> list[AttentionItem]:
        # Each branch uses its partial index (approval_requests_attention_idx,
        # action_executions_uncertain_idx), already in inbox order.
        check_limit(limit)
        position = attention_position(after)
        clauses, params = [], {"at": _ts(at), "limit": limit}
        if project_id is not None:
            clauses.append("project_id = %(project_id)s")
            params["project_id"] = project_id
        if position is not None:
            clauses.append("(since, project_id, approval_id) > (%(since)s, %(after_project)s, %(after_approval)s)")
            params.update(since=position[0], after_project=position[1], after_approval=position[2])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            "SELECT * FROM ("
            " SELECT 'decision_required' AS kind, project_id, run_id, id AS approval_id, requested_at AS since,"
            "  action_tool AS tool, action_category AS category, NULL::text AS execution_id, NULL::integer AS attempt,"
            "  expires_at"
            " FROM approval_requests WHERE status = 'pending' AND expires_at > %(at)s"
            " UNION ALL"
            " SELECT 'reconciliation_required', e.project_id, e.run_id, e.request_id, e.updated_at,"
            "  e.action_tool, e.action_category, e.id, e.attempts, NULL"
            " FROM action_executions e"
            " WHERE e.state = 'uncertain' AND e.request_id IS NOT NULL"
            "  AND NOT EXISTS (SELECT 1 FROM reconciliation_decisions d"
            "                  WHERE d.execution_id = e.id AND d.attempt = e.attempts)"
            f") items {where} ORDER BY since, project_id, approval_id LIMIT %(limit)s",
            params,
        )
        return [
            AttentionItem(
                kind=r["kind"], project_id=r["project_id"], run_id=r["run_id"], approval_id=r["approval_id"],
                since=_text(r["since"]), tool=r["tool"], category=r["category"], execution_id=r["execution_id"],  # type: ignore[arg-type]
                attempt=r["attempt"], expires_at=_text(r["expires_at"]),
            )  # fmt: skip
            for r in rows
        ]

    def pending_counts(self, project_ids: Iterable[str], *, at: str | None = None) -> dict[str, int]:
        wanted = sorted(set(project_ids))
        if not wanted:
            return {}
        unexpired = "" if at is None else " AND expires_at > %s"
        rows = self.db.query(
            "SELECT project_id, count(*) AS n FROM approval_requests WHERE status = 'pending' AND project_id = ANY(%s)"
            f"{unexpired} GROUP BY project_id",
            (wanted,) if at is None else (wanted, _ts(at)),
        )
        return _counts(rows)

    def _locked_request(self, project_id: str, request_id: str) -> ApprovalRequest:
        rows = self.db.query(
            f"SELECT {_REQUEST_COLUMNS} FROM approval_requests WHERE project_id = %s AND id = %s FOR UPDATE",
            (project_id, request_id),
        )
        if not rows:
            raise RecordNotFoundError(f"approval request {request_id} does not exist")
        return _request_from(rows[0])

    def _write_status(self, request: ApprovalRequest) -> None:
        self.db.query(
            "UPDATE approval_requests SET status = %(status)s, updated_at = %(updated_at)s "
            "WHERE project_id = %(project_id)s AND id = %(id)s",
            _request_row(request),
        )


class PostgresPrincipalRepository(PrincipalRepository):
    """Principals and grants. The authentication lookups use principals_external_identity_key
    and principal_permissions_pkey (docs/database.md#principals-and-permissions)."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def add(self, principal: Principal) -> None:
        self.db.query(_insert("principals", _PRINCIPAL_COLUMNS), _principal_row(principal))

    def get(self, principal_id: str) -> Principal | None:
        rows = self.db.query(f"SELECT {_PRINCIPAL_COLUMNS} FROM principals WHERE id = %s", (principal_id,))
        return _principal_from(rows[0]) if rows else None

    def find(self, issuer: str, subject: str) -> Principal | None:
        rows = self.db.query(
            f"SELECT {_PRINCIPAL_COLUMNS} FROM principals WHERE issuer = %s AND subject = %s", (issuer, subject)
        )
        return _principal_from(rows[0]) if rows else None

    def list(self) -> list[Principal]:
        rows = self.db.query(f"SELECT {_PRINCIPAL_COLUMNS} FROM principals ORDER BY created_at, id")
        return [_principal_from(r) for r in rows]

    def update(self, principal: Principal) -> None:
        with self.db.transaction():
            rows = self.db.query(f"SELECT {_PRINCIPAL_COLUMNS} FROM principals WHERE id = %s FOR UPDATE", (principal.id,))
            if not rows:
                raise RecordNotFoundError(f"principal {principal.id} does not exist")
            Principal.check_successor(_principal_from(rows[0]), principal)
            self.db.query(
                "UPDATE principals SET display_name = %(display_name)s, email = %(email)s, status = %(status)s, "
                "updated_at = %(updated_at)s WHERE id = %(id)s",
                _principal_row(principal),
            )

    def permissions(self, principal_id: str) -> frozenset[Permission]:
        rows = self.db.query("SELECT permission FROM principal_permissions WHERE principal_id = %s", (principal_id,))
        return check_permissions(r["permission"] for r in rows)

    def grant(self, principal_id: str, permissions: Iterable[Permission], *, at: str) -> None:
        wanted = sorted(p.value for p in check_permissions(permissions))
        check_timestamp(at, "granted_at")
        with self.db.transaction():
            self._require(principal_id)
            for permission in wanted:
                self.db.query(
                    "INSERT INTO principal_permissions (principal_id, permission, granted_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (principal_id, permission) DO NOTHING",
                    (principal_id, permission, _ts(at)),
                )

    def revoke(self, principal_id: str, permissions: Iterable[Permission]) -> None:
        unwanted = sorted(p.value for p in check_permissions(permissions))
        with self.db.transaction():
            self._require(principal_id)
            self.db.query(
                "DELETE FROM principal_permissions WHERE principal_id = %s AND permission = ANY(%s)",
                (principal_id, unwanted),
            )

    def _require(self, principal_id: str) -> None:
        if not self.db.query("SELECT 1 FROM principals WHERE id = %s FOR KEY SHARE", (principal_id,)):
            raise RecordNotFoundError(f"principal {principal_id} does not exist")


class PostgresIdempotencyRepository(IdempotencyRepository):
    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        rows = self.db.query(
            f"SELECT {_IDEMPOTENCY_COLUMNS} FROM idempotency_keys WHERE scope = %s AND key = %s", (scope, key)
        )
        return _idempotency_from(rows[0]) if rows else None

    def add(self, record: IdempotencyRecord) -> None:
        # A concurrent insert of the same key waits for the first transaction, then fails
        # with a unique violation (ConflictError) if that one committed.
        self.db.query(_insert("idempotency_keys", _IDEMPOTENCY_COLUMNS), _idempotency_row(record))

    def remove_expired(self, scope: str, key: str, *, at: str) -> bool:
        rows = self.db.query(
            "DELETE FROM idempotency_keys WHERE scope = %s AND key = %s AND expires_at <= %s RETURNING key",
            (scope, key, parse_timestamp(check_timestamp(at, "at"))),
        )
        return bool(rows)

    def purge_expired(self, *, at: str, limit: int = 100) -> int:
        check_limit(limit)
        rows = self.db.query(
            "DELETE FROM idempotency_keys WHERE (scope, key) IN "
            "(SELECT scope, key FROM idempotency_keys WHERE expires_at <= %s LIMIT %s) RETURNING key",
            (parse_timestamp(check_timestamp(at, "at")), limit),
        )
        return len(rows)


class PostgresReconciliationRepository(ReconciliationRepository):
    """Recorded reconciliation decisions (migration 0006). The database checks every reference and binding."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def add(self, decision: ReconciliationDecision) -> None:
        # Its own savepoint: a duplicate (ConflictError) leaves the caller's transaction usable, so the
        # caller can read the decision that won and answer an identical repeat as a safe retry.
        with self.db.transaction():
            self.db.query(
                _insert("reconciliation_decisions", _RECONCILIATION_DECISION_COLUMNS), _reconciliation_decision_row(decision)
            )

    def get(self, project_id: str, execution_id: str, attempt: int) -> ReconciliationDecision | None:
        rows = self.db.query(
            f"SELECT {_RECONCILIATION_DECISION_COLUMNS} FROM reconciliation_decisions "
            "WHERE execution_id = %s AND attempt = %s AND project_id = %s",
            (execution_id, attempt, project_id),
        )
        return _reconciliation_decision_from(rows[0]) if rows else None

    def list_for_execution(self, project_id: str, execution_id: str) -> list[ReconciliationDecision]:
        rows = self.db.query(
            f"SELECT {_RECONCILIATION_DECISION_COLUMNS} FROM reconciliation_decisions "
            "WHERE execution_id = %s AND project_id = %s ORDER BY attempt",
            (execution_id, project_id),
        )
        return [_reconciliation_decision_from(r) for r in rows]


class PostgresModelUsageRepository(ModelUsageRepository):
    """Run model-usage summaries (migration 0008): one row per run, replaced in the sync transaction."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def put(self, usage: RunModelUsage) -> None:
        self.db.query(
            "INSERT INTO run_model_usage (project_id, run_id, calls, failed_calls, input_tokens, output_tokens, cached_tokens, "
            "estimated_cost_usd, by_model, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, run_id) DO UPDATE SET calls = EXCLUDED.calls, failed_calls = EXCLUDED.failed_calls, "
            "input_tokens = EXCLUDED.input_tokens, output_tokens = EXCLUDED.output_tokens, cached_tokens = EXCLUDED.cached_tokens, "
            "estimated_cost_usd = EXCLUDED.estimated_cost_usd, by_model = EXCLUDED.by_model, updated_at = EXCLUDED.updated_at",
            (usage.project_id, usage.run_id, usage.calls, usage.failed_calls, usage.input_tokens, usage.output_tokens,
             usage.cached_tokens, usage.estimated_cost_usd, Jsonb([m.to_dict() for m in usage.by_model]), _ts(usage.updated_at)),
        )  # fmt: skip

    def get(self, project_id: str, run_id: str) -> RunModelUsage | None:
        rows = self.db.query(
            "SELECT calls, failed_calls, input_tokens, output_tokens, cached_tokens, estimated_cost_usd, by_model, updated_at "
            "FROM run_model_usage WHERE project_id = %s AND run_id = %s",
            (project_id, run_id),
        )
        if not rows:
            return None
        r = rows[0]
        return RunModelUsage(
            project_id=project_id, run_id=run_id, calls=r["calls"], failed_calls=r["failed_calls"],
            input_tokens=r["input_tokens"], output_tokens=r["output_tokens"], cached_tokens=r["cached_tokens"],
            estimated_cost_usd=float(r["estimated_cost_usd"]) if r["estimated_cost_usd"] is not None else None,
            updated_at=_text(r["updated_at"]),  # type: ignore[arg-type]
            by_model=tuple(ModelUsageByModel(**m) for m in r["by_model"]),
        )  # fmt: skip


class PostgresRunQueue(RunQueue):
    """Wake-ups on run_claims (migration 0005), in the caller's transaction."""

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def wake(self, project_id: str, run_id: str) -> None:
        # Claimable no later than now; an earlier availability keeps its place in the queue. wake_seq
        # counts the wake-up, so an owner holding the run now cannot release it past it (claims.RELEASE).
        self.db.query(
            "INSERT INTO run_claims (project_id, run_id, fencing_token, available_at, wake_seq, created_at, updated_at) "
            "VALUES (%s, %s, 0, now(), 1, now(), now()) "
            "ON CONFLICT (project_id, run_id) DO UPDATE "
            "SET available_at = LEAST(COALESCE(run_claims.available_at, now()), now()), "
            "wake_seq = run_claims.wake_seq + 1, updated_at = now()",
            (project_id, run_id),
        )


class PostgresExecutionRepository(ExecutionRepository):
    """Action executions with write-ahead commits.

    Every write runs in its own transaction with ``synchronous_commit = on``
    and is committed before the call returns: when ``replace`` reports a new
    dispatch, its attempt row is durable, and only then may the side effect
    begin. No transaction or row lock is held while a side effect runs.
    The compare-and-set locks the row (``SELECT ... FOR UPDATE``), so a second
    writer waits for the first to commit, then sees the new version and
    changes nothing.
    """

    def __init__(self, db: PostgresDatabase) -> None:
        self.db = db

    def _durable(self) -> None:
        # The default, made explicit: the commit returns only once the write-ahead record is on disk.
        self.db.query("SET LOCAL synchronous_commit = on")

    def get(self, project_id: str, run_id: str, execution_id: str) -> ActionExecution | None:
        rows = self.db.query(
            f"SELECT {_EXECUTION_COLUMNS} FROM action_executions WHERE id = %s AND project_id = %s AND run_id = %s",
            (execution_id, project_id, run_id),
        )
        return _execution_from(rows[0]) if rows else None

    def list_for_run(self, project_id: str, run_id: str) -> list[ActionExecution]:
        # Served by action_executions_run_idx.
        rows = self.db.query(
            f"SELECT {_EXECUTION_COLUMNS} FROM action_executions WHERE project_id = %s AND run_id = %s "
            "ORDER BY created_at, id",
            (project_id, run_id),
        )
        return [_execution_from(r) for r in rows]

    def attempts(self, project_id: str, run_id: str, execution_id: str) -> list[ExecutionAttempt]:
        rows = self.db.query(
            f"SELECT {', '.join('a.' + c.strip() for c in _ATTEMPT_COLUMNS.split(','))} "
            "FROM action_execution_attempts a JOIN action_executions e ON e.id = a.execution_id "
            "WHERE e.id = %s AND e.project_id = %s AND e.run_id = %s ORDER BY a.attempt",
            (execution_id, project_id, run_id),
        )
        return [_attempt_from(r) for r in rows]

    def reconciliations(self, project_id: str, run_id: str, execution_id: str) -> list[ActionReconciliation]:
        rows = self.db.query(
            f"SELECT {', '.join('r.' + c.strip() for c in _RECONCILIATION_COLUMNS.split(','))} "
            "FROM action_reconciliations r JOIN action_executions e ON e.id = r.execution_id "
            "WHERE e.id = %s AND e.project_id = %s AND e.run_id = %s ORDER BY r.attempt",
            (execution_id, project_id, run_id),
        )
        return [_reconciliation_from(r) for r in rows]

    def create(self, execution: ActionExecution) -> tuple[ActionExecution, bool]:
        if execution.version != 1:
            raise DomainValidationError("a new execution starts at version 1")
        with self.db.transaction():
            self._durable()
            inserted = self.db.query(
                _insert("action_executions", _EXECUTION_COLUMNS) + " ON CONFLICT (id) DO NOTHING RETURNING id",
                _execution_row(execution),
            )
            if inserted:
                return execution, True
            existing = self.get(execution.project_id, execution.run_id, execution.id)
        if existing is None:
            raise ConflictError(f"execution {execution.id} already exists in another run")
        if not same_authorization(existing, execution):
            raise ConflictError(f"execution {execution.id} already exists for a different authorization")
        return existing, False

    def replace(
        self,
        expected: ActionExecution,
        new: ActionExecution,
        *,
        attempt: ExecutionAttempt | None = None,
        reconciliation: ActionReconciliation | None = None,
    ) -> bool:
        with self.db.transaction():
            self._durable()
            rows = self.db.query(
                f"SELECT {_EXECUTION_COLUMNS} FROM action_executions "
                "WHERE id = %s AND project_id = %s AND run_id = %s FOR UPDATE",
                (expected.id, expected.project_id, expected.run_id),
            )
            if not rows:
                raise RecordNotFoundError(f"execution {expected.id} does not exist")
            current = _execution_from(rows[0])
            if current.version != expected.version:
                return False
            check_replace(current, new, attempt=attempt, reconciliation=reconciliation)
            if attempt is not None:
                if attempt.ended_at is None:
                    self.db.query(_insert("action_execution_attempts", _ATTEMPT_COLUMNS), _attempt_row(attempt))
                else:
                    stored = self.db.query(
                        f"SELECT {_ATTEMPT_COLUMNS} FROM action_execution_attempts "
                        "WHERE execution_id = %s AND attempt = %s FOR UPDATE",
                        (attempt.execution_id, attempt.attempt),
                    )
                    if (
                        not stored
                        or stored[0]["ended_at"] is not None
                        or attempt_identity(_attempt_from(stored[0])) != attempt_identity(attempt)
                    ):
                        raise ConflictError(f"attempt {attempt.attempt} of {attempt.execution_id} is not the open attempt")
                    self.db.query(
                        "UPDATE action_execution_attempts SET ended_at = %(ended_at)s, result = %(result)s, "
                        "detail = %(detail)s WHERE execution_id = %(execution_id)s AND attempt = %(attempt)s",
                        _attempt_row(attempt),
                    )
            if reconciliation is not None:
                self.db.query(
                    _insert("action_reconciliations", _RECONCILIATION_COLUMNS), _reconciliation_row(reconciliation)
                )
            row = _execution_row(new)
            assignments = ", ".join(f"{c} = %({c})s" for c in _EXECUTION_MUTABLE)
            updated = self.db.query(
                f"UPDATE action_executions SET {assignments} WHERE id = %(id)s AND version = %(expected_version)s "
                "RETURNING id",
                {**row, "expected_version": expected.version},
            )
            if not updated:  # pragma: no cover - the row is locked above
                raise ConflictError(f"execution {expected.id} changed during its compare-and-set")
        return True


__all__ = [
    "PostgresApprovalRepository",
    "PostgresDatabase",
    "PostgresExecutionRepository",
    "PostgresIdempotencyRepository",
    "PostgresMilestoneRepository",
    "PostgresProjectRepository",
    "PostgresReconciliationRepository",
    "PostgresRunQueue",
    "PostgresRunRepository",
    "SchemaNotReadyError",
    "connect",
    "connection_kwargs",
    "open_postgres_store",
    "require_current_schema",
    "store_for",
]

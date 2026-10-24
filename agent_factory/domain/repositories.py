"""The persistence boundary.

Business logic (the recorder, services, and later the API) depends only on
these abstract repositories, never on a storage technology. There are two
implementations: in memory (below), and PostgreSQL
(``agent_factory.persistence``, durable; see docs/database.md). Every
implementation must enforce the same invariants:

* **Identity.** Records are added once; a second ``add`` with the same
  identity is a ``ConflictError``.
* **Natural keys are unique,** also as ``ConflictError``: one run number per
  project; one sequence number and one key per milestone within a run; one
  decision per approval request.
* **References must exist** (``RecordNotFoundError`` otherwise): a run's
  project, a milestone's or approval request's run, the earlier milestone a
  repair ``resolves`` (same run), a decision's request.
* **Updates must be legal successors** of the stored record: state
  machines, immutable identity, final milestones never change.
* **Binding.** A decision must carry its request's action fingerprint
  (``ApprovalBindingError``). An approval-gated action execution must belong
  to an *approved* decision for the same fingerprint, and one approved
  decision has at most one execution.
* **Principals.** One principal per (issuer, subject) and per ID
  (``ConflictError``); a principal's identity never changes; permissions come
  from the closed vocabulary and belong to an enrolled principal; a decision
  by a ``user`` actor references an enrolled principal
  (``RecordNotFoundError``).
* **Reconciliation decisions.** One per uncertain attempt
  (``ConflictError`` for a second); it belongs to a real attempt of a real
  execution and carries that execution's action fingerprint.
* **Wake-ups.** ``RunQueue.wake`` makes a run want a worker no later than
  now, in the caller's transaction: a decision and its wake-up commit
  together, or neither does.
* **Notifications.** ``NotificationRepository.enqueue`` creates at most one
  notification per (attention event, destination), in the caller's
  transaction: an attention event and its notification intents commit
  together (docs/notifications.md).
* **Claims.** A run has at most one claim row; its fencing token only ever
  increases, by one per claim; renewing, releasing, and verifying succeed only
  for the current fence, and lease validity is decided by the store's own
  clock (``ClaimRepository``).
* **Compare-and-set.** An execution changes only through
  ``ExecutionRepository.replace``, which applies the change only if the
  stored version is the one the writer read: of two writers exactly one
  succeeds.
* **Order.** Queries return records in a deterministic order. Paged
  queries use *keyset* positions: the sort key of the last record already
  seen, never an offset, so a page continues exactly where the previous one
  ended however many records arrived in between.
* **Atomicity.** ``save_decision`` stores a decision and its request's new
  status atomically, and ``ProductStore.transaction()`` groups any calls so
  that they all take effect or none does.

Infrastructure failures raise ``StorageError`` (see errors.py).

The in-memory implementation is for tests and single-process use. It is not
durable: nothing survives the process.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta

from ..util import Clock, timestamp, utc_now
from .approvals import ApprovalDecision, ApprovalOutcome, ApprovalRequest, ApprovalStatus
from .attention import AttentionItem, AttentionKind, AttentionPosition, attention_order
from .claims import WORKER_ID_RE, Claim, Fence, LeasePolicy, OwnershipLost, RunClaim
from .errors import ApprovalBindingError, ConflictError, DomainValidationError, RecordNotFoundError
from .executions import ActionExecution, ActionReconciliation, ExecutionAttempt, ExecutionState, check_replace
from .idempotency import IdempotencyRecord
from .identity import ActorKind
from .ids import APPROVAL_ID_RE, PRINCIPAL_ID_RE, PROJECT_ID_RE, RUN_ID_RE
from .milestones import Milestone, MilestoneCategory, MilestoneStatus
from .model_usage import RunModelUsage
from .notification_repositories import InMemoryNotificationRepository, NotificationRepository
from .notifications import Destination, Notification, NotificationAttempt
from .principals import Permission, Principal, check_permissions
from .project import Project, Run
from .reconciliation import ReconciliationDecision
from .serialization import (
    check_enum,
    check_int,
    check_optional_timestamp,
    check_pattern,
    check_timestamp,
    format_timestamp,
    parse_timestamp,
)

#: Keyset positions: the sort key of the last record of the previous page.
ProjectPosition = tuple[str, str]  # (created_at, id); projects page newest first
MilestonePosition = tuple[str, str, int]  # (occurred_at, run_id, sequence); chronological
ApprovalPosition = tuple[str, str, str]  # (requested_at, project_id, id); oldest first


@dataclass(frozen=True)
class MilestoneQuery:
    """Which milestones to return. Time bounds apply to ``occurred_at``: since <= t < until."""

    project_id: str
    run_id: str | None = None
    since: str | None = None
    until: str | None = None
    statuses: frozenset[MilestoneStatus] | None = None
    types: frozenset[str] | None = None
    categories: frozenset[MilestoneCategory] | None = None

    def __post_init__(self) -> None:
        check_pattern(self.project_id, PROJECT_ID_RE, "query.project_id")
        if self.run_id is not None:
            check_pattern(self.run_id, RUN_ID_RE, "query.run_id")
        object.__setattr__(self, "since", check_optional_timestamp(self.since, "query.since"))
        object.__setattr__(self, "until", check_optional_timestamp(self.until, "query.until"))
        if self.since and self.until and parse_timestamp(self.until) <= parse_timestamp(self.since):
            raise DomainValidationError("query: until must be later than since")
        if self.statuses is not None:
            object.__setattr__(
                self, "statuses", frozenset(check_enum(MilestoneStatus, s, "query.statuses") for s in self.statuses)
            )
        if self.types is not None:
            object.__setattr__(self, "types", frozenset(self.types))
        if self.categories is not None:
            object.__setattr__(
                self,
                "categories",
                frozenset(check_enum(MilestoneCategory, c, "query.categories") for c in self.categories),
            )

    def matches(self, m: Milestone) -> bool:
        if m.project_id != self.project_id or (self.run_id is not None and m.run_id != self.run_id):
            return False
        at = parse_timestamp(m.occurred_at)
        if self.since is not None and at < parse_timestamp(self.since):
            return False
        if self.until is not None and at >= parse_timestamp(self.until):
            return False
        if self.statuses is not None and m.status not in self.statuses:
            return False
        if self.categories is not None and m.category not in self.categories:
            return False
        return self.types is None or m.type in self.types


def milestone_order(m: Milestone) -> tuple:
    """Chronological, with ties broken by run and per-run sequence: fully deterministic.

    Identifiers compare by code point (PostgreSQL: ``COLLATE "C"``), never by
    locale, so every store orders them identically.
    """
    return (parse_timestamp(m.occurred_at), m.run_id, m.sequence, m.id)


def check_limit(limit: int) -> int:
    return check_int(limit, "limit", minimum=1)


def project_position(after: ProjectPosition | None) -> tuple | None:
    """A validated, comparable keyset position (shared by every store)."""
    if after is None:
        return None
    created_at, project_id = after
    return (
        parse_timestamp(check_timestamp(created_at, "after.created_at")),
        check_pattern(project_id, PROJECT_ID_RE, "after.id"),
    )


def milestone_position(after: MilestonePosition | None) -> tuple | None:
    if after is None:
        return None
    occurred_at, run_id, sequence = after
    return (
        parse_timestamp(check_timestamp(occurred_at, "after.occurred_at")),
        check_pattern(run_id, RUN_ID_RE, "after.run_id"),
        check_int(sequence, "after.sequence", minimum=1),
    )


def attention_position(after: AttentionPosition | None) -> tuple | None:
    if after is None:
        return None
    since, project_id, approval_id = after
    return (parse_timestamp(check_timestamp(since, "cursor.since")), check_pattern(project_id, PROJECT_ID_RE, "cursor.project_id"),
            check_pattern(approval_id, APPROVAL_ID_RE, "cursor.approval_id"))  # fmt: skip


def approval_position(after: ApprovalPosition | None) -> tuple | None:
    if after is None:
        return None
    requested_at, project_id, request_id = after
    return (
        parse_timestamp(check_timestamp(requested_at, "after.requested_at")),
        check_pattern(project_id, PROJECT_ID_RE, "after.project_id"),
        check_pattern(request_id, APPROVAL_ID_RE, "after.id"),
    )


# ---------------------------------------------------------------- interfaces


class ProjectRepository(ABC):
    @abstractmethod
    def add(self, project: Project) -> None: ...

    @abstractmethod
    def get(self, project_id: str) -> Project | None: ...

    @abstractmethod
    def list(self) -> list[Project]: ...

    @abstractmethod
    def page(self, *, limit: int, after: ProjectPosition | None = None) -> list[Project]:
        """Up to ``limit`` projects, newest first by ``(created_at, id)``, all strictly after ``after``."""

    def get_many(self, project_ids: Iterable[str]) -> dict[str, Project]:
        """The projects that exist among ``project_ids``, by ID."""
        found = {}
        for project_id in set(project_ids):
            project = self.get(project_id)
            if project is not None:
                found[project_id] = project
        return found


class RunRepository(ABC):
    @abstractmethod
    def add(self, run: Run) -> None: ...

    @abstractmethod
    def get(self, project_id: str, run_id: str) -> Run | None: ...

    @abstractmethod
    def update(self, run: Run) -> None: ...

    @abstractmethod
    def list_for_project(self, project_id: str) -> list[Run]:
        """All runs of a project, ordered by run number."""

    def latest_for_project(self, project_id: str) -> Run | None:
        runs = self.list_for_project(project_id)
        return runs[-1] if runs else None

    @abstractmethod
    def page_for_project(self, project_id: str, *, limit: int, before_number: int | None = None) -> list[Run]:
        """Up to ``limit`` runs of a project, newest first (by number), numbered below ``before_number``."""

    def latest_for_projects(self, project_ids: Iterable[str]) -> dict[str, Run]:
        """Each listed project's latest run; projects without runs are omitted."""
        latest = {}
        for project_id in set(project_ids):
            run = self.latest_for_project(project_id)
            if run is not None:
                latest[project_id] = run
        return latest


class MilestoneRepository(ABC):
    @abstractmethod
    def add(self, milestone: Milestone) -> None: ...

    @abstractmethod
    def get(self, milestone_id: str) -> Milestone | None: ...

    @abstractmethod
    def update(self, milestone: Milestone) -> None: ...

    @abstractmethod
    def query(self, query: MilestoneQuery) -> list[Milestone]:
        """Matching milestones in :func:`milestone_order`."""

    @abstractmethod
    def next_sequence(self, project_id: str, run_id: str) -> int:
        """The sequence number the run's next new milestone gets (1-based)."""

    @abstractmethod
    def page(self, query: MilestoneQuery, *, limit: int, after: MilestonePosition | None = None) -> list[Milestone]:
        """Up to ``limit`` matching milestones by ``(occurred_at, run_id, sequence)``, all strictly after ``after``.

        Within one project ``(run_id, sequence)`` is unique, so this order is total.
        """

    @abstractmethod
    def count_since(self, project_ids: Iterable[str], since: str) -> dict[str, int]:
        """Per project, the milestones whose current status was reached at or after ``since``.

        Projects without any are omitted.
        """


class ApprovalRepository(ABC):
    @abstractmethod
    def add_request(self, request: ApprovalRequest) -> None: ...

    @abstractmethod
    def get_request(self, project_id: str, request_id: str) -> ApprovalRequest | None: ...

    @abstractmethod
    def update_request(self, request: ApprovalRequest) -> None: ...

    @abstractmethod
    def list_requests(
        self, project_id: str, *, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        """Matching requests ordered by request time, then ID."""

    @abstractmethod
    def save_decision(self, decision: ApprovalDecision, request: ApprovalRequest) -> None:
        """Atomically store ``decision`` and the request's new status. At most one decision per request."""

    @abstractmethod
    def get_decision(self, project_id: str, request_id: str) -> ApprovalDecision | None: ...

    @abstractmethod
    def page_pending(
        self, *, limit: int, project_id: str | None = None, after: ApprovalPosition | None = None, at: str | None = None
    ) -> list[ApprovalRequest]:
        """Up to ``limit`` pending requests of one project, or of every project.

        Oldest first, by ``(requested_at, project_id, id)``, all strictly after ``after``.
        With ``at``, only requests that have not expired by then.
        """

    @abstractmethod
    def pending_counts(self, project_ids: Iterable[str], *, at: str | None = None) -> dict[str, int]:
        """Pending requests per project; projects without any are omitted. With ``at``, only unexpired ones."""

    @abstractmethod
    def page_attention(
        self, *, limit: int, at: str, project_id: str | None = None, after: AttentionPosition | None = None
    ) -> list[AttentionItem]:
        """What needs a person at ``at`` (domain/attention.py), oldest first by (since, project, approval)."""


class PrincipalRepository(ABC):
    """Enrolled identities and their permissions (domain/principals.py)."""

    @abstractmethod
    def add(self, principal: Principal) -> None:
        """Enroll a principal. ``ConflictError`` if its ID or its (issuer, subject) is already enrolled."""

    @abstractmethod
    def get(self, principal_id: str) -> Principal | None: ...

    @abstractmethod
    def find(self, issuer: str, subject: str) -> Principal | None:
        """The principal enrolled for this external identity, if any: the authentication lookup."""

    @abstractmethod
    def list(self) -> list[Principal]:
        """Every principal, in enrollment order (created_at, then ID)."""

    @abstractmethod
    def update(self, principal: Principal) -> None:
        """Store a new profile or status. The identity never changes (``Principal.check_successor``)."""

    @abstractmethod
    def permissions(self, principal_id: str) -> frozenset[Permission]:
        """The permissions granted to the principal (empty for an unknown one)."""

    @abstractmethod
    def grant(self, principal_id: str, permissions: Iterable[Permission], *, at: str) -> None:
        """Grant permissions; ones already held keep their grant time. ``RecordNotFoundError`` for an unknown principal."""

    @abstractmethod
    def revoke(self, principal_id: str, permissions: Iterable[Permission]) -> None:
        """Remove permissions; ones not held are ignored. ``RecordNotFoundError`` for an unknown principal."""


class IdempotencyRepository(ABC):
    """Stored responses of idempotent requests: at most one per ``(scope, key)``."""

    @abstractmethod
    def get(self, scope: str, key: str) -> IdempotencyRecord | None: ...

    @abstractmethod
    def add(self, record: IdempotencyRecord) -> None:
        """Store a new record; ``ConflictError`` if ``(scope, key)`` already has one, expired or not."""

    @abstractmethod
    def remove_expired(self, scope: str, key: str, *, at: str) -> bool:
        """Delete this key's record if it expired at or before ``at``. True if one was deleted."""

    @abstractmethod
    def purge_expired(self, *, at: str, limit: int = 100) -> int:
        """Delete up to ``limit`` records that expired at or before ``at``; returns how many."""


class ExecutionRepository(ABC):
    """Durable action executions, their attempts, and their reconciliations (domain/executions.py).

    Implemented by the product store (in memory and PostgreSQL) and by the
    per-run checkpoint file. Every implementation applies ``check_replace``
    and writes a change, its attempt record, and its reconciliation atomically.
    """

    @abstractmethod
    def get(self, project_id: str, run_id: str, execution_id: str) -> ActionExecution | None: ...

    @abstractmethod
    def list_for_run(self, project_id: str, run_id: str) -> list[ActionExecution]:
        """A run's executions, oldest first by ``(created_at, id)``."""

    @abstractmethod
    def attempts(self, project_id: str, run_id: str, execution_id: str) -> list[ExecutionAttempt]:
        """The execution's attempts, by attempt number."""

    @abstractmethod
    def reconciliations(self, project_id: str, run_id: str, execution_id: str) -> list[ActionReconciliation]:
        """The execution's reconciliations, by attempt number."""

    @abstractmethod
    def create(self, execution: ActionExecution) -> tuple[ActionExecution, bool]:
        """Store a new execution (version 1), or return the stored one for the same authorization.

        Returns ``(record, created_now)``. A stored execution with the same ID
        but a different authorization, or a second execution for the same
        approval, is a ``ConflictError``.
        """

    @abstractmethod
    def replace(
        self,
        expected: ActionExecution,
        new: ActionExecution,
        *,
        attempt: ExecutionAttempt | None = None,
        reconciliation: ActionReconciliation | None = None,
    ) -> bool:
        """Compare-and-set: replace the stored execution with ``new`` only if its version is ``expected.version``.

        In the same atomic write, ``attempt`` is inserted (a new, open attempt)
        or closed (the open attempt, now with its result), and
        ``reconciliation`` is stored. Returns False, changing nothing, if
        another writer changed the execution first.
        """


def same_authorization(a: ActionExecution, b: ActionExecution) -> bool:
    """Do two execution records describe the use of the same authorization (what ``create`` compares)?"""
    return (a.id, a.project_id, a.run_id, a.request_id, a.action_fingerprint, a.node, a.visit) == (
        b.id, b.project_id, b.run_id, b.request_id, b.action_fingerprint, b.node, b.visit,
    )  # fmt: skip


def attempt_identity(a: ExecutionAttempt) -> tuple:
    """The fields of an attempt that never change once it is recorded."""
    return (a.execution_id, a.attempt, a.authorization_seq, a.owner, a.idempotency_key, a.dispatched_at)


class ReconciliationRepository(ABC):
    """Reconciliation decisions (domain/reconciliation.py): recorded intent, applied later by a worker."""

    @abstractmethod
    def add(self, decision: ReconciliationDecision) -> None:
        """Store a decision. A second one for the same attempt is a ``ConflictError``."""

    @abstractmethod
    def get(self, project_id: str, execution_id: str, attempt: int) -> ReconciliationDecision | None: ...

    @abstractmethod
    def list_for_execution(self, project_id: str, execution_id: str) -> list[ReconciliationDecision]:
        """The execution's decisions, by attempt."""


class ModelUsageRepository(ABC):
    """Each run's model usage summary (domain/model_usage.py): one record per run, replaced as it grows."""

    @abstractmethod
    def put(self, usage: RunModelUsage) -> None:
        """Store the run's current summary. ``RecordNotFoundError`` for an unknown run."""

    @abstractmethod
    def get(self, project_id: str, run_id: str) -> RunModelUsage | None: ...


class RunQueue(ABC):
    """Wake-ups: a run wants a worker, written in the caller's transaction (the transactional outbox)."""

    @abstractmethod
    def wake(self, project_id: str, run_id: str) -> None:
        """Make the run claimable no later than now; an earlier availability is kept. Creates its claim row if needed."""


class ClaimRepository(ABC):
    """Run claims (domain/claims.py): which worker owns a run, until when, in which generation.

    Not part of ``ProductStore``: claims coordinate worker processes, and the
    API never reads them or changes ownership. Its one write to the same rows
    is ``RunQueue.wake``, in a decision's transaction. Every implementation decides lease
    validity by its own coordinating clock (PostgreSQL's ``now()``; the
    in-memory store's injected clock), never by a worker's clock, and follows
    the same rules:

    * ``claim_next`` hands the oldest available run that has no owner with a
      valid lease, and has not finished, to one worker, adding 1 to its
      fencing token. Of several concurrent claimers, exactly one gets it.
    * ``renew`` extends a lease only for the current fence, and only while the
      lease is still valid: an expired lease is lost even if nobody has taken
      the run over yet.
    * ``release`` frees the run only for the current fence. A stale release
      changes nothing.
    * ``verify`` raises ``OwnershipLost`` unless the fence is current and its
      lease valid.
    """

    @abstractmethod
    def now(self) -> str:
        """The coordinating clock's current time (PostgreSQL: ``now()``)."""

    @abstractmethod
    def enqueue(self, project_id: str, run_id: str) -> RunClaim:
        """Make the run want a worker from now on: create its claim (generation 0) or mark it available.

        An existing owner, token, and an availability already set are kept.
        """

    @abstractmethod
    def claim_next(self, worker_id: str, policy: LeasePolicy) -> Claim | None:
        """Claim the oldest available run for ``worker_id``, or return None if there is none."""

    @abstractmethod
    def claim_run(self, project_id: str, run_id: str, worker_id: str, policy: LeasePolicy) -> Claim | None:
        """Claim one specific run for an operator command (``resume``, ``db reconcile``, the demo).

        Whether or not the run wants a worker, and even if it has finished; its
        claim row is created if needed, without making the run available. None
        if an owner holds a valid lease (or another claimer is locking it).
        """

    @abstractmethod
    def renew(self, fence: Fence, policy: LeasePolicy) -> str | None:
        """Extend the lease to now + ``policy.lease``; the new expiry, or None if the fence is not current."""

    @abstractmethod
    def release(
        self, fence: Fence, *, requeue: bool | None, delay: timedelta = timedelta(0), at: str | None = None
    ) -> bool:
        """Give up the run, returning False (changing nothing) if the fence is not current.

        ``requeue``: True, a worker may take the run again after ``delay``, or
        at ``at`` (e.g. when the approval it waits for expires); False, no
        worker should (it waits for a person, is blocked, or has finished);
        None, leave that as it was.
        """

    @abstractmethod
    def verify(self, fence: Fence) -> None:
        """Raise ``OwnershipLost`` unless ``fence`` is the run's current owner with a valid lease."""

    @abstractmethod
    def get(self, project_id: str, run_id: str) -> RunClaim | None: ...

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources such as a database connection."""


class UnitOfWork(ABC):
    """Groups repository calls into one atomic transaction, and owns the store's resources."""

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """Repository calls inside the block take effect together, or (on an exception) not at all.

        Blocks nest like savepoints: an inner block that fails is undone on
        its own, and its work otherwise commits with the outermost block.
        """

    def snapshot(self) -> AbstractContextManager[None]:
        """Reads inside the block all see one consistent state. Nothing may be written in it.

        The default is a transaction. Stores whose transactions can see
        changes committed meanwhile (PostgreSQL's READ COMMITTED) override it.
        """
        return self.transaction()

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources such as a database connection."""


@dataclass(frozen=True)
class ProductStore:
    """The repositories business logic needs, bundled for injection."""

    projects: ProjectRepository
    runs: RunRepository
    milestones: MilestoneRepository
    approvals: ApprovalRepository
    idempotency: IdempotencyRepository
    executions: ExecutionRepository
    principals: PrincipalRepository
    reconciliations: ReconciliationRepository
    queue: RunQueue
    notifications: NotificationRepository
    model_usage: ModelUsageRepository
    unit_of_work: UnitOfWork

    def transaction(self) -> AbstractContextManager[None]:
        return self.unit_of_work.transaction()

    def snapshot(self) -> AbstractContextManager[None]:
        return self.unit_of_work.snapshot()

    def close(self) -> None:
        self.unit_of_work.close()

    def __enter__(self) -> ProductStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ------------------------------------------------------------------ in memory


class InMemoryDatabase(UnitOfWork):
    """Shared state of the in-memory repositories, guarded by one re-entrant lock.

    Records are immutable, so a transaction's snapshot is a shallow copy of
    each dictionary; a failed block restores its snapshot.
    """

    _TABLES = (
        "projects", "runs", "milestones", "requests", "decisions", "idempotency",
        "executions", "attempts", "reconciliations", "principals", "grants", "claims", "reconciliation_decisions",
        "destinations", "notifications", "notification_attempts", "model_usage",
    )  # fmt: skip

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.projects: dict[str, Project] = {}
        self.runs: dict[tuple[str, str], Run] = {}
        self.milestones: dict[str, Milestone] = {}
        self.requests: dict[tuple[str, str], ApprovalRequest] = {}
        self.decisions: dict[tuple[str, str], ApprovalDecision] = {}
        self.idempotency: dict[tuple[str, str], IdempotencyRecord] = {}
        self.executions: dict[tuple[str, str, str], ActionExecution] = {}
        self.attempts: dict[tuple[str, int], ExecutionAttempt] = {}
        self.reconciliations: dict[tuple[str, int], ActionReconciliation] = {}
        self.principals: dict[str, Principal] = {}
        self.grants: dict[tuple[str, Permission], str] = {}  # -> granted_at
        self.claims: dict[tuple[str, str], RunClaim] = {}
        self.reconciliation_decisions: dict[tuple[str, int], ReconciliationDecision] = {}
        self.destinations: dict[str, Destination] = {}
        self.notifications: dict[str, Notification] = {}
        self.notification_attempts: dict[tuple[str, int], NotificationAttempt] = {}
        self.model_usage: dict[tuple[str, str], RunModelUsage] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            snapshot = {name: dict(getattr(self, name)) for name in self._TABLES}
            try:
                yield
            except BaseException:
                for name, saved in snapshot.items():
                    setattr(self, name, saved)
                raise

    def require_run(self, project_id: str, run_id: str) -> None:
        if (project_id, run_id) not in self.runs:
            raise RecordNotFoundError(f"run {project_id}/{run_id} does not exist")


class InMemoryProjectRepository(ProjectRepository):
    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def add(self, project: Project) -> None:
        with self.db.lock:
            if project.id in self.db.projects:
                raise ConflictError(f"project {project.id} already exists")
            self.db.projects[project.id] = project

    def get(self, project_id: str) -> Project | None:
        with self.db.lock:
            return self.db.projects.get(project_id)

    def list(self) -> list[Project]:
        with self.db.lock:
            return sorted(self.db.projects.values(), key=lambda p: (parse_timestamp(p.created_at), p.id))

    def page(self, *, limit: int, after: ProjectPosition | None = None) -> list[Project]:
        check_limit(limit)
        position = project_position(after)
        newest_first = reversed(self.list())
        return [p for p in newest_first if position is None or (parse_timestamp(p.created_at), p.id) < position][:limit]


class InMemoryRunRepository(RunRepository):
    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def add(self, run: Run) -> None:
        with self.db.lock:
            key = (run.project_id, run.run_id)
            if key in self.db.runs:
                raise ConflictError(f"run {run.ref} already exists")
            if run.project_id not in self.db.projects:
                raise RecordNotFoundError(f"project {run.project_id} does not exist")
            if any(r.project_id == run.project_id and r.number == run.number for r in self.db.runs.values()):
                raise ConflictError(f"project {run.project_id} already has a run number {run.number}")
            self.db.runs[key] = run

    def get(self, project_id: str, run_id: str) -> Run | None:
        with self.db.lock:
            return self.db.runs.get((project_id, run_id))

    def update(self, run: Run) -> None:
        with self.db.lock:
            key = (run.project_id, run.run_id)
            old = self.db.runs.get(key)
            if old is None:
                raise RecordNotFoundError(f"run {run.ref} does not exist")
            Run.check_successor(old, run)
            self.db.runs[key] = run

    def list_for_project(self, project_id: str) -> list[Run]:
        with self.db.lock:
            return sorted((r for r in self.db.runs.values() if r.project_id == project_id), key=lambda r: r.number)

    def page_for_project(self, project_id: str, *, limit: int, before_number: int | None = None) -> list[Run]:
        check_limit(limit)
        if before_number is not None:
            check_int(before_number, "before_number", minimum=1)
        newest_first = reversed(self.list_for_project(project_id))
        return [r for r in newest_first if before_number is None or r.number < before_number][:limit]


class InMemoryMilestoneRepository(MilestoneRepository):
    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def add(self, milestone: Milestone) -> None:
        with self.db.lock:
            if milestone.id in self.db.milestones:
                raise ConflictError(f"milestone {milestone.id} already exists")
            self.db.require_run(milestone.project_id, milestone.run_id)
            for m in self.db.milestones.values():
                if (m.project_id, m.run_id) == (milestone.project_id, milestone.run_id):
                    if m.sequence == milestone.sequence:
                        raise ConflictError(f"run {m.run_id} already has a milestone with sequence {m.sequence}")
                    if m.key == milestone.key:
                        raise ConflictError(f"run {m.run_id} already has a milestone with key {m.key!r}")
            self._check_resolves(milestone)
            self.db.milestones[milestone.id] = milestone

    def get(self, milestone_id: str) -> Milestone | None:
        with self.db.lock:
            return self.db.milestones.get(milestone_id)

    def update(self, milestone: Milestone) -> None:
        with self.db.lock:
            old = self.db.milestones.get(milestone.id)
            if old is None:
                raise RecordNotFoundError(f"milestone {milestone.id} does not exist")
            Milestone.check_successor(old, milestone)
            if milestone.resolves_id != old.resolves_id:
                self._check_resolves(milestone)
            self.db.milestones[milestone.id] = milestone

    def query(self, query: MilestoneQuery) -> list[Milestone]:
        with self.db.lock:
            return sorted((m for m in self.db.milestones.values() if query.matches(m)), key=milestone_order)

    def page(self, query: MilestoneQuery, *, limit: int, after: MilestonePosition | None = None) -> list[Milestone]:
        check_limit(limit)
        position = milestone_position(after)
        found = self.query(query)
        if position is not None:
            found = [m for m in found if (parse_timestamp(m.occurred_at), m.run_id, m.sequence) > position]
        return found[:limit]

    def count_since(self, project_ids: Iterable[str], since: str) -> dict[str, int]:
        wanted, start = set(project_ids), parse_timestamp(check_timestamp(since, "since"))
        counts: dict[str, int] = {}
        with self.db.lock:
            for m in self.db.milestones.values():
                if m.project_id in wanted and parse_timestamp(m.occurred_at) >= start:
                    counts[m.project_id] = counts.get(m.project_id, 0) + 1
        return counts

    def next_sequence(self, project_id: str, run_id: str) -> int:
        with self.db.lock:
            existing = [m.sequence for m in self.db.milestones.values() if m.project_id == project_id and m.run_id == run_id]
            return max(existing, default=0) + 1

    def _check_resolves(self, milestone: Milestone) -> None:
        if milestone.resolves_id is None:
            return
        target = self.db.milestones.get(milestone.resolves_id)
        if target is None or (target.project_id, target.run_id) != (milestone.project_id, milestone.run_id):
            raise RecordNotFoundError(
                f"milestone {milestone.resolves_id} (resolved by {milestone.id}) does not exist in run {milestone.run_id}"
            )


class InMemoryApprovalRepository(ApprovalRepository):
    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def add_request(self, request: ApprovalRequest) -> None:
        with self.db.lock:
            key = (request.project_id, request.id)
            if key in self.db.requests:
                raise ConflictError(f"approval request {request.id} already exists")
            self.db.require_run(request.project_id, request.run_id)
            self.db.requests[key] = request

    def get_request(self, project_id: str, request_id: str) -> ApprovalRequest | None:
        with self.db.lock:
            return self.db.requests.get((project_id, request_id))

    def update_request(self, request: ApprovalRequest) -> None:
        with self.db.lock:
            key = (request.project_id, request.id)
            old = self.db.requests.get(key)
            if old is None:
                raise RecordNotFoundError(f"approval request {request.id} does not exist")
            ApprovalRequest.check_successor(old, request)
            self.db.requests[key] = request

    def list_requests(
        self, project_id: str, *, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        with self.db.lock:
            found: Iterable[ApprovalRequest] = (
                r
                for r in self.db.requests.values()
                if r.project_id == project_id
                and (run_id is None or r.run_id == run_id)
                and (status is None or r.status is status)
            )
            return sorted(found, key=lambda r: (parse_timestamp(r.requested_at), r.id))

    def save_decision(self, decision: ApprovalDecision, request: ApprovalRequest) -> None:
        with self.db.lock:  # every check runs before anything is written
            key = (decision.project_id, decision.request_id)
            if (request.project_id, request.id) != key:
                raise DomainValidationError("decision and request do not belong together")
            old = self.db.requests.get(key)
            if old is None:
                raise RecordNotFoundError(f"approval request {decision.request_id} does not exist")
            if decision.action_fingerprint != old.action_fingerprint:
                raise ApprovalBindingError(f"approval {decision.request_id}: the decision is for a different action")
            if decision.actor.kind is ActorKind.USER and decision.actor.id not in self.db.principals:
                raise RecordNotFoundError(f"principal {decision.actor.id} is not enrolled")
            if key in self.db.decisions:
                raise ConflictError(f"approval request {decision.request_id} already has a decision")
            ApprovalRequest.check_successor(old, request)
            self.db.decisions[key] = decision
            self.db.requests[key] = request

    def get_decision(self, project_id: str, request_id: str) -> ApprovalDecision | None:
        with self.db.lock:
            return self.db.decisions.get((project_id, request_id))

    def page_pending(
        self, *, limit: int, project_id: str | None = None, after: ApprovalPosition | None = None, at: str | None = None
    ) -> list[ApprovalRequest]:
        check_limit(limit)
        position = approval_position(after)

        def key(r: ApprovalRequest) -> tuple:
            return (parse_timestamp(r.requested_at), r.project_id, r.id)

        with self.db.lock:
            pending = [
                r
                for r in self.db.requests.values()
                if r.status is ApprovalStatus.PENDING and (project_id is None or r.project_id == project_id)
                and (at is None or not r.is_expired(at))
            ]
        return sorted((r for r in pending if position is None or key(r) > position), key=key)[:limit]

    def page_attention(
        self, *, limit: int, at: str, project_id: str | None = None, after: AttentionPosition | None = None
    ) -> list[AttentionItem]:
        check_limit(limit)
        position = attention_position(after)
        with self.db.lock:
            items = [
                AttentionItem(
                    kind=AttentionKind.DECISION_REQUIRED, project_id=r.project_id, run_id=r.run_id, approval_id=r.id,
                    since=r.requested_at, tool=r.action.tool, category=r.action.category, expires_at=r.expires_at,
                )  # fmt: skip
                for r in self.db.requests.values()
                if r.status is ApprovalStatus.PENDING and not r.is_expired(at)
            ]
            items += [
                AttentionItem(
                    kind=AttentionKind.RECONCILIATION_REQUIRED, project_id=e.project_id, run_id=e.run_id,
                    approval_id=e.request_id, since=e.updated_at, tool=e.action.tool, category=e.action.category,
                    execution_id=e.id, attempt=e.attempts,
                )  # fmt: skip
                for e in self.db.executions.values()
                if e.state is ExecutionState.UNCERTAIN
                and e.request_id is not None
                and (e.id, e.attempts) not in self.db.reconciliation_decisions
            ]
        items = sorted((i for i in items if project_id is None or i.project_id == project_id), key=attention_order)
        if position is not None:
            items = [i for i in items if attention_order(i) > position]
        return items[:limit]

    def pending_counts(self, project_ids: Iterable[str], *, at: str | None = None) -> dict[str, int]:
        wanted = set(project_ids)
        counts: dict[str, int] = {}
        with self.db.lock:
            for r in self.db.requests.values():
                if r.status is ApprovalStatus.PENDING and r.project_id in wanted and (at is None or not r.is_expired(at)):
                    counts[r.project_id] = counts.get(r.project_id, 0) + 1
        return counts


class InMemoryIdempotencyRepository(IdempotencyRepository):
    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        with self.db.lock:
            return self.db.idempotency.get((scope, key))

    def add(self, record: IdempotencyRecord) -> None:
        with self.db.lock:
            if (record.scope, record.key) in self.db.idempotency:
                raise ConflictError(f"an idempotency record already exists for this key ({record.scope})")
            self.db.idempotency[(record.scope, record.key)] = record

    def remove_expired(self, scope: str, key: str, *, at: str) -> bool:
        at = check_timestamp(at, "at")
        with self.db.lock:
            record = self.db.idempotency.get((scope, key))
            if record is None or not record.is_expired(at):
                return False
            del self.db.idempotency[(scope, key)]
            return True

    def purge_expired(self, *, at: str, limit: int = 100) -> int:
        check_limit(limit)
        at = check_timestamp(at, "at")
        with self.db.lock:
            expired = [k for k, r in self.db.idempotency.items() if r.is_expired(at)][:limit]
            for k in expired:
                del self.db.idempotency[k]
        return len(expired)


class InMemoryExecutionRepository(ExecutionRepository):
    """Applies the same references and keys as PostgreSQL's constraints (migration 0003)."""

    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def get(self, project_id: str, run_id: str, execution_id: str) -> ActionExecution | None:
        with self.db.lock:
            return self.db.executions.get((project_id, run_id, execution_id))

    def list_for_run(self, project_id: str, run_id: str) -> list[ActionExecution]:
        with self.db.lock:
            found = [e for (p, r, _), e in self.db.executions.items() if (p, r) == (project_id, run_id)]
        return sorted(found, key=lambda e: (parse_timestamp(e.created_at), e.id))

    def attempts(self, project_id: str, run_id: str, execution_id: str) -> list[ExecutionAttempt]:
        with self.db.lock:
            if (project_id, run_id, execution_id) not in self.db.executions:
                return []
            return sorted((a for (e, _), a in self.db.attempts.items() if e == execution_id), key=lambda a: a.attempt)

    def reconciliations(self, project_id: str, run_id: str, execution_id: str) -> list[ActionReconciliation]:
        with self.db.lock:
            if (project_id, run_id, execution_id) not in self.db.executions:
                return []
            found = (r for (e, _), r in self.db.reconciliations.items() if e == execution_id)
            return sorted(found, key=lambda r: r.attempt)

    def create(self, execution: ActionExecution) -> tuple[ActionExecution, bool]:
        with self.db.lock:
            key = (execution.project_id, execution.run_id, execution.id)
            existing = self.db.executions.get(key)
            if existing is not None:
                if not same_authorization(existing, execution):
                    raise ConflictError(f"execution {execution.id} already exists for a different authorization")
                return existing, False
            if execution.version != 1:
                raise DomainValidationError("a new execution starts at version 1")
            if any(e.id == execution.id for e in self.db.executions.values()):
                raise ConflictError(f"execution {execution.id} already exists in another run")
            self.db.require_run(execution.project_id, execution.run_id)
            if execution.request_id is not None:
                self._require_approved(execution)
                if any(
                    (e.project_id, e.request_id) == (execution.project_id, execution.request_id)
                    for e in self.db.executions.values()
                ):
                    raise ConflictError(f"approval {execution.request_id} already has an execution")
            self.db.executions[key] = execution
            return execution, True

    def replace(
        self,
        expected: ActionExecution,
        new: ActionExecution,
        *,
        attempt: ExecutionAttempt | None = None,
        reconciliation: ActionReconciliation | None = None,
    ) -> bool:
        with self.db.lock:  # every check runs before anything is written
            key = (expected.project_id, expected.run_id, expected.id)
            current = self.db.executions.get(key)
            if current is None:
                raise RecordNotFoundError(f"execution {expected.id} does not exist")
            if current.version != expected.version:
                return False
            check_replace(current, new, attempt=attempt, reconciliation=reconciliation)
            if attempt is not None:
                stored = self.db.attempts.get((attempt.execution_id, attempt.attempt))
                if attempt.ended_at is None:
                    if stored is not None:
                        raise ConflictError(f"attempt {attempt.attempt} of {attempt.execution_id} already exists")
                elif stored is None or stored.ended_at is not None or attempt_identity(stored) != attempt_identity(attempt):
                    raise ConflictError(f"attempt {attempt.attempt} of {attempt.execution_id} is not the open attempt")
            if reconciliation is not None:
                rkey = (reconciliation.execution_id, reconciliation.attempt)
                if rkey in self.db.reconciliations:
                    raise ConflictError(
                        f"attempt {reconciliation.attempt} of {reconciliation.execution_id} is already reconciled"
                    )
                if rkey not in self.db.attempts:
                    raise RecordNotFoundError(
                        f"attempt {reconciliation.attempt} of {reconciliation.execution_id} does not exist"
                    )
                self.db.reconciliations[rkey] = reconciliation
            if attempt is not None:
                self.db.attempts[(attempt.execution_id, attempt.attempt)] = attempt
            self.db.executions[key] = new
            return True

    def _require_approved(self, execution: ActionExecution) -> None:
        request = self.db.requests.get((execution.project_id, execution.request_id))  # type: ignore[arg-type]
        if request is None or request.run_id != execution.run_id:
            raise RecordNotFoundError(f"approval request {execution.request_id} does not exist in run {execution.run_id}")
        decision = self.db.decisions.get((execution.project_id, execution.request_id))  # type: ignore[arg-type]
        if (
            decision is None
            or decision.outcome is not ApprovalOutcome.APPROVED
            or decision.action_fingerprint != execution.action_fingerprint
        ):
            raise ApprovalBindingError(
                f"execution {execution.id} is not bound to an approved decision for this exact action"
            )


class InMemoryPrincipalRepository(PrincipalRepository):
    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def add(self, principal: Principal) -> None:
        with self.db.lock:
            if principal.id in self.db.principals:
                raise ConflictError(f"principal {principal.id} already exists")
            if self._find(principal.issuer, principal.subject) is not None:
                raise ConflictError("this issuer and subject are already enrolled")
            self.db.principals[principal.id] = principal

    def get(self, principal_id: str) -> Principal | None:
        with self.db.lock:
            return self.db.principals.get(principal_id)

    def find(self, issuer: str, subject: str) -> Principal | None:
        with self.db.lock:
            return self._find(issuer, subject)

    def _find(self, issuer: str, subject: str) -> Principal | None:
        return next((p for p in self.db.principals.values() if p.external_identity == (issuer, subject)), None)

    def list(self) -> list[Principal]:
        with self.db.lock:
            return sorted(self.db.principals.values(), key=lambda p: (parse_timestamp(p.created_at), p.id))

    def update(self, principal: Principal) -> None:
        with self.db.lock:
            old = self.db.principals.get(principal.id)
            if old is None:
                raise RecordNotFoundError(f"principal {principal.id} does not exist")
            Principal.check_successor(old, principal)
            self.db.principals[principal.id] = principal

    def permissions(self, principal_id: str) -> frozenset[Permission]:
        with self.db.lock:
            return frozenset(p for (holder, p) in self.db.grants if holder == principal_id)

    def grant(self, principal_id: str, permissions: Iterable[Permission], *, at: str) -> None:
        wanted = check_permissions(permissions)
        check_timestamp(at, "granted_at")
        with self.db.lock:
            self._require(principal_id)
            for permission in wanted:
                self.db.grants.setdefault((principal_id, permission), at)

    def revoke(self, principal_id: str, permissions: Iterable[Permission]) -> None:
        unwanted = check_permissions(permissions)
        with self.db.lock:
            self._require(principal_id)
            for permission in unwanted:
                self.db.grants.pop((principal_id, permission), None)

    def _require(self, principal_id: str) -> None:
        check_pattern(principal_id, PRINCIPAL_ID_RE, "principal_id")
        if principal_id not in self.db.principals:
            raise RecordNotFoundError(f"principal {principal_id} does not exist")


class InMemoryReconciliationRepository(ReconciliationRepository):
    """Applies the same references and keys as PostgreSQL's constraints (migration 0006)."""

    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def add(self, decision: ReconciliationDecision) -> None:
        with self.db.lock:
            execution = self.db.executions.get((decision.project_id, decision.run_id, decision.execution_id))
            if execution is None:
                raise RecordNotFoundError(f"execution {decision.execution_id} does not exist")
            if (decision.execution_id, decision.attempt) not in self.db.attempts:
                raise RecordNotFoundError(f"attempt {decision.attempt} of {decision.execution_id} does not exist")
            if execution.action_fingerprint != decision.action_fingerprint:
                raise ApprovalBindingError(f"reconciliation of {decision.execution_id}: for a different action")
            if decision.actor.kind is ActorKind.USER and decision.actor.id not in self.db.principals:
                raise RecordNotFoundError("a decision by a user must name an enrolled principal")
            if decision.key in self.db.reconciliation_decisions:
                raise ConflictError(f"attempt {decision.attempt} of {decision.execution_id} already has a decision")
            self.db.reconciliation_decisions[decision.key] = decision

    def get(self, project_id: str, execution_id: str, attempt: int) -> ReconciliationDecision | None:
        with self.db.lock:
            found = self.db.reconciliation_decisions.get((execution_id, attempt))
            return found if found is not None and found.project_id == project_id else None

    def list_for_execution(self, project_id: str, execution_id: str) -> list[ReconciliationDecision]:
        with self.db.lock:
            found = [d for (e, _), d in self.db.reconciliation_decisions.items() if e == execution_id and d.project_id == project_id]
        return sorted(found, key=lambda d: d.attempt)


class InMemoryModelUsageRepository(ModelUsageRepository):
    def __init__(self, db: InMemoryDatabase | None = None) -> None:
        self.db = db or InMemoryDatabase()

    def put(self, usage: RunModelUsage) -> None:
        with self.db.lock:
            self.db.require_run(usage.project_id, usage.run_id)
            self.db.model_usage[(usage.project_id, usage.run_id)] = usage

    def get(self, project_id: str, run_id: str) -> RunModelUsage | None:
        with self.db.lock:
            return self.db.model_usage.get((project_id, run_id))


class InMemoryRunQueue(RunQueue):
    def __init__(self, db: InMemoryDatabase | None = None, clock: Clock = utc_now) -> None:
        self.db = db or InMemoryDatabase()
        self._clock = clock

    def wake(self, project_id: str, run_id: str) -> None:
        with self.db.lock:
            self.db.require_run(project_id, run_id)
            now = timestamp(self._clock)
            current = self.db.claims.get((project_id, run_id))
            if current is None:
                self.db.claims[(project_id, run_id)] = RunClaim(
                    project_id=project_id, run_id=run_id, fencing_token=0, created_at=now, updated_at=now,
                    available_at=now, wake_seq=1,
                )  # fmt: skip
                return
            keep = current.available_at is not None and parse_timestamp(current.available_at) <= parse_timestamp(now)
            self.db.claims[(project_id, run_id)] = replace(
                current, available_at=current.available_at if keep else now, wake_seq=current.wake_seq + 1, updated_at=now
            )


class InMemoryClaimRepository(ClaimRepository):
    """Claims for unit tests and single-process use, on an injectable clock.

    It follows the same rules as PostgreSQL, but proves nothing about
    coordination between processes; the PostgreSQL tests do that. The
    in-memory store has no fenced writes either (docs/workers.md).
    """

    def __init__(self, db: InMemoryDatabase | None = None, clock: Clock = utc_now) -> None:
        self.db = db or InMemoryDatabase()
        self._clock = clock

    def now(self) -> str:
        return timestamp(self._clock)

    @staticmethod
    def _after(now: str, delta: timedelta) -> str:
        return format_timestamp(parse_timestamp(now) + delta)

    def enqueue(self, project_id: str, run_id: str) -> RunClaim:
        with self.db.lock:
            self.db.require_run(project_id, run_id)
            now = self.now()
            current = self.db.claims.get((project_id, run_id))
            if current is None:
                claim = RunClaim(
                    project_id=project_id, run_id=run_id, fencing_token=0, created_at=now, updated_at=now,
                    available_at=now,
                )  # fmt: skip
            else:
                claim = replace(
                    current, available_at=current.available_at or now, wake_seq=current.wake_seq + 1, updated_at=now
                )
            self.db.claims[(project_id, run_id)] = claim
            return claim

    def _finished(self, claim: RunClaim) -> bool:
        run = self.db.runs.get((claim.project_id, claim.run_id))
        return run is not None and run.status.is_terminal

    def claim_next(self, worker_id: str, policy: LeasePolicy) -> Claim | None:
        check_pattern(worker_id, WORKER_ID_RE, "worker_id")
        with self.db.lock:
            now = self.now()
            candidates = sorted(
                (c for c in self.db.claims.values() if c.claimable(now) and not self._finished(c)),
                key=lambda c: (parse_timestamp(c.available_at), c.project_id, c.run_id),  # type: ignore[arg-type]
            )
            if not candidates:
                return None
            old = candidates[0]
            expires = self._after(now, policy.lease)
            new = replace(
                old, worker_id=worker_id, fencing_token=old.fencing_token + 1, claimed_at=now,
                lease_expires_at=expires, last_heartbeat_at=now, claimed_wake_seq=old.wake_seq, updated_at=now,
            )  # fmt: skip
            self.db.claims[(old.project_id, old.run_id)] = new
            return Claim(
                fence=Fence(old.project_id, old.run_id, worker_id, new.fencing_token),
                claimed_at=now,
                lease_expires_at=expires,
                previous_worker_id=old.worker_id,
                previous_fencing_token=old.fencing_token if old.worker_id else None,
                previous_lease_expires_at=old.lease_expires_at,
            )

    def claim_run(self, project_id: str, run_id: str, worker_id: str, policy: LeasePolicy) -> Claim | None:
        check_pattern(worker_id, WORKER_ID_RE, "worker_id")
        with self.db.lock:
            self.db.require_run(project_id, run_id)
            now = self.now()
            old = self.db.claims.get((project_id, run_id)) or RunClaim(
                project_id=project_id, run_id=run_id, fencing_token=0, created_at=now, updated_at=now
            )
            if old.worker_id is not None and not old.lease_expired(now):
                return None
            expires = self._after(now, policy.lease)
            new = replace(
                old, worker_id=worker_id, fencing_token=old.fencing_token + 1, claimed_at=now,
                lease_expires_at=expires, last_heartbeat_at=now, claimed_wake_seq=old.wake_seq, updated_at=now,
            )  # fmt: skip
            self.db.claims[(project_id, run_id)] = new
            return Claim(
                fence=Fence(project_id, run_id, worker_id, new.fencing_token),
                claimed_at=now,
                lease_expires_at=expires,
                previous_worker_id=old.worker_id,
                previous_fencing_token=old.fencing_token if old.worker_id else None,
                previous_lease_expires_at=old.lease_expires_at,
            )

    def renew(self, fence: Fence, policy: LeasePolicy) -> str | None:
        with self.db.lock:
            now = self.now()
            current = self.db.claims.get((fence.project_id, fence.run_id))
            if current is None or not current.admits(fence, now):
                return None
            expires = self._after(now, policy.lease)
            self.db.claims[(fence.project_id, fence.run_id)] = replace(
                current, lease_expires_at=expires, last_heartbeat_at=now, updated_at=now
            )
            return expires

    def release(
        self, fence: Fence, *, requeue: bool | None, delay: timedelta = timedelta(0), at: str | None = None
    ) -> bool:
        if delay < timedelta(0):
            raise DomainValidationError("release: the delay must not be negative")
        with self.db.lock:
            current = self.db.claims.get((fence.project_id, fence.run_id))
            if current is None or (current.worker_id, current.fencing_token) != (fence.worker_id, fence.fencing_token):
                return False
            now = self.now()
            wake = check_timestamp(at, "at") if at is not None else self._after(now, delay)
            available = current.available_at if requeue is None else (wake if requeue else None)
            if current.woken_while_claimed:
                available = now  # a decision arrived while this owner held the run: never released past it
            self.db.claims[(fence.project_id, fence.run_id)] = replace(
                current, worker_id=None, lease_expires_at=None, available_at=available, updated_at=now
            )
            return True

    def verify(self, fence: Fence) -> None:
        with self.db.lock:
            current = self.db.claims.get((fence.project_id, fence.run_id))
            now = self.now()
            if current is None:
                raise OwnershipLost(f"run {fence.project_id}/{fence.run_id} has no claim", reason="missing")
            if not current.admits(fence, now):
                raise current.refusal(fence, now)

    def get(self, project_id: str, run_id: str) -> RunClaim | None:
        with self.db.lock:
            return self.db.claims.get((project_id, run_id))


def in_memory_store(clock: Clock = utc_now) -> ProductStore:
    """A product store in memory. ``clock`` is the run queue's (what "now" means for a wake-up)."""
    db = InMemoryDatabase()
    return ProductStore(
        projects=InMemoryProjectRepository(db),
        runs=InMemoryRunRepository(db),
        milestones=InMemoryMilestoneRepository(db),
        approvals=InMemoryApprovalRepository(db),
        idempotency=InMemoryIdempotencyRepository(db),
        executions=InMemoryExecutionRepository(db),
        principals=InMemoryPrincipalRepository(db),
        reconciliations=InMemoryReconciliationRepository(db),
        queue=InMemoryRunQueue(db, clock),
        notifications=InMemoryNotificationRepository(db),
        model_usage=InMemoryModelUsageRepository(db),
        unit_of_work=db,
    )


__all__ = [
    "ApprovalPosition",
    "ApprovalRepository",
    "ClaimRepository",
    "ExecutionRepository",
    "IdempotencyRepository",
    "InMemoryApprovalRepository",
    "InMemoryClaimRepository",
    "InMemoryDatabase",
    "InMemoryExecutionRepository",
    "InMemoryIdempotencyRepository",
    "InMemoryMilestoneRepository",
    "InMemoryPrincipalRepository",
    "InMemoryProjectRepository",
    "InMemoryReconciliationRepository",
    "InMemoryRunQueue",
    "InMemoryRunRepository",
    "MilestonePosition",
    "MilestoneQuery",
    "MilestoneRepository",
    "PrincipalRepository",
    "ProductStore",
    "ProjectPosition",
    "ProjectRepository",
    "ReconciliationRepository",
    "RunQueue",
    "RunRepository",
    "UnitOfWork",
    "approval_position",
    "attention_position",
    "attempt_identity",
    "check_limit",
    "in_memory_store",
    "milestone_order",
    "milestone_position",
    "project_position",
    "same_authorization",
]

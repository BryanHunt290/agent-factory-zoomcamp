"""The checkpoint ledger: action executions of a run without a database.

One file per run, ``<runs_dir>/<run_id>/executions.json``, next to the
engine checkpoint. It implements the same ``ExecutionRepository`` as the
product store, so the execution protocol (domain/executions.py) is the same
with and without PostgreSQL.

* **Atomic and durable.** Every change rewrites the file through
  ``write_json_atomic`` (temporary file, fsync, rename, directory fsync), so
  an interruption leaves the previous version or the new one.
* **Compare-and-set.** A change applies only if the stored version is the one
  the writer read, under an exclusive OS lock on ``executions.lock``, so two
  processes or threads can never both win.
* **Integrity.** The file carries a schema version and a SHA-256 digest of its
  content. Every record is re-validated on read, and the cross-references are
  checked: attempts belong to executions, one attempt at most is open,
  reconciliations name a recorded attempt and the execution's fingerprint.
  Anything inconsistent raises ``LedgerIntegrityError``. The digest detects
  corruption and naive edits; it is not a signature, and someone with write
  access to the workspace could recompute it (the workspace is trusted).
* **Binding.** The same file records which ledger a run uses. A run whose
  executions live in the product database gets a marker file with no records,
  so it can never be resumed without that database (and the reverse), which
  could otherwise dispatch an action a second time.
* **Unavailable, never a verdict.** A filesystem error while writing (a full
  disk, a replace that stays denied) raises ``StorageUnavailableError``, as a
  database outage does: the run blocks, resumably, and nothing is dispatched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.errors import ConflictError, DomainValidationError, RecordNotFoundError, StorageUnavailableError
from ..domain.executions import (
    ActionExecution,
    ActionReconciliation,
    ExecutionAttempt,
    LedgerKind,
    check_replace,
)
from ..domain.ids import PROJECT_ID_RE
from ..domain.repositories import ExecutionRepository, attempt_identity, same_authorization
from ..domain.serialization import canonical, check_fields, check_pattern, open_envelope, parse_timestamp, seal
from ..util import sha256_hex, write_json_atomic
from .checkpoint import validate_run_id
from .locks import FileLock, LockUnavailable

LEDGER_FILE = "executions.json"
LEDGER_LOCK = "executions.lock"
RECORD_TYPE = "execution_ledger"
SCHEMA_VERSION = 1


class LedgerIntegrityError(DomainValidationError):
    """The execution ledger file is corrupt, edited, or contradicts itself. Nothing is guessed."""


class LedgerBindingError(ConflictError):
    """The run's actions are recorded in the other kind of ledger (database vs. workspace)."""


@dataclass
class LedgerFile:
    kind: LedgerKind
    project_id: str
    run_id: str
    executions: dict[str, ActionExecution] = field(default_factory=dict)
    attempts: dict[tuple[str, int], ExecutionAttempt] = field(default_factory=dict)
    reconciliations: dict[tuple[str, int], ActionReconciliation] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {
            "ledger": self.kind.value,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "executions": [e.to_dict() for e in sorted(self.executions.values(), key=lambda e: e.id)],
            "attempts": [a.to_dict() for _, a in sorted(self.attempts.items())],
            "reconciliations": [r.to_dict() for _, r in sorted(self.reconciliations.items())],
        }


def _digest(body: dict[str, Any]) -> str:
    return "sha256:" + sha256_hex(canonical(body))


def _binding_message(found: LedgerKind, run_id: str) -> str:
    if found is LedgerKind.PRODUCT_STORE:
        return f"run {run_id} records its approved actions in the product database; resume it with --database"
    return f"run {run_id} records its approved actions in its workspace, without a database; resume it without --database"


def ledger_path(run_dir: Path) -> Path:
    return Path(run_dir) / LEDGER_FILE


def read_ledger(run_dir: Path, run_id: str) -> LedgerFile | None:
    """The run's ledger file, fully validated, or None if it has none yet."""
    path = ledger_path(run_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerIntegrityError(f"{path.name} of run {run_id} is unreadable: {exc}") from None
    try:
        return _parse(data, run_id)
    except LedgerIntegrityError:
        raise
    except (DomainValidationError, KeyError, TypeError, ValueError) as exc:
        raise LedgerIntegrityError(f"{path.name} of run {run_id} failed validation: {exc}") from None


def _parse(data: Any, run_id: str) -> LedgerFile:
    body = open_envelope(data, record_type=RECORD_TYPE, version=SCHEMA_VERSION)
    check_fields(
        body,
        required=("ledger", "project_id", "run_id", "executions", "attempts", "reconciliations", "digest"),
        where=RECORD_TYPE,
    )
    digest = body.pop("digest")
    if digest != _digest(body):
        raise LedgerIntegrityError(f"the execution ledger of run {run_id} does not match its digest (corrupted or edited)")
    ledger = LedgerFile(
        kind=LedgerKind(body["ledger"]),
        project_id=check_pattern(body["project_id"], PROJECT_ID_RE, "project_id"),
        run_id=body["run_id"],
    )
    if ledger.run_id != run_id:
        raise LedgerIntegrityError(f"the execution ledger in run {run_id}'s directory belongs to run {ledger.run_id}")
    for item in body["executions"]:
        execution = ActionExecution.from_dict(item)
        if (execution.project_id, execution.run_id) != (ledger.project_id, ledger.run_id) or execution.id in ledger.executions:
            raise LedgerIntegrityError(f"execution {execution.id} is duplicated or belongs to another run")
        ledger.executions[execution.id] = execution
    for item in body["attempts"]:
        attempt = ExecutionAttempt.from_dict(item)
        key = (attempt.execution_id, attempt.attempt)
        if attempt.execution_id not in ledger.executions or key in ledger.attempts:
            raise LedgerIntegrityError(f"attempt {key} is duplicated or belongs to no execution")
        ledger.attempts[key] = attempt
    for item in body["reconciliations"]:
        reconciliation = ActionReconciliation.from_dict(item)
        key = (reconciliation.execution_id, reconciliation.attempt)
        execution = ledger.executions.get(reconciliation.execution_id)
        if key not in ledger.attempts or key in ledger.reconciliations or execution is None:
            raise LedgerIntegrityError(f"reconciliation {key} names no recorded attempt, or is duplicated")
        if reconciliation.action_fingerprint != execution.action_fingerprint:
            raise LedgerIntegrityError(f"reconciliation {key} is for a different action than its execution")
        ledger.reconciliations[key] = reconciliation
    if ledger.kind is LedgerKind.PRODUCT_STORE and (ledger.executions or ledger.attempts or ledger.reconciliations):
        raise LedgerIntegrityError(f"run {run_id}'s binding marker must not hold execution records")
    for execution in ledger.executions.values():
        numbers = sorted(n for (e, n) in ledger.attempts if e == execution.id)
        if numbers != list(range(1, execution.attempts + 1)):
            raise LedgerIntegrityError(f"execution {execution.id}: its attempt records do not match its attempt count")
        open_numbers = [n for (e, n), a in ledger.attempts.items() if e == execution.id and a.ended_at is None]
        expected_open = [execution.open_attempt] if execution.open_attempt is not None else []
        if open_numbers != expected_open:
            raise LedgerIntegrityError(f"execution {execution.id}: its open attempt does not match its state")
    return ledger


def write_ledger(run_dir: Path, ledger: LedgerFile) -> None:
    body = ledger.body()
    write_json_atomic(ledger_path(run_dir), seal(RECORD_TYPE, SCHEMA_VERSION, {**body, "digest": _digest(body)}))


def check_binding(run_dir: Path, run_id: str, kind: LedgerKind) -> None:
    """Refuse to use ``kind`` for a run whose actions are recorded in the other kind of ledger."""
    existing = read_ledger(run_dir, run_id)
    if existing is not None and existing.kind is not LedgerKind(kind):
        raise LedgerBindingError(_binding_message(existing.kind, run_id))


def ensure_binding(run_dir: Path, run_id: str, project_id: str, kind: LedgerKind, *, lock_timeout: float = 10.0) -> None:
    """Record, durably and before the first execution is written, which ledger the run uses."""
    kind = LedgerKind(kind)
    with _ledger_io(run_id), _file_lock(run_dir, lock_timeout):
        existing = read_ledger(run_dir, run_id)
        if existing is None:
            write_ledger(run_dir, LedgerFile(kind=kind, project_id=project_id, run_id=run_id))
            return
        if existing.kind is not kind:
            raise LedgerBindingError(_binding_message(existing.kind, run_id))
        if existing.project_id != project_id:
            raise LedgerIntegrityError(f"run {run_id}'s execution ledger belongs to project {existing.project_id}")


@contextmanager
def _ledger_io(run_id: str) -> Iterator[None]:
    """Translate a filesystem failure into unavailability, as the PostgreSQL store does for its driver."""
    try:
        yield
    except OSError as exc:  # the message names no path: it can reach a run's blocked reason
        reason = exc.strerror or type(exc).__name__
        raise StorageUnavailableError(f"the execution ledger of run {run_id} could not be written: {reason}") from None


@contextmanager
def _file_lock(run_dir: Path, timeout: float) -> Iterator[None]:
    lock = FileLock(Path(run_dir) / LEDGER_LOCK)
    try:
        lock.acquire(timeout=timeout)
    except LockUnavailable as exc:
        raise ConflictError(f"the execution ledger is busy: {exc}") from None
    try:
        yield
    finally:
        lock.release()


class CheckpointExecutionRepository(ExecutionRepository):
    """Action executions of the runs in one workspace, one ledger file per run."""

    def __init__(self, runs_dir: Path | str, *, lock_timeout: float = 10.0) -> None:
        self.runs_dir = Path(runs_dir)
        self.lock_timeout = lock_timeout

    def _run_dir(self, run_id: str) -> Path:
        return self.runs_dir / validate_run_id(run_id)

    def _load(self, project_id: str, run_id: str) -> LedgerFile | None:
        ledger = read_ledger(self._run_dir(run_id), run_id)
        if ledger is None:
            return None
        if ledger.kind is not LedgerKind.CHECKPOINT:
            raise LedgerBindingError(_binding_message(ledger.kind, run_id))
        if ledger.project_id != project_id:
            raise LedgerIntegrityError(f"run {run_id}'s execution ledger belongs to project {ledger.project_id}")
        return ledger

    # ------------------------------------------------------------- reads

    def get(self, project_id: str, run_id: str, execution_id: str) -> ActionExecution | None:
        ledger = self._load(project_id, run_id)
        return None if ledger is None else ledger.executions.get(execution_id)

    def list_for_run(self, project_id: str, run_id: str) -> list[ActionExecution]:
        ledger = self._load(project_id, run_id)
        if ledger is None:
            return []
        return sorted(ledger.executions.values(), key=lambda e: (parse_timestamp(e.created_at), e.id))

    def attempts(self, project_id: str, run_id: str, execution_id: str) -> list[ExecutionAttempt]:
        ledger = self._load(project_id, run_id)
        if ledger is None:
            return []
        return [a for (e, _), a in sorted(ledger.attempts.items()) if e == execution_id]

    def reconciliations(self, project_id: str, run_id: str, execution_id: str) -> list[ActionReconciliation]:
        ledger = self._load(project_id, run_id)
        if ledger is None:
            return []
        return [r for (e, _), r in sorted(ledger.reconciliations.items()) if e == execution_id]

    # ------------------------------------------------------------ writes

    def create(self, execution: ActionExecution) -> tuple[ActionExecution, bool]:
        if execution.version != 1:
            raise DomainValidationError("a new execution starts at version 1")
        run_dir = self._run_dir(execution.run_id)
        with _ledger_io(execution.run_id), _file_lock(run_dir, self.lock_timeout):
            ledger = self._load(execution.project_id, execution.run_id)
            if ledger is None:
                ledger = LedgerFile(kind=LedgerKind.CHECKPOINT, project_id=execution.project_id, run_id=execution.run_id)
            existing = ledger.executions.get(execution.id)
            if existing is not None:
                if not same_authorization(existing, execution):
                    raise ConflictError(f"execution {execution.id} already exists for a different authorization")
                return existing, False
            if execution.request_id is not None and any(
                e.request_id == execution.request_id for e in ledger.executions.values()
            ):
                raise ConflictError(f"approval {execution.request_id} already has an execution")
            ledger.executions[execution.id] = execution
            write_ledger(run_dir, ledger)
            return execution, True

    def replace(
        self,
        expected: ActionExecution,
        new: ActionExecution,
        *,
        attempt: ExecutionAttempt | None = None,
        reconciliation: ActionReconciliation | None = None,
    ) -> bool:
        run_dir = self._run_dir(expected.run_id)
        with _ledger_io(expected.run_id), _file_lock(run_dir, self.lock_timeout):
            ledger = self._load(expected.project_id, expected.run_id)
            current = None if ledger is None else ledger.executions.get(expected.id)
            if ledger is None or current is None:
                raise RecordNotFoundError(f"execution {expected.id} does not exist")
            if current.version != expected.version:
                return False
            check_replace(current, new, attempt=attempt, reconciliation=reconciliation)
            if attempt is not None:
                key = (attempt.execution_id, attempt.attempt)
                stored = ledger.attempts.get(key)
                if attempt.ended_at is None:
                    if stored is not None:
                        raise ConflictError(f"attempt {attempt.attempt} of {attempt.execution_id} already exists")
                elif stored is None or stored.ended_at is not None or attempt_identity(stored) != attempt_identity(attempt):
                    raise ConflictError(f"attempt {attempt.attempt} of {attempt.execution_id} is not the open attempt")
                ledger.attempts[key] = attempt
            if reconciliation is not None:
                key = (reconciliation.execution_id, reconciliation.attempt)
                if key in ledger.reconciliations:
                    raise ConflictError(f"attempt {reconciliation.attempt} of {reconciliation.execution_id} is already reconciled")
                if key not in ledger.attempts:
                    raise RecordNotFoundError(f"attempt {reconciliation.attempt} of {reconciliation.execution_id} does not exist")
                ledger.reconciliations[key] = reconciliation
            ledger.executions[new.id] = new
            write_ledger(run_dir, ledger)
            return True


__all__ = [
    "LEDGER_FILE",
    "CheckpointExecutionRepository",
    "LedgerBindingError",
    "LedgerFile",
    "LedgerIntegrityError",
    "check_binding",
    "ensure_binding",
    "read_ledger",
    "write_ledger",
]

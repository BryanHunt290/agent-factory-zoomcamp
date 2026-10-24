"""One execution ledger contract, three stores: the checkpoint file, in memory, and PostgreSQL.

The same tests prove the same semantics everywhere (docs/design/action-execution.md):
idempotent authorization, write-ahead dispatch, exactly one winner of a
compare-and-set, reconciliation rules, and failing closed on anything
impossible. PostgreSQL variants run with ``-m postgres --run-postgres``.
"""

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_factory.domain import ApprovalOutcome, ApprovalRequest, ApprovalService, Project, Run, risk_for
from agent_factory.domain.approvals import ExactAction, approval_expiry
from agent_factory.domain.errors import (
    ApprovalBindingError,
    ConflictError,
    DomainValidationError,
    RecordNotFoundError,
    StatusTransitionError,
    StorageUnavailableError,
)
from agent_factory.domain.executions import (
    AttemptResult,
    Dispatch,
    ExecutionLedger,
    ExecutionSpec,
    ExecutionState,
    LedgerKind,
    ReconciliationChoice,
    bounded_result,
)
from agent_factory.domain.identity import Actor
from agent_factory.domain.milestones import standard_milestone_policy
from agent_factory.domain.repositories import in_memory_store
from agent_factory.engine.execution_store import (
    LEDGER_FILE,
    CheckpointExecutionRepository,
    LedgerBindingError,
    LedgerIntegrityError,
    check_binding,
    ensure_binding,
)
from agent_factory.safety.execution import EffectClass, ExecutionSemantics, Idempotency
from agent_factory.state import RunStatus

P = "prj-" + "c" * 20
RUN = "run-1"
REQUEST = "apr-" + "3" * 16
T0 = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
ACTION = ExactAction(tool="send_notice", category="external_communication", params={"to": "a@example.test"})
C = ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT)
B_KEYED = ExecutionSemantics(
    effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.KEYED, key_retention=timedelta(hours=1), max_attempts=3
)
OWNER = "test-host:1:aaaa"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass
class Env:
    kind: str
    repository: Any
    store: Any
    clock: Clock
    new_repository: Any  # a second, independent handle (a reconnect / another process)

    def ledger(self, repository=None) -> ExecutionLedger:
        kind = LedgerKind.CHECKPOINT if self.kind == "checkpoint" else LedgerKind.PRODUCT_STORE
        return ExecutionLedger(repository or self.repository, kind=kind, clock=self.clock)


@pytest.fixture(params=["checkpoint", "memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def env(request, tmp_path):
    clock = Clock()
    if request.param == "checkpoint":
        runs = tmp_path / "runs"
        return Env("checkpoint", CheckpointExecutionRepository(runs), None, clock, lambda: CheckpointExecutionRepository(runs))
    if request.param == "memory":
        store = in_memory_store()
        return Env("memory", store.executions, store, clock, lambda: store.executions)
    open_store = request.getfixturevalue("pg_open")
    store = open_store()
    return Env("postgres", store.executions, store, clock, lambda: open_store().executions)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def product_env(request, tmp_path):
    """Only the product stores: they bind executions to stored approvals (database constraints)."""
    clock = Clock()
    if request.param == "memory":
        store = in_memory_store()
        return Env("memory", store.executions, store, clock, lambda: store.executions)
    open_store = request.getfixturevalue("pg_open")
    store = open_store()
    return Env("postgres", store.executions, store, clock, lambda: open_store().executions)


def seed(env: Env, action=ACTION, request_id=REQUEST, outcome=ApprovalOutcome.APPROVED) -> None:
    """The approval an execution belongs to (product stores enforce it; the checkpoint ledger relies on the engine)."""
    store = env.store
    if store is None:
        return
    if store.projects.get(P) is None:
        store.projects.add(Project(id=P, name="Ledger", created_at="2026-10-03T09:00:00.000000Z",
                                   milestone_policy=standard_milestone_policy()))  # fmt: skip
        store.runs.add(Run(project_id=P, run_id=RUN, number=1, graph_name="operate-v1", status=RunStatus.RUNNING,
                           created_at="2026-10-03T09:00:00.000000Z", updated_at="2026-10-03T09:00:00.000000Z"))  # fmt: skip
    store.approvals.add_request(ApprovalRequest(
        id=request_id, project_id=P, run_id=RUN, node="operate", agent="operator", action=action,
        action_fingerprint=action.fingerprint, reason="needs approval", risk=risk_for(action.category),
        requested_at="2026-10-03T09:30:00.000000Z",
        expires_at=approval_expiry("2026-10-03T09:30:00.000000Z"),
    ))  # fmt: skip
    ApprovalService(store.approvals).decide(
        project_id=P, request_id=request_id, action_fingerprint=action.fingerprint, outcome=outcome,
        actor=Actor.unauthenticated("dana"), at="2026-10-03T09:31:00.000000Z",
    )  # fmt: skip


def spec(semantics=C, action=ACTION, request_id=REQUEST, until=None, visit=1) -> ExecutionSpec:
    return ExecutionSpec(
        project_id=P, run_id=RUN, request_id=request_id, node="operate", visit=visit, agent="operator", action=action,
        semantics=semantics, authorized_at="2026-10-03T09:31:00.000000Z", authorized_until=until,
    )  # fmt: skip


def authorized(env: Env, semantics=C, **kwargs):
    seed(env)
    execution, _ = env.ledger().authorize(spec(semantics, **kwargs))
    return execution


def payload(summary="sent"):
    return bounded_result(True, summary, {"n": 1}, replayable=True)


# ------------------------------------------------------------- authorize


def test_authorization_is_idempotent_and_durable(env):
    seed(env)
    ledger = env.ledger()
    first, created = ledger.authorize(spec())
    again, created_again = ledger.authorize(spec())
    assert (created, created_again) == (True, False) and again == first
    assert first.state is ExecutionState.AUTHORIZED and first.version == 1
    # Another handle (a new connection, another process) sees exactly the same record.
    assert env.ledger(env.new_repository()).get(P, RUN, first.id) == first


def test_a_second_authorization_of_the_same_approval_is_refused(env):
    first = authorized(env)
    with pytest.raises(ConflictError, match="different authorization"):
        env.ledger().authorize(spec(visit=2))  # same execution ID, different node visit
    assert env.ledger().get(P, RUN, first.id) == first


@pytest.mark.parametrize("outcome", [ApprovalOutcome.REJECTED, None])
def test_product_store_executions_belong_to_an_approved_decision(product_env, outcome):
    env = product_env
    if outcome is None:  # no decision at all
        store = env.store
        seed(env)  # creates project/run/request/decision for REQUEST
        orphan = ExactAction(tool="send_notice", category="external_communication", params={"to": "z@example.test"})
        with pytest.raises((ApprovalBindingError, RecordNotFoundError)):
            env.ledger().authorize(spec(action=orphan, request_id="apr-" + "4" * 16))
        assert store.executions.list_for_run(P, RUN) == []
        return
    seed(env, outcome=outcome)
    with pytest.raises(ApprovalBindingError, match="approved decision"):
        env.ledger().authorize(spec())


def test_an_execution_for_a_different_action_than_approved_is_refused(product_env):
    env = product_env
    seed(env)
    other = ExactAction(tool="send_notice", category="external_communication", params={"to": "evil@example.test"})
    with pytest.raises(ApprovalBindingError):
        env.ledger().authorize(spec(action=other))  # same approval request, different exact action


# ---------------------------------------------------------- write-ahead


def test_begin_is_durable_before_it_returns(env):
    execution = authorized(env)
    dispatch = env.ledger().begin(execution, owner=OWNER)
    assert isinstance(dispatch, Dispatch)
    other = env.ledger(env.new_repository())  # what a crash-and-restart would read
    stored = other.get(P, RUN, execution.id)
    assert stored.state is ExecutionState.DISPATCHING and stored.owner == OWNER and stored.open_attempt == 1
    [attempt] = other.attempts(stored)
    assert attempt.ended_at is None and attempt.owner == OWNER


def test_a_stale_writer_never_gets_to_dispatch(env):
    execution = authorized(env)
    assert isinstance(env.ledger().begin(execution, owner=OWNER), Dispatch)
    assert env.ledger(env.new_repository()).begin(execution, owner="other:2:bbbb") is None


def test_concurrent_dispatches_of_one_execution_admit_exactly_one(env):
    execution = authorized(env)
    barrier = threading.Barrier(6)
    results = []
    lock = threading.Lock()

    def contender(index):
        ledger = env.ledger(env.new_repository())  # each one a separate handle / connection
        barrier.wait()
        got = ledger.begin(execution, owner=f"worker-{index}:1:cccc")
        with lock:
            results.append(got)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sum(isinstance(r, Dispatch) for r in results) == 1
    assert sum(r is None for r in results) == 5
    stored = env.ledger(env.new_repository()).get(P, RUN, execution.id)
    assert stored.attempts == 1 and len(env.ledger().attempts(stored)) == 1


def test_outcomes_close_the_attempt_and_record_a_replayable_result(env):
    execution = authorized(env)
    dispatch = env.ledger().begin(execution, owner=OWNER)
    done = env.ledger().finish(dispatch.execution, dispatch.attempt, result=AttemptResult.SUCCEEDED, payload=payload())
    reread = env.ledger(env.new_repository()).get(P, RUN, execution.id)
    assert reread == done and done.state is ExecutionState.SUCCEEDED and done.result["data"] == {"n": 1}
    [attempt] = env.ledger().attempts(done)
    assert attempt.result is AttemptResult.SUCCEEDED and attempt.ended_at is not None


def test_a_keyed_retry_reuses_the_same_key(env):
    execution = authorized(env, B_KEYED)
    first = env.ledger().begin(execution, owner=OWNER)
    retry = env.ledger().finish(first.execution, first.attempt, result=AttemptResult.INDETERMINATE)
    assert retry.state is ExecutionState.AUTHORIZED
    second = env.ledger().begin(retry, owner=OWNER)
    keys = [a.idempotency_key for a in env.ledger().attempts(second.execution)]
    assert keys == [first.attempt.idempotency_key] * 2 and keys[0].startswith("af1-axe-")


def test_a_dispatch_must_begin_before_the_approval_expires(env):
    execution = authorized(env, until="2026-10-03T10:05:00.000000Z")
    env.clock.advance(minutes=5)
    closed = env.ledger().begin(execution, owner=OWNER)
    assert closed.state is ExecutionState.LAPSED and closed.lapse_reason == "authorization_expired"
    assert env.ledger().attempts(closed) == []  # nothing was dispatched


# ---------------------------------------------------------- reconciliation


def uncertain(env: Env):
    execution = authorized(env)
    dispatch = env.ledger().begin(execution, owner=OWNER)
    return env.ledger().finish(dispatch.execution, dispatch.attempt, result=AttemptResult.INDETERMINATE)


def reconcile(env: Env, execution, choice, actor="dana", **changes):
    values = dict(attempt=execution.attempts, action_fingerprint=execution.action_fingerprint, choice=choice,
                  actor=Actor.unauthenticated(actor), comment="checked the provider")  # fmt: skip
    values.update(changes)
    return env.ledger().reconcile(P, RUN, execution.id, **values)


def test_a_retry_after_reconciliation_is_a_new_fifteen_minute_authorization(env):
    execution = uncertain(env)
    assert execution.state is ExecutionState.UNCERTAIN
    retried, rec, recorded = reconcile(env, execution, ReconciliationChoice.AUTHORIZE_RETRY)
    assert recorded and retried.state is ExecutionState.AUTHORIZED and retried.authorization_seq == 2
    assert retried.authorized_until == "2026-10-03T10:15:00.000000Z" and rec.retry_authorized_until == retried.authorized_until
    stored = env.ledger(env.new_repository())
    assert stored.reconciliations(retried) == [rec] and stored.get(P, RUN, execution.id) == retried
    if env.store is not None:  # the original approval is untouched: still the same, final decision
        assert env.store.approvals.get_decision(P, REQUEST).outcome is ApprovalOutcome.APPROVED


def test_reconciliation_is_idempotent_and_never_overwritten(env):
    execution = uncertain(env)
    done, rec, recorded = reconcile(env, execution, ReconciliationChoice.IT_HAPPENED)
    again, rec_again, recorded_again = reconcile(env, execution, ReconciliationChoice.IT_HAPPENED)
    assert recorded and not recorded_again and (again, rec_again) == (done, rec)
    with pytest.raises(ConflictError, match="already reconciled"):
        reconcile(env, execution, ReconciliationChoice.DID_NOT_HAPPEN)
    with pytest.raises(ConflictError):
        reconcile(env, execution, ReconciliationChoice.IT_HAPPENED, actor="someone-else")


def test_reconciliation_must_name_the_exact_action(env):
    execution = uncertain(env)
    with pytest.raises(DomainValidationError, match="different action"):
        reconcile(env, execution, ReconciliationChoice.IT_HAPPENED, action_fingerprint="sha256:" + "0" * 64)
    with pytest.raises(DomainValidationError, match="attempt"):
        reconcile(env, execution, ReconciliationChoice.IT_HAPPENED, attempt=5)
    assert env.ledger().get(P, RUN, execution.id).state is ExecutionState.UNCERTAIN


def test_nothing_to_reconcile_is_refused(env):
    execution = authorized(env)
    with pytest.raises(StatusTransitionError, match="nothing to reconcile"):
        reconcile(env, execution, ReconciliationChoice.IT_HAPPENED, attempt=1)


# ----------------------------------------------------------- fail closed


def test_impossible_changes_are_refused_and_change_nothing(env):
    execution = authorized(env)
    dispatch = env.ledger().begin(execution, owner=OWNER)
    stranger = replace(dispatch.attempt, owner="stranger:9:dddd").close(result=AttemptResult.SUCCEEDED, at="2026-10-03T10:00:01.000000Z")
    new, _ = dispatch.execution.finish_attempt(dispatch.attempt, result=AttemptResult.SUCCEEDED,
                                               at="2026-10-03T10:00:01.000000Z", payload=payload())  # fmt: skip
    with pytest.raises((ConflictError, DomainValidationError)):
        env.repository.replace(dispatch.execution, new, attempt=stranger)  # not the open attempt
    with pytest.raises(DomainValidationError):
        env.repository.replace(dispatch.execution, new)  # leaving dispatching without closing the attempt
    assert env.ledger().get(P, RUN, execution.id) == dispatch.execution


def test_unused_authorizations_lapse(env):
    execution = authorized(env)
    lapsed = env.ledger().close(execution, reason="node_visit_ended")
    assert lapsed.state is ExecutionState.LAPSED and lapsed.lapse_reason == "node_visit_ended"
    assert env.ledger(env.new_repository()).get(P, RUN, execution.id) == lapsed


# ------------------------------------------------- the checkpoint file itself


@pytest.fixture
def ledger_file(tmp_path):
    runs = tmp_path / "runs"
    ledger = ExecutionLedger(CheckpointExecutionRepository(runs), kind=LedgerKind.CHECKPOINT, clock=Clock())
    execution, _ = ledger.authorize(spec())
    ledger.begin(execution, owner=OWNER)
    return runs / RUN / LEDGER_FILE, runs


def _rewrite(path: Path, change, *, fix_digest: bool) -> None:
    from agent_factory.domain.serialization import canonical
    from agent_factory.util import sha256_hex

    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    if fix_digest:
        body = {k: v for k, v in data.items() if k not in ("record_type", "schema_version", "digest")}
        data["digest"] = "sha256:" + sha256_hex(canonical(body))
    path.write_text(json.dumps(data), encoding="utf-8")


def test_an_edited_ledger_file_fails_its_digest(ledger_file):
    path, runs = ledger_file
    _rewrite(path, lambda d: d["executions"][0].update(state="authorized"), fix_digest=False)
    with pytest.raises(LedgerIntegrityError, match="digest"):
        CheckpointExecutionRepository(runs).list_for_run(P, RUN)


def test_a_self_contradictory_ledger_file_is_refused_even_with_a_valid_digest(ledger_file):
    path, runs = ledger_file
    _rewrite(path, lambda d: d["attempts"].clear(), fix_digest=True)  # dispatching, but no attempt record
    with pytest.raises(LedgerIntegrityError, match="attempt"):
        CheckpointExecutionRepository(runs).list_for_run(P, RUN)


def test_a_torn_or_unreadable_ledger_file_is_refused(ledger_file):
    path, runs = ledger_file
    path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")
    with pytest.raises(LedgerIntegrityError, match="unreadable"):
        CheckpointExecutionRepository(runs).list_for_run(P, RUN)


def test_a_filesystem_error_makes_the_checkpoint_ledger_unavailable_not_wrong(tmp_path, monkeypatch):
    """A full disk or a denied replace is an outage, as for the database: the caller blocks the run."""
    import agent_factory.engine.execution_store as execution_store

    runs = tmp_path / "runs"
    ledger = ExecutionLedger(CheckpointExecutionRepository(runs), kind=LedgerKind.CHECKPOINT, clock=Clock())
    execution, _ = ledger.authorize(spec())

    def full_disk(path, obj):
        raise OSError(28, "No space left on device", str(path))

    monkeypatch.setattr(execution_store, "write_json_atomic", full_disk)
    with pytest.raises(StorageUnavailableError, match="could not be written: No space left") as raised:
        ledger.begin(execution, owner=OWNER)
    message = str(raised.value)  # no path, in any spelling: the message can reach a run's blocked reason
    assert tmp_path.name not in message and LEDGER_FILE not in message
    with pytest.raises(StorageUnavailableError):
        ensure_binding(tmp_path / "other" / RUN, RUN, P, LedgerKind.PRODUCT_STORE)
    monkeypatch.undo()
    stored = CheckpointExecutionRepository(runs).get(P, RUN, execution.id)
    assert stored == execution and stored.state is ExecutionState.AUTHORIZED  # nothing began
    assert isinstance(ledger.begin(stored, owner=OWNER), Dispatch)  # and it can, once the disk recovers


def test_a_run_is_bound_to_one_kind_of_ledger(tmp_path):
    runs = tmp_path / "runs"
    run_dir = runs / RUN
    ensure_binding(run_dir, RUN, P, LedgerKind.PRODUCT_STORE)  # its executions live in the database
    with pytest.raises(LedgerBindingError, match="with --database"):
        check_binding(run_dir, RUN, LedgerKind.CHECKPOINT)
    with pytest.raises(LedgerBindingError):
        CheckpointExecutionRepository(runs).list_for_run(P, RUN)
    other = tmp_path / "other" / RUN
    ledger = ExecutionLedger(CheckpointExecutionRepository(other.parent), kind=LedgerKind.CHECKPOINT, clock=Clock())
    ledger.authorize(spec())
    with pytest.raises(LedgerBindingError, match="without --database"):
        ensure_binding(other, RUN, P, LedgerKind.PRODUCT_STORE)


# ------------------------------------------------- PostgreSQL itself refuses


@pytest.mark.postgres
def test_the_database_refuses_invalid_executions_on_raw_sql(pg_open):
    from agent_factory.persistence.postgres import _execution_row, _insert, _EXECUTION_COLUMNS

    store = pg_open()
    env = Env("postgres", store.executions, store, Clock(), lambda: store.executions)
    seed(env, outcome=ApprovalOutcome.REJECTED)
    from agent_factory.domain.executions import ActionExecution

    execution = ActionExecution.authorized(spec(), at="2026-10-03T10:00:00.000000Z")
    with pytest.raises(ApprovalBindingError):  # the decision rejected the action: no matching approved row
        store.unit_of_work.query(_insert("action_executions", _EXECUTION_COLUMNS), _execution_row(execution))
    row = _execution_row(execution)
    row.update(id="axe-" + "e" * 20, request_id=None, decision_outcome=None, effect_class="non_idempotent", max_attempts=2)
    with pytest.raises(DomainValidationError, match="non_idempotent_single"):
        store.unit_of_work.query(_insert("action_executions", _EXECUTION_COLUMNS), row)

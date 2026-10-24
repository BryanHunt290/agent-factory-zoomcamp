"""E5 on real PostgreSQL: human decisions consumed by workers through real races and real process deaths.

    set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\\test-database.url
    .venv\\Scripts\\python -m pytest -m postgres --run-postgres tests/test_postgres_decisions.py

Concurrency: separate sessions race for real (threads released by a
barrier, each with its own connection), and workers hold runs at exact points
(after looking for a decision, after reading it) while the test acts.

Crashes: a worker in a child process (tests/decision_child.py) dies with
``os._exit(137)`` at one boundary of decision consumption; its lease is then
expired and a new worker recovers the run. Effects are counted in the
fixture's oracle file, never by asking Agent Factory: no dangerous fixture
action ever runs more often than a person authorized.
"""

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from decision_support import Flow, claimed, once, released
from worker_support import age_claim, race

from agent_factory.domain.approvals import ApprovalStatus
from agent_factory.domain.errors import ApprovalExpiredError, ConflictError, StatusTransitionError
from agent_factory.domain.executions import ExecutionLedger, ExecutionState, LedgerKind, ReconciliationChoice
from agent_factory.domain.serialization import format_timestamp, parse_timestamp
from agent_factory.state import RunStatus

pytestmark = pytest.mark.postgres

CHILD = Path(__file__).resolve().parent / "decision_child.py"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def admin(pg_schema):
    from agent_factory.persistence.postgres import connect

    conn = connect(pg_schema)
    yield conn
    conn.close()


@pytest.fixture
def pg_flow(tmp_path, pg_schema, pg_store):
    def make(kind="notice", name="flow"):
        return Flow.postgres(tmp_path / name, pg_schema, pg_store, kind)

    return make


def in_thread(fn):
    """Start ``fn`` in a thread; ``join()`` returns its result (or raises its error)."""
    box: dict = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread in join()
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def join(timeout=60):
        thread.join(timeout)
        assert not thread.is_alive(), "the thread did not finish"
        if "error" in box:
            raise box["error"]
        return box.get("result")

    return join


def attempt(fn):
    """``fn()``'s result, or the exception it raised (a race has winners and losers)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the loser's refusal is the result under test
        return exc


def hold(method_name, after_call=True):
    """Adjust an executor's recorder: block in ``method_name`` (after the real call) until released."""
    reached, release = threading.Event(), threading.Event()

    def adjust(ex):
        real = getattr(ex.recorder, method_name)

        def held(*args, **kwargs):
            result = real(*args, **kwargs) if after_call else None
            reached.set()
            assert release.wait(60), "never released"
            return result if after_call else real(*args, **kwargs)

        setattr(ex.recorder, method_name, held)

    return adjust, reached, release


def short_expiry(monkeypatch, seconds=2.0):
    """Approvals requested from now on expire ``seconds`` after they were requested (this process only)."""
    import agent_factory.engine.executor as executor_module

    def approval_expiry(requested_at, validity=None):
        return format_timestamp(parse_timestamp(requested_at) + timedelta(seconds=seconds))

    monkeypatch.setattr(executor_module, "approval_expiry", approval_expiry)


def wait_past(moment: str, margin: float = 0.05) -> None:
    deadline = parse_timestamp(moment) + timedelta(seconds=margin)
    while datetime.now(UTC) < deadline:
        time.sleep(0.01)


# ======================================================== 11 concurrency tests


def test_c1_two_workers_race_for_a_newly_approved_run_and_exactly_one_runs_it(pg_flow):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    first, second = race([flow.work, flow.work])
    assert sum(claimed(events) is not None for events in (first, second)) == 1
    assert flow.notices() == 1 and len(flow.events("approval_decision")) == 1
    assert flow.checkpoint().status is RunStatus.COMPLETED


def test_c2_a_decision_arriving_while_a_worker_holds_the_run_is_not_lost(pg_flow, admin):
    """The worker looked (no decision yet) and is about to release "until expiry": the wake-up must survive."""
    flow = pg_flow()
    flow.paused()
    admin.execute("UPDATE run_claims SET available_at = now()")  # an earlier wake-up with nothing to apply yet
    adjust, looked, go = hold("consumable_decision")
    flow.adjusters.append(once(adjust))
    worker_a = in_thread(flow.work)
    assert looked.wait(30)
    flow.decide("approved")  # while A holds the run, after A looked
    go.set()
    first = worker_a()
    assert released(first)["reason"] == "waiting_for_person" and flow.notices() == 0
    second = flow.work()
    assert claimed(second) is not None, "the decision's wake-up was lost by A's release"
    assert flow.notices() == 1 and flow.checkpoint().status is RunStatus.COMPLETED


def test_c3_two_identical_approvals_record_one_decision_and_run_the_action_once(pg_flow, pg_open):
    flow = pg_flow()
    flow.paused()
    a, b = pg_open(), pg_open()
    results = race([lambda: flow.decide("approved", store=a), lambda: flow.decide("approved", store=b)])
    assert sorted(r.recorded_now for r in results) == [False, True]
    flow.work()
    assert flow.notices() == 1 and len(flow.events("approval_decision")) == 1


def test_c4_conflicting_approvals_settle_on_exactly_one_and_only_it_reaches_the_engine(pg_flow, pg_open):
    flow = pg_flow()
    flow.paused()
    a, b = pg_open(), pg_open()
    results = race([lambda: attempt(lambda: flow.decide("approved", store=a)),
                    lambda: attempt(lambda: flow.decide("rejected", store=b))])  # fmt: skip
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1 and all(isinstance(r, ConflictError) for r in results if isinstance(r, Exception))
    outcome = winners[0].decision.outcome.value
    flow.work()
    [applied] = flow.events("approval_decision")
    assert applied["approved"] is (outcome == "approved")
    assert flow.notices() == (1 if outcome == "approved" else 0)


def test_c5_an_approval_that_expires_while_the_worker_is_claiming_is_never_used(pg_flow, monkeypatch):
    short_expiry(monkeypatch, seconds=2)
    flow = pg_flow()
    flow.paused()
    expires_at = flow.request().expires_at
    flow.decide("approved")  # in time
    flow.adjusters.append(once(lambda ex: wait_past(expires_at)))  # claimed in time, applied after the expiry
    flow.work()
    assert flow.notices() == 0
    [applied] = flow.events("approval_decision")
    assert (applied["approved"], applied["refused"]) == (False, "expired")
    assert flow.request().status is ApprovalStatus.APPROVED  # the person's decision is kept as made


@pytest.mark.parametrize("offset", [-0.3, -0.05, 0.0, 0.1])
def test_c6_a_decision_racing_the_expiry_never_runs_the_action_after_it(pg_flow, pg_open, monkeypatch, offset):
    short_expiry(monkeypatch, seconds=1.5)
    flow = pg_flow()
    flow.paused()
    expires_at = flow.request().expires_at
    deciding_store = pg_open()

    def person():
        target = parse_timestamp(expires_at) + timedelta(seconds=offset)
        while datetime.now(UTC) < target:
            time.sleep(0.002)
        return attempt(lambda: flow.decide("approved", store=deciding_store))

    def worker():
        events = []
        deadline = time.monotonic() + 15
        while claimed(events) is None and time.monotonic() < deadline:
            events = flow.work(idle_exit=0.05)
        return events

    decision, events = race([person, worker])
    assert claimed(events) is not None
    state = flow.checkpoint()
    assert state.status is RunStatus.COMPLETED
    if flow.notices():
        [attempt_row] = flow.store.executions.attempts(flow.project_id, "run-1", flow.execution().id)
        assert parse_timestamp(attempt_row.dispatched_at) < parse_timestamp(expires_at)  # began before the expiry
        assert not isinstance(decision, Exception)
    else:
        [applied] = flow.events("approval_decision")
        assert applied["approved"] is False
    if isinstance(decision, Exception):
        assert isinstance(decision, (ApprovalExpiredError, StatusTransitionError))
        assert flow.store.approvals.get_decision(flow.project_id, flow.request().id) is None


def test_c7_two_reconciliation_answers_settle_on_exactly_one(pg_flow, pg_open):
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    a, b = pg_open(), pg_open()
    results = race([lambda: attempt(lambda: flow.reconcile("it_happened", store=a)),
                    lambda: attempt(lambda: flow.reconcile("did_not_happen_retry", store=b))])  # fmt: skip
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1 and all(isinstance(r, ConflictError) for r in results if isinstance(r, Exception))
    choice = winners[0].decision.choice
    flow.work()
    assert len(flow.store.reconciliations.list_for_execution(flow.project_id, flow.execution().id)) == 1
    assert flow.notices() == (2 if choice is ReconciliationChoice.AUTHORIZE_RETRY else 1)
    assert flow.checkpoint().status is RunStatus.COMPLETED


def test_c8_an_answer_arriving_while_a_worker_holds_the_run_is_not_lost(pg_flow, admin):
    """The worker found no answer and is about to release the run as "not wanted": the wake-up must survive."""
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    admin.execute("UPDATE run_claims SET available_at = now()")
    adjust, looked, go = hold("reconciliation_decision")
    flow.adjusters.append(once(adjust))
    worker_a = in_thread(flow.work)
    assert looked.wait(30)
    flow.reconcile("it_happened")
    go.set()
    first = worker_a()
    assert (released(first)["reason"], released(first)["requeued"]) == ("waiting_for_person", False)
    second = flow.work()
    assert claimed(second) is not None, "the answer's wake-up was lost by A's release"
    assert flow.execution().state is ExecutionState.SUCCEEDED and flow.notices() == 1


def test_c9_an_old_worker_cannot_consume_a_decision_after_a_takeover(pg_flow, admin):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    adjust, read, go = hold("consumable_decision")  # A read the decision, and stalls before applying it
    flow.adjusters.append(once(adjust))
    worker_a = in_thread(flow.work)
    assert read.wait(30)
    age_claim(admin, flow.project_id, "run-1")  # A's lease is over
    b = flow.work()  # B takes over; A still holds the run's lock on this host
    assert [e["reason"] for e in b if e["event"] == "run_released"] == ["run_busy"]
    go.set()
    a = worker_a()
    assert [(e["operation"], e["reason"]) for e in a if e["event"] == "stale_worker_rejected"] == [("advance", "superseded")]
    assert flow.events("approval_decision") == [] and flow.notices() == 0  # A applied nothing
    c = flow.work()
    [applied] = flow.events("approval_decision")
    assert applied["owner"] == f"{c[0]['worker_id']}@{claimed(c)['fencing_token']}"
    assert flow.notices() == 1 and flow.checkpoint().status is RunStatus.COMPLETED


def test_c10_a_stale_fencing_token_cannot_apply_a_reconciliation(pg_flow, admin):
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    flow.reconcile("did_not_happen_retry")
    adjust, read, go = hold("reconciliation_decision")
    flow.adjusters.append(once(adjust))
    worker_a = in_thread(flow.work)
    assert read.wait(30)
    age_claim(admin, flow.project_id, "run-1")
    flow.work()  # B: taken over, but the run is busy on this host
    go.set()
    a = worker_a()
    assert [e["reason"] for e in a if e["event"] == "stale_worker_rejected"] == ["superseded"]
    execution = flow.execution()
    assert execution.state is ExecutionState.UNCERTAIN and execution.authorization_seq == 1  # nothing applied by A
    flow.work()
    execution = flow.execution()
    assert (execution.authorization_seq, execution.attempts, flow.notices()) == (2, 2, 2)
    assert len(flow.store.executions.reconciliations(flow.project_id, "run-1", execution.id)) == 1


def test_c11_a_retry_authorization_is_created_once_however_many_try_to_apply_it(pg_flow, pg_open, pg_schema):
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    flow.reconcile("did_not_happen_retry")
    decision = flow.store.reconciliations.get(flow.project_id, flow.execution().id, 1)
    execution = flow.execution()

    def apply_on_own_session():
        ledger = ExecutionLedger(pg_open().executions, kind=LedgerKind.PRODUCT_STORE)
        return attempt(lambda: ledger.reconcile(
            flow.project_id, "run-1", execution.id, attempt=1, action_fingerprint=execution.action_fingerprint,
            choice=decision.choice, actor=decision.actor, decided_at=decision.decided_at,
        ))  # fmt: skip

    results = race([apply_on_own_session] * 4)
    recorded = [r for r in results if not isinstance(r, Exception) and r[2]]
    assert len(recorded) == 1  # the others saw the identical record, or lost the compare-and-set
    assert all(isinstance(r, ConflictError) or not r[2] for r in results if r not in recorded)
    authorized = flow.execution()
    assert (authorized.state, authorized.authorization_seq) == (ExecutionState.AUTHORIZED, 2)
    assert len(flow.store.executions.reconciliations(flow.project_id, "run-1", execution.id)) == 1
    race([flow.work, flow.work])  # two workers for the run: one claims it
    assert flow.notices() == 2 and flow.execution().attempts == 2  # one retry, not two


# ============================================================= 10 crash tests


@pytest.fixture
def crash(pg_schema, admin):
    """Run decision_child.py on the flow until it dies at ``fault``; then expire its lease."""

    def run(flow, fault):
        env = {**os.environ, "DECISION_CHILD_SCHEMA": pg_schema.schema, "PYTHONDONTWRITEBYTECODE": "1"}
        child = subprocess.run(
            [sys.executable, str(CHILD), str(flow.root), flow.kind, fault], cwd=REPO, env=env, capture_output=True,
            text=True, timeout=120,
        )  # fmt: skip
        assert child.returncode == 137, (child.returncode, child.stdout, child.stderr)
        age_claim(admin, flow.project_id, "run-1")  # its lease runs out; the child is gone
        return child

    return run


def test_k1_a_decision_stored_before_any_claim_survives_a_worker_dying_in_its_claim(pg_flow, crash, admin):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    token_before = admin.execute("SELECT fencing_token FROM run_claims").fetchone()[0]
    crash(flow, "in_claim")
    assert admin.execute("SELECT fencing_token, worker_id FROM run_claims").fetchone() == (token_before, None)  # rolled back
    flow.work()
    assert flow.notices() == 1 and flow.checkpoint().status is RunStatus.COMPLETED


def test_k2_a_claim_that_dies_before_applying_the_decision_is_recovered_by_the_next_worker(pg_flow, crash):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    crash(flow, "before_apply")
    assert flow.checkpoint().status is RunStatus.AWAITING_APPROVAL and flow.events("approval_decision") == []
    events = flow.work()
    assert any(e["event"] == "run_taken_over" for e in events)
    assert flow.notices() == 1 and len(flow.events("approval_decision")) == 1


def test_k3_a_decision_applied_but_not_checkpointed_is_applied_again_and_the_action_runs_once(pg_flow, crash):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    crash(flow, "after_apply_before_checkpoint")
    assert flow.checkpoint().status is RunStatus.AWAITING_APPROVAL  # the checkpoint never saw it
    assert len(flow.events("approval_decision")) == 1  # but the log did
    flow.work()
    assert flow.notices() == 1 and flow.checkpoint().status is RunStatus.COMPLETED
    owners = [e["owner"] for e in flow.events("approval_decision")]
    assert len(owners) == 2 and owners[0] != owners[1]  # both applications are on the record, by their workers


def test_k4_a_checkpoint_before_the_dispatch_record_dispatches_once_after_recovery(pg_flow, crash):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    crash(flow, "before_dispatch_record")
    assert flow.checkpoint().status is RunStatus.RUNNING and flow.notices() == 0
    flow.work()
    assert flow.notices() == 1 and flow.execution().attempts == 1


def test_k5_a_death_after_the_side_effect_never_resends_it_and_a_person_settles_it(pg_flow, crash):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    crash(flow, "after_tool_before_outcome")
    assert flow.notices() == 1
    flow.work()
    assert flow.checkpoint().status is RunStatus.AWAITING_RECONCILIATION and flow.notices() == 1  # never resent
    flow.reconcile("it_happened")
    flow.work()
    assert flow.notices() == 1 and flow.checkpoint().status is RunStatus.COMPLETED


def test_k6_an_outcome_recorded_before_the_node_checkpoint_is_replayed_not_resent(pg_flow, crash):
    flow = pg_flow()
    flow.paused()
    flow.decide("approved")
    crash(flow, "after_outcome")
    assert flow.execution().state is ExecutionState.SUCCEEDED and flow.notices() == 1
    flow.work()
    state = flow.checkpoint()
    assert state.status is RunStatus.COMPLETED and flow.notices() == 1
    assert state.implementation["results"][0]["replayed"] is True


def test_k7_an_answer_stored_before_any_claim_survives_a_worker_dying_in_its_claim(pg_flow, crash):
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    flow.reconcile("it_happened")
    crash(flow, "in_claim")
    flow.work()
    assert flow.execution().state is ExecutionState.SUCCEEDED and flow.notices() == 1
    assert flow.checkpoint().status is RunStatus.COMPLETED


def test_k8_an_answer_applied_to_the_ledger_but_not_checkpointed_lets_the_run_continue(pg_flow, crash):
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    flow.reconcile("it_happened")
    crash(flow, "after_reconcile_before_checkpoint")
    assert flow.execution().state is ExecutionState.SUCCEEDED  # applied in the ledger
    assert flow.checkpoint().status is RunStatus.AWAITING_RECONCILIATION  # not in the checkpoint
    flow.work()
    assert flow.checkpoint().status is RunStatus.COMPLETED and flow.notices() == 1
    assert len(flow.store.executions.reconciliations(flow.project_id, "run-1", flow.execution().id)) == 1


def test_k9_a_retry_authorized_before_a_death_dispatches_exactly_once_within_its_window(pg_flow, crash):
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    flow.reconcile("did_not_happen_retry")
    crash(flow, "before_retry_dispatch")
    execution = flow.execution()
    assert (execution.state, execution.authorization_seq, flow.notices()) == (ExecutionState.AUTHORIZED, 2, 1)
    flow.work()
    execution = flow.execution()
    assert (execution.state, execution.attempts, flow.notices()) == (ExecutionState.SUCCEEDED, 2, 2)


def test_k10_a_retry_that_took_effect_before_a_death_is_never_sent_a_third_time(pg_flow, crash):
    flow = pg_flow("unknown-notice")
    flow.uncertain()
    flow.reconcile("did_not_happen_retry")
    crash(flow, "after_retry_tool_before_outcome")
    assert flow.notices() == 2
    flow.work()
    execution = flow.execution()
    assert flow.checkpoint().status is RunStatus.AWAITING_RECONCILIATION and flow.notices() == 2
    assert (execution.state, execution.attempts) == (ExecutionState.UNCERTAIN, 2)  # a person must say again
    flow.reconcile("it_happened")
    flow.work()
    assert flow.notices() == 2 and flow.checkpoint().status is RunStatus.COMPLETED

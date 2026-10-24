"""Approved actions through the engine: write-ahead dispatch, replay, crashes, and reconciliation.

These are the regression tests for the defects found in the design phase
(docs/design/action-execution.md, section 1) and the evidence for E1-E2:

* a node needing two approvals no longer re-runs its first approved action;
* a crash after a non-idempotent side effect never resends it automatically:
  the run waits for a person (``awaiting_reconciliation``);
* the dispatch is in the hash-chained audit log before the side effect, so it
  survives the process dying inside the tool.

Real process death is exercised with ``tests/crash_child.py``, which ends a
child process with ``os._exit(137)`` at a named boundary; the parent then
resumes the run and counts real effects in the fixtures' oracle files.
"""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import action_fixtures as fx
import pytest

from agent_factory.demo import APPROVE, CALC_POLISHED, PLAN, _dev
from agent_factory.domain import ApprovalOutcome, ApprovalService, MilestoneQuery, in_memory_store
from agent_factory.domain.errors import StorageUnavailableError
from agent_factory.domain.executions import ExecutionState, ReconciliationChoice
from agent_factory.domain.identity import Actor
from agent_factory.domain.milestones import ACTION_EXECUTION, MilestoneStatus
from agent_factory.engine import ExecutionLog
from agent_factory.engine.execution_store import LedgerBindingError
from agent_factory.engine.executor import ReconciliationPending, RunLocked
from agent_factory.engine.locks import FileLock
from agent_factory.factory import build_executor
from agent_factory.providers import FakeProvider
from agent_factory.state import RunStatus
from agent_factory.templates import CALCULATOR_TEMPLATE
from agent_factory.tools.base import OutcomeUnknown
from agent_factory.tools.filesystem import DeleteFileTool

CHILD = Path(__file__).resolve().parent / "crash_child.py"
REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def execution_events(ex, run_id="run-1"):
    return [e for e in ex.log_for(run_id).entries() if e["event"].startswith(("action_", "reconciliation"))]


def notices(oracle):
    return fx.read_lines(oracle / "notices.jsonl")


def charges(oracle):
    return fx.read_lines(oracle / "charges.jsonl"), fx.read_lines(oracle / "charge_calls.jsonl")


@pytest.fixture
def oracle(tmp_path):
    return tmp_path / "oracle"


def started_and_approved(tmp_path, oracle, kind, **kwargs):
    ws = fx.workspace(tmp_path / "ws")
    ex = fx.executor(ws, kind, oracle, **kwargs)
    state = ex.start(fx_task(), run_id="run-1")
    assert state.status is RunStatus.AWAITING_APPROVAL
    fx.approve_pending(ex)
    return ws, ex


def fx_task():
    from agent_factory.state import TaskSpec

    return TaskSpec(title="fixture", goal="exercise one approved action")


# =================================================== regression: two approvals


@pytest.fixture(params=["checkpoint", "memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def product_store(request):
    if request.param == "checkpoint":
        return None
    if request.param == "memory":
        return in_memory_store()
    return request.getfixturevalue("pg_store")


def test_a_node_with_two_approvals_runs_its_first_approved_action_once(tmp_path, monkeypatch, product_store):
    dispatched = []
    real_run = DeleteFileTool.run

    def counting(self, workspace, *, path):
        dispatched.append(path)
        return real_run(self, workspace, path=path)

    monkeypatch.setattr(DeleteFileTool, "run", counting)
    ws = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
    ws.write_text("old_a.py", "# old\n")
    dev = _dev("Implement and delete two files.", CALC_POLISHED, delete=["legacy_calc.py", "old_a.py"])
    provider = FakeProvider({"planner": [PLAN], "developer": [dev], "reviewer": [APPROVE]})
    ex = build_executor(CALCULATOR_TEMPLATE, provider, ws, product_store=product_store)

    assert ex.start(CALCULATOR_TEMPLATE.task, run_id="run-1").status is RunStatus.AWAITING_APPROVAL
    fx.approve_pending(ex)
    assert ex.resume("run-1").status is RunStatus.AWAITING_APPROVAL  # paused again, for the second deletion
    assert dispatched == ["legacy_calc.py"]  # the first deletion ran once, before the second pause
    fx.approve_pending(ex)
    state = ex.resume("run-1")

    assert state.status is RunStatus.COMPLETED
    assert dispatched == ["legacy_calc.py", "old_a.py"]  # the first approved action was NOT run again
    events = execution_events(ex)
    replays = [e for e in events if e["event"] == "action_replayed"]
    assert len(replays) == 1 and replays[0]["outcome"] == "succeeded"
    assert state.implementation["files_deleted"] == ["legacy_calc.py", "old_a.py"]  # the node continued normally
    executions = ex.ledger.list_for_run(ws.project_id, "run-1")
    assert sorted(e.state for e in executions) == [ExecutionState.SUCCEEDED] * 2
    assert ex.log_for("run-1").verify() > 0


def test_an_identical_repeated_call_within_one_visit_is_one_effect(tmp_path, oracle):
    agent = fx.OperatorAgent("send_notice", fx.params_for("notice"), repeat=2)
    ws, ex = started_and_approved(tmp_path, oracle, "notice", agent=agent)
    state = ex.resume("run-1")
    assert state.status is RunStatus.COMPLETED
    assert len(notices(oracle)) == 1  # one approval, one effect
    assert [r["replayed"] for r in state.implementation["results"]] == [False, True]


# ============================================ regression: crash after the effect


def run_child(ws_dir, oracle, kind, phase, fault, env=None, timeout=60):
    environ = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, str(CHILD), str(ws_dir), str(oracle), kind, phase, fault],
        cwd=REPO, env=environ, capture_output=True, text=True, timeout=timeout,
    )  # fmt: skip


def test_a_crash_after_a_non_idempotent_side_effect_never_resends_it(tmp_path, oracle):
    ws, ex = started_and_approved(tmp_path, oracle, "notice")
    child = run_child(ws.root, oracle, "notice", "resume", "after_tool_before_outcome")
    assert child.returncode == 137, child.stderr  # the process really died, inside the tool
    assert len(notices(oracle)) == 1

    restarted = fx.executor(fx.workspace(ws.root), "notice", oracle)  # a new process
    state = restarted.resume("run-1")
    assert state.status is RunStatus.AWAITING_RECONCILIATION
    assert len(notices(oracle)) == 1  # never resent automatically
    pending = state.pending_reconciliation
    assert (pending["tool"], pending["category"], pending["agent"], pending["node"]) == (
        "send_notice", "external_communication", "operator", "operate",
    )  # fmt: skip
    assert pending["attempt"] == 1 and pending["dispatched_at"] and pending["action_fingerprint"].startswith("sha256:")
    assert "stopped after the dispatch was recorded" in pending["reason"]
    with pytest.raises(ReconciliationPending):
        restarted.resume("run-1")
    assert len(notices(oracle)) == 1


def test_the_dispatch_is_in_the_audit_log_even_if_the_process_dies_inside_the_tool(tmp_path, oracle):
    ws, ex = started_and_approved(tmp_path, oracle, "notice")
    child = run_child(ws.root, oracle, "notice", "resume", "before_tool")
    assert child.returncode == 137, child.stderr
    log = ExecutionLog(ws.runs_dir / "run-1" / ExecutionLog.FILENAME)
    events = [e["event"] for e in log.entries()]
    assert "action_dispatching" in events  # written and fsync'd before the side effect could begin
    assert not {"action_succeeded", "action_failed"} & set(events)  # and no outcome, because there was none
    dispatching = next(e for e in log.entries() if e["event"] == "action_dispatching")
    assert dispatching["attempt"] == 1 and dispatching["execution_id"].startswith("axe-")
    assert "to" not in dispatching and "params" not in dispatching  # fingerprints, not parameters
    assert log.verify() == len(events)  # the hash chain is intact across the crash


# =========================================================== the crash matrix

C_MATRIX = {
    # fault point: (notices after the crash, final status after restart, notices after restart)
    "before_dispatch_record": (0, RunStatus.COMPLETED, 1),
    "after_dispatch_record": (0, RunStatus.AWAITING_RECONCILIATION, 0),  # "may have been sent": a false positive
    "before_tool": (0, RunStatus.AWAITING_RECONCILIATION, 0),
    "after_tool_before_outcome": (1, RunStatus.AWAITING_RECONCILIATION, 1),
    "after_log_outcome": (1, RunStatus.COMPLETED, 1),  # the outcome was on disk: recovered, no person needed
    "after_outcome": (1, RunStatus.COMPLETED, 1),  # replayed
    "before_node_checkpoint": (1, RunStatus.COMPLETED, 1),  # replayed
    "after_node_checkpoint": (1, RunStatus.COMPLETED, 1),
}


@pytest.mark.parametrize("fault", sorted(C_MATRIX))
def test_crash_matrix_for_a_non_idempotent_action(tmp_path, oracle, fault):
    before, final, after = C_MATRIX[fault]
    ws, _ = started_and_approved(tmp_path, oracle, "notice")
    child = run_child(ws.root, oracle, "notice", "resume", fault)
    assert child.returncode == 137, child.stderr
    assert len(notices(oracle)) == before
    state = fx.executor(fx.workspace(ws.root), "notice", oracle).resume("run-1")
    assert state.status is final
    assert len(notices(oracle)) == after <= 1  # never more than one notice, at any boundary


B_MATRIX = {
    # fault point: (effects after restart, provider calls after restart)
    "before_dispatch_record": (1, 1),
    "after_dispatch_record": (1, 1),  # retried with the same key
    "before_tool": (1, 1),
    "after_tool_before_outcome": (1, 2),  # retried with the same key: the provider deduplicated it
    "after_log_outcome": (1, 1),
    "after_outcome": (1, 1),
    "before_node_checkpoint": (1, 1),
    "after_node_checkpoint": (1, 1),
}


@pytest.mark.parametrize("fault", sorted(B_MATRIX))
def test_crash_matrix_for_a_keyed_idempotent_action(tmp_path, oracle, fault):
    effects, calls = B_MATRIX[fault]
    ws, _ = started_and_approved(tmp_path, oracle, "charge")
    child = run_child(ws.root, oracle, "charge", "resume", fault)
    assert child.returncode == 137, child.stderr
    state = fx.executor(fx.workspace(ws.root), "charge", oracle).resume("run-1")
    assert state.status is RunStatus.COMPLETED  # class B retries safely: no person needed
    made, attempted = charges(oracle)
    assert (len(made), len(attempted)) == (effects, calls)
    assert len({c["key"] for c in attempted}) == 1  # every call carried the same idempotency key


def test_a_keyed_retry_outside_the_providers_window_waits_for_a_person(tmp_path, oracle):
    clock = Clock()
    clock.now = datetime.now(timezone.utc)  # approved now: the child dispatches on the real clock, before it expires
    ws, _ = started_and_approved(tmp_path, oracle, "charge", clock=clock)
    child = run_child(ws.root, oracle, "charge", "resume", "after_tool_before_outcome")
    assert child.returncode == 137, child.stderr
    # The child stamped the attempt with the real clock; a restart after the provider's window has
    # passed must not reuse the key, because the provider may have forgotten it.
    clock.now = datetime.now(timezone.utc) + fx.KEY_RETENTION + timedelta(minutes=1)
    state = fx.executor(fx.workspace(ws.root), "charge", oracle, clock=clock).resume("run-1")
    assert state.status is RunStatus.AWAITING_RECONCILIATION
    assert len(charges(oracle)[1]) == 1  # no second call


# ============================================================ reconciliation


class UnknownOnceNotice(fx.SendNoticeTool):
    """Sends the notice, then reports that the outcome is unknown (a timeout after sending)."""

    def run(self, workspace, *, to, text):
        super().run(workspace, to=to, text=text)
        raise OutcomeUnknown("the gateway timed out after accepting the message")


@pytest.fixture
def uncertain_run(tmp_path, oracle, monkeypatch):
    clock = Clock()
    ws = fx.workspace(tmp_path / "ws")
    monkeypatch.setattr(fx, "tool_for", lambda kind, o: UnknownOnceNotice(o))
    ex = fx.executor(ws, "notice", oracle, clock=clock)
    ex.start(fx_task(), run_id="run-1")
    fx.approve_pending(ex)
    state = ex.resume("run-1")
    assert state.status is RunStatus.AWAITING_RECONCILIATION and len(notices(oracle)) == 1
    monkeypatch.setattr(fx, "tool_for", lambda kind, o: fx.SendNoticeTool(o))  # the gateway is healthy again
    return ws, clock, state.pending_reconciliation


def reconciled(ws, oracle, clock, pending, choice):
    ex = fx.executor(ws, "notice", oracle, clock=clock)
    return ex, ex.reconcile_action(
        "run-1", pending["execution_id"], attempt=pending["attempt"],
        action_fingerprint=pending["action_fingerprint"], choice=choice, approver="dana",
    )  # fmt: skip


def test_it_happened_lets_the_run_continue_without_resending(uncertain_run, oracle):
    ws, clock, pending = uncertain_run
    ex, state = reconciled(ws, oracle, clock, pending, ReconciliationChoice.IT_HAPPENED)
    assert state.status is RunStatus.RUNNING and state.pending_reconciliation is None
    state = ex.resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 1
    [result] = state.implementation["results"]
    assert result["replayed"] and "Confirmed by dana" in result["summary"]


def test_did_not_happen_never_retries(uncertain_run, oracle):
    ws, clock, pending = uncertain_run
    ex, _ = reconciled(ws, oracle, clock, pending, ReconciliationChoice.DID_NOT_HAPPEN)
    state = ex.resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 1
    assert state.implementation["results"] == [] and len(state.implementation["refused"]) == 1


def test_a_retry_authorization_allows_exactly_one_more_attempt_within_fifteen_minutes(uncertain_run, oracle):
    ws, clock, pending = uncertain_run
    ex, _ = reconciled(ws, oracle, clock, pending, ReconciliationChoice.AUTHORIZE_RETRY)
    clock.advance(minutes=14)
    state = ex.resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 2  # the person authorized one more
    [execution] = ex.ledger.list_for_run(ws.project_id, "run-1")
    assert execution.authorization_seq == 2 and execution.attempts == 2
    [rec] = ex.ledger.reconciliations(execution)
    assert rec.choice is ReconciliationChoice.AUTHORIZE_RETRY and rec.attempt == 1 and rec.actor.display_name == "dana"


def test_a_retry_authorization_expires_unused_after_fifteen_minutes(uncertain_run, oracle):
    ws, clock, pending = uncertain_run
    ex, _ = reconciled(ws, oracle, clock, pending, ReconciliationChoice.AUTHORIZE_RETRY)
    clock.advance(minutes=15)
    state = ex.resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 1  # never silently renewed
    [execution] = ex.ledger.list_for_run(ws.project_id, "run-1")
    assert execution.state is ExecutionState.LAPSED and execution.lapse_reason == "authorization_expired"
    assert any(e["event"] == "action_lapsed" for e in execution_events(ex))


def test_a_reconciliation_for_another_action_or_attempt_is_refused(uncertain_run, oracle):
    ws, clock, pending = uncertain_run
    ex = fx.executor(ws, "notice", oracle, clock=clock)
    for changes in ({"action_fingerprint": "sha256:" + "0" * 64}, {"attempt": 7}):
        args = {"attempt": pending["attempt"], "action_fingerprint": pending["action_fingerprint"], **changes}
        with pytest.raises(Exception, match="different action|attempt"):
            ex.reconcile_action("run-1", pending["execution_id"], choice="it_happened", **args)
    assert ex.store.load("run-1").status is RunStatus.AWAITING_RECONCILIATION


def test_repeated_reconciliation_is_idempotent(uncertain_run, oracle):
    ws, clock, pending = uncertain_run
    reconciled(ws, oracle, clock, pending, ReconciliationChoice.IT_HAPPENED)
    ex, state = reconciled(ws, oracle, clock, pending, ReconciliationChoice.IT_HAPPENED)  # a double tap
    events = [e for e in execution_events(ex) if e["event"] == "action_reconciled"]
    assert [e["recorded_now"] for e in events] == [True, False] and state.status is RunStatus.RUNNING


# ===================================================== other engine behaviour


class SwallowingOperator(fx.OperatorAgent):
    """A badly written agent that swallows every exception, including the reconciliation signal."""

    def run(self, ctx):
        from agent_factory.safety import ApprovalRequired

        try:
            ctx.tools.call(self.tool, **self.params)
        except ApprovalRequired:
            raise
        except Exception:  # noqa: BLE001 - deliberately wrong: swallows ReconciliationRequired
            pass
        from agent_factory.agents.base import AgentResult

        return AgentResult("done", {}, summary="pretended nothing happened")


def test_a_swallowed_reconciliation_signal_still_pauses_the_run(tmp_path, oracle, monkeypatch):
    monkeypatch.setattr(fx, "tool_for", lambda kind, o: UnknownOnceNotice(o))
    agent = SwallowingOperator("send_notice", fx.params_for("notice"))
    ws, ex = started_and_approved(tmp_path, oracle, "notice", agent=agent)
    state = ex.resume("run-1")
    assert state.status is RunStatus.AWAITING_RECONCILIATION  # the node was not committed
    assert state.step == 0 and len(notices(oracle)) == 1


def test_an_approval_the_rerun_node_no_longer_asks_for_lapses(tmp_path, oracle):
    ws, ex = started_and_approved(tmp_path, oracle, "notice")
    quiet = fx.executor(ws, "notice", oracle, agent=fx.OperatorAgent("send_notice", fx.params_for("notice"), ask=False))
    state = quiet.resume("run-1")
    assert state.status is RunStatus.COMPLETED and notices(oracle) == []
    [execution] = quiet.ledger.list_for_run(ws.project_id, "run-1")
    assert execution.state is ExecutionState.LAPSED and execution.lapse_reason == "node_visit_ended"
    assert any(e["event"] == "action_lapsed" for e in execution_events(quiet))


def test_an_expired_approval_cannot_dispatch(tmp_path, oracle):
    store, clock = in_memory_store(), Clock()
    ws = fx.workspace(tmp_path / "ws")
    recorder = fx.recorder_for(store, ws)
    ex = fx.executor(ws, "notice", oracle, recorder=recorder, clock=clock)
    state = ex.start(fx_task(), run_id="run-1")
    request = store.approvals.get_request(ws.project_id, state.pending_approval["request_id"])
    expiring = replace(request, id="apr-" + "9" * 16)  # stand-in: pre-register an expiring request
    del expiring
    # Give the stored request an expiry the engine must respect (approval policy comes in a later stage).
    store.unit_of_work.requests[(ws.project_id, request.id)] = replace(request, expires_at="2026-10-03T10:10:00.000000Z")
    fx.approve_pending(ex)  # decided in time
    clock.advance(minutes=10)
    state = ex.resume("run-1")  # too late to BEGIN the dispatch
    assert state.status is RunStatus.COMPLETED and notices(oracle) == []
    assert len(state.implementation["refused"]) == 1
    [execution] = ex.ledger.list_for_run(ws.project_id, "run-1")
    assert execution.state is ExecutionState.LAPSED and execution.lapse_reason == "authorization_expired"


class FlakyLedgerRepo:
    """Wraps a repository; fails once at a chosen operation, like a database that drops out."""

    def __init__(self, inner, fail_on):
        self.inner, self.fail_on, self.failed = inner, fail_on, False

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if name != self.fail_on:
            return attribute

        def wrapper(*args, **kwargs):
            if not self.failed and (name != "replace" or kwargs.get("attempt") is not None and kwargs["attempt"].ended_at):
                self.failed = True
                raise StorageUnavailableError("the database went away")
            return attribute(*args, **kwargs)

        return wrapper


def test_an_unavailable_ledger_blocks_the_run_and_the_outcome_is_recovered_from_the_log(tmp_path, oracle):
    from agent_factory.domain.executions import ExecutionLedger, LedgerKind

    store = in_memory_store()
    ws = fx.workspace(tmp_path / "ws")
    recorder = fx.recorder_for(store, ws)
    flaky = ExecutionLedger(FlakyLedgerRepo(store.executions, "replace"), kind=LedgerKind.PRODUCT_STORE)
    ex = fx.executor(ws, "notice", oracle, recorder=recorder, ledger=flaky)
    ex.start(fx_task(), run_id="run-1")
    fx.approve_pending(ex)
    state = ex.resume("run-1")  # the notice is sent, then recording its outcome fails
    assert state.status is RunStatus.BLOCKED and state.blocked["type"] == "ExecutionLedgerUnavailable"
    assert len(notices(oracle)) == 1
    healthy = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(store, ws))
    state = healthy.resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 1  # known from the log: not resent
    assert any(e["event"] == "action_recovered" and e["source"] == "execution_log" for e in execution_events(healthy))


def test_a_filesystem_error_at_the_write_ahead_blocks_the_run_and_sends_nothing(tmp_path, oracle, monkeypatch):
    """Without a database, a full disk when recording the dispatch blocks the run (resumable); it never fails it."""
    import agent_factory.engine.execution_store as execution_store

    ws, _ = started_and_approved(tmp_path, oracle, "notice")
    real, writes = execution_store.write_json_atomic, []

    def second_write_fails(path, obj):  # the first write records the authorization, the second the dispatch
        writes.append(path)
        if len(writes) == 2:
            raise OSError(28, "No space left on device", str(path))
        real(path, obj)

    monkeypatch.setattr(execution_store, "write_json_atomic", second_write_fails)
    state = fx.executor(ws, "notice", oracle).resume("run-1")
    assert state.status is RunStatus.BLOCKED and state.blocked["type"] == "ExecutionLedgerUnavailable"
    message = state.blocked["message"]  # no path, in any spelling (an OSError shows its filename as a repr)
    assert "No space left" in message and tmp_path.name not in message and "executions.json" not in message
    assert notices(oracle) == []  # never dispatched without its durable record
    monkeypatch.undo()
    state = fx.executor(ws, "notice", oracle).resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 1


def test_a_run_cannot_switch_ledgers(tmp_path, oracle, monkeypatch):
    monkeypatch.setattr(fx, "tool_for", lambda kind, o: UnknownOnceNotice(o))  # leaves the run unfinished
    store = in_memory_store()
    ws = fx.workspace(tmp_path / "db")
    ex = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(store, ws))
    ex.start(fx_task(), run_id="run-1")
    fx.approve_pending(ex)
    assert ex.resume("run-1").status is RunStatus.AWAITING_RECONCILIATION
    with pytest.raises(LedgerBindingError, match="with --database"):
        fx.executor(ws, "notice", oracle).resume("run-1")  # without the database it could resend

    ws2, ex2 = started_and_approved(tmp_path / "local", oracle, "notice")
    assert ex2.resume("run-1").status is RunStatus.AWAITING_RECONCILIATION
    with pytest.raises(LedgerBindingError, match="without --database"):
        fx.executor(ws2, "notice", oracle, recorder=fx.recorder_for(in_memory_store(), ws2)).resume("run-1")
    assert len(notices(oracle)) == 2  # one per run; neither was resent by the refused resumes


def test_one_process_at_a_time_changes_a_run(tmp_path, oracle):
    ws, ex = started_and_approved(tmp_path, oracle, "notice")
    holder = FileLock(ws.runs_dir / "run-1" / "run.lock")
    assert holder.try_acquire()
    try:
        with pytest.raises(RunLocked):
            ex.resume("run-1")
        with pytest.raises(RunLocked):
            ex.decide_approval("run-1", "apr-" + "0" * 16, approved=True)
    finally:
        holder.release()
    assert ex.resume("run-1").status is RunStatus.COMPLETED and len(notices(oracle)) == 1


def test_two_runs_of_one_workspace_never_change_its_files_at_the_same_time(tmp_path, oracle):
    """Runs share their workspace's project files: while one runs, another run of the same workspace waits."""
    ws, ex = started_and_approved(tmp_path, oracle, "notice")
    holder = FileLock(ws.root / ".agent_factory" / "workspace.lock")  # another run is changing the files
    assert holder.try_acquire()
    try:
        with pytest.raises(RunLocked, match="another run of this workspace"):
            ex.resume("run-1")
        with pytest.raises(RunLocked, match="another run of this workspace"):
            ex.advance("run-1")
        assert ex.create(fx_task(), run_id="run-2").status is RunStatus.RUNNING  # only its own files: allowed
        assert notices(oracle) == []
    finally:
        holder.release()
    assert ex.resume("run-1").status is RunStatus.COMPLETED and len(notices(oracle)) == 1


def test_a_second_process_cannot_resume_a_run_another_process_holds(tmp_path, oracle):
    ws, _ = started_and_approved(tmp_path, oracle, "notice")
    oracle.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(ws.root), str(oracle), "notice", "resume", "hold"],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while not (oracle / "holding").exists():
            assert child.poll() is None and time.monotonic() < deadline, child.stderr.read() if child.poll() else "timeout"
            time.sleep(0.02)
        with pytest.raises(RunLocked):  # the other process holds the run, mid-dispatch
            fx.executor(fx.workspace(ws.root), "notice", oracle).resume("run-1")
    finally:
        (oracle / "release").write_text("go", encoding="utf-8")
        out, err = child.communicate(timeout=60)
    assert child.returncode == 0, err
    assert out.strip() == "completed" and len(notices(oracle)) == 1


def test_repeated_approval_processing_changes_nothing(tmp_path, oracle):
    ws, ex = started_and_approved(tmp_path, oracle, "notice")
    [request_id] = ex.store.load("run-1").approvals
    with pytest.raises(Exception, match="no pending approval"):
        ex.decide_approval("run-1", request_id, approved=True)  # the same decision delivered twice
    assert ex.resume("run-1").status is RunStatus.COMPLETED and len(notices(oracle)) == 1
    with pytest.raises(Exception, match="no pending approval"):
        ex.decide_approval("run-1", request_id, approved=True)  # and again after it was used
    assert len(notices(oracle)) == 1


# =================================================================== milestones


def test_product_store_milestones_show_the_action_and_the_reconciliation(tmp_path, oracle, monkeypatch):
    store = in_memory_store()
    monkeypatch.setattr(fx, "tool_for", lambda kind, o: UnknownOnceNotice(o))
    ws = fx.workspace(tmp_path / "ws")
    ex = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(store, ws))
    ex.start(fx_task(), run_id="run-1")
    fx.approve_pending(ex)
    ex.resume("run-1")
    actions = store.milestones.query(MilestoneQuery(project_id=ws.project_id, types=frozenset({ACTION_EXECUTION})))
    [blocked] = actions
    assert blocked.status is MilestoneStatus.BLOCKED and "Action outcome unknown" in blocked.summary
    assert "billing@example.test" not in blocked.summary  # never the action's parameters
    assert store.runs.get(ws.project_id, "run-1").status is RunStatus.AWAITING_RECONCILIATION
    pending = ex.store.load("run-1").pending_reconciliation
    monkeypatch.setattr(fx, "tool_for", lambda kind, o: fx.SendNoticeTool(o))
    ex2 = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(store, ws))
    ex2.reconcile_action("run-1", pending["execution_id"], attempt=1, action_fingerprint=pending["action_fingerprint"],
                         choice="it_happened")  # fmt: skip
    ex2.resume("run-1")
    [done] = store.milestones.query(MilestoneQuery(project_id=ws.project_id, types=frozenset({ACTION_EXECUTION})))
    assert done.status is MilestoneStatus.COMPLETED and "confirmed" in done.summary


# ============================================ PostgreSQL: connection loss and real processes


def _killer(pg_schema, store):
    from agent_factory.persistence.postgres import connect

    def kill():
        admin = connect(pg_schema)
        try:
            admin.execute("SELECT pg_terminate_backend(%s)", (store.unit_of_work.conn.info.backend_pid,))
        finally:
            admin.close()

    return kill


@pytest.mark.postgres
def test_losing_the_database_before_the_write_ahead_commit_dispatches_nothing(tmp_path, oracle, pg_open, pg_schema, monkeypatch):
    from agent_factory.domain.executions import ExecutionLedger

    store = pg_open()
    ws = fx.workspace(tmp_path / "ws")
    ex = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(store, ws))
    ex.start(fx_task(), run_id="run-1")
    fx.approve_pending(ex)
    kill, real_begin = _killer(pg_schema, store), ExecutionLedger.begin

    def begin(self, *a, **k):
        kill()  # the connection dies just before the dispatch would be recorded
        return real_begin(self, *a, **k)

    monkeypatch.setattr(ExecutionLedger, "begin", begin)
    with pytest.raises(StorageUnavailableError):
        ex.resume("run-1")  # recording the blocked status needs the database too: the outage surfaces
    assert notices(oracle) == []  # nothing was dispatched without a durable record
    assert ex.store.load("run-1").status is RunStatus.BLOCKED  # the checkpoint says why the run stopped
    monkeypatch.setattr(ExecutionLedger, "begin", real_begin)
    fresh = pg_open()
    state = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(fresh, ws)).resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 1


@pytest.mark.postgres
def test_losing_the_database_after_the_side_effect_never_resends_it(tmp_path, oracle, pg_open, pg_schema, monkeypatch):
    store = pg_open()
    ws = fx.workspace(tmp_path / "ws")
    kill = _killer(pg_schema, store)

    class KillingNotice(fx.SendNoticeTool):
        def run(self, workspace, *, to, text):
            result = super().run(workspace, to=to, text=text)
            kill()  # the database goes away after the message is out
            return result

    monkeypatch.setattr(fx, "tool_for", lambda kind, o: KillingNotice(o))
    ex = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(store, ws))
    ex.start(fx_task(), run_id="run-1")
    fx.approve_pending(ex)
    with pytest.raises(StorageUnavailableError):
        ex.resume("run-1")  # blocking the run needs the database too: the outage surfaces as an error
    assert len(notices(oracle)) == 1
    monkeypatch.setattr(fx, "tool_for", lambda kind, o: fx.SendNoticeTool(o))
    fresh = pg_open()
    state = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(fresh, ws)).resume("run-1")
    assert state.status is RunStatus.COMPLETED and len(notices(oracle)) == 1  # recovered from the log, not resent


@pytest.mark.postgres
@pytest.mark.parametrize("fault,final,sent", [
    ("after_tool_before_outcome", RunStatus.AWAITING_RECONCILIATION, 1),
    ("after_dispatch_record", RunStatus.AWAITING_RECONCILIATION, 0),
    ("after_outcome", RunStatus.COMPLETED, 1),
])  # fmt: skip
def test_real_process_death_with_the_postgresql_ledger(tmp_path, oracle, pg_open, pg_schema, fault, final, sent):
    store = pg_open()
    ws = fx.workspace(tmp_path / "ws")
    ex = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(store, ws))
    ex.start(fx_task(), run_id="run-1")
    fx.approve_pending(ex)
    child = run_child(ws.root, oracle, "notice", "resume", fault, env={"CRASH_CHILD_SCHEMA": pg_schema.schema})
    assert child.returncode == 137, child.stderr
    fresh = pg_open()
    state = fx.executor(ws, "notice", oracle, recorder=fx.recorder_for(fresh, ws)).resume("run-1")
    assert state.status is final and len(notices(oracle)) == sent
    assert fresh.runs.get(ws.project_id, "run-1").status is final

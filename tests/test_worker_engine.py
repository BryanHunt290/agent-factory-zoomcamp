"""The executor under a worker's ownership (no database): what a claim protects inside the engine.

A scripted ownership stands in for a worker's claim, so each protected
boundary can be hit exactly: the proof before every checkpoint write, the
proof immediately before a dispatch, the hand-back at node boundaries, and
the rule that a lost ownership writes nothing more. The database side of the
same guarantee (fenced writes) is proved against PostgreSQL in
tests/test_postgres_claims.py.
"""

from pathlib import Path

import action_fixtures as fx
import pytest

from agent_factory.agents.base import Agent, AgentResult
from agent_factory.demo import scenario_provider
from agent_factory.domain.claims import OwnershipLost, new_worker_id
from agent_factory.domain.executions import ExecutionState
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.engine.executor import ExecutorError
from agent_factory.factory import build_executor
from agent_factory.state import RunStatus, TaskSpec
from agent_factory.templates import CALCULATOR_TEMPLATE

WORKER = new_worker_id()
OUTCOME_EVENTS = {"run_failed", "run_blocked", "run_exhausted", "run_completed", "run_paused"}


class ScriptedOwnership:
    """A worker's claim, scripted: ``verify`` fails from call ``fail_at`` on; ``should_yield`` after ``yield_after`` boundaries."""

    def __init__(self, run_id="run-1", token=7, *, fail_at=None, yield_after=None, on_verify=None):
        self.run_id = run_id
        self.owner = f"{WORKER}@{token}"
        self.fail_at = fail_at
        self.yield_after = yield_after
        self.on_verify = on_verify
        self.verifies = 0
        self.boundaries = 0

    def verify(self):
        self.verifies += 1
        if self.on_verify is not None:
            self.on_verify(self.verifies)
        if self.fail_at is not None and self.verifies >= self.fail_at:
            raise OwnershipLost(f"generation {self.owner} was superseded", reason="superseded")

    def should_yield(self):
        self.boundaries += 1
        return self.yield_after is not None and self.boundaries > self.yield_after


def created_calculator_run(tmp_path):
    """A calculator run created (first checkpoint only), as ``worker enqueue`` creates one."""
    ws = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
    ex = build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), ws)
    state = ex.create(CALCULATOR_TEMPLATE.task, run_id="run-1", metadata={"provider": "fake", "scenario": "repair"})
    assert (state.status, state.step) == (RunStatus.RUNNING, 0)
    return ws


def claimed(ws, ownership):
    return build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), ws, ownership=ownership)


def checkpoint_bytes(ws, run_id="run-1"):
    return CheckpointStore(ws.runs_dir).path(run_id).read_bytes()


def events(ex, run_id="run-1"):
    return [e["event"] for e in ex.log_for(run_id).entries()]


# ---------------------------------------------------------------- checkpoints


def test_ownership_is_proved_before_every_checkpoint_write(tmp_path, monkeypatch):
    ws = created_calculator_run(tmp_path)
    ownership = ScriptedOwnership()
    proofs_at_save = []
    real_save = CheckpointStore.save

    def save(self, state):
        proofs_at_save.append(ownership.verifies)
        return real_save(self, state)

    monkeypatch.setattr(CheckpointStore, "save", save)
    state = claimed(ws, ownership).advance("run-1")
    assert state.status is RunStatus.COMPLETED and len(proofs_at_save) >= 4
    # One proof when the run lock is taken, then a fresh one before every save (an outcome is proved before
    # it is logged, and again before its save).
    assert proofs_at_save[0] >= 2
    assert all(later > earlier for earlier, later in zip(proofs_at_save, proofs_at_save[1:], strict=False))


def test_a_lost_ownership_writes_no_checkpoint_and_records_no_outcome(tmp_path, monkeypatch):
    ws = created_calculator_run(tmp_path)
    committed = {}
    real_save = CheckpointStore.save

    def save(self, state):
        path = real_save(self, state)
        committed["bytes"], committed["step"] = path.read_bytes(), state.step
        return path

    monkeypatch.setattr(CheckpointStore, "save", save)
    ownership = ScriptedOwnership(fail_at=4)  # the lock, two committed nodes, then lost
    ex = claimed(ws, ownership)
    with pytest.raises(OwnershipLost):
        ex.advance("run-1")
    assert committed["step"] == 2
    assert checkpoint_bytes(ws) == committed["bytes"]  # the third node's result was never committed
    stored = CheckpointStore(ws.runs_dir).load("run-1")
    assert (stored.status, stored.step) == (RunStatus.RUNNING, 2)
    assert not OUTCOME_EVENTS & set(events(ex))  # not failed, not blocked: it simply stopped
    # The next owner re-runs the uncommitted node, as after a crash, and finishes the run.
    assert claimed(ws, ScriptedOwnership(token=8)).advance("run-1").status is RunStatus.COMPLETED


class SwallowingAgent(Agent):
    """An agent that catches every Exception around a call that loses ownership."""

    name = "planner"
    writes = frozenset({"plan"})

    def run(self, ctx):
        try:
            raise OwnershipLost("the store refused the write: generation 7 was superseded", reason="superseded")
        except Exception:  # noqa: BLE001 - the defect this test guards against
            return AgentResult("planned", {"plan": {}}, summary="swallowed")


def test_an_agent_cannot_swallow_a_lost_ownership_and_the_run_is_not_marked_failed(tmp_path):
    ws = created_calculator_run(tmp_path)
    before = checkpoint_bytes(ws)
    ex = claimed(ws, ScriptedOwnership())
    ex.agents["planner"] = SwallowingAgent()
    with pytest.raises(OwnershipLost):
        ex.advance("run-1")
    assert checkpoint_bytes(ws) == before
    assert not OUTCOME_EVENTS & set(events(ex))


# ------------------------------------------------------------------- yielding


def test_the_executor_hands_the_run_back_between_nodes_and_the_next_owner_continues(tmp_path):
    ws = created_calculator_run(tmp_path)
    first = claimed(ws, ScriptedOwnership(token=7, yield_after=1))
    state = first.advance("run-1")
    assert (state.status, state.step) == (RunStatus.RUNNING, 1)
    stored = CheckpointStore(ws.runs_dir).load("run-1")
    assert (stored.status, stored.step) == (RunStatus.RUNNING, 1)  # everything before the hand-back is committed
    yielded = [e for e in first.log_for("run-1").entries() if e["event"] == "run_yielded"]
    assert yielded and yielded[0]["owner"] == f"{WORKER}@7" and yielded[0]["step"] == 1
    second = claimed(ws, ScriptedOwnership(token=8))
    assert second.advance("run-1").status is RunStatus.COMPLETED
    owners = [e.get("owner") for e in second.log_for("run-1").entries() if e["event"] == "run_resumed"]
    assert owners == [f"{WORKER}@7", f"{WORKER}@8"]


# ------------------------------------------------------------- wrong use refused


def test_an_ownership_of_another_run_is_refused_before_anything_is_touched(tmp_path):
    ws = created_calculator_run(tmp_path)
    before, log_before = checkpoint_bytes(ws), events(claimed(ws, None))
    ownership = ScriptedOwnership(run_id="run-2")
    with pytest.raises(OwnershipLost) as lost:
        claimed(ws, ownership).advance("run-1")
    assert lost.value.reason == "wrong_run" and ownership.verifies == 0
    assert checkpoint_bytes(ws) == before and events(claimed(ws, None)) == log_before


def test_a_claimed_executor_never_creates_a_run(tmp_path):
    ws = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
    ex = claimed(ws, ScriptedOwnership())
    for create in (ex.start, ex.create):
        with pytest.raises(ExecutorError, match="never creates"):
            create(CALCULATOR_TEMPLATE.task, run_id="run-1")
    assert not CheckpointStore(ws.runs_dir).exists("run-1")


# ------------------------------------------------- runs a worker must not continue


def test_advance_leaves_a_run_waiting_for_a_person_untouched(tmp_path):
    oracle = tmp_path / "oracle"
    ws = fx.workspace(tmp_path / "ws")
    paused = fx.executor(ws, "notice", oracle).start(TaskSpec(title="fixture", goal="g"), run_id="run-1")
    assert paused.status is RunStatus.AWAITING_APPROVAL
    before, log_before = checkpoint_bytes(ws), fx.executor(ws, "notice", oracle).log_for("run-1").entries()
    ex = fx.executor(ws, "notice", oracle)
    ex.ownership = ScriptedOwnership()
    state = ex.advance("run-1")
    assert state.status is RunStatus.AWAITING_APPROVAL
    assert checkpoint_bytes(ws) == before and ex.log_for("run-1").entries() == log_before  # nothing written
    assert ex.ownership.verifies == 1  # the claim was still checked when the run lock was taken


def test_advance_leaves_blocked_and_finished_runs_untouched(tmp_path):
    ws = created_calculator_run(tmp_path)
    store = CheckpointStore(ws.runs_dir)
    for status in (RunStatus.BLOCKED, RunStatus.COMPLETED, RunStatus.FAILED):
        state = store.load("run-1")
        state.status = status
        state.blocked = {"type": "SandboxBlocked", "message": "m", "node": "plan", "step": 0, "at": state.updated_at} if status is RunStatus.BLOCKED else None
        store.save(state)
        before = checkpoint_bytes(ws)
        assert claimed(ws, ScriptedOwnership()).advance("run-1").status is status
        assert checkpoint_bytes(ws) == before


# ------------------------------------------------------------------ dispatch


def test_no_dispatch_when_ownership_is_lost_after_the_write_ahead_record(tmp_path):
    """The last check before a side effect: ownership lost between the write-ahead commit and the tool."""
    oracle = tmp_path / "oracle"
    ws = fx.workspace(tmp_path / "ws")
    ex = fx.executor(ws, "notice", oracle)
    ex.start(TaskSpec(title="fixture", goal="g"), run_id="run-1")
    fx.approve_pending(ex)
    seen = {}

    def at_verify(n):
        if n == 2:  # the check right before the tool: the write-ahead record must already exist
            (execution,) = ex.ledger.list_for_run(ws.project_id, "run-1")
            seen["state"], seen["owner"] = execution.state, execution.owner

    stale = fx.executor(ws, "notice", oracle)
    stale.ownership = ScriptedOwnership(token=7, fail_at=2, on_verify=at_verify)
    with pytest.raises(OwnershipLost):
        stale.advance("run-1")
    assert fx.read_lines(oracle / "notices.jsonl") == []  # NOT dispatched
    assert seen == {"state": ExecutionState.DISPATCHING, "owner": f"{WORKER}@7"}
    log = [e for e in stale.log_for("run-1").entries() if e["event"].startswith("action_")]
    assert [e["event"] for e in log][-2:] == ["action_dispatching", "action_not_dispatched"]
    assert log[-1]["error"]["type"] == "OwnershipLost" and log[-2]["owner"] == f"{WORKER}@7"

    # The next owner recovers the open attempt from that evidence and sends the notice exactly once.
    nxt = fx.executor(ws, "notice", oracle)
    nxt.ownership = ScriptedOwnership(token=8)
    assert nxt.advance("run-1").status is RunStatus.COMPLETED
    assert len(fx.read_lines(oracle / "notices.jsonl")) == 1
    (execution,) = nxt.ledger.list_for_run(ws.project_id, "run-1")
    attempts = nxt.ledger.attempts(execution)
    assert [(a.owner, a.result.value) for a in attempts] == [(f"{WORKER}@7", "not_dispatched"), (f"{WORKER}@8", "succeeded")]
    recovered = [e for e in nxt.log_for("run-1").entries() if e["event"] == "action_recovered"]
    assert recovered[0]["source"] == "execution_log" and recovered[0]["previous_owner"] == f"{WORKER}@7"


def test_ownership_proved_at_dispatch_does_not_change_runs_without_an_owner(tmp_path):
    """Without an ownership the executor behaves as before E4 (the single-host run lock only)."""
    oracle = tmp_path / "oracle"
    ws = fx.workspace(tmp_path / "ws")
    ex = fx.executor(ws, "notice", oracle)
    assert ex.ownership is None
    ex.start(TaskSpec(title="fixture", goal="g"), run_id="run-1")
    fx.approve_pending(ex)
    assert ex.resume("run-1").status is RunStatus.COMPLETED and len(fx.read_lines(oracle / "notices.jsonl")) == 1
    (execution,) = ex.ledger.list_for_run(ws.project_id, "run-1")
    assert not ex.ledger.attempts(execution)[0].owner.startswith("wkr-")


def test_create_writes_the_first_checkpoint_without_running_anything(tmp_path):
    ws = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
    ex = build_executor(CALCULATOR_TEMPLATE, scenario_provider("repair"), ws)
    state = ex.create(CALCULATOR_TEMPLATE.task, run_id="run-1", metadata={"scenario": "repair"})
    assert (state.status, state.step, state.history) == (RunStatus.RUNNING, 0, [])
    assert events(ex) == ["run_started"]
    with pytest.raises(ExecutorError, match="already exists"):
        ex.create(CALCULATOR_TEMPLATE.task, run_id="run-1")
    assert Path(CheckpointStore(ws.runs_dir).path("run-1")).is_file()


class BlockedPlanner(Agent):
    """A first node whose code cannot run safely: the executor would block the run."""

    name = "planner"
    writes = frozenset({"plan"})

    def run(self, ctx):
        from agent_factory.sandbox.types import SandboxBlocked

        raise SandboxBlocked("the sandbox is unavailable")


def test_a_stale_worker_records_no_outcome_not_even_blocked(tmp_path):
    ws = created_calculator_run(tmp_path)
    before = checkpoint_bytes(ws)
    ex = claimed(ws, ScriptedOwnership(fail_at=2))  # proved at the lock; lost before the outcome is recorded
    ex.agents["planner"] = BlockedPlanner()
    with pytest.raises(OwnershipLost):
        ex.advance("run-1")
    assert checkpoint_bytes(ws) == before
    assert not OUTCOME_EVENTS & set(events(ex))  # no run_blocked: only a proven owner records an outcome
    blocked = claimed(ws, ScriptedOwnership(token=8))
    blocked.agents["planner"] = BlockedPlanner()
    assert blocked.advance("run-1").status is RunStatus.BLOCKED  # the same path, for the current owner


def _imports(path):
    import ast

    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    return names


def test_the_worker_layers_keep_their_boundaries():
    """The engine and domain never reach a database driver; the worker loop works with any claim store;
    workers never touch authentication code (they are not users)."""
    root = Path(__file__).resolve().parent.parent / "agent_factory"
    offenders = []
    for package in ("engine", "domain"):
        for path in (root / package).rglob("*.py"):
            for name in _imports(path):
                if name.startswith("psycopg") or "persistence" in name.split("."):
                    offenders.append(f"{path.name}: {name}")
    for module in ("__init__", "loop", "lease", "events", "identity"):
        for name in _imports(root / "worker" / f"{module}.py"):
            if name.startswith("psycopg") or "persistence" in name.split("."):
                offenders.append(f"worker/{module}.py: {name}")
    for path in (root / "worker").rglob("*.py"):
        for name in _imports(path):
            if name.startswith(("jwt", "cryptography")) or "api" in name.split("."):
                offenders.append(f"worker/{path.name}: {name}")
    assert offenders == []

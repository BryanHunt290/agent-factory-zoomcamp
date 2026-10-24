"""The engine with a PostgreSQL product store: restarts, reconciliation, unchanged behaviour.

    .venv\\Scripts\\python -m pytest -m postgres --run-postgres    (docs/database.md)
"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_factory.agents import TesterAgent, default_agents
from agent_factory.demo import scenario_provider
from agent_factory.domain import ApprovalOutcome, ApprovalStatus, ConflictError, MilestoneQuery, MilestoneStatus, in_memory_store
from agent_factory.domain.errors import StorageUnavailableError
from agent_factory.engine import ApprovalPending
from agent_factory.factory import build_executor
from agent_factory.state import RunStatus
from agent_factory.templates import CALCULATOR_TEMPLATE as T

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parent.parent


def _milestones(store, ws, run_id=None):
    return store.milestones.query(MilestoneQuery(project_id=ws.project_id, run_id=run_id))


def _shape(milestones):
    return [(m.key, m.type, m.status.value, m.sequence) for m in milestones]


def _reference(tmp_path, scenario):
    store = in_memory_store()
    ws = T.create_workspace(tmp_path / f"ref-{scenario}")
    build_executor(T, scenario_provider(scenario), ws, product_store=store).start(T.task, run_id="run-1")
    return _shape(_milestones(store, ws))


@pytest.mark.parametrize("scenario", ["repair", "review", "full", "exhaust"])
def test_graph_behaviour_is_unchanged_and_records_match_the_in_memory_store(pg_open, tmp_path, scenario):
    plain_ws = T.create_workspace(tmp_path / "plain")
    plain_ex = build_executor(T, scenario_provider(scenario), plain_ws)
    plain = plain_ex.start(T.task, run_id="run-1")
    store = pg_open()
    ws = T.create_workspace(tmp_path / "pg")
    ex = build_executor(T, scenario_provider(scenario), ws, product_store=store)
    state = ex.start(T.task, run_id="run-1")
    assert state.status is plain.status
    assert [(h["node"], h["outcome"]) for h in state.history] == [(h["node"], h["outcome"]) for h in plain.history]
    events = [(e["event"], e.get("node"), e.get("tool")) for e in ex.log_for("run-1").entries()]
    assert events == [(e["event"], e.get("node"), e.get("tool")) for e in plain_ex.log_for("run-1").entries()]
    assert _shape(_milestones(store, ws)) == _reference(tmp_path, scenario)
    assert store.runs.get(ws.project_id, "run-1").status is state.status


def _cli(*args, env):
    return subprocess.run([sys.executable, "-m", "agent_factory", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=300)  # fmt: skip


def test_product_records_survive_agent_factory_restarts(pg_schema, pg_open, tmp_path):
    """Three separate processes - start, approve, resume - share one durable record of the run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_FACTORY_DATABASE")}
    env.update(AGENT_FACTORY_DATABASE_URL=pg_schema.url, AGENT_FACTORY_DATABASE_SCHEMA=pg_schema.schema)
    ws_dir = tmp_path / "restart-ws"
    started = _cli("demo", "--scenario", "approval", "--workspace", str(ws_dir), "--no-git", "--database", env=env)
    assert started.returncode == 0, started.stderr
    assert pg_schema.url not in started.stdout + started.stderr  # the URL (and its password) is never printed
    checkpoint = json.loads((ws_dir / ".agent_factory" / "runs" / "demo" / "checkpoint.json").read_text(encoding="utf-8"))
    request_id = checkpoint["pending_approval"]["request_id"]

    approved = _cli("approve", "--workspace", str(ws_dir), "--run-id", "demo", "--request-id", request_id,
                    "--approver", "alice", "--note", "dead code", "--database", env=env)  # fmt: skip
    assert approved.returncode == 0, approved.stderr
    resumed = _cli("resume", "--workspace", str(ws_dir), "--run-id", "demo", "--database", env=env)
    assert resumed.returncode == 0, resumed.stderr

    from agent_factory.workspace import Workspace

    ws = Workspace.open(ws_dir)
    store = pg_open()  # this test process never wrote anything: everything below came from the three processes
    project = store.projects.get(ws.project_id)
    assert project is not None and project.template == "calculator"
    run = store.runs.get(ws.project_id, "demo")
    assert run.status is RunStatus.COMPLETED and run.number == 1
    request = store.approvals.get_request(ws.project_id, request_id)
    assert request.status is ApprovalStatus.APPROVED and request.action.params == {"path": "legacy_calc.py"}
    decision = store.approvals.get_decision(ws.project_id, request_id)
    assert decision.outcome is ApprovalOutcome.APPROVED and decision.actor.id == "local:alice"
    assert decision.comment == "dead code" and decision.action_fingerprint == request.action_fingerprint
    milestones = _milestones(store, ws, "demo")
    assert [m.sequence for m in milestones] == list(range(1, len(milestones) + 1))
    assert [m.type for m in milestones if m.type == "human_decision"] == ["human_decision"]
    assert milestones[-1].type == "run_finished" and milestones[-1].status is MilestoneStatus.COMPLETED


class _TesterThatLosesTheDatabase(TesterAgent):
    """Runs the real tests, but the product database connection dies while it does."""

    kill = None

    def run(self, ctx):
        if _TesterThatLosesTheDatabase.kill is not None:
            _TesterThatLosesTheDatabase.kill()
            _TesterThatLosesTheDatabase.kill = None
        return super().run(ctx)


def test_reconcile_after_the_database_failed_mid_run(pg_open, pg_schema, tmp_path):
    """Checkpoint succeeds, database write fails: the error surfaces, and reconciliation fills the gap."""
    from agent_factory.persistence.postgres import connect

    store = pg_open()

    def kill():
        admin = connect(pg_schema)
        try:
            admin.execute("SELECT pg_terminate_backend(%s)", (store.unit_of_work.conn.info.backend_pid,))
        finally:
            admin.close()

    template = replace(T, agent_factory=lambda: {**default_agents(), "tester": _TesterThatLosesTheDatabase()})
    _TesterThatLosesTheDatabase.kill = kill
    ws = template.create_workspace(tmp_path / "outage")
    with pytest.raises(StorageUnavailableError):
        build_executor(template, scenario_provider("repair"), ws, product_store=store).start(template.task, run_id="run-1")
    restarted_store = pg_open()  # a new process would open a new connection
    ex = build_executor(template, scenario_provider("repair"), ws, product_store=restarted_store)
    committed = ex.store.load("run-1")
    assert committed.step == 3  # the tester's step was checkpointed even though recording it failed
    recorded = _milestones(restarted_store, ws)
    assert [m.key for m in recorded] == ["step:0", "step:1"]  # the product database is behind the checkpoint
    ex.reconcile("run-1")
    assert [m.key for m in _milestones(restarted_store, ws)] == ["step:0", "step:1", "step:2"]  # gap filled
    assert ex.resume("run-1").status is RunStatus.COMPLETED
    assert _shape(_milestones(restarted_store, ws)) == _reference(tmp_path, "repair")


def test_a_decision_stored_before_a_crash_is_applied_by_reconcile(pg_open, tmp_path, monkeypatch):
    store = pg_open()
    ws = T.create_workspace(tmp_path / "crash")
    ex = build_executor(T, scenario_provider("approval"), ws, product_store=store)
    request_id = ex.start(T.task, run_id="run-1").pending_approval["request_id"]

    def crash(state):
        raise OSError("simulated crash before the checkpoint was written")

    monkeypatch.setattr(ex.store, "save", crash)
    with pytest.raises(OSError):
        ex.decide_approval("run-1", request_id, approved=True, approver="alice")
    monkeypatch.undo()
    # Database: APPROVED. Engine checkpoint: still waiting. The action has not run.
    restarted_store = pg_open()
    assert restarted_store.approvals.get_decision(ws.project_id, request_id).outcome is ApprovalOutcome.APPROVED
    restarted = build_executor(T, scenario_provider("approval"), ws, product_store=restarted_store)
    assert restarted.store.load("run-1").status is RunStatus.AWAITING_APPROVAL
    assert (ws.project_dir / "legacy_calc.py").exists()
    with pytest.raises(ApprovalPending):
        restarted.resume("run-1")
    with pytest.raises(ConflictError):
        restarted.decide_approval("run-1", request_id, approved=False, approver="mallory")
    assert restarted.reconcile("run-1").approvals[request_id]["approved"] is True
    assert restarted.resume("run-1").status is RunStatus.COMPLETED
    assert not (ws.project_dir / "legacy_calc.py").exists()


class _FailingMilestones:
    """Wraps the PostgreSQL milestone repository; the n-th add fails like a dropped connection."""

    def __init__(self, inner, fail_on):
        self.inner, self.adds, self.fail_on = inner, 0, fail_on

    def add(self, milestone):
        self.adds += 1
        if self.adds == self.fail_on:
            raise StorageUnavailableError("simulated outage")
        self.inner.add(milestone)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_sync_that_fails_halfway_leaves_no_partial_records(pg_open, tmp_path):
    store = pg_open()
    flaky = replace(store, milestones=_FailingMilestones(store.milestones, fail_on=7))  # run_finished
    ws = T.create_workspace(tmp_path / "partial")
    with pytest.raises(StorageUnavailableError):
        build_executor(T, scenario_provider("repair"), ws, product_store=flaky).start(T.task, run_id="run-1")
    fresh = pg_open()
    # The final sync changed the run's status and then failed adding its milestone: both rolled back.
    assert fresh.runs.get(ws.project_id, "run-1").status is RunStatus.RUNNING
    assert "run_finished" not in [m.type for m in _milestones(fresh, ws)]
    build_executor(T, scenario_provider("repair"), ws, product_store=fresh).reconcile("run-1")
    assert fresh.runs.get(ws.project_id, "run-1").status is RunStatus.COMPLETED
    assert _shape(_milestones(fresh, ws)) == _reference(tmp_path, "repair")

"""Operator commands with --database never race a worker (E5, docs/workers.md "operator commands").

    .venv\\Scripts\\python -m pytest -m postgres --run-postgres tests/test_cli_decisions.py

* ``approve --database`` and ``reconcile-action --database`` record intent
  only (the decision and the run's wake-up, one transaction), like the API.
* ``resume --database`` claims the run like a worker, applies the decision
  through the same re-checks under that claim, and releases it; it refuses
  while a worker's lease is valid.

Every command runs as a separate process, as an operator would run it.
Without --database the commands act on the workspace directly
(tests/test_cli.py).
"""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from decision_support import Flow

from agent_factory.domain.claims import LeasePolicy, new_worker_id
from agent_factory.domain.executions import ExecutionState
from agent_factory.domain.identity import ActorKind
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.state import RunStatus
from agent_factory.workspace import Workspace

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli(pg_schema):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_FACTORY_DATABASE")}
    env.update(AGENT_FACTORY_DATABASE_URL=pg_schema.url, AGENT_FACTORY_DATABASE_SCHEMA=pg_schema.schema,
               PYTHONDONTWRITEBYTECODE="1")  # fmt: skip

    def run(*args):
        done = subprocess.run([sys.executable, "-m", "agent_factory", *args], cwd=REPO, env=env, capture_output=True,
                              text=True, timeout=300)  # fmt: skip
        assert pg_schema.url not in done.stdout + done.stderr  # never printed
        return done

    return run


@pytest.fixture
def admin(pg_schema):
    from agent_factory.persistence.postgres import connect

    conn = connect(pg_schema)
    yield conn
    conn.close()


@pytest.fixture
def paused_demo(cli, tmp_path):
    ws_dir = tmp_path / "ws"
    started = cli("demo", "--scenario", "approval", "--workspace", str(ws_dir), "--no-git", "--database")
    assert started.returncode == 0, started.stderr
    state = CheckpointStore(Workspace.open(ws_dir).runs_dir).load("demo")
    assert state.status is RunStatus.AWAITING_APPROVAL
    return ws_dir, state.pending_approval["request_id"]


def checkpoint(ws_dir):
    return CheckpointStore(Workspace.open(ws_dir).runs_dir).load("demo")


def log(ws_dir):
    path = Workspace.open(ws_dir).runs_dir / "demo" / "execution_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_approve_with_database_records_the_decision_and_wakes_the_run_but_changes_nothing_else(
    cli, paused_demo, pg_store, admin
):
    ws_dir, request_id = paused_demo
    before = checkpoint(ws_dir)
    approved = cli("approve", "--workspace", str(ws_dir), "--run-id", "demo", "--request-id", request_id,
                   "--approver", "alice", "--database")  # fmt: skip
    assert approved.returncode == 0, approved.stderr
    assert "recorded; a worker applies it" in approved.stdout
    after = checkpoint(ws_dir)
    assert (after.status, after.pending_approval, after.approvals) == (before.status, before.pending_approval, {})
    project_id = Workspace.open(ws_dir).project_id
    decision = pg_store.approvals.get_decision(project_id, request_id)
    assert decision.actor.kind is ActorKind.UNAUTHENTICATED and decision.actor.id == "local:alice"
    assert admin.execute("SELECT available_at <= now(), worker_id FROM run_claims").fetchone() == (True, None)


def test_resume_with_database_claims_the_run_and_applies_the_decision_through_the_same_checks(cli, paused_demo, admin):
    ws_dir, request_id = paused_demo
    cli("approve", "--workspace", str(ws_dir), "--run-id", "demo", "--request-id", request_id, "--database")
    token = admin.execute("SELECT fencing_token FROM run_claims").fetchone()[0]
    resumed = cli("resume", "--workspace", str(ws_dir), "--run-id", "demo", "--database")
    assert resumed.returncode == 0, resumed.stderr
    assert checkpoint(ws_dir).status is RunStatus.COMPLETED
    assert admin.execute("SELECT fencing_token, worker_id FROM run_claims").fetchone() == (token + 1, None)  # claimed, released
    [applied] = [e for e in log(ws_dir) if e["event"] == "approval_decision"]
    assert applied["source"] == "product_store" and applied["owner"].startswith("wkr-")  # under a claim, by an identity
    assert applied["owner"].endswith(f"@{token + 1}")


def test_an_operator_command_refuses_a_run_a_worker_holds_and_changes_nothing(cli, paused_demo, pg_schema, admin):
    from agent_factory.persistence.claims import open_claim_repository

    ws_dir, request_id = paused_demo
    cli("approve", "--workspace", str(ws_dir), "--run-id", "demo", "--request-id", request_id, "--database")
    claims = open_claim_repository(pg_schema)
    try:
        held = claims.claim_run(Workspace.open(ws_dir).project_id, "demo", new_worker_id(), LeasePolicy(lease=timedelta(minutes=5),
                                heartbeat_interval=timedelta(minutes=1)))  # fmt: skip
        before = checkpoint(ws_dir)
        refused = cli("resume", "--workspace", str(ws_dir), "--run-id", "demo", "--database")
        assert refused.returncode == 2 and "worker" in refused.stderr.lower(), (refused.stdout, refused.stderr)
        assert checkpoint(ws_dir) == before
        assert admin.execute("SELECT worker_id FROM run_claims").fetchone()[0] == held.fence.worker_id  # untouched
        assert claims.release(held.fence, requeue=True)
    finally:
        claims.close()
    resumed = cli("resume", "--workspace", str(ws_dir), "--run-id", "demo", "--database")
    assert resumed.returncode == 0, resumed.stderr
    assert checkpoint(ws_dir).status is RunStatus.COMPLETED


def test_reconcile_action_with_database_records_the_answer_only_and_a_worker_applies_it(cli, tmp_path, pg_schema, pg_store):
    flow = Flow.postgres(tmp_path, pg_schema, pg_store, "unknown-notice")
    flow.uncertain()
    execution = flow.execution()
    answered = cli(
        "reconcile-action", "--workspace", str(flow.ws.root), "--run-id", "run-1", "--execution-id", execution.id,
        "--attempt", "1", "--fingerprint", execution.action_fingerprint, "--choice", "it-happened", "--approver", "dana",
        "--database",
    )  # fmt: skip
    assert answered.returncode == 0, answered.stderr
    assert "a worker applies it" in answered.stdout
    recorded = pg_store.reconciliations.get(flow.project_id, execution.id, 1)
    assert recorded.actor.id == "local:dana" and recorded.choice.value == "it_happened"
    assert flow.execution().state is ExecutionState.UNCERTAIN  # recording is not applying
    assert flow.checkpoint().status is RunStatus.AWAITING_RECONCILIATION
    flow.work()
    assert flow.execution().state is ExecutionState.SUCCEEDED and flow.notices() == 1
    assert flow.checkpoint().status is RunStatus.COMPLETED

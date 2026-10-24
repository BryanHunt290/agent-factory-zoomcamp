"""Worker coordination with real processes: one owner, death and takeover, a zombie fenced out, shutdown.

    .venv\\Scripts\\python -m pytest -m postgres --run-postgres tests/test_worker_processes.py

Each worker here is a separate Python process (tests/worker_child.py) with
its own database connections, sharing one PostgreSQL test schema and one
workspace directory with the test process, as workers on one host do. Deaths
are real (``os._exit(137)`` or the operating system killing the process).
Effects are counted in the fixtures' oracle files, never by asking Agent
Factory.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import action_fixtures as fx
import pytest
from worker_support import NoticeSource, approved_notice_run, lease_expired, new_calculator_run, wait_until

from agent_factory.domain.claims import LeasePolicy, new_worker_id
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.state import RunStatus, TaskSpec
from agent_factory.worker import Worker, WorkerEvents, WorkerIdentity, WorkerSettings

pytestmark = pytest.mark.postgres

CHILD = Path(__file__).resolve().parent / "worker_child.py"
REPO = Path(__file__).resolve().parent.parent
POLICY = LeasePolicy()


@pytest.fixture
def admin(pg_schema):
    from agent_factory.persistence.postgres import connect

    conn = connect(pg_schema)
    yield conn
    conn.close()


@pytest.fixture
def claims(pg_schema):
    from agent_factory.persistence.claims import open_claim_repository

    repo = open_claim_repository(pg_schema)
    yield repo
    repo.close()


@pytest.fixture
def spawn(pg_schema):
    """Start worker_child.py processes on the test schema; every one is gone after the test."""
    procs = []

    def start(*args, new_group=False):
        env = {**os.environ, "WORKER_CHILD_SCHEMA": pg_schema.schema, "PYTHONDONTWRITEBYTECODE": "1"}
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if (new_group and os.name == "nt") else 0
        proc = subprocess.Popen(
            [sys.executable, str(CHILD), *map(str, args)], cwd=REPO, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=flags,
        )  # fmt: skip
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=30)


def finish(proc, timeout=60):
    out, err = proc.communicate(timeout=timeout)
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    report = next((json.loads(line[7:]) for line in out.splitlines() if line.startswith("REPORT ")), None)
    return SimpleNamespace(code=proc.returncode, events=events, report=report, err=err, out=out)


def worker_in_this_process(pg_schema, source, *, events=None, **settings):
    from agent_factory.persistence.claims import open_claim_repository

    identity = WorkerIdentity.new()
    return Worker(
        identity, WorkerSettings(**{"poll_interval": 0.05, "max_runs": 1, **settings}),
        open_claims=lambda: open_claim_repository(pg_schema), source=source,
        events=WorkerEvents(identity.worker_id, sink=events.append if events is not None else None),
    )  # fmt: skip


@pytest.fixture
def calculator_run(tmp_path, pg_store, claims):
    base = tmp_path / "workspaces"
    ws = new_calculator_run(pg_store, base)
    claims.enqueue(ws.project_id, "run-1")
    return SimpleNamespace(base=base, ws=ws, oracle=tmp_path / "oracle", project_id=ws.project_id)


def claim_of(claims, project_id):
    return claims.get(project_id, "run-1")


# ============================================================ claiming deaths


def test_a_worker_dying_inside_its_claim_transaction_claims_nothing(calculator_run, spawn, claims):
    r = calculator_run
    child = spawn("die-in-claim", r.oracle)
    wait_until(lambda: (r.oracle / "claiming").exists())
    assert json.loads((r.oracle / "claiming").read_text())["run_id"] == "run-1"  # its claim statement ran...
    assert claims.claim_next(new_worker_id(), POLICY) is None  # ...the row is locked: skipped, never waited on
    assert (claim_of(claims, r.project_id).worker_id, claim_of(claims, r.project_id).fencing_token) == (None, 0)
    (r.oracle / "die").write_text("1")
    assert finish(child).code == 137
    wait_until(lambda: claims.claim_next(new_worker_id(), POLICY) is not None, timeout=10)  # no lease to wait for
    assert claim_of(claims, r.project_id).fencing_token == 1  # the dead claim never counted as a generation


def test_a_worker_dying_right_after_its_claim_commit_keeps_the_run_until_its_lease_expires(calculator_run, spawn, claims, admin):
    r = calculator_run
    child = spawn("claim-then-die", r.oracle, 3)
    assert finish(child).code == 137
    dead = json.loads((r.oracle / "claimed").read_text())
    stored = claim_of(claims, r.project_id)
    assert (stored.worker_id, stored.fencing_token) == (dead["worker_id"], 1)  # the claim outlives its owner
    assert claims.claim_next(new_worker_id(), POLICY) is None  # its lease is still valid: no takeover yet
    wait_until(lambda: lease_expired(admin, r.project_id, "run-1"), timeout=10)
    taken = claims.claim_next(new_worker_id(), POLICY)
    assert taken.fence.fencing_token == 2 and taken.previous_worker_id == dead["worker_id"]


# ================================================================= ownership


def test_only_one_of_several_worker_processes_owns_a_run(calculator_run, spawn, claims, pg_store):
    r = calculator_run
    children = [spawn("work", "calculator", r.base, r.oracle, "--hold", "--idle-exit", "3") for _ in range(4)]
    wait_until(lambda: (r.oracle / "holding").exists(), timeout=30)
    # The three without the run reach their idle limit and exit; the owner is still inside its node.
    wait_until(lambda: sum(c.poll() is not None for c in children) == 3, timeout=30)
    (r.oracle / "release").write_text("1")
    results = [finish(c) for c in children]
    claimers = [res for res in results if any(e["event"] == "run_claimed" for e in res.events)]
    assert len(claimers) == 1, [res.events for res in results]
    assert all(res.code == 0 for res in results), [res.err for res in results]
    assert claim_of(claims, r.project_id).fencing_token == 1  # one ownership generation, ever
    assert pg_store.runs.get(r.project_id, "run-1").status is RunStatus.COMPLETED


def test_a_worker_process_killed_mid_run_is_taken_over_once_its_lease_expires(calculator_run, spawn, claims, admin, pg_schema, pg_store):
    from agent_factory.worker.sources import WorkspaceRunSource

    r = calculator_run
    child = spawn("work", "calculator", r.base, r.oracle, "--hold", "--lease", "3", "--heartbeat", "1")
    wait_until(lambda: (r.oracle / "holding").exists(), timeout=30)
    dead_worker = claim_of(claims, r.project_id).worker_id
    child.kill()  # the operating system ends it: no finally blocks, no release
    assert finish(child).code != 0
    assert claim_of(claims, r.project_id).worker_id == dead_worker
    assert claims.claim_next(new_worker_id(), POLICY) is None  # not before the lease runs out
    wait_until(lambda: lease_expired(admin, r.project_id, "run-1"), timeout=10)
    got = []
    worker_in_this_process(pg_schema, WorkspaceRunSource(pg_schema, r.base), events=got).run()
    taken = [e for e in got if e["event"] == "run_taken_over"]
    assert taken and taken[0]["previous_worker_id"] == dead_worker and taken[0]["fencing_token"] == 2
    assert [e["detected_by"] for e in got if e["event"] == "lease_expired"] == ["takeover"]
    assert pg_store.runs.get(r.project_id, "run-1").status is RunStatus.COMPLETED


@pytest.fixture
def notice_run(tmp_path, pg_store, claims):
    ws = approved_notice_run(pg_store, claims, tmp_path / "ws", tmp_path / "oracle")
    return SimpleNamespace(ws=ws, oracle=tmp_path / "oracle", project_id=ws.project_id)


def test_a_zombie_worker_process_is_fenced_out_after_a_takeover(notice_run, spawn, claims, admin, pg_schema):
    """Worker A starts sending, stalls without knowing it, and wakes after B took over: A cannot record anything."""
    r = notice_run
    checkpoint = CheckpointStore(r.ws.runs_dir).path("run-1")
    zombie = spawn("work", "notice", r.ws.root, r.oracle, "--hold", "--zombie", "--lease", "3", "--heartbeat", "1")
    wait_until(lambda: (r.oracle / "holding").exists(), timeout=30)  # A dispatched: its write-ahead row is committed
    before = checkpoint.read_bytes()
    zombie_id = claim_of(claims, r.project_id).worker_id
    wait_until(lambda: lease_expired(admin, r.project_id, "run-1"), timeout=10)  # A never renewed

    # B takes the run over. A still holds the run's lock on this host, so B cannot advance it yet.
    got_b = []
    worker_in_this_process(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle), events=got_b).run()
    assert [e["fencing_token"] for e in got_b if e["event"] == "run_taken_over"] == [2]
    assert [e["reason"] for e in got_b if e["event"] == "run_released"] == ["run_busy"]

    (r.oracle / "release").write_text("1")  # A wakes up, still sure that it owns the run
    a = finish(zombie)
    assert a.code == 0, a.err
    assert len(fx.read_lines(r.oracle / "notices.jsonl")) == 1  # the notice A had already started went out once
    rejected = [(e["operation"], e["reason"], e["fencing_token"]) for e in a.events if e["event"] == "stale_worker_rejected"]
    assert rejected == [("advance", "superseded", 1)]  # recording its outcome was refused
    assert "run_released" not in [e["event"] for e in a.events]
    assert checkpoint.read_bytes() == before  # A advanced nothing after the takeover
    assert admin.execute("SELECT state, owner FROM action_executions").fetchone() == ("dispatching", f"{zombie_id}@1")

    # C, the next owner, recovers A's attempt from A's own evidence: sent, so never sent again.
    worker_in_this_process(pg_schema, NoticeSource(pg_schema, r.ws.root, r.oracle)).run()
    assert len(fx.read_lines(r.oracle / "notices.jsonl")) == 1
    assert admin.execute("SELECT state FROM action_executions").fetchone() == ("succeeded",)
    assert admin.execute("SELECT status FROM runs").fetchone() == ("completed",)
    assert claim_of(claims, r.project_id).fencing_token == 3
    attempts = admin.execute("SELECT owner, result FROM action_execution_attempts").fetchall()
    assert attempts == [(f"{zombie_id}@1", "succeeded")]


def test_a_worker_process_stopped_by_a_signal_finishes_its_node_and_releases_the_run(calculator_run, spawn, claims, pg_schema, pg_store):
    from agent_factory.worker.sources import WorkspaceRunSource

    r = calculator_run
    child = spawn("work", "calculator", r.base, r.oracle, "--hold", new_group=True)
    wait_until(lambda: (r.oracle / "holding").exists(), timeout=30)
    if os.name == "nt":
        os.kill(child.pid, signal.CTRL_BREAK_EVENT)  # Ctrl+Break: the first stop signal
    else:
        os.kill(child.pid, signal.SIGTERM)
    time.sleep(0.5)
    (r.oracle / "release").write_text("1")  # the node in progress completes; nothing is abandoned halfway
    result = finish(child)
    assert result.code == 0, result.err
    released = [e for e in result.events if e["event"] == "run_released"]
    assert [(e["reason"], e["requeued"]) for e in released] == [("shutdown", True)]
    assert result.events[-1]["event"] == "worker_stopped" and result.events[-1]["reason"] == "stop_requested"
    state = CheckpointStore(r.ws.runs_dir).load("run-1")
    assert (state.status, state.step) == (RunStatus.RUNNING, 1)
    stored = claim_of(claims, r.project_id)
    assert stored.worker_id is None and stored.available_at is not None
    worker_in_this_process(pg_schema, WorkspaceRunSource(pg_schema, r.base)).run()
    assert pg_store.runs.get(r.project_id, "run-1").status is RunStatus.COMPLETED
    assert claim_of(claims, r.project_id).fencing_token == 2

"""A real PostgreSQL restart while a worker owns a run (live: needs Docker and the local dev database).

    .venv\\Scripts\\python -m pytest -m docker_live --run-docker-live --run-postgres -v tests/test_worker_restart_live.py

The test restarts THIS project's PostgreSQL container, and only after
checking that the container is named for this project and publishes the test
database's port (docs/testing.md). Nothing else on the machine is touched.
The run is the action fixture's (one approved, class C notice), which starts
no process: the live suites' guard allows the docker CLI only.

While the database is down, the owner must stop advancing the run and must
send nothing (fail closed). Once the database is back, another worker takes
the run over with a new generation, and the notice is sent exactly once.
"""

import os
import subprocess
import threading
from datetime import timedelta

import action_fixtures as fx
import pytest
from worker_support import BeforeAgent, NoticeSource, approved_notice_run, wait_until

from agent_factory.domain.claims import LeasePolicy
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.worker import Worker, WorkerEvents, WorkerIdentity, WorkerSettings

pytestmark = [pytest.mark.docker_live, pytest.mark.postgres]

CONTAINER = os.environ.get("AGENT_FACTORY_TEST_POSTGRES_CONTAINER", "agent-factory-postgres-dev")
POLICY = LeasePolicy(lease=timedelta(seconds=6), heartbeat_interval=timedelta(seconds=2))


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def container(postgres_config):
    if not CONTAINER.startswith("agent-factory-postgres"):
        pytest.fail(f"refusing to restart {CONTAINER!r}: only this project's PostgreSQL container may be restarted")
    ports = _docker("port", CONTAINER, "5432/tcp")
    if ports.returncode != 0:
        pytest.fail(f"container {CONTAINER!r} is not available: {ports.stderr.strip()}")
    if f":{postgres_config.port}" not in ports.stdout:
        pytest.fail(f"container {CONTAINER!r} does not publish the test database's port {postgres_config.port}")
    return CONTAINER


def _reachable(config) -> bool:
    from agent_factory.domain.errors import StorageError
    from agent_factory.persistence.postgres import connect

    try:
        connect(config).close()
        return True
    except StorageError:
        return False


def _worker(pg_schema, source, events):
    from agent_factory.persistence.claims import open_claim_repository

    identity = WorkerIdentity.new()
    return Worker(
        identity, WorkerSettings(policy=POLICY, poll_interval=0.1, max_backoff=1.0, max_runs=1),
        open_claims=lambda: open_claim_repository(pg_schema), source=source,
        events=WorkerEvents(identity.worker_id, sink=events.append),
    )  # fmt: skip


def test_a_database_restart_stops_the_owner_and_another_worker_recovers_the_run(container, pg_schema, pg_store, tmp_path):
    from agent_factory.persistence.claims import open_claim_repository
    from agent_factory.persistence.postgres import open_postgres_store

    oracle = tmp_path / "oracle"
    claims = open_claim_repository(pg_schema)
    ws = approved_notice_run(pg_store, claims, tmp_path / "ws", oracle)
    claims.close()
    checkpoint = CheckpointStore(ws.runs_dir).path("run-1")
    before = checkpoint.read_bytes()
    holding, release = threading.Event(), threading.Event()

    def hold():
        holding.set()
        release.wait(120)

    def adjust(ex):
        ex.agents["operator"] = BeforeAgent(ex.agents["operator"], hold)

    got_a = []
    a = _worker(pg_schema, NoticeSource(pg_schema, ws.root, oracle, adjust), got_a)
    thread = threading.Thread(target=a.run, daemon=True)
    thread.start()
    assert holding.wait(30)  # A owns the run (generation 1), inside the node that sends the notice

    restart = subprocess.Popen(["docker", "restart", "-t", "5", container], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_until(lambda: not _reachable(pg_schema), timeout=60, poll=0.1)  # the database is down
    release.set()  # A goes on while the database is unreachable...
    thread.join(60)
    assert not thread.is_alive()
    _, err = restart.communicate(timeout=180)
    assert restart.returncode == 0, err

    assert fx.read_lines(oracle / "notices.jsonl") == []  # ...sends nothing: no write-ahead record was possible
    assert checkpoint.read_bytes() == before  # ...and commits nothing: it cannot prove it owns the run
    events_a = [e["event"] for e in got_a]
    rejected = [(e["operation"], e["reason"]) for e in got_a if e["event"] == "stale_worker_rejected"]
    assert ("advance", "unconfirmed") in rejected and "run_released" not in events_a
    log = [e["event"] for e in fx.executor(ws, "notice", oracle).log_for("run-1").entries()]
    assert "run_blocked" not in log  # a worker that cannot prove ownership records no outcome

    wait_until(lambda: _reachable(pg_schema), timeout=120, poll=0.5)  # the database is back
    claims = open_claim_repository(pg_schema)
    try:
        wait_until(lambda: claims.now() >= claims.get(ws.project_id, "run-1").lease_expires_at, timeout=30, poll=0.2)
        got_b = []
        _worker(pg_schema, NoticeSource(pg_schema, ws.root, oracle), got_b).run()
        taken = [e for e in got_b if e["event"] == "run_taken_over"]
        assert taken and taken[0]["fencing_token"] == 2 and taken[0]["previous_worker_id"] == a.identity.worker_id
        assert [e["reason"] for e in got_b if e["event"] == "run_released"] == ["finished"]
    finally:
        claims.close()
    assert len(fx.read_lines(oracle / "notices.jsonl")) == 1  # sent exactly once, by the new owner
    fresh = open_postgres_store(pg_schema)
    try:
        assert fresh.runs.get(ws.project_id, "run-1").status.value == "completed"
    finally:
        fresh.close()

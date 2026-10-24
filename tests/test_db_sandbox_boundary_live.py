"""Live: untrusted code in the real Docker sandbox can neither see nor reach the product database.

    .venv\\Scripts\\python -m pytest -m "docker_live and postgres" --run-docker-live --run-postgres

The trusted orchestrator connects to PostgreSQL (the control). With the
database URL and a password in the orchestrator's own environment, untrusted
code then runs in the sandbox and looks for them. The process guard for live
suites applies: the host may launch only the docker CLI.
"""

from urllib.parse import urlsplit

import pytest
from conftest import Harness
from test_sandbox_live import (  # noqa: F401 - backend and no_leftovers are fixtures
    EXPECTED_ENV,
    SimulatedRealModel,
    _report,
    _req,
    _ws,
    backend,
    no_leftovers,
)

from agent_factory.demo import APPROVE, DEV_BUGGY, DEV_FIX, PLAN
from agent_factory.domain import MilestoneQuery
from agent_factory.sandbox import CodeTrust, ExecutionStatus, SandboxManager, run_sandboxed
from agent_factory.state import RunStatus

pytestmark = [pytest.mark.docker_live, pytest.mark.postgres]

DB_PROBE = r"""
import json, os, socket
r = {"env": sorted(os.environ), "urls": sorted(k for k, v in os.environ.items() if "://" in v), "connect": {}}
for host, port in (("127.0.0.1", {port}), ("host.docker.internal", {port}), ("agent-factory-postgres-dev", 5432),
                   ("172.17.0.1", {port})):
    try:
        socket.create_connection((host, port), timeout=3).close()
        r["connect"]["%s:%s" % (host, port)] = "connected"
    except OSError as e:
        r["connect"]["%s:%s" % (host, port)] = type(e).__name__
print("RESULT " + json.dumps(r))
"""


def test_untrusted_code_cannot_see_or_reach_the_product_database(backend, tmp_path, postgres_config, monkeypatch):
    from agent_factory.persistence.checks import check_connection

    assert check_connection(postgres_config).server_version  # control: the trusted orchestrator gets through
    password = urlsplit(postgres_config.url).password
    monkeypatch.setenv("AGENT_FACTORY_DATABASE_URL", postgres_config.url)  # in the orchestrator's environment
    monkeypatch.setenv("PGPASSWORD", password)
    probe = DB_PROBE.replace("{port}", str(postgres_config.port))
    result = run_sandboxed(backend, _req(_ws(tmp_path, {"probe.py": probe})))
    assert result.status is ExecutionStatus.COMPLETED and result.exit_code == 0, result.stderr
    r = _report(result.stdout)
    assert set(r["env"]) == EXPECTED_ENV, r["env"]  # nothing from the host environment
    assert r["urls"] == []
    assert all(outcome != "connected" for outcome in r["connect"].values()), r["connect"]
    assert password not in result.stdout + result.stderr
    print(f"\n[db-boundary] {r['connect']}")


def test_an_untrusted_run_records_to_postgres_while_its_code_runs_isolated(backend, tmp_path, pg_open):
    store = pg_open()
    h = Harness(tmp_path / "e2e-db")
    provider = SimulatedRealModel({"planner": [PLAN], "developer": [DEV_BUGGY, DEV_FIX], "reviewer": [APPROVE]})
    from agent_factory.factory import build_executor

    executor = build_executor(h.template, provider, h.workspace, sandbox=SandboxManager([backend]), product_store=store)
    state = executor.start(h.template.task, run_id="live-db")
    assert state.status is RunStatus.COMPLETED, state.blocked or state.error
    assert state.test_report["sandbox"]["backend"]["isolation_level"] == "CONTAINER"
    assert h.workspace.trust is CodeTrust.UNTRUSTED
    milestones = store.milestones.query(MilestoneQuery(project_id=h.workspace.project_id, run_id="live-db"))
    assert [(m.type, m.status.value) for m in milestones] == [
        ("plan_ready", "completed"), ("implementation_ready", "completed"), ("tests_passing", "failed"),
        ("implementation_ready", "completed"), ("tests_passing", "completed"), ("review_approved", "completed"),
        ("run_finished", "completed"),
    ]  # fmt: skip

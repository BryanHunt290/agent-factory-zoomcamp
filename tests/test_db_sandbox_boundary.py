"""Database credentials never reach sandboxed code (Tier 0: no Docker, no database).

The trust boundary is:

    TRUSTED ORCHESTRATOR ----> PostgreSQL
    UNTRUSTED SANDBOX    --X-> PostgreSQL

These tests put database configuration into the orchestrator's environment and
check every path to sandboxed code: the local fixture environment, the request
validator, the docker argv, and a whole run with a product store attached. The
live counterpart is tests/test_db_sandbox_boundary_live.py.
"""

import pytest
from docker_fakes import FakeDocker

from agent_factory.demo import APPROVE, DEV_POLISH, PLAN
from agent_factory.domain import in_memory_store
from agent_factory.factory import build_executor
from agent_factory.providers import FakeProvider
from agent_factory.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_SANDBOX_ENV,
    CodeTrust,
    ExecutionRequest,
    FakeOutcome,
    FakeSandboxBackend,
    IsolationLevel,
    SandboxManager,
    run_sandboxed,
)
from agent_factory.sandbox.docker import DockerBackend, DockerConfig
from agent_factory.sandbox.local import scrubbed_env
from agent_factory.sandbox.validation import validate_env
from agent_factory.state import RunStatus
from agent_factory.templates import CALCULATOR_TEMPLATE
from agent_factory.workspace import Workspace

DSN = "postgresql://agent_factory_app:canary-db-password-7f3c@127.0.0.1:55432/agent_factory_dev"
DB_ENV = {
    "AGENT_FACTORY_DATABASE_URL": DSN,
    "AGENT_FACTORY_DATABASE_URL_FILE": ".local/database.url",
    "AGENT_FACTORY_TEST_DATABASE_URL": DSN,
    "DATABASE_URL": DSN,
    "PGPASSWORD": "canary-db-password-7f3c",
    "PGHOST": "127.0.0.1",
}


@pytest.fixture
def orchestrator_env(monkeypatch):
    for name, value in DB_ENV.items():
        monkeypatch.setenv(name, value)


def _leaks(text):
    return [v for v in ("canary-db-password-7f3c", "agent_factory_dev", "55432") if v in text]


def test_trusted_local_fixtures_get_no_database_configuration(orchestrator_env):
    env = scrubbed_env()
    assert not set(env) & set(DB_ENV)
    assert _leaks(" ".join(env.values())) == []


@pytest.mark.parametrize(
    "env",
    [
        {"AGENT_FACTORY_DATABASE_URL": DSN},
        {"DATABASE_URL": DSN},
        {"PG_DSN": "host=db user=app"},
        {"PGPASSFILE": "/home/app/.pgpass"},
        {"PGSERVICEFILE": "/home/app/.pg_service.conf"},
        {"APP_CONFIG": DSN},  # an innocent name carrying a credential-bearing URL
        {"MIRROR": "https://user:token@example.invalid/simple"},
    ],
)
def test_database_settings_are_rejected_even_when_allowlisted(env):
    problems = validate_env(env, frozenset(env) | DEFAULT_ENV_ALLOWLIST)
    assert problems, env
    assert any("credential" in p for p in problems), problems


def test_plain_urls_without_credentials_are_still_allowed():
    assert validate_env({"MIRROR": "https://example.invalid/simple"}, frozenset({"MIRROR"})) == []


def test_docker_sandbox_receives_no_database_configuration(orchestrator_env, tmp_path):
    ws = Workspace.create(tmp_path / "ws", scaffold={"tests/__init__.py": "", "tests/test_x.py": "pass\n"})
    fake = FakeDocker()
    backend = DockerBackend(DockerConfig(image_id="sha256:" + "ab" * 32), runner=fake, wall_clock=lambda: 1_800_000_000.0)
    assert backend.probe().available
    request = ExecutionRequest(
        execution_id="af-boundary-check1", workspace=ws, command=("python", "-m", "unittest"),
        env=dict(DEFAULT_SANDBOX_ENV), env_allowlist=DEFAULT_ENV_ALLOWLIST, trust=CodeTrust.UNTRUSTED, run_id="r",
    )  # fmt: skip
    run_sandboxed(backend, request)
    create = next(args for _, args, _ in fake.calls if args[0] == "create")
    passed = {a.split("=", 1)[0] for a, prev in zip(create[1:], create) if prev == "--env"}
    assert passed <= set(DEFAULT_SANDBOX_ENV) | {"HOME", "PATH"}
    assert ["--network", "none"] == create[create.index("--network") : create.index("--network") + 2]
    everything = " ".join(" ".join(args) for _, args, _ in fake.calls)
    assert _leaks(everything) == []


class _UntrustedModel(FakeProvider):
    name = "simulated-real-model"
    produces_trusted_code = False


def test_a_run_recording_product_data_hands_sandboxed_code_nothing_about_the_database(orchestrator_env, tmp_path):
    container = FakeSandboxBackend(
        level=IsolationLevel.CONTAINER, outcome=FakeOutcome(stderr="Ran 5 tests in 0.01s\n\nOK\n")
    )
    store = in_memory_store()  # stands in for PostgreSQL: the engine only ever sees the repository interfaces
    ws = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "ws")
    provider = _UntrustedModel({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    ex = build_executor(CALCULATOR_TEMPLATE, provider, ws, product_store=store,
                        sandbox=SandboxManager([container], allow_test_doubles=True))  # fmt: skip
    state = ex.start(CALCULATOR_TEMPLATE.task, run_id="run-1")
    assert state.status is RunStatus.COMPLETED and container.requests
    for request in container.requests:
        assert request.trust is CodeTrust.UNTRUSTED
        assert set(request.env) <= set(DEFAULT_SANDBOX_ENV), request.env
        assert _leaks(" ".join(request.env.values()) + " ".join(request.command)) == []
    staged = [p for p in ws.project_dir.rglob("*") if p.is_file()]
    assert all(_leaks(p.read_text(encoding="utf-8", errors="replace")) == [] for p in staged)

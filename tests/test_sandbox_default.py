"""Default backend routing (v0.2 pre-release), Tier 0.

untrusted code -> healthy approved DockerBackend -> executes in Docker
untrusted code -> Docker unavailable / unhealthy / misconfigured -> BLOCKED
trusted fixtures -> LocalProcessBackend (Docker never contacted)
There is never a fallback to LocalProcessBackend for untrusted code.
"""

import subprocess

import pytest
from conftest import Harness
from docker_fakes import IMAGE_ID, FakeDocker, StartBehaviour

from agent_factory.demo import APPROVE, DEV_POLISH, PLAN
from agent_factory.providers import FakeProvider
from agent_factory.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_SANDBOX_ENV,
    CodeTrust,
    ExecutionRequest,
    LocalProcessBackend,
    SandboxCleanupError,
    SandboxManager,
    SandboxUnavailable,
)
from agent_factory.sandbox.config import docker_config_from_lock, load_image_record
from agent_factory.sandbox.docker import DockerBackend, DockerConfig
from agent_factory.state import RunStatus
from agent_factory.tools import default_registry
from agent_factory.workspace import Workspace

CONFIG = DockerConfig(image_id=IMAGE_ID)


class SimulatedRealModel(FakeProvider):
    name = "simulated-real-model"
    produces_trusted_code = False


SCRIPT = {"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]}


@pytest.fixture
def ws(tmp_path):
    ws = Workspace.create(tmp_path / "ws", scaffold={"ok.py": "print('fixture ran')\n"})
    return ws


def _req(ws, trust, command=("python", "ok.py")):
    return ExecutionRequest(
        execution_id="af-default-0001",
        workspace=ws,
        command=command,
        env=dict(DEFAULT_SANDBOX_ENV),
        env_allowlist=DEFAULT_ENV_ALLOWLIST,
        trust=trust,
    )


@pytest.fixture
def no_local_process(monkeypatch):
    """Fail if anything launches a local process (i.e. LocalProcessBackend ran)."""

    def refuse(*a, **k):
        raise AssertionError(f"a local process was started: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_default_manager_composition_uses_the_approved_image():
    manager = SandboxManager.default()
    assert [type(b) for b in manager.backends] == [LocalProcessBackend, DockerBackend]
    docker = manager.backends[1]
    record = load_image_record()
    assert record is not None and docker.config.image_id == record.image_id == docker_config_from_lock().image_id
    assert manager.ineligibility(manager.backends[0], CodeTrust.UNTRUSTED) is not None  # local never eligible
    assert manager.ineligibility(docker, CodeTrust.UNTRUSTED) is None


def test_build_executor_and_run_tests_tool_use_the_default_routing():
    tool = default_registry().get("run_tests")
    manager = tool._sandbox.__self__
    assert [type(b) for b in manager.backends] == [LocalProcessBackend, DockerBackend]


def test_untrusted_code_runs_in_docker_by_default(ws, no_local_process):
    fake = FakeDocker()
    manager = SandboxManager.default(docker_config=CONFIG, docker_runner=fake)
    result = manager.run(_req(ws, CodeTrust.UNTRUSTED))
    assert result.backend.name == "docker" and result.cleanup_verified
    assert ("create",) in fake.subcommands() and ("start",) in fake.subcommands()


@pytest.mark.parametrize(
    "fake_kw, config, reason",
    [
        ({"engine_up": False}, CONFIG, "not reachable"),
        ({"context_host": "tcp://10.0.0.9:2376"}, CONFIG, "remote engines are refused"),
        ({"image_present": False}, CONFIG, "never pulled"),
        ({"image_id": "sha256:" + "cd" * 32}, CONFIG, "mismatch"),
        ({"server_os": "windows"}, CONFIG, "Linux engine"),
        ({"self_test_report": {"uid": 0}}, CONFIG, "self-test"),
        ({}, DockerConfig(image_id=None, unavailable_reason="sandbox image is stale"), "stale"),
    ],
)
def test_untrusted_code_never_falls_back_to_local(ws, no_local_process, fake_kw, config, reason):
    fake = FakeDocker(**fake_kw)
    manager = SandboxManager.default(docker_config=config, docker_runner=fake)
    with pytest.raises(SandboxUnavailable) as exc:
        manager.run(_req(ws, CodeTrust.UNTRUSTED))
    message = str(exc.value)
    assert reason in message and "local-process: not a sandbox" in message
    starts = [a for _, a, _ in fake.calls if a[0] == "start"]
    assert all(any("af-selftest-" in part for part in a) for a in starts), starts  # never project code
    project_creates = [a for _, a, _ in fake.calls if a[0] == "create" and "af-default-0001" in a]
    assert project_creates == []


def test_unhealthy_docker_blocks_untrusted_without_local_fallback(ws, no_local_process):
    fake = FakeDocker()
    manager = SandboxManager.default(docker_config=CONFIG, docker_runner=fake)
    manager.run(_req(ws, CodeTrust.UNTRUSTED))
    fake.sticky = True  # cleanup can no longer be verified
    with pytest.raises(SandboxCleanupError):
        manager.run(_req(ws, CodeTrust.UNTRUSTED))
    fake.sticky = False
    with pytest.raises(SandboxUnavailable, match="unhealthy"):
        manager.run(_req(ws, CodeTrust.UNTRUSTED))


def test_trusted_fixtures_stay_local_and_never_contact_docker(ws):
    fake = FakeDocker()
    manager = SandboxManager.default(docker_config=CONFIG, docker_runner=fake)
    result = manager.run(_req(ws, CodeTrust.TRUSTED_FIXTURE))
    assert result.backend.name == "local-process" and result.stdout.strip() == "fixture ran"
    assert fake.calls == []


def test_executor_default_routing_untrusted_executes_in_docker(tmp_path):
    h = Harness(tmp_path / "e2e")
    fake = FakeDocker(start=StartBehaviour())
    manager = SandboxManager.default(docker_config=CONFIG, docker_runner=fake)
    state = h.executor(SimulatedRealModel(SCRIPT), sandbox=manager).start(h.template.task, run_id="run-1")
    assert state.status is RunStatus.COMPLETED
    assert state.test_report["sandbox"]["backend"]["name"] == "docker"


def test_executor_default_routing_blocks_when_docker_unavailable(tmp_path):
    # build_executor() with no sandbox argument: the real default. Tier 0 has no Docker.
    h = Harness(tmp_path / "e2e")
    provider = SimulatedRealModel(SCRIPT)
    state = h.executor(provider).start(h.template.task, run_id="run-1")
    assert state.status is RunStatus.BLOCKED
    assert state.blocked["type"] == "SandboxUnavailable"
    assert "docker" in state.blocked["message"] and "local-process: not a sandbox" in state.blocked["message"]
    assert "tester" not in state.node_visits and len(provider.calls_for("developer")) == 1


def test_executor_default_routing_keeps_trusted_demo_local(tmp_path):
    from agent_factory.demo import scenario_provider

    h = Harness(tmp_path / "demo")
    state = h.executor(scenario_provider("repair")).start(h.template.task, run_id="run-1")
    assert state.status is RunStatus.COMPLETED
    assert state.test_report["sandbox"]["backend"]["name"] == "local-process"

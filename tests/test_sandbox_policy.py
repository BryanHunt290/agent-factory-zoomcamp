"""Stage C: trust tracking, SandboxManager fail-closed policy, BLOCKED/resume."""

import json
import subprocess
import threading
import time

import pytest
from conftest import Harness, events

from agent_factory.demo import DEV_BUGGY, DEV_POLISH, PLAN, APPROVE
from agent_factory.engine import RunNotResumable
from agent_factory.providers import FakeProvider, ModelProvider
from agent_factory.providers.base import provider_produces_trusted_code
from agent_factory.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_SANDBOX_ENV,
    CodeTrust,
    ExecutionDenied,
    ExecutionRequest,
    ExecutionStatus,
    FakeOutcome,
    FakeSandboxBackend,
    IsolationLevel,
    LimitHit,
    LocalProcessBackend,
    SandboxCleanupError,
    SandboxManager,
    SandboxUnavailable,
)
from agent_factory.state import ProjectState, RunStatus, StateError
from agent_factory.workspace import MARKER, Workspace

PASSING = FakeOutcome(stderr="Ran 5 tests in 0.010s\n\nOK\n")


class SimulatedRealModel(FakeProvider):
    """Scripted, offline, but declared untrusted - exactly how a real model provider behaves."""

    name = "simulated-real-model"
    produces_trusted_code = False


def _untrusted(script=None, **kw):
    return SimulatedRealModel(script or {"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]}, **kw)


def _container(**kw):
    kw.setdefault("outcome", PASSING)
    return FakeSandboxBackend(level=IsolationLevel.CONTAINER, **kw)


def _req(ws, trust=CodeTrust.UNTRUSTED):
    return ExecutionRequest(
        execution_id="af-policy-0001",
        workspace=ws,
        command=("python", "-m", "unittest"),
        env=dict(DEFAULT_SANDBOX_ENV),
        env_allowlist=DEFAULT_ENV_ALLOWLIST,
        trust=trust,
    )


@pytest.fixture
def no_spawn(monkeypatch):
    """Fail the test if any process is started."""

    def refuse(*a, **k):
        raise AssertionError(f"process started: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)


@pytest.fixture
def ws(tmp_path):
    return Workspace.create(tmp_path / "ws")


# ----------------------------------------------------------------- trust


def test_providers_are_untrusted_unless_they_declare_otherwise():
    class SomeRealProvider(ModelProvider):
        name = "real"

        def complete(self, request):
            raise NotImplementedError

    assert ModelProvider.produces_trusted_code is False
    assert provider_produces_trusted_code(SomeRealProvider()) is False
    assert provider_produces_trusted_code(FakeProvider({})) is True
    assert provider_produces_trusted_code(SimulatedRealModel({})) is False


def test_workspace_trust_defaults_and_is_monotonic(tmp_path):
    plain = Workspace.create(tmp_path / "plain")
    assert plain.trust is CodeTrust.UNTRUSTED  # fail-closed default
    h = Harness(tmp_path / "tpl")
    assert h.workspace.trust is CodeTrust.TRUSTED_FIXTURE  # template scaffold
    assert h.workspace.mark_untrusted("test") is True
    assert h.workspace.mark_untrusted("again") is False
    assert Workspace.open(h.workspace.root).trust is CodeTrust.UNTRUSTED
    assert not hasattr(Workspace, "mark_trusted")


@pytest.mark.parametrize("content", ["{not json", json.dumps({"format": 1}), json.dumps({"trust": "TRUSTED"})])
def test_missing_or_corrupt_trust_record_means_untrusted(tmp_path, content):
    h = Harness(tmp_path / "ws")
    (h.workspace.meta_dir / MARKER).write_text(content, encoding="utf-8")
    assert h.workspace.trust is CodeTrust.UNTRUSTED


# --------------------------------------------------------------- manager


def test_default_manager_blocks_untrusted_when_docker_unavailable(ws, no_spawn):
    # Tier 0 has no Docker: the default manager refuses instead of falling back.
    with pytest.raises(SandboxUnavailable, match="docker engine not reachable") as exc:
        SandboxManager.default().run(_req(ws))
    assert "local-process: not a sandbox" in str(exc.value)


def test_local_only_manager_denies_untrusted_without_spawning(ws, no_spawn):
    with pytest.raises(ExecutionDenied, match="requires CONTAINER"):
        SandboxManager([LocalProcessBackend()]).run(_req(ws))


def test_no_fallback_from_unavailable_container_to_local(ws, no_spawn):
    container = _container(available=False)
    manager = SandboxManager([container, LocalProcessBackend()], allow_test_doubles=True)
    with pytest.raises(SandboxUnavailable) as exc:
        manager.run(_req(ws))
    assert "not a sandbox" in str(exc.value)
    assert "prepare" not in container.calls


def test_local_backend_is_never_eligible_for_untrusted_even_if_mislabelled(ws, no_spawn):
    class Mislabelled(LocalProcessBackend):
        isolation_level = IsolationLevel.CONTAINER
        is_security_sandbox = True

    with pytest.raises(ExecutionDenied, match="not a sandbox"):
        SandboxManager([Mislabelled()]).run(_req(ws))


def test_insufficient_isolation_level_is_denied(ws):
    weak = FakeSandboxBackend(level=IsolationLevel.PROCESS)
    with pytest.raises(ExecutionDenied, match="PROCESS < required CONTAINER"):
        SandboxManager([weak], allow_test_doubles=True).run(_req(ws))
    assert weak.calls == []


def test_test_doubles_refused_unless_explicitly_allowed(ws):
    with pytest.raises(ExecutionDenied, match="test double"):
        SandboxManager([_container()]).run(_req(ws))


def test_failed_self_test_blocks_execution(ws):
    container = _container(self_test_passes=False)
    with pytest.raises(SandboxUnavailable, match="self-test failed"):
        SandboxManager([container], allow_test_doubles=True).run(_req(ws))
    assert "prepare" not in container.calls


def test_janitor_and_self_test_run_once_then_cached(ws):
    container = _container()
    manager = SandboxManager([container], allow_test_doubles=True)
    manager.run(_req(ws))
    manager.run(_req(ws))
    assert container.calls.count("janitor") == 1 and container.calls.count("self_test") == 1
    assert container.calls.count("prepare") == 2


def test_falls_through_only_to_another_qualifying_backend(ws):
    down = _container(available=False, label="primary")
    up = _container(label="secondary")
    result = SandboxManager([down, up], allow_test_doubles=True).run(_req(ws))
    assert result.backend.name == "secondary"


def test_trusted_fixture_may_use_local_when_container_is_down(ws):
    # Not a fallback for untrusted code: trusted fixtures never needed a container.
    ws.write_text("ok.py", "print('fine')\n")
    req = ExecutionRequest(
        execution_id="af-policy-0002",
        workspace=ws,
        command=("python", "ok.py"),
        trust=CodeTrust.TRUSTED_FIXTURE,
    )
    manager = SandboxManager([_container(available=False), LocalProcessBackend()], allow_test_doubles=True)
    assert manager.run(req).backend.name == "local-process"


def test_unverified_cleanup_marks_backend_unhealthy(ws):
    container = _container(cleanup_verified=False)
    manager = SandboxManager([container], allow_test_doubles=True)
    with pytest.raises(SandboxCleanupError):
        manager.run(_req(ws))
    container.cleanup_verified = True  # even if the backend later looks fine...
    with pytest.raises(SandboxUnavailable, match="unhealthy"):
        manager.run(_req(ws))
    assert container.calls.count("prepare") == 1  # ...nothing more was executed
    manager.recover(container)
    assert manager.run(_req(ws)).cleanup_verified


def test_janitor_failure_marks_backend_unhealthy(ws):
    container = _container(janitor_ok=False)
    manager = SandboxManager([container], allow_test_doubles=True)
    with pytest.raises(SandboxUnavailable, match="janitor"):
        manager.run(_req(ws))
    container.janitor_ok = True
    with pytest.raises(SandboxUnavailable, match="unhealthy"):
        manager.run(_req(ws))
    assert "prepare" not in container.calls


def test_concurrency_is_bounded(ws):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(request):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return PASSING

    manager = SandboxManager([_container(outcome=slow)], allow_test_doubles=True, max_concurrent=1)
    threads = [threading.Thread(target=manager.run, args=(_req(ws),)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1


# ------------------------------------------------- executor: BLOCKED / resume


def test_untrusted_run_blocks_at_tester_without_consuming_retries(harness, no_spawn):
    ex = harness.executor(_untrusted())  # default manager: no container backend
    state = ex.start(harness.template.task, run_id="run-1")

    assert state.status is RunStatus.BLOCKED
    assert state.current_node == "tester"
    assert state.blocked["type"] == "SandboxUnavailable" and "docker" in state.blocked["message"]
    assert "tester" not in state.node_visits
    assert not any(k.startswith("tester:") for k in state.edge_traversals)
    assert state.test_report is None
    assert harness.workspace.trust is CodeTrust.UNTRUSTED

    names = [e["event"] for e in events(ex)]
    assert names.index("workspace_trust") < names.index("node_entered")
    assert names[-1] == "run_blocked"
    action = [e for e in events(ex) if e["event"] == "action" and e.get("tool") == "run_tests"][-1]
    assert action["result"] == "error" and "SandboxUnavailable" in action["summary"]


def test_blocked_run_stays_blocked_until_a_qualifying_backend_exists(harness):
    provider = _untrusted()
    ex = harness.executor(provider)
    ex.start(harness.template.task, run_id="run-1")

    # Resume with Docker still "down": blocked again, still no retries consumed.
    down = SandboxManager([_container(available=False), LocalProcessBackend()], allow_test_doubles=True)
    state = harness.executor(provider, sandbox=down).resume("run-1")
    assert state.status is RunStatus.BLOCKED and state.blocked["type"] == "SandboxUnavailable"
    assert "tester" not in state.node_visits
    assert len(provider.calls_for("developer")) == 1  # developer not re-run

    # A qualifying backend appears: the run completes from the tester node.
    up = SandboxManager([_container()], allow_test_doubles=True)
    state = harness.executor(provider, sandbox=up).resume("run-1")
    assert state.status is RunStatus.COMPLETED
    assert state.blocked is None
    assert state.test_report["sandbox"]["backend"]["isolation_level"] == "CONTAINER"
    assert [e["previous_status"] for e in events(ex) if e["event"] == "run_resumed"] == ["blocked", "blocked"]


def test_trust_downgrade_survives_switching_to_a_trusted_provider(harness):
    harness.executor(_untrusted()).start(harness.template.task, run_id="run-1")
    # Later resumed with the trusted FakeProvider: the workspace stays untrusted,
    # so the local backend is still refused.
    trusted = FakeProvider({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    state = harness.executor(trusted).resume("run-1")
    assert state.status is RunStatus.BLOCKED
    assert harness.workspace.trust is CodeTrust.UNTRUSTED


@pytest.mark.parametrize(
    "backend_kw, expected_type",
    [
        ({"fail_at": "execute"}, "SandboxInfrastructureError"),
        ({"fail_at": "prepare"}, "SandboxInfrastructureError"),
        ({"cleanup_verified": False}, "SandboxCleanupError"),
        ({"self_test_passes": False}, "SandboxUnavailable"),
    ],
)
def test_infrastructure_failures_block_not_fail(harness, backend_kw, expected_type):
    manager = SandboxManager([_container(**backend_kw)], allow_test_doubles=True)
    state = harness.executor(_untrusted(), sandbox=manager).start(harness.template.task, run_id="run-1")
    assert state.status is RunStatus.BLOCKED
    assert state.blocked["type"] == expected_type
    assert "tester" not in state.node_visits


def test_code_caused_timeout_is_a_test_failure_that_uses_a_retry(harness):
    timeouts_then_pass = iter(
        [FakeOutcome(status=ExecutionStatus.TIMED_OUT, exit_code=None, limits_hit=(LimitHit.TIME,)), PASSING]
    )
    manager = SandboxManager([_container(outcome=lambda r: next(timeouts_then_pass))], allow_test_doubles=True)
    provider = _untrusted({"planner": [PLAN], "developer": [DEV_BUGGY, DEV_POLISH], "reviewer": [APPROVE]})
    state = harness.executor(provider, sandbox=manager).start(harness.template.task, run_id="run-1")
    assert state.status is RunStatus.COMPLETED
    assert state.edge_traversals["tester:fail->developer"] == 1
    first_fail = [h for h in state.history if h["node"] == "tester"][0]
    assert first_fail["outcome"] == "fail" and "timed_out" in first_fail["summary"]


def test_trusted_demo_still_runs_locally(harness):
    from agent_factory.demo import scenario_provider

    state = harness.executor(scenario_provider("repair")).start(harness.template.task, run_id="run-1")
    assert state.status is RunStatus.COMPLETED
    assert state.test_report["sandbox"]["backend"]["name"] == "local-process"
    assert harness.workspace.trust is CodeTrust.TRUSTED_FIXTURE


def test_completed_and_failed_runs_are_still_not_resumable(harness):
    from agent_factory.demo import scenario_provider

    ex = harness.executor(scenario_provider("repair"))
    ex.start(harness.template.task, run_id="run-1")
    with pytest.raises(RunNotResumable):
        ex.resume("run-1")


def test_v1_checkpoint_is_upgraded(harness):
    from agent_factory.demo import scenario_provider

    ex = harness.executor(scenario_provider("repair"))
    state = ex.start(harness.template.task, run_id="run-1")
    data = state.to_dict()
    data["schema_version"] = 1
    del data["blocked"]
    del data["pending_reconciliation"]  # neither existed in a v1 checkpoint
    upgraded = ProjectState.from_dict(data)
    assert upgraded.schema_version == 3 and upgraded.blocked is None and upgraded.pending_reconciliation is None


def test_v2_checkpoint_is_upgraded_to_v3(harness):
    from agent_factory.demo import scenario_provider

    ex = harness.executor(scenario_provider("repair"))
    data = ex.start(harness.template.task, run_id="run-1").to_dict()
    data["schema_version"] = 2
    del data["pending_reconciliation"]
    upgraded = ProjectState.from_dict(data)
    assert upgraded.schema_version == 3 and upgraded.pending_reconciliation is None
    data["schema_version"] = 4
    with pytest.raises(StateError, match="unsupported"):
        ProjectState.from_dict(data)  # an engine never guesses at a newer checkpoint


def test_cli_reports_blocked_with_exit_code(tmp_path, capsys, monkeypatch):
    from agent_factory import cli

    h = Harness(tmp_path / "ws")
    h.executor(_untrusted()).start(h.template.task, run_id="demo")
    code = cli.main(["status", "--workspace", str(h.workspace.root), "--run-id", "demo"])
    out = capsys.readouterr().out
    assert code == 0 and "BLOCKED" in out and "no retry consumed" in out

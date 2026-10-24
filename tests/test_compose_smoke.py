"""Offline regression tests for the opt-in Compose smoke's safety and evidence."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("compose_smoke", ROOT / "scripts/compose_smoke.py")
smoke = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(smoke)
sys.path.pop(0)

PROJECT = "prj-" + "a" * 20


def test_documented_direct_script_entrypoint_loads_without_services():
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/compose_smoke.py"), "--help"],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "--project" in result.stdout and "--no-build" in result.stdout


def approval():
    return {"id": "apr-" + "a" * 16, "project_id": PROJECT, "run_id": "review-approval",
            "status": "pending", "decision": None, "execution": None,
            "action": {"tool": "delete_file", "category": "destructive_delete", "params": {"path": "legacy_calc.py"}},
            "action_fingerprint": "sha256:" + "a" * 64}


def completed():
    request = approval()
    request.update(status="approved", decision={"id": "decision-1", "outcome": "approved"},
                   execution={"id": "execution-1", "state": "succeeded", "attempt": 1},
                   timeline=[{"kind": kind} for kind in ("approval_requested", "approved", "action_dispatching", "action_succeeded", "resolved")])
    return {"status": "completed", "pending_approval_id": None}, request


@pytest.mark.parametrize("project", ["agent-factory", "af-smoke-x", "af-smoke-../other", "af-smoke-ABC12345", "--project"])
def test_unsafe_or_non_smoke_project_names_are_rejected(project):
    with pytest.raises(smoke.SmokeError, match="fresh af-smoke"):
        smoke.Stack(project, 15473)


def test_inherited_compose_provider_and_docker_overrides_cannot_select_other_resources(monkeypatch):
    for name in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "DOCKER_HOST", "AGENT_FACTORY_MODEL_PROVIDER", "PGHOSTADDR"):
        monkeypatch.setenv(name, "foreign-resource")
    stack = smoke.Stack("af-smoke-12345678", 15474)
    assert "foreign-resource" not in stack.env.values()
    assert stack.env["AF_DASHBOARD_PORT"] == "15474"
    assert stack.prefix[-2:] == ["--project-name", "af-smoke-12345678"]


def test_remote_daemon_is_refused_before_resource_mutation(monkeypatch):
    stack = smoke.Stack("af-smoke-12345678", 15473)
    calls = []
    monkeypatch.setattr(stack, "docker", lambda *args: calls.append(args) or json.dumps([{"Endpoints": {"docker": {"Host": "ssh://foreign"}}}]))
    with pytest.raises(smoke.SmokeError, match="local Docker"):
        stack.start()
    stack.stop()
    assert calls == [("context", "inspect")]
    assert not stack.created


@pytest.mark.parametrize("kind", ["container", "volume", "network"])
def test_existing_project_is_never_started_or_stopped(monkeypatch, kind):
    stack = smoke.Stack("af-smoke-12345678", 15473)
    calls = []

    def docker(*args):
        calls.append(args)
        if args == ("context", "inspect"):
            return json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}])
        if args[0] == "info":
            return "linux"
        return "existing" if args[0] == kind else ""

    monkeypatch.setattr(stack, "docker", docker)
    monkeypatch.setattr(stack, "compose", lambda *a, **k: pytest.fail("Preexisting resources must never be mutated"))
    with pytest.raises(smoke.SmokeError, match="Existing Compose"):
        stack.start()
    stack.stop()
    assert not stack.created


def test_failed_partial_start_still_stops_only_its_new_project(monkeypatch):
    stack = smoke.Stack("af-smoke-12345678", 15473)
    calls = []
    monkeypatch.setattr(stack, "preflight", lambda: None)

    def compose(*args, **kwargs):
        calls.append(args)
        if args[0] == "up":
            raise smoke.SmokeError("failed build")

    monkeypatch.setattr(stack, "compose", compose)
    with pytest.raises(smoke.SmokeError, match="failed build"):
        stack.start()
    stack.stop()
    assert calls == [("up", "--detach", "--wait", "--wait-timeout", "180", "--build"), ("stop", "--timeout", "30")]


@pytest.mark.parametrize("change", [
    {"project_id": "prj-" + "b" * 20}, {"run_id": "other"}, {"status": "approved"},
    {"decision": {"outcome": "approved"}}, {"execution": {"state": "succeeded"}},
    {"action_fingerprint": "bad"},
    {"action": {"tool": "delete_file", "category": "destructive_delete", "params": {"path": "important-data"}}},
    {"action": {"tool": "deploy", "category": "deployment", "params": {}}},
])
def test_smoke_can_approve_only_its_exact_fresh_fixture(change):
    request = approval()
    request.update(change)
    with pytest.raises(smoke.SmokeError):
        smoke.check_fixture(request, PROJECT)


def test_exact_fixture_is_accepted():
    smoke.check_fixture(approval(), PROJECT)


@pytest.mark.parametrize("state,attempt", [("uncertain", 1), ("succeeded", 2), ("dispatching", 1), ("lapsed", 0)])
def test_approval_alone_or_repeated_execution_never_counts_as_success(state, attempt):
    run, request = completed()
    request["execution"].update(state=state, attempt=attempt)
    with pytest.raises(smoke.SmokeError, match="one recorded attempt"):
        smoke.check_completed(run, request)


def test_duplicate_dispatch_in_timeline_is_rejected():
    run, request = completed()
    request["timeline"].append({"kind": "action_dispatching"})
    with pytest.raises(smoke.SmokeError, match="one decision"):
        smoke.check_completed(run, request)


def test_command_failure_never_exposes_captured_secret(monkeypatch, capsys):
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *a, **k: SimpleNamespace(returncode=1, communicate=lambda **kw: ("sensitive-token", "sensitive-password")))
    with pytest.raises(smoke.SmokeError) as error:
        smoke.command(["docker", "inspect"], {})
    assert "sensitive" not in str(error.value) + capsys.readouterr().out


@pytest.mark.parametrize("endpoint", ["npipe:////server/pipe/docker_engine", "npipe://remote/pipe/docker_engine", "unix://remote/var/run/docker.sock", "tcp://127.0.0.1:2375", "unix:////server/path"])
def test_remote_pipe_or_nonlocal_socket_cannot_be_used(endpoint):
    assert not smoke.local_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", ["npipe:////./pipe/dockerDesktopLinuxEngine", "unix:///var/run/docker.sock"])
def test_local_daemon_endpoint_is_pinned_after_inspection(monkeypatch, endpoint):
    stack = smoke.Stack("af-smoke-12345678", 15473)
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "context":
            return json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}])
        if args[0] == "info":
            return "linux"
        return ""

    monkeypatch.setattr(stack, "docker", docker)
    monkeypatch.setattr(smoke.reviewer, "port_available", lambda _: None)
    stack.preflight()
    assert stack.endpoint == endpoint
    assert stack.prefix[:4] == ["docker", "--host", endpoint, "compose"]


def test_unlabeled_existing_volume_name_is_also_refused(monkeypatch):
    stack = smoke.Stack("af-smoke-12345678", 15473)

    def docker(*args):
        if args[0] == "context":
            return json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}])
        if args[0] == "info":
            return "linux"
        if args[0] == "volume" and "--format" in args:
            return "af-smoke-12345678_database"
        return ""

    monkeypatch.setattr(stack, "docker", docker)
    with pytest.raises(smoke.SmokeError, match="without Compose"):
        stack.start()
    assert not stack.created


@pytest.mark.parametrize("interruption", [subprocess.TimeoutExpired("docker", 1), KeyboardInterrupt()])
def test_command_interruption_stops_owned_tree_before_returning(monkeypatch, interruption):
    process = SimpleNamespace(communicate=lambda **kw: (_ for _ in ()).throw(interruption))
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *a, **k: process)
    stopped = []
    monkeypatch.setattr(smoke, "terminate_command", lambda child: stopped.append(child))
    with pytest.raises(KeyboardInterrupt if isinstance(interruption, KeyboardInterrupt) else smoke.SmokeError):
        smoke.command(["docker", "compose", "up"], {})
    assert stopped == [process]


def test_cleanup_targets_only_the_owned_process_tree(monkeypatch):
    process = SimpleNamespace(pid=123456, poll=lambda: None, communicate=lambda **kw: ("", ""))
    calls = []
    if smoke.os.name == "nt":
        monkeypatch.setattr(smoke.subprocess, "run", lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0))
        smoke.terminate_command(process)
        assert calls == [["taskkill", "/PID", "123456", "/T", "/F"]]
    else:
        monkeypatch.setattr(smoke.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
        smoke.terminate_command(process)
        assert calls == [(123456, smoke.signal.SIGKILL)]


@pytest.mark.parametrize("tamper", ["record", "resource", "workspace", "duplicate_run"])
def test_restart_must_preserve_records_resources_and_single_run(monkeypatch, tmp_path, tamper):
    stack = smoke.Stack("af-smoke-12345678", 15473)
    initial_run, initial_approval = completed()
    resource_reads = []
    approval_reads = []
    workspace_reads = []
    commands = []
    monkeypatch.setattr(stack, "copy_identity", lambda _: ("secret-token", PROJECT))
    monkeypatch.setattr(stack, "compose", lambda *args, **kw: commands.append(args))

    def resources():
        resource_reads.append(True)
        return {"app": "changed" if tamper == "resource" and len(resource_reads) > 1 else "same"}

    monkeypatch.setattr(stack, "resource_identity", resources)
    def workspace(_project):
        workspace_reads.append(True)
        return {"artifact": "changed" if tamper == "workspace" and len(workspace_reads) > 1 else "same"}
    monkeypatch.setattr(stack, "workspace_identity", workspace)
    monkeypatch.setattr(smoke, "wait_run", lambda *a, **k: {"pending_approval_id": approval()["id"]} if a[3] == "awaiting_approval" else initial_run)

    def api(_stack, path, **kwargs):
        if kwargs.get("expected") == 401 or path.endswith("/decision"):
            return None
        if path == "/api/v1/projects":
            return {"items": [{"id": PROJECT}], "next_cursor": None}
        if path.endswith("/runs"):
            return {"items": [{"run_id": smoke.RUN}] * (2 if tamper == "duplicate_run" else 1), "next_cursor": None}
        approval_reads.append(True)
        if len(approval_reads) == 1:
            return approval()
        request = copy.deepcopy(initial_approval)
        if tamper == "record" and len(approval_reads) > 2:
            request["decision"]["id"] = "changed"
        return request

    monkeypatch.setattr(smoke, "api", api)
    with pytest.raises(smoke.SmokeError, match="Restart"):
        smoke.exercise(stack, tmp_path)
    assert commands == [("stop", "--timeout", "30", "app", "db"), ("start", "--wait", "--wait-timeout", "180", "db", "app")]

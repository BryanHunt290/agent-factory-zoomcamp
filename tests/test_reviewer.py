"""Reviewer launcher boundaries, without Docker, services or package downloads."""

from __future__ import annotations

import importlib.util
import builtins
import io
import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reviewer.py"
spec = importlib.util.spec_from_file_location("reviewer_launcher", SCRIPT)
reviewer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(reviewer)


def paths(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    result = reviewer.runtime_paths(root)
    result["local"].mkdir(parents=True)
    result["logs"].mkdir()
    return result


def test_inherited_live_and_credential_configuration_never_reaches_services(monkeypatch):
    for name in ("AGENT_FACTORY_DATABASE_URL", "AGENT_FACTORY_MODEL_PROVIDER", "AWS_ACCESS_KEY_ID", "ANTHROPIC_API_KEY",
                 "OPENAI_API_KEY", "HTTP_PROXY", "https_proxy", "DOCKER_HOST", "PYTHONPATH", "NODE_OPTIONS",
                 "PGHOSTADDR", "PGSERVICE", "PGOPTIONS", "PGPASSFILE", "PGPASSWORD"):
        monkeypatch.setenv(name, "must-not-reach-a-child")
    monkeypatch.setenv("PATH", "safe-path")
    env = reviewer.clean_env(AGENT_FACTORY_AUTH_MODE="development")
    assert "must-not-reach-a-child" not in env.values()
    assert env["PATH"] == "safe-path"
    assert env["AGENT_FACTORY_AUTH_MODE"] == "development"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


def test_current_process_clears_libpq_overrides_before_driver_use(monkeypatch):
    monkeypatch.setattr(os, "environ", {"PGHOSTADDR": "203.0.113.4", "PGSERVICE": "unrelated",
                                       "PGOPTIONS": "-c search_path=other", "PATH": "safe-path"})
    reviewer.sanitize_process_environment()
    assert not any(name.upper().startswith("PG") for name in os.environ)
    assert os.environ["PATH"] == "safe-path"
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_already_in_reviewer_venv_still_sanitizes_current_process(tmp_path, monkeypatch):
    config = paths(tmp_path)
    monkeypatch.setattr(os, "environ", {"PGHOSTADDR": "203.0.113.4", "PGSERVICE": "unrelated", "PGOPTIONS": "unsafe"})
    monkeypatch.setattr(reviewer.sys, "prefix", str(config["local"] / ".venv"))
    monkeypatch.setattr(reviewer.sys, "path", list(reviewer.sys.path))
    monkeypatch.setattr(reviewer, "runtime_paths", lambda: config)
    monkeypatch.setattr(reviewer, "require_setup", lambda _: None)
    monkeypatch.setattr(reviewer, "exclusive", lambda _: nullcontext())
    monkeypatch.setattr(reviewer, "layout", lambda _: {})
    seen = []
    monkeypatch.setattr(reviewer, "up", lambda *_: seen.append(dict(os.environ)))
    assert reviewer.main(["up"]) == 0
    assert len(seen) == 1
    assert not any(name.upper().startswith("PG") for name in seen[0])
    assert seen[0]["AWS_EC2_METADATA_DISABLED"] == "true"


def test_secrets_are_new_nonempty_and_existing_credentials_are_never_replaced(tmp_path, capsys):
    config = paths(tmp_path)
    reviewer.ensure_secrets(config)
    initial = {key: config[key].read_bytes() for key in ("password", "admin_url", "url", "test_url")}
    reviewer.ensure_secrets(config)
    assert initial == {key: config[key].read_bytes() for key in initial}
    assert all(initial.values())
    assert initial["password"].strip() not in capsys.readouterr().out.encode()
    assert b"127.0.0.1:55433/agent_factory_reviewer_test" in initial["test_url"]


def test_partial_credentials_fail_closed(tmp_path):
    config = paths(tmp_path)
    config["password"].write_text("existing", encoding="utf-8")
    with pytest.raises(reviewer.ReviewerError, match="incomplete"):
        reviewer.ensure_secrets(config)
    assert config["password"].read_text() == "existing"
    assert not config["url"].exists()


@pytest.mark.parametrize("key", ["admin_url", "url", "test_url"])
@pytest.mark.parametrize("override", ["?hostaddr=203.0.113.4", "?host=unrelated.example", "#fragment", "wrong-scheme"])
def test_url_overrides_are_rejected_before_driver_import_or_connect(tmp_path, monkeypatch, key, override):
    config = paths(tmp_path)
    reviewer.ensure_secrets(config)
    original = config[key].read_text(encoding="utf-8").strip()
    tampered = original.replace("postgresql://", "http://") if override == "wrong-scheme" else original + override
    config[key].write_text(tampered, encoding="utf-8")
    imports = []
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "psycopg":
            imports.append(name)
            raise AssertionError("Invalid database URL reached the driver import.")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(reviewer.ReviewerError, match="no query options"):
        reviewer.provision(config)
    assert imports == []


@pytest.mark.parametrize("labels", [None, {}, {reviewer.LABEL: "wrong"}, {reviewer.LABEL: "owner", "agent-factory.service": "other"}])
def test_foreign_docker_resources_are_refused(labels):
    with pytest.raises(reviewer.ReviewerError, match="ownership"):
        reviewer.assert_owned({"Labels": labels}, {"instance": "owner"})


def test_owned_resource_has_both_service_and_instance_identity():
    labels = {reviewer.LABEL: "owner", "agent-factory.service": "postgres-reviewer"}
    reviewer.assert_owned({"Labels": labels}, {"instance": "owner"})
    reviewer.assert_owned({"Config": {"Labels": labels}}, {"instance": "owner"}, container=True)


def test_remote_docker_context_is_rejected_before_info_or_mutation(tmp_path, monkeypatch):
    config = paths(tmp_path)
    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps([{"Endpoints": {"docker": {"Host": "ssh://remote-host"}}}]))

    monkeypatch.setattr(reviewer, "run", fake_run)
    with pytest.raises(reviewer.ReviewerError, match="local Docker socket"):
        reviewer.local_docker("docker", config)
    assert calls == [["docker", "context", "inspect"]]


def test_inspection_failure_is_not_treated_as_a_missing_resource(tmp_path, monkeypatch):
    config = paths(tmp_path)
    monkeypatch.setattr(reviewer, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="permission denied"))
    with pytest.raises(reviewer.ReviewerError, match="inspection failed"):
        reviewer.inspect("docker", "volume", "name", config)


def test_seed_reuses_saved_project_without_another_post(tmp_path, monkeypatch):
    config = paths(tmp_path)
    project = "prj-" + "a" * 20
    (config["local"] / "project.json").write_text(json.dumps({"id": project}), encoding="utf-8")
    calls = []

    def fake_http(port, path, **kwargs):
        calls.append((path, kwargs))
        return 200, {"id": project}

    monkeypatch.setattr(reviewer, "http", fake_http)
    assert reviewer.seed_project(config, {"instance": "owner"}, "secret") == project
    assert len(calls) == 1
    assert "body" not in calls[0][1]


def test_missing_saved_project_is_not_silently_replaced(tmp_path, monkeypatch):
    config = paths(tmp_path)
    (config["local"] / "project.json").write_text(json.dumps({"id": "prj-" + "a" * 20}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(reviewer, "http", lambda *a, **k: (calls.append(k) or (404, None)))
    with pytest.raises(reviewer.ReviewerError, match="duplicate"):
        reviewer.seed_project(config, {"instance": "owner"}, "secret")
    assert len(calls) == 1 and "body" not in calls[0]


def test_first_project_create_uses_a_stable_key_and_persists_identity(tmp_path, monkeypatch):
    config = paths(tmp_path)
    project = "prj-" + "b" * 20
    calls = []

    def fake_http(*args, **kwargs):
        calls.append(kwargs)
        return 201, {"id": project}

    monkeypatch.setattr(reviewer, "http", fake_http)
    assert reviewer.seed_project(config, {"instance": "abcd"}, "secret") == project
    assert calls[0]["key"] == "reviewer-abcd"
    assert calls[0]["body"]["template"] == "calculator"
    assert json.loads((config["local"] / "project.json").read_text())["id"] == project


def test_layout_keeps_workspaces_outside_source_and_identity_is_stable(tmp_path):
    config = paths(tmp_path)
    first = reviewer.layout(config)
    assert reviewer.layout(config) == first
    assert not Path(first["workspace"]).is_relative_to(config["root"])
    assert first["name"].startswith("agent-factory-postgres-reviewer-")


def test_layout_refuses_workspace_inside_checkout(tmp_path):
    config = paths(tmp_path)
    (config["local"] / "instance.json").write_text(json.dumps({"instance": "a" * 16,
        "workspace": str(config["root"] / "nested"), "ports": reviewer.PORTS}), encoding="utf-8")
    with pytest.raises(reviewer.ReviewerError, match="invalid"):
        reviewer.layout(config)


@pytest.mark.parametrize("status", ["awaiting_approval", "completed", "failed", "awaiting_reconciliation"])
def test_existing_run_is_not_requeued_even_when_terminal(tmp_path, monkeypatch, status):
    config = paths(tmp_path)
    calls = []
    monkeypatch.setattr(reviewer, "http", lambda *a, **k: (200, {"status": status}))
    monkeypatch.setattr(reviewer, "cli", lambda *a, **k: calls.append(a))
    assert reviewer.ensure_run(config, {"workspace": "unused"}, "secret", "prj-" + "a" * 20) == "review-approval"
    assert calls == []


def test_failed_run_read_never_enqueues_a_replacement(tmp_path, monkeypatch):
    config = paths(tmp_path)
    calls = []
    monkeypatch.setattr(reviewer, "http", lambda *a, **k: (503, None))
    monkeypatch.setattr(reviewer, "cli", lambda *a, **k: calls.append(a))
    with pytest.raises(reviewer.ReviewerError, match="no new run"):
        reviewer.ensure_run(config, {"workspace": "unused"}, "secret", "prj-" + "a" * 20)
    assert calls == []


def test_http_client_rejects_authority_paths_and_never_follows_redirects():
    with pytest.raises(reviewer.ReviewerError):
        reviewer.http(18000, "//example.com")
    assert reviewer.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com") is None


def test_html_readiness_request_accepts_vite_spa_response(monkeypatch):
    requests = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return b"<!doctype html><html></html>"

    def open_request(request, **_):
        requests.append(request)
        return Response()

    monkeypatch.setattr(reviewer, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    assert reviewer.http(14173, "/") == (200, None)
    assert "text/html" in requests[0].get_header("Accept")
    assert "application/json" in requests[0].get_header("Accept")


def test_foreign_container_is_never_started_or_changed(tmp_path, monkeypatch):
    config = paths(tmp_path)
    state = reviewer.layout(config)
    commands = []
    monkeypatch.setattr(reviewer, "local_docker", lambda *args: None)
    monkeypatch.setattr(reviewer, "inspect", lambda d, kind, n, p: {"Config": {"Labels": {}}} if kind == "container" else None)
    monkeypatch.setattr(reviewer, "run", lambda argv, *a, **k: commands.append(argv))
    with pytest.raises(reviewer.ReviewerError, match="ownership"):
        reviewer.database_container(config, state, "docker")
    assert commands == []


@pytest.mark.parametrize("problem", [ProcessLookupError("private detail"), OSError("private detail"),
                                   reviewer.subprocess.TimeoutExpired("private detail", 15)])
def test_cleanup_visits_remaining_children_after_one_child_fails(tmp_path, monkeypatch, problem):
    config = paths(tmp_path)
    visited = []

    class Child:
        def __init__(self, name, failure=False):
            self.name, self.failure, self.pid = name, failure, 123

        def poll(self):
            visited.append(self.name)
            if self.failure:
                raise problem
            return 0

    healthy_log, failed_log = io.StringIO(), io.StringIO()
    services = reviewer.Services(config)
    services.children = [("api", Child("api"), healthy_log), ("worker", Child("worker", True), failed_log)]
    with pytest.raises(reviewer.ReviewerError, match="worker") as error:
        services.close()
    assert "api" in visited
    assert healthy_log.closed and failed_log.closed
    assert "private detail" not in str(error.value)


def test_cleanup_tolerates_process_group_already_gone(tmp_path, monkeypatch):
    config = paths(tmp_path)
    waited = []
    child = SimpleNamespace(pid=123, poll=lambda: None, wait=lambda **_: waited.append(True))
    log = io.StringIO()
    services = reviewer.Services(config)
    services.children = [("api", child, log)]
    # Replace only the launcher's os binding, preserving pathlib's platform.
    monkeypatch.setattr(reviewer, "os", SimpleNamespace(name="posix", killpg=lambda *_: (_ for _ in ()).throw(ProcessLookupError())))
    services.close()
    assert waited == [True]
    assert log.closed

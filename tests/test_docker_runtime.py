"""Compose bootstrap boundaries, using only temporary files and fake processes."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("docker_runtime", ROOT / "scripts/docker_runtime.py")
runtime = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runtime)
sys.path.pop(0)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    # The root-only prepare container owns chown. Offline tests do not change
    # host ownership (and run on Windows, where os.chown is absent).
    monkeypatch.setattr(runtime.os, "chown", lambda *a: None, raising=False)
    result = {name: tmp_path / name for name in ("state", "workspaces", "credentials", "database")}
    for path in result.values():
        path.mkdir()
    return result


def paths(roots):
    state = roots["state"]
    return {"root": ROOT, "local": state, "python": Path(sys.executable), "logs": state / "logs",
            "url": state / "database.url", "issuer": state / "dev-issuer", "token": state / "token.txt"}


def files(roots):
    return {str(file): file.read_bytes() for root in roots.values() for file in root.rglob("*") if file.is_file()}


def prepared(roots):
    runtime.prepare("isolated-reviewer", roots)
    return runtime.validate_owner("isolated-reviewer", roots)


def test_new_volumes_get_one_shared_identity_and_private_nonprinted_credentials(roots, capsys):
    owner = prepared(roots)
    assert owner["project"] == "isolated-reviewer" and len(owner["instance"]) == 32
    assert all(json.loads((root / runtime.MARKER).read_text()) == owner for root in roots.values())
    passwords = runtime.load_passwords(roots)
    assert len(set(passwords)) == 2 and all(len(value) == 43 for value in passwords)
    assert not any(value in capsys.readouterr().out for value in passwords)


def test_repeated_prepare_never_replaces_retained_passwords_or_data(roots):
    prepared(roots)
    (roots["database"] / "pgdata").mkdir()
    (roots["database"] / "pgdata" / "retained").write_text("existing database")
    (roots["workspaces"] / "retained.py").write_text("existing generated file")
    before = files(roots)
    runtime.prepare("isolated-reviewer", roots)
    assert files(roots) == before


@pytest.mark.parametrize("name", ["state", "workspaces", "credentials", "database"])
def test_nonempty_unowned_volume_is_never_adopted(roots, name):
    (roots[name] / "existing").write_text("retain me")
    before = files(roots)
    with pytest.raises(runtime.reviewer.ReviewerError, match="unowned"):
        runtime.prepare("isolated-reviewer", roots)
    assert files(roots) == before


@pytest.mark.parametrize("name", ["state", "workspaces", "credentials", "database"])
def test_missing_retained_owner_marker_never_reinitializes_any_volume(roots, name):
    prepared(roots)
    (roots[name] / runtime.MARKER).unlink()
    before = files(roots)
    with pytest.raises(runtime.reviewer.ReviewerError, match="ownership"):
        runtime.prepare("isolated-reviewer", roots)
    assert files(roots) == before


@pytest.mark.parametrize("field,value", [("project", "unrelated"), ("instance", "b" * 32), ("version", 2)])
def test_mixed_or_foreign_volume_identity_is_refused_without_writes(roots, field, value):
    record = prepared(roots)
    record[field] = value
    (roots["workspaces"] / runtime.MARKER).write_text(json.dumps(record))
    before = files(roots)
    with pytest.raises(runtime.reviewer.ReviewerError):
        runtime.prepare("isolated-reviewer", roots)
    assert files(roots) == before


@pytest.mark.parametrize("name", runtime.SECRET_FILES)
def test_missing_retained_password_is_not_regenerated(roots, name):
    prepared(roots)
    (roots["credentials"] / name).unlink()
    before = files(roots)
    with pytest.raises(runtime.reviewer.ReviewerError, match="incomplete"):
        runtime.prepare("isolated-reviewer", roots)
    assert files(roots) == before


@pytest.mark.parametrize("value", ["short", "!" * 43, "a" * 44])
def test_invalid_password_never_repaired_or_printed(roots, value, capsys):
    prepared(roots)
    (roots["credentials"] / runtime.SECRET_FILES[0]).write_text(value)
    before = files(roots)
    with pytest.raises(runtime.reviewer.ReviewerError) as error:
        runtime.load_passwords(roots)
    assert files(roots) == before
    assert value not in str(error.value) + capsys.readouterr().out


@pytest.mark.parametrize("invalid", ["", "../other", "UPPERCASE", "a" * 64])
def test_invalid_project_cannot_initialize_volumes(roots, invalid):
    with pytest.raises(runtime.reviewer.ReviewerError, match="project"):
        runtime.prepare(invalid, roots)
    assert files(roots) == {}


@pytest.mark.parametrize("url", ["postgresql://foreign/db", "postgresql://other:secret@db:5432/agent_factory_compose",
                                  "postgresql://agent_factory_app:secret@db:5432/agent_factory_compose?hostaddr=203.0.113.4"])
def test_foreign_database_url_is_refused_before_importing_driver(roots, monkeypatch, url):
    prepared(roots)
    config = paths(roots)
    config["url"].write_text(url)
    before = files(roots)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "psycopg":
            pytest.fail("Foreign database URL reached the driver")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(runtime.reviewer.ReviewerError, match="configuration differs"):
        runtime.provision(config, roots)
    assert files(roots) == before


def test_foreign_ownership_does_not_delete_readiness_or_create_logs(roots, monkeypatch):
    prepared(roots)
    config = paths(roots)
    ready = config["local"] / "ready.json"
    ready.write_text("existing readiness")
    before = files(roots)
    monkeypatch.setattr(runtime, "runtime_paths", lambda: config)
    monkeypatch.setattr(runtime, "provision", lambda *a: pytest.fail("Foreign volumes reached provisioning"))
    with pytest.raises(runtime.reviewer.ReviewerError):
        runtime.serve("other-project", roots)
    assert files(roots) == before
    assert not config["logs"].exists()


def test_busy_instance_keeps_existing_readiness_and_never_provisions(roots, monkeypatch):
    prepared(roots)
    config = paths(roots)
    (config["local"] / "ready.json").write_text("existing readiness")
    before = files(roots)
    monkeypatch.setattr(runtime, "runtime_paths", lambda: config)

    @contextmanager
    def busy(_paths):
        raise runtime.reviewer.ReviewerError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(runtime.reviewer, "exclusive", busy)
    monkeypatch.setattr(runtime, "provision", lambda *a: pytest.fail("Busy instance reached provisioning"))
    with pytest.raises(runtime.reviewer.ReviewerError, match="busy"):
        runtime.serve("isolated-reviewer", roots)
    assert files(roots) == before


def test_retained_project_with_lost_signing_identity_is_not_reinitialized(roots, monkeypatch):
    prepared(roots)
    config = paths(roots)
    (config["local"] / "project.json").write_text(json.dumps({"id": "prj-" + "a" * 20}))
    monkeypatch.setattr(runtime, "runtime_paths", lambda: config)
    monkeypatch.setattr(runtime, "provision", lambda *a: pytest.fail("Lost identity reached database mutation"))
    monkeypatch.setattr(runtime, "identity", lambda *a: pytest.fail("Lost identity was replaced"))
    with pytest.raises(runtime.reviewer.ReviewerError, match="missing its signing identity"):
        runtime.serve("isolated-reviewer", roots)
    assert not config["issuer"].exists()


def test_token_refresh_cannot_replace_missing_signing_identity(roots, monkeypatch):
    prepared(roots)
    monkeypatch.setenv("AF_COMPOSE_PROJECT", "isolated-reviewer")
    monkeypatch.setattr(runtime.reviewer, "sanitize_process_environment", lambda: None)
    monkeypatch.setattr(runtime.sys, "path", list(sys.path))
    monkeypatch.setattr(runtime, "volume_roots", lambda: roots)
    monkeypatch.setattr(runtime, "runtime_paths", lambda: paths(roots))
    monkeypatch.setattr(runtime, "identity", lambda *a: pytest.fail("Refresh initialized another signing key"))
    assert runtime.main(["token"]) == 1
    assert not paths(roots)["issuer"].exists()


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval", "completed", "failed", "blocked", "exhausted"])
def test_every_existing_run_is_reused_without_enqueue(roots, monkeypatch, status):
    monkeypatch.setattr(runtime.reviewer, "http", lambda *a, **k: (200, {"status": status}))
    monkeypatch.setattr(runtime, "cli", lambda *a, **k: pytest.fail("Existing run was requeued"))
    assert runtime.ensure_run(paths(roots), roots["workspaces"], "token", "prj-" + "a" * 20) == "review-approval"


@pytest.mark.parametrize("status", [401, 403, 500, None])
def test_unknown_saved_run_does_not_create_replacement(roots, monkeypatch, status):
    monkeypatch.setattr(runtime.reviewer, "http", lambda *a, **k: (status, None))
    monkeypatch.setattr(runtime, "cli", lambda *a, **k: pytest.fail("Uninspectable run was replaced"))
    with pytest.raises(runtime.reviewer.ReviewerError, match="no replacement"):
        runtime.ensure_run(paths(roots), roots["workspaces"], "token", "prj-" + "a" * 20)


def test_only_explicit_missing_run_enqueues_fixed_fixture(roots, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.reviewer, "http", lambda *a, **k: (404, None))
    monkeypatch.setattr(runtime, "cli", lambda *a, **k: calls.append((a, k)))
    runtime.ensure_run(paths(roots), roots["workspaces"], "token", "prj-" + "a" * 20)
    assert len(calls) == 1
    args = calls[0][0]
    assert args[1:3] == ("worker", "enqueue")
    assert args[args.index("--scenario") + 1] == "approval"
    assert args[args.index("--workspace-base") + 1] == str(roots["workspaces"])
    assert "--no-git" in args
    assert "token" not in args


@pytest.mark.parametrize("ready", [{}, ["api", "worker", "frontend"], {"api": 100, "frontend": 101}, {"api": 100, "worker": 1, "frontend": 101},
                                  {"api": 100, "worker": "102", "frontend": 101}])
def test_health_fails_for_incomplete_or_invalid_child_identity(roots, monkeypatch, ready):
    config = paths(roots)
    (config["local"] / "ready.json").write_text(json.dumps(ready))
    monkeypatch.setattr(runtime.os, "kill", lambda *a: None)
    monkeypatch.setattr(runtime.reviewer, "http", lambda *a, **k: pytest.fail("Invalid child identity reached HTTP"))
    assert not runtime.health(config)


def test_health_requires_live_worker_and_anonymous_auth_refusal(roots, monkeypatch):
    config = paths(roots)
    (config["local"] / "ready.json").write_text(json.dumps({"api": 100, "worker": 102, "frontend": 101}))
    monkeypatch.setattr(runtime.os, "kill", lambda *a: None)
    monkeypatch.setattr(runtime.reviewer, "http", lambda port, path: (200 if path != "/api/v1/projects" else 401, None))
    assert runtime.health(config)
    monkeypatch.setattr(runtime.reviewer, "http", lambda *a, **k: (200, None))
    assert not runtime.health(config)
    monkeypatch.setattr(runtime.os, "kill", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    assert not runtime.health(config)


@pytest.mark.parametrize("failure", ["anonymous_access", "proxy", "readiness_cleanup"])
def test_startup_failure_still_closes_owned_children(roots, monkeypatch, failure):
    prepared(roots)
    config = paths(roots)
    config["token"].write_text("private-test-token")
    monkeypatch.setattr(runtime, "runtime_paths", lambda: config)
    monkeypatch.setattr(runtime, "provision", lambda *a: None)
    monkeypatch.setattr(runtime, "identity", lambda *a: {})
    monkeypatch.setattr(runtime, "ensure_run", lambda *a: "review-approval")
    monkeypatch.setattr(runtime.reviewer, "seed_project", lambda *a: "prj-" + "a" * 20)
    started, closed = [], []

    class Services:
        def __init__(self, _paths):
            self.children = []
        def start(self, name, args, *other):
            started.append((name, args))
            self.children.append((name, SimpleNamespace(pid=100 + len(started)), None))
        def wait_http(self, *args):
            pass
        def healthy(self):
            raise runtime.reviewer.ReviewerError("owned child exited")
        def close(self):
            closed.append(True)

    monkeypatch.setattr(runtime.reviewer, "Services", Services)
    def http(_port, _path, **kwargs):
        if kwargs.get("token"):
            return (503 if failure == "proxy" else 200), None
        return (200 if failure == "anonymous_access" else 401), None
    monkeypatch.setattr(runtime.reviewer, "http", http)
    ready = config["local"] / "ready.json"
    original_unlink = Path.unlink
    if failure == "readiness_cleanup":
        def unlink(path, *args, **kwargs):
            if path == ready and path.exists():
                raise OSError("readiness cleanup failed")
            return original_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError if failure == "readiness_cleanup" else runtime.reviewer.ReviewerError):
        runtime.serve("isolated-reviewer", roots)
    assert closed == [True]
    assert started[0][0] == "api"
    api_args = started[0][1]
    assert api_args[api_args.index("--host") + 1] == "127.0.0.1"
    if failure != "anonymous_access":
        frontend_args = next(args for name, args in started if name == "frontend")
        assert frontend_args[frontend_args.index("--configLoader") + 1] == "native"  # no config cache write in read-only image


def test_main_strips_inherited_live_provider_and_libpq_settings(roots, monkeypatch):
    monkeypatch.setattr(os, "environ", {"AF_COMPOSE_PROJECT": "isolated-reviewer", "PGHOSTADDR": "foreign",
                                       "AGENT_FACTORY_MODEL_MODE": "live", "AWS_ACCESS_KEY_ID": "secret", "HTTP_PROXY": "foreign"})
    monkeypatch.setattr(runtime, "volume_roots", lambda: roots)
    monkeypatch.setattr(runtime, "runtime_paths", lambda: paths(roots))
    monkeypatch.setattr(runtime.sys, "path", list(sys.path))
    seen = []
    monkeypatch.setattr(runtime, "prepare", lambda project, _: seen.append((project, dict(os.environ))))
    assert runtime.main(["prepare"]) == 0
    assert len(seen) == 1 and seen[0][0] == "isolated-reviewer"
    sanitized = seen[0][1]
    assert sanitized["AWS_EC2_METADATA_DISABLED"] == "true"
    assert not {"PGHOSTADDR", "AGENT_FACTORY_MODEL_MODE", "AWS_ACCESS_KEY_ID", "HTTP_PROXY"} & sanitized.keys()
    assert "foreign" not in sanitized.values() and "secret" not in sanitized.values()


def test_compose_topology_keeps_db_private_and_runtime_restricted():
    import yaml

    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    app, database, prepare = (config["services"][name] for name in ("app", "db", "prepare"))
    assert app["ports"] == ["127.0.0.1:${AF_DASHBOARD_PORT:-15473}:14173"]
    assert "ports" not in database and "ports" not in prepare
    assert config["networks"]["reviewer"]["internal"] is True
    assert database["networks"] == ["reviewer"]
    assert set(app["networks"]) == {"reviewer", "dashboard"}
    assert not config["networks"].get("dashboard") or not config["networks"]["dashboard"].get("external", False)
    assert prepare["network_mode"] == "none"
    assert app["cap_drop"] == ["ALL"] and app["read_only"] is True
    assert "no-new-privileges:true" in app["security_opt"]
    assert not any("database:" in mount for mount in app["volumes"])
    for service in config["services"].values():
        assert not service.get("privileged", False)
        assert "container_name" not in service and "pid" not in service
        for mount in service.get("volumes", []):
            assert mount.split(":", 1)[0] in config["volumes"]  # named volumes only, no socket/host mounts
        assert not any(name.startswith(("AWS_", "ANTHROPIC_", "OPENAI_", "AGENT_FACTORY_MODEL_"))
                       for name in service.get("environment", {}))
    assert all(not value or not value.get("external", False) for value in config["volumes"].values())
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert all("@sha256:" in line for line in dockerfile.splitlines() if line.startswith("FROM "))


def test_docker_context_secret_exclusions_override_source_allowlist():
    lines = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert lines[0] == "**"  # New top-level files are excluded unless deliberately included.
    last_inclusion = max(i for i, line in enumerate(lines) if line.startswith("!"))
    # Docker applies patterns in order: a later source include must never reopen
    # credential directories or key files inside otherwise allowed source trees.
    for pattern in ("**/.local/", "**/.venv/", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
                    "**/secrets/", "**/credentials.json", "**/.env", "**/.env.*"):
        assert pattern in lines and lines.index(pattern) > last_inclusion

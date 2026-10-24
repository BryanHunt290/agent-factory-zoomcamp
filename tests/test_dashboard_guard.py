"""The process guard for tests in both the browser and a docker suite (tests/conftest.py).

The PostgreSQL-restart dashboard scenario must start the docker CLI and also the API, workers, the
frontend server, and the browser driver. Its guard allows exactly those executables, and never anything
that names a hostile fixture; the live and adversarial suites keep their docker-only guard.
"""

import sys
from pathlib import Path

import conftest
import pytest


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(conftest, "_real_popen_init", lambda self, args, *a, **kw: calls.append(args))
    return calls


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "restart", "agent-factory-postgres-dev"],
        [str(Path("installed tools") / "Docker" / "docker.exe"), "ps"],
        ["node", "node_modules/vite/bin/vite.js", "preview"],
        [str(Path("venv") / "Lib" / "site-packages" / "playwright" / "driver" / "node.exe"), "cli.js", "run-driver"],
        [sys.executable, "-m", "agent_factory", "api", "serve"],
    ],
)
def test_the_allowed_executables_start(launched, argv):
    conftest._browser_and_docker_popen_init(object(), argv)
    assert launched == [argv]


@pytest.mark.parametrize(
    "argv",
    [
        [sys.executable, "tests/hostile/fork_bomb.hpy"],
        ["node", r"tests\hostile\anything.js"],
        ["docker", "run", "x", "/work/escape.HPY"],
        ["cmd.exe", "/c", "dir"],
        ["powershell", "-Command", "Get-Process"],
        ["python", "-c", "print(1)"],  # another interpreter than this one
        "bash -c id",
    ],
)
def test_everything_else_is_refused(launched, argv):
    with pytest.raises(conftest.ForbiddenOperation):
        conftest._browser_and_docker_popen_init(object(), argv)
    assert launched == []


def test_the_live_suites_keep_their_docker_only_guard(launched, monkeypatch):
    monkeypatch.setattr(conftest, "_real_popen_init", lambda self, args, *a, **kw: launched.append(args))
    with pytest.raises(conftest.ForbiddenOperation):
        conftest._docker_only_popen_init(object(), ["node", "x.js"])
    with pytest.raises(conftest.ForbiddenOperation):
        conftest._docker_only_popen_init(object(), [sys.executable, "-c", "1"])


def test_only_this_projects_database_container_may_be_stopped(monkeypatch):
    """The dashboard's PostgreSQL scenario stops a container: only this project's, serving the test database."""
    import subprocess
    from types import SimpleNamespace

    import dashboard_support

    asked = []

    def docker(*args, timeout=180):
        asked.append(args)
        return subprocess.CompletedProcess(["docker", *args], 0, stdout="0.0.0.0:55432\n", stderr="")

    monkeypatch.setattr(dashboard_support, "docker", docker)
    test_database = SimpleNamespace(port=55432)
    monkeypatch.setenv("AGENT_FACTORY_TEST_POSTGRES_CONTAINER", "pgvector")
    with pytest.raises(AssertionError, match="refusing"):
        dashboard_support.guarded_container(test_database)
    assert asked == []  # refused before Docker is even asked
    monkeypatch.setenv("AGENT_FACTORY_TEST_POSTGRES_CONTAINER", "agent-factory-postgres-dev")
    with pytest.raises(AssertionError, match="does not publish"):
        dashboard_support.guarded_container(SimpleNamespace(port=5432))  # another database's port
    assert dashboard_support.guarded_container(test_database) == "agent-factory-postgres-dev"

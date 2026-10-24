"""``python -m agent_factory worker run | enqueue``: configuration checks, and two real worker processes.

The PostgreSQL test starts the CLI as separate processes, as a developer
would in two terminals, and checks what they print and what they did.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_factory.cli import main

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("args,message", [
    (["--lease", "1"], "lease must be between"),
    (["--lease", "301"], "lease must be between"),
    (["--lease", "60", "--heartbeat", "30"], "three times per lease"),
    (["--step-budget", "0"], "step_budget"),
    (["--poll", "0"], "poll_interval"),
])  # fmt: skip
def test_worker_run_refuses_unsafe_settings_before_touching_the_database(args, message, capsys, monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGENT_FACTORY_DATABASE_URL_FILE", raising=False)
    assert main(["worker", "run", *args]) == 2
    assert message in capsys.readouterr().err


def test_worker_run_needs_a_database_configuration(capsys, monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGENT_FACTORY_DATABASE_URL_FILE", raising=False)
    assert main(["worker", "run"]) == 2
    assert "AGENT_FACTORY_DATABASE_URL" in capsys.readouterr().err


def _cli(*args, env, timeout=300):
    return subprocess.run([sys.executable, "-m", "agent_factory", *args], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=timeout)  # fmt: skip


@pytest.mark.postgres
def test_two_worker_processes_from_the_cli_share_the_queued_runs(pg_schema, pg_open, tmp_path):
    from agent_factory.domain import Project
    from agent_factory.domain.ids import new_project_id
    from agent_factory.templates import TEMPLATES
    from agent_factory.util import timestamp, utc_now

    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_FACTORY_DATABASE")}
    env.update(AGENT_FACTORY_DATABASE_URL=pg_schema.url, AGENT_FACTORY_DATABASE_SCHEMA=pg_schema.schema)
    store = pg_open()
    template = TEMPLATES["calculator"]
    project = Project(id=new_project_id(), name="cli worker", created_at=timestamp(utc_now),
                      milestone_policy=template.milestone_policy, template=template.name)  # fmt: skip
    store.projects.add(project)  # as the API would have created it
    base = tmp_path / "workspaces"
    for run_id, scenario in (("run-a", "repair"), ("run-b", "review")):
        queued = _cli("worker", "enqueue", "--project-id", project.id, "--run-id", run_id, "--scenario", scenario,
                      "--workspace-base", str(base), "--no-git", env=env)  # fmt: skip
        assert queued.returncode == 0, queued.stderr
        assert f"created and queued run {run_id}" in queued.stdout
    again = _cli("worker", "enqueue", "--project-id", project.id, "--run-id", "run-a", "--workspace-base", str(base), env=env)
    assert again.returncode == 0 and "queued existing run run-a" in again.stdout

    args = ["worker", "run", "--workspace-base", str(base), "--idle-exit", "4", "--poll", "0.2", "--lease", "10", "--heartbeat", "3"]
    procs = [subprocess.Popen([sys.executable, "-m", "agent_factory", *args], cwd=REPO, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]  # fmt: skip
    outputs = [p.communicate(timeout=300) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], [err for _, err in outputs]
    ids = [re.search(r"^worker (wkr-[0-9a-f]{20}) \(pid \d+ on ", out, re.M).group(1) for out, _ in outputs]
    assert ids[0] != ids[1]  # each process has its own identity, shown when it starts
    events = [json.loads(line) for out, _ in outputs for line in out.splitlines() if line.startswith("{")]
    claimed = [e["run_id"] for e in events if e["event"] == "run_claimed"]
    released = [(e["run_id"], e["reason"]) for e in events if e["event"] == "run_released"]
    finished = sorted(run for run, reason in released if reason == "finished")
    assert finished == ["run-a", "run-b"]  # each run finished once, by one of the two workers
    # Both runs share the project's workspace, so they never run at the same time: a run whose workspace
    # was busy was released at once, untouched, and claimed again shortly.
    busy = [run for run, reason in released if reason == "run_busy"]
    assert sorted(claimed) == sorted(["run-a", "run-b"] + busy)
    assert sorted(e["reason"] for e in events if e["event"] == "worker_stopped") == ["idle", "idle"]
    for out, err in outputs:
        assert pg_schema.url not in out + err and "password" not in (out + err).lower()  # never printed
    assert {store.runs.get(project.id, r).status.value for r in ("run-a", "run-b")} == {"completed"}

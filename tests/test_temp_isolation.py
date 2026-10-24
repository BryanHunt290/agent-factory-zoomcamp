"""Test infrastructure: every pytest run has its own temporary root, outside this repository.

pyproject.toml used to pin ``--basetemp=.pytest_tmp``: one directory inside
the repository, shared by every run and wiped when a run starts. Concurrent
runs then deleted each other's files, and every test workspace (a git
repository) was nested inside Agent Factory's own repository. Without the
pin, pytest gives each run a numbered directory under the system temp
directory, locked while the run is alive; conftest.py refuses a temporary root
inside the repository.
"""

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE_REPORT = "AGENT_FACTORY_TEMP_PROBE_REPORT"
PROBE = "tests/test_temp_isolation.py::test_this_runs_temporary_root_is_its_own_and_outside_the_repository"


def inside_repository(path):
    path = Path(path).resolve()
    return path == REPO or REPO in path.parents


def run_pytest(*args, **env):
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=REPO,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_this_runs_temporary_root_is_its_own_and_outside_the_repository(tmp_path, tmp_path_factory):
    """Also the probe that the concurrency test below runs in several pytest processes at once."""
    basetemp = tmp_path_factory.getbasetemp()
    assert not inside_repository(basetemp)
    mine = tmp_path / "mine.txt"
    mine.write_text(str(os.getpid()), encoding="utf-8")
    report = os.environ.get(PROBE_REPORT)
    if report:  # a child run: stay alive while the other runs start, work, and clean up
        time.sleep(2)
        Path(report).write_text(json.dumps({"basetemp": str(basetemp)}), encoding="utf-8")
    assert mine.read_text(encoding="utf-8") == str(os.getpid())


def test_the_configuration_pins_no_shared_temporary_directory():
    options = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert "--basetemp" not in options.get("addopts", "")


def test_concurrent_pytest_runs_never_share_or_delete_each_others_files(tmp_path):
    reports = [tmp_path / f"run-{n}.json" for n in range(3)]
    runs = [run_pytest(PROBE, **{PROBE_REPORT: str(report)}) for report in reports]
    outputs = [run.communicate(timeout=180)[0] for run in runs]
    assert [run.returncode for run in runs] == [0, 0, 0], outputs
    basetemps = {json.loads(report.read_text(encoding="utf-8"))["basetemp"] for report in reports}
    assert len(basetemps) == 3 and not any(inside_repository(b) for b in basetemps)


def test_a_temporary_root_inside_the_repository_is_refused():
    inside = REPO / ".pytest_tmp_refused"  # must never be created: the run stops before using it
    run = run_pytest(f"--basetemp={inside}", "tests/test_temp_isolation.py::test_the_configuration_pins_no_shared_temporary_directory")
    output = run.communicate(timeout=180)[0]
    assert run.returncode == 4 and "inside the repository" in output, output
    assert not inside.exists()

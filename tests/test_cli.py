import shutil

import pytest

from agent_factory.cli import main
from agent_factory.engine import CheckpointStore
from agent_factory.state import RunStatus
from agent_factory.workspace import Workspace


def test_cli_demo_repair(tmp_path, capsys):
    ws = tmp_path / "demo"
    assert main(["demo", "--workspace", str(ws), "--no-git"]) == 0
    out = capsys.readouterr().out
    assert "COMPLETED" in out and "[fail]" in out and "-> DONE" in out
    assert main(["demo", "--workspace", str(ws), "--no-git"]) == 2  # refuses to clobber
    assert main(["log", "--workspace", str(ws), "--run-id", "demo"]) == 0
    assert "hash chain verified" in capsys.readouterr().out


def test_cli_approval_flow(tmp_path, capsys):
    ws = tmp_path / "demo"
    assert main(["demo", "--scenario", "approval", "--workspace", str(ws), "--no-git"]) == 0
    assert "APPROVAL REQUIRED" in capsys.readouterr().out
    state = CheckpointStore(Workspace.open(ws).runs_dir).load("demo")
    request_id = state.pending_approval["request_id"]

    assert main(["resume", "--workspace", str(ws), "--run-id", "demo"]) == 2  # still pending
    assert main(["approve", "--workspace", str(ws), "--run-id", "demo", "--request-id", request_id]) == 0
    assert main(["resume", "--workspace", str(ws), "--run-id", "demo"]) == 0
    state = CheckpointStore(Workspace.open(ws).runs_dir).load("demo")
    assert state.status is RunStatus.COMPLETED
    assert not (ws / "project" / "legacy_calc.py").exists()


def test_cli_reconciliation_flow(tmp_path, capsys, monkeypatch):
    """An approved deletion whose outcome stays unknown: status explains it, a person reconciles, the run continues."""
    from agent_factory.tools.base import OutcomeUnknown
    from agent_factory.tools.filesystem import DeleteFileTool

    ws = tmp_path / "demo"
    assert main(["demo", "--scenario", "approval", "--workspace", str(ws), "--no-git"]) == 0
    request_id = CheckpointStore(Workspace.open(ws).runs_dir).load("demo").pending_approval["request_id"]
    assert main(["approve", "--workspace", str(ws), "--run-id", "demo", "--request-id", request_id]) == 0

    def unknown(self, workspace, *, path):
        raise OutcomeUnknown("the filesystem stopped answering")

    monkeypatch.setattr(DeleteFileTool, "run", unknown)  # every automatic attempt ends without an outcome
    assert main(["resume", "--workspace", str(ws), "--run-id", "demo"]) == 5  # awaiting reconciliation
    capsys.readouterr()
    assert main(["status", "--workspace", str(ws), "--run-id", "demo"]) == 0
    out = capsys.readouterr().out
    assert "RECONCILIATION REQUIRED" in out and "reconcile-action" in out and "delete_file" in out
    pending = CheckpointStore(Workspace.open(ws).runs_dir).load("demo").pending_reconciliation
    assert main(["resume", "--workspace", str(ws), "--run-id", "demo"]) == 2  # refused while unreconciled
    monkeypatch.undo()
    args = ["reconcile-action", "--workspace", str(ws), "--run-id", "demo", "--execution-id", pending["execution_id"],
            "--attempt", str(pending["attempt"]), "--fingerprint", pending["action_fingerprint"]]  # fmt: skip
    assert main([*args, "--choice", "it-happened", "--approver", "dana"]) == 0
    assert main(["resume", "--workspace", str(ws), "--run-id", "demo"]) == 0
    state = CheckpointStore(Workspace.open(ws).runs_dir).load("demo")
    assert state.status is RunStatus.COMPLETED
    assert (ws / "project" / "legacy_calc.py").exists()  # the person said it happened: it was never re-run


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_cli_fresh_recreates_git_workspace(tmp_path, capsys):
    # Regression: git marks object files read-only on Windows, which made
    # --fresh crash half-way through deleting the previous workspace.
    ws = tmp_path / "demo"
    assert main(["demo", "--workspace", str(ws)]) == 0
    assert (ws / "project" / ".git").is_dir()
    assert main(["demo", "--workspace", str(ws), "--fresh"]) == 0
    assert "COMPLETED" in capsys.readouterr().out
    assert CheckpointStore(Workspace.open(ws).runs_dir).load("demo").status is RunStatus.COMPLETED


def test_cli_fresh_refuses_non_workspace(tmp_path, capsys):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("important")
    assert main(["demo", "--workspace", str(target), "--fresh", "--no-git"]) == 2
    assert (target / "keep.txt").exists()

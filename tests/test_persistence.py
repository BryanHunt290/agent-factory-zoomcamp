import json
import os
import shutil

import pytest

from agent_factory import util
from agent_factory.engine.checkpoint import CheckpointNotFound, CheckpointStore
from agent_factory.engine.log import ExecutionLog, LogIntegrityError
from agent_factory.state import ProjectState, RunStatus, StateError, TaskSpec
from agent_factory.workspace import PathViolation, Workspace, WorkspaceError


def _state(run_id="r1"):
    return ProjectState(
        run_id=run_id,
        graph_name="g",
        task=TaskSpec(title="t", goal="g", requirements=["a"]),
        current_node="planner",
    )


def test_state_round_trip_and_snapshot_is_independent():
    s = _state()
    s.plan = {"steps": [{"id": 1}]}
    s.status = RunStatus.AWAITING_APPROVAL
    loaded = ProjectState.from_dict(json.loads(json.dumps(s.to_dict())))
    assert loaded == s
    snap = s.snapshot()
    snap.plan["steps"].append({"id": 2})
    assert len(s.plan["steps"]) == 1


def test_state_rejects_unknown_schema():
    data = _state().to_dict()
    data["schema_version"] = 99
    with pytest.raises(StateError):
        ProjectState.from_dict(data)


def test_checkpoint_save_load(tmp_path):
    store = CheckpointStore(tmp_path)
    s = _state()
    store.save(s)
    assert store.load("r1") == s
    assert store.list_runs() == ["r1"]
    text = store.path("r1").read_text(encoding="utf-8")
    assert text.endswith("\n") and '\r' not in text
    assert not list(tmp_path.rglob("*.tmp"))
    with pytest.raises(CheckpointNotFound):
        store.load("missing")
    with pytest.raises(ValueError):
        store.path("../escape")


def _denied_replace(monkeypatch, *, transient, denials):
    """Make os.replace refuse ``denials`` times, as Windows does while another process has the file open."""
    real, calls = os.replace, []

    def replace(source, target):
        calls.append(target)
        if len(calls) <= denials:
            raise PermissionError(13, "Access is denied")
        real(source, target)

    monkeypatch.setattr(util, "_REPLACE_DENIAL_IS_TRANSIENT", transient)
    monkeypatch.setattr(util, "_REPLACE_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(os, "replace", replace)
    return calls


def test_a_transient_replace_denial_is_retried(tmp_path, monkeypatch):
    calls = _denied_replace(monkeypatch, transient=True, denials=2)
    util.write_json_atomic(tmp_path / "state.json", {"a": 1})
    assert len(calls) == 3
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_replace_denial_that_persists_still_raises(tmp_path, monkeypatch):
    calls = _denied_replace(monkeypatch, transient=True, denials=99)
    with pytest.raises(PermissionError):
        util.write_json_atomic(tmp_path / "state.json", {"a": 1})
    assert len(calls) == 4  # bounded: three retries, then the error
    assert not (tmp_path / "state.json").exists()


def test_a_replace_denial_is_not_retried_where_it_is_not_transient(tmp_path, monkeypatch):
    calls = _denied_replace(monkeypatch, transient=False, denials=1)  # POSIX: a real permission problem
    with pytest.raises(PermissionError):
        util.write_json_atomic(tmp_path / "state.json", {"a": 1})
    assert len(calls) == 1


@pytest.mark.stress
def test_rapid_checkpoint_rewrites_survive_transient_replace_denials(tmp_path):
    """Opt-in (--run-stress): thousands of real replaces, as a busy run makes them; nothing is mocked."""
    store = CheckpointStore(tmp_path)
    state = _state()
    for i in range(3000):
        state.plan = {"rewrite": i}
        store.save(state)
        assert store.load("r1").plan == {"rewrite": i}


def test_log_is_append_only_and_hash_chained(tmp_path):
    path = tmp_path / "log.jsonl"
    log = ExecutionLog(path)
    log.append("node_entered", node="planner")
    log.append("transition", source="planner", target="developer")
    # A new instance continues the chain (resume after restart).
    log2 = ExecutionLog(path)
    e = log2.append("node_entered", node="developer")
    assert e["seq"] == 3
    assert log2.verify() == 3
    assert all("timestamp" in x for x in log2.entries())

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["target"] = "done"
    lines[1] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(LogIntegrityError, match="hash mismatch"):
        ExecutionLog(path).verify()

    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(LogIntegrityError):
        ExecutionLog(path).verify()


def test_log_rejects_reserved_fields(tmp_path):
    with pytest.raises(ValueError):
        ExecutionLog(tmp_path / "l.jsonl").append("x", seq=5)


def test_workspace_confines_paths(tmp_path):
    ws = Workspace.create(tmp_path / "ws", scaffold={"tests/test_a.py": "x\n", "a.py": "y\n"}, protected=["tests"])
    assert ws.list_files() == ["a.py", "tests/test_a.py"]
    ws.write_text("pkg/b.py", "z\n")
    assert ws.read_text("pkg/b.py") == "z\n"
    for bad in ["../outside.py", "/etc/passwd", "C:/Windows/x", "a/../../x", ".git/config", ""]:
        with pytest.raises(PathViolation):
            ws.write_text(bad, "nope")
    with pytest.raises(PathViolation, match="protected"):
        ws.write_text("tests/test_a.py", "overwritten")
    with pytest.raises(PathViolation, match="protected"):
        ws.delete("tests/test_a.py")
    assert ws.read_text("tests/test_a.py") == "x\n"
    assert not (tmp_path / "outside.py").exists()
    with pytest.raises(WorkspaceError):
        Workspace.create(tmp_path / "ws")
    reopened = Workspace.open(tmp_path / "ws")
    assert reopened.protected == ("tests",)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_workspace_git_snapshots(tmp_path):
    ws = Workspace.create(tmp_path / "ws", scaffold={"a.py": "1\n"}, git=True)
    assert ws.is_git
    assert ws.commit_all("noop") is None
    ws.write_text("a.py", "2\n")
    sha = ws.commit_all("change")
    assert sha and len(sha) == 40
    assert ws.list_files() == [".gitignore", "a.py"]

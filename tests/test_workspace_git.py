"""Workspace git confinement: git acts on exactly <workspace>/project/.git, or not at all.

Git finds a repository by walking up from its working directory. A workspace
whose own .git had gone missing therefore ran its git commands in whatever
repository contained it: during E1-E2 testing, a concurrent pytest run deleted
a test workspace's .git and the next commands set ``user.email`` in, and very
likely staged every file of, Agent Factory's own repository.

Every scenario here nests the workspace inside a parent repository, with a
commit, a staged file, and local config, and checks that the parent's .git is
byte-for-byte unchanged afterwards.
"""

import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from agent_factory.tools.base import ToolError
from agent_factory.tools.git import GitCommitTool
from agent_factory.workspace import GitConfinementError, Workspace, WorkspaceError


def git(cwd, *args):
    """Plain git for building fixtures, isolated from the user's configuration and environment."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def remove_tree(path):
    """Delete a directory tree, including git's read-only object files on Windows."""

    def force(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=force)


def link_directory(link, target):
    """A directory link git follows: a junction on Windows (no privilege needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def snapshot(repo):
    """Every file under ``repo/.git``, byte for byte."""
    git_dir = Path(repo) / ".git"
    return {p.relative_to(git_dir).as_posix(): p.read_bytes() for p in sorted(git_dir.rglob("*")) if p.is_file()}


def same_place(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@pytest.fixture
def parent(tmp_path):
    """The bystander repository the workspace is nested in: one commit, one staged file, local config."""
    root = tmp_path / "parent"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Parent Owner")
    git(root, "config", "user.email", "owner@example.test")
    (root / "tracked.txt").write_text("committed\n", encoding="utf-8")
    git(root, "add", "tracked.txt")
    git(root, "commit", "-q", "-m", "parent commit")
    (root / "staged.txt").write_text("staged, not committed\n", encoding="utf-8")
    git(root, "add", "staged.txt")
    return root


def nested_workspace(parent, *, git_repo=True):
    return Workspace.create(parent / "ws", scaffold={"app.py": "v1\n"}, git=git_repo)


# ------------------------------------------------------------- 1. normal use


def test_a_workspace_repository_works_and_is_its_own(parent):
    before = snapshot(parent)
    ws = nested_workspace(parent)
    assert ws.is_git and ws.expects_git
    ws.write_text("app.py", "v2\n")
    sha = ws.commit_all("change")
    assert sha == ws._git("rev-parse", "HEAD")
    assert ws._git("log", "--format=%s").splitlines() == ["change", "agent-factory: initial scaffold"]
    assert ws._git("log", "-1", "--format=%an <%ae>") == "Agent Factory <agent-factory@localhost.invalid>"
    assert ws._git("status", "--porcelain") == ""
    assert ws.commit_all("unchanged") is None
    assert snapshot(parent) == before  # creating and using a nested workspace never touches its parent


def test_a_workspace_created_without_git_still_skips_commits(parent):
    before = snapshot(parent)
    ws = nested_workspace(parent, git_repo=False)
    result = GitCommitTool().run(ws, message="change")
    assert result.data is None and "skipped" in result.summary
    assert not os.path.lexists(ws.project_dir / ".git")
    assert snapshot(parent) == before


# ------------------------------------------------- 2. a deleted repository


def test_a_deleted_repository_refuses_every_git_operation(tmp_path):
    ws = Workspace.create(tmp_path / "ws", scaffold={"app.py": "v1\n"}, git=True)
    remove_tree(ws.project_dir / ".git")
    ws.write_text("app.py", "v2\n")
    for operation in (
        lambda: ws.commit_all("change"),
        lambda: ws._git("add", "-A"),
        lambda: ws._git("status", "--porcelain"),
        lambda: ws._git("config", "user.email", "someone@example.test"),
    ):
        with pytest.raises(GitConfinementError, match="no repository"):
            operation()
    # The workspace was created with git, so its tool refuses instead of skipping silently.
    with pytest.raises(ToolError, match="no repository"):
        GitCommitTool().run(ws, message="change")
    assert not os.path.lexists(ws.project_dir / ".git")  # and nothing initialized a new one


# ------------------------- 3 and 5. nested, its own .git missing: the parent is untouched


def test_a_nested_workspace_without_its_repository_leaves_the_parent_byte_for_byte_unchanged(parent):
    ws = nested_workspace(parent)
    remove_tree(ws.project_dir / ".git")
    # Control: unconfined git, run from project/, now finds the parent. This is how the escape happened.
    assert same_place(git(ws.project_dir, "rev-parse", "--show-toplevel"), parent)
    before = snapshot(parent)
    index, config, head = (parent / ".git" / name for name in ("index", "config", "HEAD"))
    index_bytes, config_bytes, head_bytes = index.read_bytes(), config.read_bytes(), head.read_bytes()

    ws.write_text("app.py", "v2\n")
    for operation in (
        lambda: ws._git("config", "user.email", "agent-factory@localhost.invalid"),  # the incident's command
        lambda: ws._git("add", "-A"),
        lambda: ws.commit_all("would have been committed to the parent"),
        lambda: ws._git("status", "--porcelain"),
    ):
        with pytest.raises(GitConfinementError):
            operation()
    with pytest.raises(ToolError):
        GitCommitTool().run(ws, message="change")

    assert index.read_bytes() == index_bytes  # staged files
    assert config.read_bytes() == config_bytes  # no user.email override
    assert head.read_bytes() == head_bytes
    assert snapshot(parent) == before  # nothing else under .git either: no objects, refs, or logs
    status = git(parent, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
    assert status == "A  staged.txt"


# ------------------------------------------ 4. a malformed or replaced .git


def _gitfile_to_parent(git_dir, parent):
    remove_tree(git_dir)
    git_dir.write_text(f"gitdir: {parent / '.git'}\n", encoding="utf-8")


def _link_to_parent(git_dir, parent):
    remove_tree(git_dir)
    link_directory(git_dir, parent / ".git")


def _plain_file(git_dir, parent):
    remove_tree(git_dir)
    git_dir.write_text("not a repository\n", encoding="utf-8")


def _empty_directory(git_dir, parent):
    remove_tree(git_dir)
    git_dir.mkdir()


def _garbage_head(git_dir, parent):
    (git_dir / "HEAD").write_text("garbage\n", encoding="utf-8")


def _worktree_is_the_parent(git_dir, parent):
    git(git_dir.parent, "config", "--file", str(git_dir / "config"), "core.worktree", str(parent))


def _objects_and_refs_in_the_parent(git_dir, parent):
    (git_dir / "commondir").write_text(f"{parent / '.git'}\n", encoding="utf-8")


DAMAGE = {
    "a .git file pointing at the parent": _gitfile_to_parent,
    "a link or junction to the parent's .git": _link_to_parent,
    "a plain file": _plain_file,
    "an empty directory": _empty_directory,
    "a corrupt HEAD": _garbage_head,
    "core.worktree set to the parent": _worktree_is_the_parent,
    "a commondir pointing at the parent": _objects_and_refs_in_the_parent,
}


@pytest.mark.parametrize("damage", list(DAMAGE))
def test_a_malformed_or_replaced_repository_fails_closed(parent, damage):
    ws = nested_workspace(parent)
    git_dir = ws.project_dir / ".git"
    DAMAGE[damage](git_dir, parent)
    before = snapshot(parent)
    ws.write_text("app.py", "v2\n")
    try:
        for operation in (lambda: ws.commit_all("change"), lambda: ws._git("status", "--porcelain")):
            with pytest.raises(GitConfinementError):
                operation()
        with pytest.raises(ToolError):
            GitCommitTool().run(ws, message="change")
        assert snapshot(parent) == before
    finally:  # remove a link itself, never its target
        if git_dir.is_symlink():
            os.unlink(git_dir)
        elif git_dir.is_junction():
            os.rmdir(git_dir)


def test_initialization_never_runs_over_an_existing_git_entry(parent):
    before = snapshot(parent)
    project = parent / "ws" / "project"
    project.mkdir(parents=True)
    (project / ".git").write_text(f"gitdir: {parent / '.git'}\n", encoding="utf-8")
    with pytest.raises(GitConfinementError, match="already exists"):
        Workspace.create(parent / "ws", scaffold={"app.py": "v1\n"}, git=True)
    assert snapshot(parent) == before


# --------------------------------- 6. concurrent or temporary workspace failure


def test_the_incident_replayed_a_repository_lost_between_two_commands_redirects_nothing(parent, monkeypatch):
    """The E1-E2 incident: project/.git vanished between `config user.name` and `config user.email`."""
    before = snapshot(parent)
    run_git = Workspace._run_git

    def lose_the_repository_after_the_name(self, args, *, pinned):
        out = run_git(self, args, pinned=pinned)
        if list(args[:2]) == ["config", "user.name"]:
            remove_tree(self.project_dir / ".git")  # as a concurrent run's cleanup did
        return out

    monkeypatch.setattr(Workspace, "_run_git", lose_the_repository_after_the_name)
    with pytest.raises(GitConfinementError, match="no repository"):
        nested_workspace(parent)
    assert snapshot(parent) == before
    assert "localhost.invalid" not in (parent / ".git" / "config").read_text(encoding="utf-8")


@pytest.mark.parametrize("loss", ["the repository", "the whole workspace"])
def test_a_repository_lost_after_its_proof_cannot_redirect_the_command(parent, monkeypatch, loss):
    """Even between the proof and the command, git is pinned to project/.git: losing it fails, never redirects."""
    ws = nested_workspace(parent)
    before = snapshot(parent)
    ws.write_text("app.py", "v2\n")
    prove = Workspace._require_own_repository

    def prove_then_lose(self):
        prove(self)
        remove_tree(self.project_dir / ".git" if loss == "the repository" else self.root)

    monkeypatch.setattr(Workspace, "_require_own_repository", prove_then_lose)
    with pytest.raises(WorkspaceError):
        ws.commit_all("change")
    assert snapshot(parent) == before


def test_inherited_git_variables_cannot_redirect_workspace_git(parent, monkeypatch):
    """A git hook (or a parent tool) sets GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE for its own repository."""
    ws = nested_workspace(parent)
    before = snapshot(parent)
    monkeypatch.setenv("GIT_DIR", str(parent / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(parent))
    monkeypatch.setenv("GIT_INDEX_FILE", str(parent / ".git" / "index"))
    ws.write_text("app.py", "v2\n")
    assert ws.commit_all("change") is not None
    assert ws._git("log", "-1", "--format=%s") == "change"  # committed in the workspace's own repository
    remove_tree(ws.project_dir / ".git")
    with pytest.raises(GitConfinementError):
        ws.commit_all("again")
    assert snapshot(parent) == before


@pytest.mark.stress
def test_a_workspace_damaged_concurrently_never_redirects_git_to_the_parent(parent):
    """Opt-in (--run-stress): a thread deletes and re-creates project/.git while commits run."""
    ws = nested_workspace(parent)
    before = snapshot(parent)
    stop = threading.Event()

    def vandal():
        while not stop.is_set():
            try:
                remove_tree(ws.project_dir / ".git")
                git(ws.project_dir, "init", "-q", "-b", "main", str(ws.project_dir))
            except (OSError, subprocess.CalledProcessError):
                pass

    thread = threading.Thread(target=vandal)
    thread.start()
    refused = 0
    try:
        for i in range(60):
            ws.write_text("app.py", f"v{i + 2}\n")
            try:
                ws.commit_all(f"change {i}")
            except WorkspaceError:
                refused += 1
    finally:
        stop.set()
        thread.join()
    assert refused > 0  # the damage really raced the commits
    assert snapshot(parent) == before

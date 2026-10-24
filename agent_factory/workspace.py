"""Git-friendly project workspaces.

Layout::

    <root>/
      project/            the code agents work on (optionally its own git repo)
      .agent_factory/
        workspace.json    marker + template info
        runs/<run_id>/
          checkpoint.json
          execution_log.jsonl

Orchestration metadata lives outside ``project/`` so the project's git
history contains only project code. All file access by tools goes through
:meth:`Workspace.resolve`, which refuses paths that escape ``project/``,
point into ``.git``, or touch template-protected paths.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from .domain.ids import PROJECT_ID_RE, new_project_id
from .sandbox.types import CodeTrust
from .util import write_json_atomic

META_DIR = ".agent_factory"
PROJECT_DIR = "project"
MARKER = "workspace.json"
_IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache"}

DEFAULT_GITIGNORE = "__pycache__/\n*.pyc\n.pytest_cache/\n"


class WorkspaceError(RuntimeError):
    pass


class PathViolation(WorkspaceError):
    """A path escapes the workspace or targets a protected location."""


class GitConfinementError(WorkspaceError):
    """A git command would not act on exactly the workspace's own repository, so it was not run."""


def _real(path: Path | str) -> str:
    """``path`` with links resolved, in the platform's case, for comparing locations."""
    return os.path.normcase(os.path.realpath(path))


def workspace_root_for(base: Path | str, project_id: str) -> Path:
    """The workspace directory for a project: ``<base>/<project_id>``.

    Only the project's ID becomes part of the path. Its format (``prj-`` and
    20 hex digits) can contain no separator, dot segment, drive letter, or
    reserved name, so the result is always a direct child of ``base``. Project
    *names* are labels chosen by users and never become paths.
    """
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise WorkspaceError(f"invalid project id {project_id!r}")
    base = Path(base).resolve()
    root = base / project_id
    if root.is_symlink() or root.is_junction():
        raise WorkspaceError("project workspace must not be a symlink or junction")
    if root.resolve().parent != base:
        raise WorkspaceError("project workspace would not be a direct child of the base directory")
    return root


class Workspace:
    def __init__(self, root: Path | str, *, protected: Iterable[str] = ()) -> None:
        self.root = Path(root).resolve()
        self.project_dir = self.root / PROJECT_DIR
        self.meta_dir = self.root / META_DIR
        self.runs_dir = self.meta_dir / "runs"
        self.protected = tuple(p.strip("/") for p in protected)

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def create(
        cls,
        root: Path | str,
        *,
        scaffold: Mapping[str, str] | None = None,
        protected: Iterable[str] = (),
        git: bool = False,
        info: Mapping[str, object] | None = None,
        trust: CodeTrust = CodeTrust.UNTRUSTED,
        project_id: str | None = None,
    ) -> Workspace:
        """Create a workspace at ``root``.

        ``project_id`` binds the workspace to a project that already exists,
        for example one created through the API, so the workspace, its runs,
        and the database all use the same identity. Without it a new ID is
        generated.
        """
        if project_id is not None and not (isinstance(project_id, str) and PROJECT_ID_RE.fullmatch(project_id)):
            raise WorkspaceError(f"invalid project id {project_id!r}")
        ws = cls(root, protected=protected)
        if ws.meta_dir.exists():
            raise WorkspaceError(f"workspace already exists at {ws.root}")
        ws.project_dir.mkdir(parents=True, exist_ok=True)
        ws.runs_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            ws.meta_dir / MARKER,
            {
                "format": 1,
                "protected": list(ws.protected),
                "info": dict(info or {}),
                # Lets a later git command tell a damaged repository from none at all.
                "git": bool(git),
                "trust": CodeTrust(trust).value,
                "trust_reason": "set at workspace creation",
                # Stable, opaque project identity. Never derived from the path,
                # so the workspace can move without changing which project it is.
                "project_id": project_id or new_project_id(),
            },
        )
        for rel, content in (scaffold or {}).items():
            path = ws._resolve_unchecked(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        if git:
            ws.init_git()
        return ws

    @staticmethod
    def remove(root: Path | str) -> None:
        """Delete a workspace directory, refusing anything that is not one.

        ``project/`` goes first and the marker last, so an interrupted removal
        still leaves a directory recognisable as a workspace. Read-only files
        (git object files on Windows) are made writable and retried.
        """
        root = Path(root)
        if not (root / META_DIR / MARKER).is_file():
            raise WorkspaceError(f"refusing to delete {root}: not an Agent Factory workspace")

        def _force(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        if (root / PROJECT_DIR).exists():
            shutil.rmtree(root / PROJECT_DIR, onexc=_force)
        shutil.rmtree(root, onexc=_force)

    @classmethod
    def open(cls, root: Path | str) -> Workspace:
        root = Path(root)
        marker = root / META_DIR / MARKER
        if not marker.is_file():
            raise WorkspaceError(f"{root} is not an Agent Factory workspace (missing {marker})")
        data = json.loads(marker.read_text(encoding="utf-8"))
        return cls(root, protected=data.get("protected", ()))

    # ----------------------------------------------------------------- trust

    def _marker(self) -> dict:
        try:
            data = json.loads((self.meta_dir / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def trust(self) -> CodeTrust:
        """Trust of the code in ``project/``. Missing or unreadable means UNTRUSTED."""
        value = self._marker().get("trust")
        return CodeTrust.TRUSTED_FIXTURE if value == CodeTrust.TRUSTED_FIXTURE.value else CodeTrust.UNTRUSTED

    def mark_untrusted(self, reason: str) -> bool:
        """Permanently mark the workspace untrusted. Returns True if this changed it.

        There is deliberately no way back to TRUSTED_FIXTURE.
        """
        data = self._marker()
        if not data:
            raise WorkspaceError(f"cannot record trust: workspace marker missing or unreadable in {self.meta_dir}")
        if data.get("trust") == CodeTrust.UNTRUSTED.value:
            return False
        data["trust"] = CodeTrust.UNTRUSTED.value
        data["trust_reason"] = reason
        write_json_atomic(self.meta_dir / MARKER, data)
        return True

    # ------------------------------------------------------------ identity

    @property
    def project_id(self) -> str | None:
        """The project's stable ID, or None for a workspace created before project IDs existed (v0.2.0 and earlier)."""
        value = self._marker().get("project_id")
        return value if isinstance(value, str) and PROJECT_ID_RE.fullmatch(value) else None

    def ensure_project_id(self) -> str:
        """Return the project ID, assigning and persisting a new one if the workspace has none."""
        data = self._marker()
        if not data:
            raise WorkspaceError(f"cannot record project identity: workspace marker missing or unreadable in {self.meta_dir}")
        current = data.get("project_id")
        if isinstance(current, str) and PROJECT_ID_RE.fullmatch(current):
            return current
        if current is not None:
            # Never silently replace an identity that some other record may reference.
            raise WorkspaceError(f"workspace has an invalid project id {current!r}")
        data["project_id"] = new_project_id()
        write_json_atomic(self.meta_dir / MARKER, data)
        return data["project_id"]

    @property
    def info(self) -> dict:
        marker = self.meta_dir / MARKER
        if not marker.is_file():
            return {}
        return json.loads(marker.read_text(encoding="utf-8")).get("info", {})

    # ----------------------------------------------------------------- paths

    def _resolve_unchecked(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise PathViolation("path must be a non-empty string")
        pure = PurePosixPath(rel.replace("\\", "/"))
        if pure.is_absolute() or ":" in rel or any(part == ".." for part in pure.parts):
            raise PathViolation(f"path {rel!r} must be relative and stay inside the project")
        base = self.project_dir.resolve()
        target = (base / Path(*pure.parts)).resolve()
        if target != base and base not in target.parents:
            raise PathViolation(f"path {rel!r} escapes the project directory")
        if ".git" in target.relative_to(base).parts:
            raise PathViolation(f"path {rel!r} points into .git")
        return target

    def relpath(self, path: Path) -> str:
        return path.resolve().relative_to(self.project_dir.resolve()).as_posix()

    def is_protected(self, rel: str) -> bool:
        posix = self.relpath(self._resolve_unchecked(rel))
        return any(posix == p or posix.startswith(p + "/") for p in self.protected)

    def resolve(self, rel: str, *, for_write: bool = False) -> Path:
        """Resolve ``rel`` inside ``project/``; writes to protected paths are refused."""
        path = self._resolve_unchecked(rel)
        if for_write and self.is_protected(rel):
            raise PathViolation(f"path {rel!r} is protected by the project template")
        return path

    # ----------------------------------------------------------- file access

    def list_files(self) -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.project_dir):
            dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
            for name in filenames:
                if name.endswith(".pyc"):
                    continue
                out.append(self.relpath(Path(dirpath) / name))
        return sorted(out)

    def read_text(self, rel: str) -> str:
        return self.resolve(rel).read_text(encoding="utf-8")

    def write_text(self, rel: str, content: str) -> Path:
        path = self.resolve(rel, for_write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def delete(self, rel: str) -> bool:
        path = self.resolve(rel, for_write=True)
        if not path.exists():
            return False
        if path.is_dir():
            raise WorkspaceError(f"refusing to delete directory {rel!r}")
        path.unlink()
        return True

    # ------------------------------------------------------------------- git
    #
    # Git acts only on the workspace's own repository, project/.git. Git
    # normally *discovers* a repository by walking up from the working
    # directory, so a workspace whose .git was missing used to act on whatever
    # repository contained it (Agent Factory's own, for tests that nested
    # workspaces in its working tree). Every command therefore:
    #
    # * drops inherited GIT_* variables (a git hook sets GIT_DIR, GIT_INDEX_FILE, ...);
    # * first proves, by git's own discovery capped at project/, that the
    #   repository root, git directory, and common directory are exactly
    #   project/ and project/.git, and that .git is a plain directory (a .git
    #   file, link, or junction could redirect git elsewhere);
    # * then runs pinned to that repository (GIT_DIR, GIT_WORK_TREE), so a
    #   repository that disappears after the proof makes the command fail
    #   instead of redirecting it.

    @property
    def is_git(self) -> bool:
        return (self.project_dir / ".git").is_dir()

    @property
    def expects_git(self) -> bool:
        """True if the workspace was created with its own repository (unknown, so False, for older workspaces)."""
        return self._marker().get("git") is True

    def _git_env(self, *, pinned: bool) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(
            {
                # Isolate from the user's global/system git config: no credential
                # helpers, hooks, signing keys, or remotes leak into agent commits.
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                # Never look for a repository above project/.
                "GIT_CEILING_DIRECTORIES": str(self.project_dir.parent),
            }
        )
        if pinned:
            env["GIT_DIR"] = str(self.project_dir / ".git")
            env["GIT_WORK_TREE"] = str(self.project_dir)
        return env

    def _run_git(self, args: Sequence[str], *, pinned: bool) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                env=self._git_env(pinned=pinned),
                timeout=60,
            )
        except OSError as exc:  # git missing, or project/ gone
            raise WorkspaceError(f"git {args[0]} could not run: {exc.strerror or exc}") from None
        if proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _require_own_repository(self) -> None:
        """Prove that git would act on exactly ``project/.git``; refuse otherwise (fail closed)."""
        git_dir = self.project_dir / ".git"
        try:
            mode = os.lstat(git_dir).st_mode
        except FileNotFoundError:
            raise GitConfinementError("refusing to run git: the workspace has no repository (project/.git is missing)") from None
        except OSError as exc:
            raise GitConfinementError(f"refusing to run git: project/.git cannot be inspected ({exc.strerror})") from None
        if not stat.S_ISDIR(mode) or git_dir.is_junction():  # lstat: a symbolic link is not S_ISDIR
            raise GitConfinementError(
                "refusing to run git: project/.git is not a plain directory (a file, link, or junction could redirect git)"
            )
        try:
            found = self._run_git(
                ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir"], pinned=False
            ).splitlines()
        except WorkspaceError as exc:
            raise GitConfinementError(f"refusing to run git: project/.git is not a valid repository ({exc})") from None
        project = _real(self.project_dir)
        own = os.path.join(project, ".git")
        if [_real(p) for p in found] != [project, own, own]:
            raise GitConfinementError("refusing to run git: the repository found from project/ is not the workspace's own")

    def _git(self, *args: str) -> str:
        """Run one git command on the workspace's own repository, and never on any other."""
        self._require_own_repository()
        return self._run_git(args, pinned=True)

    def init_git(self) -> None:
        """The trusted initialization path: create project/.git, prove it is ours, then configure and commit."""
        if os.path.lexists(self.project_dir / ".git"):
            raise GitConfinementError("refusing to initialize git: project/.git already exists")
        gitignore = self.project_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(DEFAULT_GITIGNORE, encoding="utf-8", newline="\n")
        self._run_git(["init", "-q", "-b", "main", str(self.project_dir)], pinned=False)  # creates exactly project/.git
        self._git("config", "user.name", "Agent Factory")
        self._git("config", "user.email", "agent-factory@localhost.invalid")
        self._git("config", "core.autocrlf", "false")
        self.commit_all("agent-factory: initial scaffold")

    def commit_all(self, message: str) -> str | None:
        """Commit every change in ``project/``; returns the commit sha or None if clean."""
        self._git("add", "-A")
        if not self._git("status", "--porcelain"):
            return None
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

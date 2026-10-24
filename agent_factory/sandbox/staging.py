"""Safe copy-in staging: snapshot ``project/`` into a deterministic in-memory tar.

This is the only code that walks a workspace for sandboxed execution. It
never follows links:

* every entry is examined with ``lstat``; symlinks, junctions and other
  reparse points, hardlinks (``st_nlink > 1``), and special files are
  rejected, not skipped;
* names must be portable and safe (no separators, ``..``, ``:``/ADS,
  control characters, trailing dots/spaces, or reserved Windows device
  names);
* every resolved path must stay inside ``project/``;
* after opening a file, ``fstat`` must match the earlier ``lstat`` (same
  file, same size, still a single link), defeating swap-after-check races;
* total bytes, file count, depth, and path length are bounded.

Any violation rejects the whole staging (:class:`StagingRejected`); nothing
partial is ever sent to a sandbox.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..workspace import Workspace
from .types import StagingRejected

MAX_FILES = 5000
MAX_DEPTH = 32
MAX_PATH_CHARS = 240
#: tmpfs allocates whole pages; staged usage is counted in pages.
TMPFS_PAGE = 4096
#: Unicode categories never allowed in names: controls, format (incl. bidi
#: overrides and zero-width characters), separators, surrogates, private use,
#: unassigned.
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
#: Directories never copied into a sandbox (and never followed).
EXCLUDED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache"})

_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)
_BAD_CHARS = frozenset('<>:"|?*\\/') | frozenset(chr(c) for c in range(32)) | {"\x7f"}
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

Lstat = Callable[[str], os.stat_result]


@dataclass(frozen=True)
class StagedArchive:
    data: bytes
    files: tuple[str, ...]
    total_bytes: int
    sha256: str


def check_name(name: str) -> str | None:
    """Problem with a single path component, or None."""
    if not name or name in (".", ".."):
        return "empty or relative path component"
    if any(ch in _BAD_CHARS for ch in name):
        return "contains a separator, ':' (alternate data stream), or control character"
    if name[-1] in ". ":
        return "ends with a dot or space"
    if unicodedata.normalize("NFC", name) != name:
        return "not in Unicode NFC normal form"
    if any(unicodedata.category(ch) in _BAD_CATEGORIES for ch in name):
        return "contains an invisible, format, or unassigned Unicode character"
    if name.split(".")[0].upper() in _RESERVED:
        return "reserved Windows device name"
    return None


def check_rel_path(rel: str) -> str | None:
    if len(rel) > MAX_PATH_CHARS:
        return f"path longer than {MAX_PATH_CHARS} characters"
    parts = PurePosixPath(rel).parts
    if len(parts) > MAX_DEPTH:
        return f"deeper than {MAX_DEPTH} levels"
    for part in parts:
        problem = check_name(part)
        if problem:
            return problem
    return None


def check_lstat(st: os.stat_result) -> str | None:
    """Problem with an entry's type, or None for a plain file or directory."""
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return "symbolic link"
    if (getattr(st, "st_file_attributes", 0) or 0) & _REPARSE or getattr(st, "st_reparse_tag", 0):
        return "reparse point (junction, mount point, or link)"
    if stat.S_ISDIR(mode):
        return None
    if not stat.S_ISREG(mode):
        return "special file (device, pipe, or socket)"
    if st.st_nlink != 1:
        return "hard link"
    return None


def tmpfs_footprint(size: int) -> int:
    """Bytes of tmpfs a file of ``size`` bytes occupies (whole pages)."""
    return -(-size // TMPFS_PAGE) * TMPFS_PAGE


def stage_workspace(
    workspace: Workspace,
    *,
    max_bytes: int,  # maximum tmpfs footprint (ResourceLimits.max_input_bytes)
    max_files: int = MAX_FILES,
    lstat: Lstat = os.lstat,
) -> StagedArchive:
    base = str(workspace.project_dir)
    problems: list[str] = []

    root_problem = check_lstat(lstat(base))
    if root_problem or not stat.S_ISDIR(lstat(base).st_mode):
        raise StagingRejected(f"project directory is not a plain directory: {root_problem or 'not a directory'}")
    base_real = os.path.realpath(base)

    found: list[tuple[str, str, os.stat_result]] = []

    def walk(directory: str, rel_parts: list[str]) -> None:
        if len(rel_parts) > MAX_DEPTH:
            problems.append(f"{'/'.join(rel_parts)}: deeper than {MAX_DEPTH} levels")
            return
        with os.scandir(directory) as it:
            names = sorted(entry.name for entry in it)
        for name in names:
            rel = "/".join([*rel_parts, name])
            if name in EXCLUDED_DIRS:
                continue  # never followed, never copied
            problem = check_name(name) or check_rel_path(rel)
            if problem:
                problems.append(f"{rel}: {problem}")
                continue
            path = os.path.join(directory, name)
            st = lstat(path)
            problem = check_lstat(st)
            if problem:
                problems.append(f"{rel}: {problem}")
                continue
            real = os.path.realpath(path)
            if os.path.commonpath([real, base_real]) != base_real:
                problems.append(f"{rel}: resolves outside the project")
                continue
            if stat.S_ISDIR(st.st_mode):
                walk(path, [*rel_parts, name])
            else:
                found.append((rel, path, st))
                if len(found) > max_files:
                    problems.append(f"more than {max_files} files")
                    return

    walk(base, [])
    if problems:
        raise StagingRejected(_summarise(problems))

    footprint = sum(tmpfs_footprint(st.st_size) for _, _, st in found)
    if footprint > max_bytes:
        raise StagingRejected(
            f"workspace needs {footprint} bytes of /work but the staging limit is {max_bytes} bytes "
            "(the rest of /work is reserved as writable headroom)"
        )
    items: list[tuple[str, bytes]] = []
    total = 0
    for rel, path, st in found:
        total += st.st_size
        with open(path, "rb") as fh:
            fst = os.fstat(fh.fileno())
            if (fst.st_ino, fst.st_dev, fst.st_size) != (st.st_ino, st.st_dev, st.st_size) or fst.st_nlink != 1:
                raise StagingRejected(f"{rel}: file changed during staging")
            data = fh.read(st.st_size + 1)
        if len(data) != st.st_size:
            raise StagingRejected(f"{rel}: file changed during staging")
        items.append((rel, data))

    archive = _build_tar(items)
    return StagedArchive(
        data=archive,
        files=tuple(rel for rel, _ in items),
        total_bytes=total,
        sha256=hashlib.sha256(archive).hexdigest(),
    )


def _build_tar(items: list[tuple[str, bytes]]) -> bytes:
    """Deterministic tar: sorted regular files, fixed mode/owner/mtime."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, data in sorted(items):
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def inspect_archive(data: bytes) -> list[str]:
    """Defence in depth: problems with an archive about to enter a sandbox."""
    problems = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for member in tar.getmembers():
                if not member.isreg():
                    problems.append(f"{member.name}: not a regular file")
                if member.name.startswith("/") or check_rel_path(member.name):
                    problems.append(f"{member.name}: unsafe name")
    except tarfile.TarError as exc:
        problems.append(f"unreadable archive: {exc}")
    return problems


def _summarise(problems: list[str], limit: int = 20) -> str:
    head = "; ".join(problems[:limit])
    more = f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    return f"workspace cannot be staged safely: {head}{more}"

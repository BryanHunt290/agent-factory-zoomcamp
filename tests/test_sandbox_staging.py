"""Stage D: copy-in staging rejects anything that could escape or alias the workspace."""

import io
import os
import stat
import tarfile

import pytest

from agent_factory.sandbox.staging import (
    check_lstat,
    check_name,
    check_rel_path,
    inspect_archive,
    stage_workspace,
)
from agent_factory.sandbox.types import StagingRejected
from agent_factory.workspace import Workspace

try:
    import _winapi

    CAN_JUNCTION = hasattr(_winapi, "CreateJunction")
except ImportError:  # pragma: no cover - non-Windows
    CAN_JUNCTION = False

MB = 1024 * 1024


@pytest.fixture
def ws(tmp_path):
    return Workspace.create(
        tmp_path / "ws",
        scaffold={"calc.py": "x = 1\n", "tests/__init__.py": "", "tests/test_calc.py": "import calc\n"},
    )


@pytest.fixture
def outside(tmp_path):
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("TOP-SECRET-CANARY", encoding="utf-8")
    return secret_dir


def _members(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return tar.getmembers()


def test_staging_produces_deterministic_regular_file_tar(ws):
    (ws.project_dir / "__pycache__").mkdir()
    (ws.project_dir / "__pycache__" / "x.pyc").write_bytes(b"\0")
    a = stage_workspace(ws, max_bytes=MB)
    b = stage_workspace(ws, max_bytes=MB)
    assert a.data == b.data and a.sha256 == b.sha256
    assert a.files == ("calc.py", "tests/__init__.py", "tests/test_calc.py")
    for m in _members(a.data):
        assert m.isreg() and m.mode == 0o644 and m.mtime == 0 and m.uid == 0 and m.uname == ""
    assert inspect_archive(a.data) == []


def test_git_metadata_is_never_staged(tmp_path):
    from agent_factory.templates import CALCULATOR_TEMPLATE

    ws = CALCULATOR_TEMPLATE.create_workspace(tmp_path / "g", git=True)
    staged = stage_workspace(ws, max_bytes=MB)
    assert not any(f.startswith(".git/") for f in staged.files)


@pytest.mark.skipif(not CAN_JUNCTION, reason="junctions need Windows")
def test_junction_to_outside_is_rejected(ws, outside):
    _winapi.CreateJunction(str(outside), str(ws.project_dir / "evil"))
    with pytest.raises(StagingRejected, match="evil: reparse point"):
        stage_workspace(ws, max_bytes=MB)


@pytest.mark.skipif(not CAN_JUNCTION, reason="junctions need Windows")
def test_junction_inside_excluded_dir_is_not_followed(ws, outside):
    _winapi.CreateJunction(str(outside), str(ws.project_dir / "__pycache__"))
    staged = stage_workspace(ws, max_bytes=MB)
    assert all("secret" not in f for f in staged.files)
    assert b"TOP-SECRET-CANARY" not in staged.data


@pytest.mark.skipif(not CAN_JUNCTION, reason="junctions need Windows")
def test_project_dir_replaced_by_junction_is_rejected(tmp_path, outside):
    ws = Workspace.create(tmp_path / "ws2")
    os.rmdir(ws.project_dir)
    _winapi.CreateJunction(str(outside), str(ws.project_dir))
    with pytest.raises(StagingRejected, match="not a plain directory"):
        stage_workspace(ws, max_bytes=MB)


def test_hardlink_is_rejected(ws, outside):
    os.link(outside / "secret.txt", ws.project_dir / "alias.txt")
    with pytest.raises(StagingRejected, match="alias.txt: hard link"):
        stage_workspace(ws, max_bytes=MB)


def test_real_symlink_is_rejected_when_platform_allows_creating_one(ws, outside):
    try:
        os.symlink(outside / "secret.txt", ws.project_dir / "link.txt")
    except OSError:
        pytest.skip("creating symlinks requires privileges on this machine (covered by the lstat tests)")
    with pytest.raises(StagingRejected, match="symbolic link|reparse point"):
        stage_workspace(ws, max_bytes=MB)


def _fake_lstat(target_name, mode=None, attrs=None, nlink=None):
    """Wrap os.lstat so one entry reports a different type."""

    def lstat(path):
        st = os.lstat(path)
        if os.path.basename(path) != target_name:
            return st
        fields = list(st[:10])
        if mode is not None:
            fields[0] = mode
        if nlink is not None:
            fields[3] = nlink

        class Fake:
            pass

        fake = Fake()
        for i, attr in enumerate(
            ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size", "st_atime", "st_mtime", "st_ctime"]
        ):
            setattr(fake, attr, fields[i])
        fake.st_file_attributes = attrs if attrs is not None else getattr(st, "st_file_attributes", 0)
        fake.st_reparse_tag = 0
        return fake

    return lstat


@pytest.mark.parametrize(
    "kw, message",
    [
        ({"mode": stat.S_IFLNK | 0o777}, "symbolic link"),
        ({"attrs": 0x400}, "reparse point"),
        ({"mode": stat.S_IFIFO | 0o644}, "special file"),
        ({"mode": stat.S_IFCHR | 0o644}, "special file"),
        ({"nlink": 3}, "hard link"),
    ],
)
def test_link_and_special_types_are_rejected(ws, kw, message):
    with pytest.raises(StagingRejected, match=f"calc.py: {message}"):
        stage_workspace(ws, max_bytes=MB, lstat=_fake_lstat("calc.py", **kw))


@pytest.mark.parametrize(
    "name",
    ["CON", "con.txt", "NUL", "LPT1.py", "COM9", "a:b", "x\x01y", "trail.", "trail ", "..", ".", "a\\b", "a/b", ""],
)
def test_unsafe_names_are_rejected(name):
    assert check_name(name) is not None


@pytest.mark.parametrize("name", ["calc.py", "test_calc.py", ".gitignore", "README.md", "con_tents.py", "a.b.c"])
def test_ordinary_names_are_accepted(name):
    assert check_name(name) is None


def test_long_and_deep_paths_are_rejected():
    assert check_rel_path("a/" * 40 + "x") is not None
    assert check_rel_path("x" * 241) is not None
    assert check_rel_path("pkg/mod.py") is None


def test_size_and_count_caps(ws):
    ws.write_text("big.bin", "x" * 5000)
    with pytest.raises(StagingRejected, match="staging limit"):
        stage_workspace(ws, max_bytes=4096)
    with pytest.raises(StagingRejected, match="more than 2 files"):
        stage_workspace(ws, max_bytes=MB, max_files=2)


def test_file_swapped_after_check_is_rejected(ws):
    def lying_lstat(path):
        st = os.lstat(path)
        if os.path.basename(path) != "calc.py":
            return st
        return os.stat_result((st.st_mode, st.st_ino + 1, *st[2:10]))

    with pytest.raises(StagingRejected, match="changed during staging"):
        stage_workspace(ws, max_bytes=MB, lstat=lying_lstat)


def _tar_with(member: tarfile.TarInfo, data: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(member, io.BytesIO(data) if member.isreg() else None)
    return buf.getvalue()


@pytest.mark.parametrize(
    "name, kind",
    [("../escape.py", tarfile.REGTYPE), ("/etc/passwd", tarfile.REGTYPE), ("link", tarfile.SYMTYPE), ("dev", tarfile.CHRTYPE)],
)
def test_archive_inspection_rejects_malicious_members(name, kind):
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind == tarfile.SYMTYPE:
        info.linkname = "/etc/passwd"
    assert inspect_archive(_tar_with(info)) != []


# ------------------------------------------------- headroom (adversarial prep)

from agent_factory.sandbox.staging import TMPFS_PAGE, tmpfs_footprint
from agent_factory.sandbox.types import MiB, ResourceLimits


def test_input_limit_leaves_half_of_work_writable():
    limits = ResourceLimits()  # 256 MiB /work, 512 MiB memory
    assert limits.max_input_bytes == 128 * MiB
    assert limits.work_headroom_bytes == 128 * MiB
    assert limits.max_input_bytes + limits.work_headroom_bytes == limits.disk_bytes


def test_input_limit_is_also_bounded_by_memory():
    limits = ResourceLimits(disk_mb=1024, memory_mb=64)
    assert limits.max_input_bytes == 16 * MiB  # af-entry buffers the archive; tmpfs counts as memory
    assert limits.work_headroom_bytes == 1008 * MiB


def test_footprint_counts_whole_tmpfs_pages():
    assert tmpfs_footprint(0) == 0
    assert tmpfs_footprint(1) == TMPFS_PAGE
    assert tmpfs_footprint(TMPFS_PAGE) == TMPFS_PAGE
    assert tmpfs_footprint(TMPFS_PAGE + 1) == 2 * TMPFS_PAGE


def _ws_with_footprint(tmp_path, footprint):
    ws = Workspace.create(tmp_path / f"fp{footprint}")
    (ws.project_dir / "blob.bin").write_bytes(b"x" * footprint)
    return ws


def test_staging_at_exactly_the_input_maximum_succeeds(tmp_path):
    limits = ResourceLimits(disk_mb=1)  # max input 512 KiB, headroom 512 KiB
    staged = stage_workspace(_ws_with_footprint(tmp_path, limits.max_input_bytes), max_bytes=limits.max_input_bytes)
    assert staged.total_bytes == limits.max_input_bytes


def test_staging_one_page_over_the_maximum_is_rejected(tmp_path):
    limits = ResourceLimits(disk_mb=1)
    ws = _ws_with_footprint(tmp_path, limits.max_input_bytes + 1)  # rounds up to one more page
    with pytest.raises(StagingRejected, match="reserved as writable headroom"):
        stage_workspace(ws, max_bytes=limits.max_input_bytes)


def test_many_tiny_files_are_counted_by_pages_not_bytes(tmp_path):
    ws = Workspace.create(tmp_path / "tiny")
    for i in range(3):
        (ws.project_dir / f"f{i}.txt").write_bytes(b"x")
    with pytest.raises(StagingRejected):
        stage_workspace(ws, max_bytes=2 * TMPFS_PAGE)  # 3 bytes, but 3 pages
    assert stage_workspace(ws, max_bytes=3 * TMPFS_PAGE).total_bytes == 3


@pytest.mark.parametrize(
    "name",
    ["café.py", "evil‮gnp.py", "zero​width.py", "bom﻿.py", "line sep.py"],
)
def test_unicode_spoofing_names_are_rejected(ws, name):
    try:
        (ws.project_dir / name).write_text("x", encoding="utf-8")
    except OSError:
        pytest.skip("filesystem refused the name")
    with pytest.raises(StagingRejected):
        stage_workspace(ws, max_bytes=MB)


def test_nfc_unicode_names_are_accepted(ws):
    (ws.project_dir / "café.py").write_text("x", encoding="utf-8")
    assert "café.py" in stage_workspace(ws, max_bytes=MB).files

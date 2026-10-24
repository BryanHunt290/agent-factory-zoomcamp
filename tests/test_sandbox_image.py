"""Stage E Tier-0: af-entry protocol logic, human-only image builder, image lock config.

af-entry is loaded from sandbox_image/af-entry and exercised on the host
against temporary directories. No Docker is used.
"""

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import tarfile
from pathlib import Path

import pytest
from docker_fakes import IMAGE_ID, LOCAL_PIPE

from agent_factory.sandbox.config import (
    ImageRecord,
    docker_config_from_lock,
    load_image_record,
    save_image_record,
    source_hashes,
)
from agent_factory.sandbox.docker import ALLOWED_SUBCOMMANDS, DockerBackend
from agent_factory.sandbox.image import BUILD_SUBCOMMANDS, ImageBuilder, ImageBuildError
from agent_factory.sandbox.process import ProcessOutcome

REPO = Path(__file__).resolve().parents[1]
AF_ENTRY = REPO / "sandbox_image" / "af-entry"
BASE_ID = "sha256:" + "fd" * 32


def _load_af_entry():
    loader = importlib.machinery.SourceFileLoader("af_entry", str(AF_ENTRY))
    spec = importlib.util.spec_from_loader("af_entry", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


af = _load_af_entry()


def _tar(*entries):
    """entries: (name, data) for regular files, or a prepared TarInfo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                tar.addfile(entry, io.BytesIO(b"x" * entry.size) if entry.isreg() else None)
            else:
                name, data = entry
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _special(name, kind, **kw):
    info = tarfile.TarInfo(name)
    info.type = kind
    for k, v in kw.items():
        setattr(info, k, v)
    return info


# ---------------------------------------------------------------- af-entry


def test_af_entry_is_lf_python_with_isolated_shebang():
    raw = AF_ENTRY.read_bytes()
    assert raw.startswith(b"#!/usr/local/bin/python3 -I\n") and b"\r\n" not in raw


def test_af_entry_parses_protocol_arguments():
    wd, size, digest, cmd = af.parse_args(["--workdir", ".", "--archive-bytes", "10", "--archive-sha256", "a" * 64, "--", "python", "-c", "x"])
    assert (wd, size, digest, cmd) == (".", 10, "a" * 64, ["python", "-c", "x"])


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--workdir", ".", "--archive-bytes", "10", "--archive-sha256", "a" * 64],  # no command
        ["--workdir", ".", "--archive-bytes", "-1", "--archive-sha256", "a" * 64, "--", "x"],
        ["--workdir", ".", "--archive-bytes", "10", "--archive-sha256", "XYZ", "--", "x"],
        ["--shell", "sh", "--", "x"],
    ],
)
def test_af_entry_rejects_bad_usage(argv):
    with pytest.raises(af.EntryError) as exc:
        af.parse_args(argv)
    assert exc.value.code == af.EXIT_USAGE


def test_af_entry_extracts_regular_files(tmp_path):
    names = af.extract(_tar(("a.py", b"print(1)\n"), ("pkg/b.py", b""), ("pkg/sub/c.bin", bytes(range(256)))), str(tmp_path))
    assert names == ["a.py", "pkg/b.py", "pkg/sub/c.bin"]
    assert (tmp_path / "pkg" / "sub" / "c.bin").read_bytes() == bytes(range(256))


@pytest.mark.parametrize(
    "entry, message",
    [
        (("../escape.py", b"x"), "escapes|normalised"),
        (("/etc/passwd", b"x"), "normalised"),
        (("a/../../x", b"x"), "normalised|escapes"),
        (("./a", b"x"), "normalised"),
        (("a//b", b"x"), "normalised"),
        (("a\\b", b"x"), "invalid characters"),
        (("a\x01b", b"x"), "invalid characters"),
        ((".", b"x"), "escapes"),
        ((_special("link", tarfile.SYMTYPE, linkname="/etc/passwd")), "not allowed"),
        ((_special("hard", tarfile.LNKTYPE, linkname="a.py")), "not allowed"),
        ((_special("dir", tarfile.DIRTYPE)), "not allowed"),
        ((_special("dev", tarfile.CHRTYPE, devmajor=1, devminor=3)), "not allowed"),
        ((_special("fifo", tarfile.FIFOTYPE)), "not allowed"),
    ],
)
def test_af_entry_rejects_unsafe_entries(tmp_path, entry, message):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(af.EntryError, match=message) as exc:
        af.extract(_tar(entry), str(work))
    assert exc.value.code == af.EXIT_STAGING
    assert not (tmp_path / "escape.py").exists()


def test_af_entry_never_overwrites_or_duplicates(tmp_path):
    with pytest.raises(af.EntryError, match="duplicate"):
        af.extract(_tar(("a.py", b"1"), ("a.py", b"2")), str(tmp_path))
    assert (tmp_path / "a.py").read_bytes() == b"1"


def test_af_entry_ignores_archive_permissions_and_ownership(tmp_path):
    info = tarfile.TarInfo("setuid.py")
    info.size = 1
    info.mode = 0o6777
    info.uid = info.gid = 0
    af.extract(_tar(info), str(tmp_path))
    mode = os.stat(tmp_path / "setuid.py").st_mode
    assert not mode & 0o6000  # no setuid/setgid on any platform
    if os.name == "posix":
        assert mode & 0o777 == 0o644


def test_af_entry_reads_exact_size_without_needing_eof():
    r, w = os.pipe()
    os.write(w, b"0123456789EXTRA")  # writer still open: no EOF
    try:
        assert af.read_exact(r, 10) == b"0123456789"
    finally:
        os.close(w)
        os.close(r)


def test_af_entry_detects_short_stdin():
    r, w = os.pipe()
    os.write(w, b"abc")
    os.close(w)
    with pytest.raises(af.EntryError, match="stdin ended after 3 of 10"):
        af.read_exact(r, 10)
    os.close(r)


def test_af_entry_rejects_digest_mismatch(monkeypatch):
    data = _tar(("a.py", b"1"))
    monkeypatch.setattr(af, "read_exact", lambda fd, n: data)
    with pytest.raises(af.EntryError, match="sha256 mismatch") as exc:
        af.main(["--workdir", ".", "--archive-bytes", str(len(data)), "--archive-sha256", "0" * 64, "--", "python"])
    assert exc.value.code == af.EXIT_STAGING


def test_af_entry_executes_without_shell(monkeypatch, tmp_path):
    data = _tar(("a.py", b"1"))
    calls = {}
    monkeypatch.setattr(af, "WORK", str(tmp_path))
    monkeypatch.setattr(af, "read_exact", lambda fd, n: data)
    monkeypatch.setattr(af, "extract", lambda d, work=None: ["a.py"])
    monkeypatch.setattr(af.os, "dup2", lambda a, b: None)
    monkeypatch.setattr(af.os, "chdir", lambda p: calls.setdefault("cwd", p))

    def fake_exec(prog, argv):
        calls["exec"] = (prog, argv)
        raise SystemExit(0)

    monkeypatch.setattr(af.os, "execvp", fake_exec)
    argv = ["python", "-c", "print(1); import os", "; rm -rf / #", "$(id)"]
    with pytest.raises(SystemExit):
        af.main(["--workdir", ".", "--archive-bytes", str(len(data)), "--archive-sha256", hashlib.sha256(data).hexdigest(), "--", *argv])
    assert calls["exec"] == ("python", argv)  # argv passed through verbatim, no shell


# ------------------------------------------------------------- image lock


def _record(**kw):
    hashes = source_hashes()
    base = dict(
        image_id=IMAGE_ID,
        image_ref="agent-factory/sandbox-python:3.12",
        base_image=f"docker.io/library/python@{BASE_ID}",
        base_image_id=BASE_ID,
        base_python_version="3.12.10",
        built_at="2026-10-01T00:00:00Z",
        docker_server_version="29.6.1",
        **hashes,
    )
    base.update(kw)
    return ImageRecord(**base)


def test_lock_round_trip_and_config(tmp_path):
    path = tmp_path / "image.lock.json"
    save_image_record(_record(), path)
    assert load_image_record(path) == _record()
    cfg = docker_config_from_lock(path)
    assert cfg.image_id == IMAGE_ID and cfg.problems() == []


def test_missing_lock_fails_closed(tmp_path):
    cfg = docker_config_from_lock(tmp_path / "absent.json")
    assert cfg.image_id is None and "build-image" in cfg.problems()[0]


def test_stale_lock_fails_closed(tmp_path):
    path = tmp_path / "image.lock.json"
    save_image_record(_record(af_entry_sha256="0" * 64), path)
    assert "stale" in docker_config_from_lock(path).problems()[0]


@pytest.mark.parametrize("content", ["{bad", json.dumps({"image_id": "latest"}), json.dumps({"surprise": 1})])
def test_invalid_lock_fails_closed(tmp_path, content):
    path = tmp_path / "image.lock.json"
    path.write_text(content, encoding="utf-8")
    assert docker_config_from_lock(path).image_id is None


def test_backend_with_unbuilt_image_is_unavailable(tmp_path):
    backend = DockerBackend(docker_config_from_lock(tmp_path / "absent.json"), runner=lambda *a, **k: pytest.fail("ran docker"))
    status = backend.probe()
    assert not status.available and "build-image" in status.reasons[0]


# ---------------------------------------------------------- image builder


def _ok(stdout="", stderr="", code=0):
    return ProcessOutcome(code, stdout.encode(), stderr.encode(), False, False, False, False, 0.0)


class FakeBuildDocker:
    def __init__(self, *, base_present=True, repo_digests=None, built_env=None, build_log="#5 DONE 0.1s\n"):
        self.calls = []
        self.base_present = base_present
        self.repo_digests = [f"python@{BASE_ID}"] if repo_digests is None else repo_digests
        self.built_env = built_env or ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
        self.build_log = build_log

    def __call__(self, args, *, host, stdin, timeout_s, output_limit, on_kill):
        self.calls.append((host, list(args)))
        if args[0] == "version":
            return _ok(json.dumps({"Server": {"Version": "29.6.1", "Os": "linux", "Arch": "amd64"}}))
        if args[:2] == ["context", "inspect"]:
            return _ok(json.dumps([{"Endpoints": {"docker": {"Host": LOCAL_PIPE}}}]))
        if args[:2] == ["image", "inspect"]:
            ref = args[-1]
            if ref == BASE_ID:
                if not self.base_present:
                    return _ok("", "No such image", 1)
                return _ok(json.dumps({
                    "Id": BASE_ID, "RepoTags": ["python:3.12.10-slim"], "RepoDigests": self.repo_digests,
                    "Os": "linux", "Architecture": "amd64", "Created": "2025-05-08T22:27:23Z",
                    "Config": {"Env": ["PATH=/x", "PYTHON_VERSION=3.12.10", "GPG_KEY=k"], "User": ""},
                }))  # fmt: skip
            return _ok(json.dumps({
                "Id": IMAGE_ID, "Os": "linux",
                "Config": {"User": "10001:10001", "Env": self.built_env,
                           "Labels": {"org.agent-factory.sandbox.base": f"docker.io/library/python@{BASE_ID}"}},
            }))  # fmt: skip
        if args[0] == "build":
            return _ok("", self.build_log)
        raise AssertionError(f"unexpected docker call {args}")


def test_inspect_base_identifies_image_read_only():
    fake = FakeBuildDocker()
    info = ImageBuilder(runner=fake).inspect_base(BASE_ID)
    assert info.digest_ref == f"docker.io/library/python@{BASE_ID}" and info.python_version == "3.12.10"
    assert {tuple(a[:2]) if a[0] in ("context", "image") else (a[0],) for _, a in fake.calls} <= {
        ("version",), ("context", "inspect"), ("image", "inspect")
    }


def test_absent_base_stops_with_pull_instructions_and_never_pulls():
    fake = FakeBuildDocker(base_present=False)
    with pytest.raises(ImageBuildError, match=r"never pulls.*docker pull docker.io/library/python@sha256:fd"):
        ImageBuilder(runner=fake).build(BASE_ID, confirm=True)
    assert not any(a[0] in ("build", "pull") for _, a in fake.calls)


@pytest.mark.parametrize("digests", [[], ["python@sha256:" + "00" * 32], [f"a@{BASE_ID}", f"b@{BASE_ID}"]])
def test_unprovable_provenance_stops(digests):
    with pytest.raises(ImageBuildError, match="provenance"):
        ImageBuilder(runner=FakeBuildDocker(repo_digests=digests)).inspect_base(BASE_ID)


@pytest.mark.parametrize("ref", ["python:3.12.10-slim", "latest", "sha256:abc", ""])
def test_base_must_be_a_full_digest(ref):
    with pytest.raises(ImageBuildError, match="full sha256"):
        ImageBuilder(runner=FakeBuildDocker()).inspect_base(ref)


def test_build_requires_confirmation():
    fake = FakeBuildDocker()
    with pytest.raises(ImageBuildError, match="confirm"):
        ImageBuilder(runner=fake).build(BASE_ID, confirm=False)
    assert fake.calls == []


def test_build_is_offline_pinned_and_recorded(tmp_path):
    fake = FakeBuildDocker()
    lock = tmp_path / "image.lock.json"
    result = ImageBuilder(runner=fake).build(BASE_ID, confirm=True, lock_path=lock)
    build = next(a for _, a in fake.calls if a[0] == "build")
    assert "--pull=false" in build and build[build.index("--network") + 1] == "none" and "--no-cache" in build
    assert f"BASE_IMAGE=docker.io/library/python@{BASE_ID}" in build
    assert all(host == LOCAL_PIPE for host, a in fake.calls if a[0] in ("build", "image"))
    assert load_image_record(lock) == result.record and result.record.image_id == IMAGE_ID


@pytest.mark.parametrize(
    "kw, message",
    [
        ({"build_log": "#2 pulling docker.io/library/python\n"}, "registry access"),
        ({"build_env": None, "built_env": ["PATH=/x", "GPG_KEY=1"]}, "unexpected ENV"),
    ],
)
def test_build_rejects_suspicious_results(tmp_path, kw, message):
    kw.pop("build_env", None)
    lock = tmp_path / "image.lock.json"
    with pytest.raises(ImageBuildError, match=message):
        ImageBuilder(runner=FakeBuildDocker(**kw)).build(BASE_ID, confirm=True, lock_path=lock)
    assert not lock.exists()


@pytest.mark.parametrize("args", [["pull", "x"], ["run", "x"], ["push", "x"], ["create", "x"], ["image", "pull", "x"]])
def test_builder_subcommand_allowlist(args):
    builder = ImageBuilder(runner=FakeBuildDocker())
    builder.connect()
    with pytest.raises(ImageBuildError, match="refusing"):
        builder._docker(args)


def test_agent_backend_can_never_build_or_pull():
    assert ("build",) not in ALLOWED_SUBCOMMANDS and ("pull",) not in ALLOWED_SUBCOMMANDS
    assert ("pull",) not in BUILD_SUBCOMMANDS and ("run",) not in BUILD_SUBCOMMANDS


def test_agent_run_path_never_imports_the_image_builder():
    pkg = REPO / "agent_factory"
    agent_path = [*pkg.joinpath("engine").glob("*.py"), *pkg.joinpath("agents").glob("*.py"),
                  *pkg.joinpath("tools").glob("*.py"), pkg / "factory.py", pkg / "sandbox" / "manager.py",
                  pkg / "sandbox" / "docker.py", pkg / "sandbox" / "local.py", pkg / "sandbox" / "__init__.py",
                  pkg / "sandbox" / "config.py"]  # fmt: skip
    offenders = [p.name for p in agent_path if re.search(r"sandbox\.image|from \.image|import image", p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_dockerfile_has_no_frontend_directive_and_pins_base_by_argument():
    text = (REPO / "sandbox_image" / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^#\s*syntax\s*=", text, re.MULTILINE | re.IGNORECASE)  # no parser directive
    assert "FROM ${BASE_IMAGE} AS base" in text and "FROM scratch" in text
    assert "USER 10001:10001" in text
    assert not re.search(r"^\s*(ADD|ENV)\b", text, re.MULTILINE)  # no remote ADD, no inherited/added ENV


def test_af_entry_file_dir_conflict_is_a_staging_error(tmp_path):
    # Regression (adversarial phase): "a" then "a/b" used to crash af-entry with an
    # uncaught FileExistsError (exit 1), which the host misread as a project failure.
    with pytest.raises(af.EntryError, match="cannot create parent") as exc:
        af.extract(_tar(("a", b"1"), ("a/b", b"2")), str(tmp_path))
    assert exc.value.code == af.EXIT_STAGING


def test_af_entry_unexpected_errors_use_a_reserved_exit_code(monkeypatch):
    def boom(argv):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(af, "main", boom)
    err = io.StringIO()
    assert af.entrypoint(["x"], stderr=err) == af.EXIT_STAGING
    assert err.getvalue().startswith("af-entry: internal error: RuntimeError")


def test_af_entry_reports_entry_errors_with_their_code(monkeypatch):
    def bad(argv):
        raise af.EntryError(af.EXIT_WORKDIR, "nope")

    monkeypatch.setattr(af, "main", bad)
    err = io.StringIO()
    assert af.entrypoint(["x"], stderr=err) == af.EXIT_WORKDIR and err.getvalue() == "af-entry: nope\n"

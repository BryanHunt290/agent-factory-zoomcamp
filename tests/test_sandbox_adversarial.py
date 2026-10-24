"""v0.2 live adversarial acceptance suite (marker: docker_adversarial).

Run ONLY with:
    py -3.12 -m pytest -m docker_adversarial --run-docker-adversarial -v -s

Hostile fixtures live in tests/hostile/*.hpy (not importable). Every one is
executed only through SandboxManager -> DockerBackend, the same path an
untrusted provider's code takes, after require_container_isolation() has
verified isolation CONTAINER or stronger. While these tests run, the host
may launch only the docker CLI (conftest guard), so hostile code can never
run on Windows.

The suite fails if Docker is unavailable, the approved image does not
match the lock, isolation is below CONTAINER, any test skips, or any Agent
Factory container remains afterwards. It tests containment boundaries; it
does not attempt to exploit Docker Desktop or the kernel.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness
from hostmon import HostMonitor

from agent_factory.demo import APPROVE, DEV_POLISH, PLAN
from agent_factory.providers import FakeProvider
from agent_factory.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_SANDBOX_ENV,
    CodeTrust,
    ExecutionRequest,
    ExecutionStatus,
    IsolationLevel,
    LimitHit,
    LocalProcessBackend,
    ResourceLimits,
    SandboxCleanupError,
    SandboxInfrastructureError,
    SandboxManager,
    StagingRejected,
)
from agent_factory.sandbox import docker as docker_mod
from agent_factory.sandbox.config import (
    ImageRecord,
    docker_config_from_lock,
    load_image_record,
    save_image_record,
)
from agent_factory.sandbox.docker import LABEL_EXEC, DockerBackend, DockerConfig, SubprocessDockerRunner
from agent_factory.sandbox.process import ProcessOutcome
from agent_factory.sandbox.staging import StagedArchive
from agent_factory.state import RunStatus
from agent_factory.workspace import Workspace

pytestmark = pytest.mark.docker_adversarial

HOSTILE = Path(__file__).parent / "hostile"
MiB = 1024 * 1024
#: scenario -> execution id whose container was verified removed
CLEANUP: dict[str, str] = {}


def log(label: str, data) -> None:
    print(f"\n[adv] {label}: {json.dumps(data, default=str)[:3000]}")


# ------------------------------------------------------------ infrastructure


class RecordingRunner:
    """Wraps the real docker CLI runner and records every call (evidence)."""

    def __init__(self, inner, *, drop_rm_for_project=False, interrupt_project_start=False):
        self.inner = inner
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.drop_rm_for_project = drop_rm_for_project
        self.interrupt_project_start = interrupt_project_start

    @staticmethod
    def _is_project(args) -> bool:
        return not any("af-selftest-" in a for a in args)

    def __call__(self, args, **kw):
        args = list(args)
        if self.interrupt_project_start and args[0] == "start" and self._is_project(args):
            self._record(args, None, b"", b"")
            raise KeyboardInterrupt("simulated orchestrator interruption")
        if self.drop_rm_for_project and args[0] == "rm" and self._is_project(args):
            self._record(args, "dropped", b"", b"")
            return ProcessOutcome(0, b"", b"", False, False, False, False, 0.0)
        t0 = time.monotonic()
        out = self.inner(args, **kw)
        keep = args[:2] == ["container", "inspect"] or args[0] in ("kill", "rm", "ps", "start")
        self._record(args, out.exit_code, out.stdout[:4000] if keep else b"", out.stderr[:2000] if keep else b"", t0)
        return out

    def _record(self, args, code, stdout, stderr, t0=None):
        now = time.monotonic()
        with self.lock:
            self.calls.append({"t0": t0 if t0 is not None else now, "t": now, "args": args, "exit": code,
                               "stdout": stdout, "stderr": stderr})

    def for_exec(self, exec_id: str) -> list[dict]:
        return [c for c in self.calls if any(exec_id in a for a in c["args"])]

    def project_starts(self) -> list[dict]:
        return [c for c in self.calls if c["args"][0] == "start" and self._is_project(c["args"])]


@pytest.fixture(scope="module")
def sb():
    config = docker_config_from_lock()
    if config.problems():
        pytest.fail("approved sandbox image not usable: " + "; ".join(config.problems()))
    record = load_image_record()
    runner = RecordingRunner(SubprocessDockerRunner())
    backend = DockerBackend(config, runner=runner)
    status = backend.self_test()
    if not status.available:
        pytest.fail("Docker sandbox unavailable or failed its isolation self-test: " + "; ".join(status.reasons))
    image = json.loads(backend._docker(["image", "inspect", "--format", "{{json .}}", config.image_id]).stdout)
    labels = image["Config"].get("Labels") or {}
    if image["Id"] != record.image_id or labels.get("org.agent-factory.sandbox.af-entry-sha256") != record.af_entry_sha256:
        pytest.fail("the image in Docker does not match the approved image lock")
    ok, before = backend._containers_with_label(LABEL_EXEC)
    if not ok or before:
        pytest.fail(f"Agent Factory containers exist before the suite: {before}")
    manager = SandboxManager([backend], max_concurrent=2)
    yield SimpleNamespace(manager=manager, backend=backend, runner=runner, record=record, config=config)
    ok, after = backend._containers_with_label(LABEL_EXEC)
    assert ok and after == [], f"Agent Factory containers remain after the adversarial suite: {after}"


def require_container_isolation(manager: SandboxManager) -> None:
    """Fail BEFORE any untrusted/hostile code is sent unless every backend the manager
    would consider for untrusted code is a CONTAINER-or-stronger security sandbox."""
    if manager.allow_test_doubles:
        pytest.fail("refusing to run hostile code: test doubles are allowed")
    eligible = [b for b in manager.backends if manager.ineligibility(b, CodeTrust.UNTRUSTED) is None]
    if not eligible:
        pytest.fail("refusing to run hostile code: no backend is eligible for untrusted code")
    for b in eligible:
        if (
            isinstance(b, LocalProcessBackend)
            or not isinstance(b, DockerBackend)
            or not b.is_security_sandbox
            or b.isolation_level < IsolationLevel.CONTAINER
        ):
            pytest.fail(f"refusing to run hostile code: backend {b.name!r} is below CONTAINER isolation")


def run_hostile(sb, tmp_path, fixture, args=(), *, limits=None, extra=None, manager=None):
    manager = manager or sb.manager
    require_container_isolation(manager)
    ws = Workspace.create(tmp_path / f"hostile-{fixture}-{secrets.token_hex(3)}")
    assert ws.trust is CodeTrust.UNTRUSTED
    (ws.project_dir / "probe.py").write_bytes((HOSTILE / f"{fixture}.hpy").read_bytes())
    for rel, data in (extra or {}).items():
        (ws.project_dir / rel).write_bytes(data)
    request = ExecutionRequest(
        execution_id=f"af-adv-{secrets.token_hex(6)}",
        workspace=ws,
        command=("python", "probe.py", *args),
        limits=limits or ResourceLimits(timeout_s=60),
        env=dict(DEFAULT_SANDBOX_ENV),
        env_allowlist=DEFAULT_ENV_ALLOWLIST,
        trust=CodeTrust.UNTRUSTED,
        run_id="adversarial",
    )
    return manager.run(request), request.execution_id, ws


def report(result):
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line is not None, f"no RESULT line; stderr={result.stderr[-500:]}"
    return json.loads(line[len("RESULT ") :])


def assert_cleaned(sb, exec_id, scenario, runner=None):
    runner = runner or sb.runner
    ok, left = sb.backend._containers_with_label(f"{LABEL_EXEC}={exec_id}")
    assert ok and left == [], f"{scenario}: container still present: {left}"
    calls = [c["args"][0] for c in runner.for_exec(exec_id)]
    assert "rm" in calls and calls[-1] == "ps", f"{scenario}: no rm + verification recorded ({calls})"
    CLEANUP[scenario] = exec_id


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture(scope="module")
def canary(tmp_path_factory):
    """Random host canary files outside every Agent Factory workspace."""
    d = tmp_path_factory.mktemp("host-canary")
    token = f"AF-CANARY-{secrets.token_hex(16)}"
    name = f"canary-{secrets.token_hex(4)}.txt"
    (d / name).write_text(token, encoding="utf-8")
    (d / "aws_credentials").write_text(f"[default]\naws_secret_access_key = {token}\n", encoding="utf-8")
    (d / "nested").mkdir()
    (d / "nested" / "deep.txt").write_text(token, encoding="utf-8")

    def variants(path: Path) -> list[str]:
        posix = path.as_posix()  # C:/Users/...
        drive, rest = posix[0].lower(), posix[2:]
        return [
            str(path), posix, f"/mnt/{drive}{rest}", f"/run/desktop/mnt/host/{drive}{rest}",
            f"/host_mnt/{drive}{rest}", f"/mnt/host/{drive}{rest}", f"../../../../../../mnt/{drive}{rest}",
            f"/proc/1/root/mnt/{drive}{rest}",
        ]  # fmt: skip

    return SimpleNamespace(dir=d, token=token, name=name, variants=variants, snapshot=lambda: tree_snapshot(d))


# ---------------------------------------------------------------- 1. reads


def test_01_host_filesystem_read_escape(sb, tmp_path, canary):
    before = canary.snapshot()
    targets = canary.variants(canary.dir / canary.name) + canary.variants(canary.dir / "aws_credentials")
    result, eid, ws = run_hostile(sb, tmp_path, "read_escape", targets)
    assert result.status is ExecutionStatus.COMPLETED and result.exit_code == 0, result.stderr[-500:]
    assert canary.token not in result.stdout + result.stderr
    r = report(result)
    assert r["search_hits"] == []
    for t in targets:
        assert "errno" in r[t], (t, r[t])  # every host-path variant failed
    assert canary.snapshot() == before
    readable = sorted(p for p, v in r.items() if isinstance(v, dict) and ("read" in v or "dir" in v))
    log("01 read escape", {"host_variants_attempted": len(targets), "all_failed": True,
                           "container_paths_readable (container's own files)": readable})  # fmt: skip
    assert_cleaned(sb, eid, "success-read-probe")


# --------------------------------------------------------------- 2. writes


def test_02_host_filesystem_write_escape(sb, tmp_path, canary):
    before = canary.snapshot()
    targets = canary.variants(canary.dir / "injected.txt") + canary.variants(canary.dir / canary.name)
    result, eid, ws = run_hostile(sb, tmp_path, "write_escape", targets)
    project_after = tree_snapshot(ws.project_dir)
    r = report(result)
    for path, outcome in r.items():
        if isinstance(outcome, dict) and "written" in outcome:
            # Only relative-looking names (e.g. "C:\\Users\\...") may be written, and only inside the sandbox tmpfs.
            assert outcome["written"].startswith(("/work/", "/tmp/")), (path, outcome)
        elif isinstance(outcome, dict):
            assert outcome["errno"] in (30, 13, 2, 1), (path, outcome)
        else:
            assert outcome != "SUCCEEDED", path
    for system_path in ("/af-escape", "/etc/af-escape", "/usr/af-escape", "/usr/local/bin/af-escape", "/work/../af-escape"):
        assert r[system_path] == {"errno": 30}, (system_path, r[system_path])
    assert canary.snapshot() == before
    assert project_after == {"probe.py": hashlib.sha256((HOSTILE / "write_escape.hpy").read_bytes()).hexdigest()}
    contained = sorted(v["written"] for v in r.values() if isinstance(v, dict) and "written" in v)
    log("02 write escape", {"attempts": len(r), "contained_relative_writes": contained,
                            "victim_tamper": {k: v for k, v in r.items() if ":" in k and k.split(":")[0] in ("chmod", "unlink", "rename")}})  # fmt: skip
    assert_cleaned(sb, eid, "success-write-probe")


# -------------------------------------------------------------- 3. network


def test_03_network_escape(sb, tmp_path):
    result, eid, ws = run_hostile(sb, tmp_path, "network")
    r = report(result)
    assert r["interfaces"] == ["lo"]
    assert r["/proc/net/route"].strip().count("\n") == 0  # header only: no routes, no default gateway
    assert r["dns"]["af-dns-check.invalid"] != [] and isinstance(r["dns"]["af-dns-check.invalid"], str)
    for name, outcome in r["dns"].items():
        if isinstance(outcome, list):  # only acceptable if it came from the static /etc/hosts file
            assert all(addr in r["/etc/hosts"] for addr in outcome), (name, outcome)
    assert all(v != "CONNECTED" for v in r["tcp"].values()), r["tcp"]
    assert r["udp"] != "SENT" and r["loopback"] == "ok"
    log("03 network", {"interfaces": r["interfaces"], "dns": r["dns"], "tcp_errnos": r["tcp"], "udp": r["udp"]})
    assert_cleaned(sb, eid, "success-network-probe")


# ------------------------------------------------------- 4. env / secrets


def test_04_host_environment_and_secret_theft(sb, tmp_path, canary, monkeypatch):
    secrets_set = {
        "OPENAI_API_KEY": f"sk-canary-{secrets.token_hex(16)}",
        "ANTHROPIC_API_KEY": f"sk-ant-canary-{secrets.token_hex(16)}",
        "AWS_SECRET_ACCESS_KEY": f"aws-canary-{secrets.token_hex(16)}",
        "AF_CANARY_SECRET": f"af-canary-{secrets.token_hex(16)}",
    }
    for k, v in secrets_set.items():
        monkeypatch.setenv(k, v)
    home = Path(os.environ.get("USERPROFILE", "C:/Users/someone"))
    targets = canary.variants(canary.dir / "aws_credentials") + [
        str(home / ".aws" / "credentials"), (home / ".aws" / "credentials").as_posix(),
        str(home / ".docker" / "config.json"),
    ]  # fmt: skip
    result, eid, ws = run_hostile(sb, tmp_path, "env_theft", targets)
    blob = result.stdout + result.stderr
    for value in [*secrets_set.values(), canary.token]:
        assert value not in blob
    r = report(result)
    assert set(r["environ"]) == set(DEFAULT_SANDBOX_ENV) | {"HOME", "PATH", "HOSTNAME"}
    for t in targets:
        assert str(r["files"][t]).startswith("errno"), (t, r["files"][t])
    log("04 secrets", {"container_env_names": sorted(r["environ"]), "proc_environ_pids": sorted(r["proc_environ"]),
                       "credential_locations": {p: (v if isinstance(v, str) and v.startswith("errno") else "listed/read (container-local)")
                                                for p, v in r["files"].items()}})  # fmt: skip
    assert_cleaned(sb, eid, "success-env-probe")


# -------------------------------------------------------------- 5. timeout


def test_05_wall_clock_timeout(sb, tmp_path):
    t0 = time.monotonic()
    result, eid, ws = run_hostile(sb, tmp_path, "infinite", limits=ResourceLimits(timeout_s=3))
    elapsed = time.monotonic() - t0
    assert result.status is ExecutionStatus.TIMED_OUT and result.timed_out
    assert LimitHit.TIME in result.limits_hit
    kills = [c for c in sb.runner.for_exec(eid) if c["args"][0] == "kill"]
    assert kills and kills[0]["exit"] == 0, kills
    state = json.loads(next(c for c in sb.runner.for_exec(eid) if c["args"][:2] == ["container", "inspect"])["stdout"])
    assert elapsed < 3 + 20
    log("05 timeout", {"limit_s": 3, "elapsed_s": round(elapsed, 2), "execution_s": round(result.duration_s, 2),
                       "kill": kills[0]["args"], "kill_exit": kills[0]["exit"], "container_exit": state["ExitCode"],
                       "status": result.status, "limits_hit": result.limits_hit})  # fmt: skip
    assert_cleaned(sb, eid, "timeout")


# ------------------------------------------------------ 6. child survival


def test_06_child_process_survival(sb, tmp_path):
    seen = {}

    def watch(name):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and "top" not in seen:
            out = sb.runner.inner(
                ["top", name, "-eo", "pid,ppid,sid,stat,args"],
                host=sb.backend._endpoint, stdin=None, timeout_s=10, output_limit=65536, on_kill=None,
            )  # fmt: skip
            text = out.stdout.decode(errors="replace")
            if out.exit_code == 0 and text.count("sleep") >= 1 and text.count("python") >= 3:
                seen["top"] = text
            time.sleep(0.3)

    exec_holder = {}
    real_run = sb.manager.run

    def run_and_watch(request):
        exec_holder["id"] = request.execution_id
        threading.Thread(target=watch, args=(request.execution_id,), daemon=True).start()
        return real_run(request)

    sb.manager.run = run_and_watch
    try:
        result, eid, ws = run_hostile(sb, tmp_path, "children", limits=ResourceLimits(timeout_s=6))
    finally:
        sb.manager.run = real_run
    assert result.status is ExecutionStatus.TIMED_OUT
    spawned = report(result)
    assert "top" in seen, "children were never observed inside the container"
    after = sb.runner.inner(["top", eid, "-eo", "pid"], host=sb.backend._endpoint, stdin=None, timeout_s=10,
                            output_limit=65536, on_kill=None)  # fmt: skip
    assert after.exit_code != 0  # the container (and its whole PID namespace) is gone
    log("06 children", {"reported": spawned, "processes_inside_before_timeout": seen["top"].strip().splitlines(),
                        "docker_top_after": after.stderr.decode(errors="replace").strip()})  # fmt: skip
    assert_cleaned(sb, eid, "children-timeout")


# --------------------------------------------------------------- 7. memory


def test_07_memory_exhaustion(sb, tmp_path):
    limits = ResourceLimits(memory_mb=64, disk_mb=8, timeout_s=60)
    with HostMonitor() as hm:
        result, eid, ws = run_hostile(sb, tmp_path, "memory", limits=limits)
    assert "UNBOUNDED" not in result.stdout
    progress = [int(x) for x in result.stdout.split() if x.isdigit()]
    state = json.loads(next(c for c in sb.runner.for_exec(eid) if c["args"][:2] == ["container", "inspect"])["stdout"])
    assert state["OOMKilled"] is True
    assert result.status is ExecutionStatus.LIMIT_EXCEEDED and LimitHit.MEMORY in result.limits_hit
    assert max(progress, default=0) <= 64 * MiB
    assert hm.max_gap < 2.0
    log("07 memory", {"limit_mb": 64, "max_allocated_reported_mb": max(progress, default=0) / MiB,
                      "OOMKilled": state["OOMKilled"], "exit": state["ExitCode"], "limits_hit": result.limits_hit,
                      "host": hm.summary()})  # fmt: skip
    assert_cleaned(sb, eid, "oom")


# ----------------------------------------------------------------- 8. disk


def test_08_disk_exhaustion(sb, tmp_path):
    limits = ResourceLimits(disk_mb=16, timeout_s=60)
    with HostMonitor() as hm:
        result, eid, ws = run_hostile(sb, tmp_path, "disk", limits=limits)
    r = report(result)
    (work_written, work_err), (tmp_written, tmp_err) = r["work"], r["tmp"]
    assert work_err[0] == 28 and tmp_err[0] == 28  # ENOSPC on both tmpfs mounts
    assert work_written <= 16 * MiB and tmp_written <= 64 * MiB
    assert result.status is ExecutionStatus.LIMIT_EXCEEDED and LimitHit.DISK in result.limits_hit
    assert hm.disk_after - hm.disk_before > -64 * MiB  # host disk not materially consumed
    assert tree_snapshot(ws.project_dir) == {"probe.py": hashlib.sha256((HOSTILE / "disk.hpy").read_bytes()).hexdigest()}
    log("08 disk", {"work_written_mb": work_written / MiB, "work_errno": work_err, "tmp_written_mb": tmp_written / MiB,
                    "tmp_errno": tmp_err, "limits_hit": result.limits_hit, "host": hm.summary()})  # fmt: skip
    assert_cleaned(sb, eid, "disk-exhaustion")


# ----------------------------------------------------------------- 9. pids


def test_09_pid_exhaustion(sb, tmp_path):
    limits = ResourceLimits(pids=16, timeout_s=60)
    with HostMonitor() as hm:
        result, eid, ws = run_hostile(sb, tmp_path, "pids", limits=limits)
    r = report(result)
    assert r["error"] is not None and r["error"][0] == 11  # EAGAIN
    assert r["spawned"] < 16
    assert result.status is ExecutionStatus.LIMIT_EXCEEDED and LimitHit.PIDS in result.limits_hit
    assert abs(hm.proc_after - hm.proc_before) < 25
    log("09 pids", {"pids_limit": 16, "children_spawned_before_EAGAIN": r["spawned"], "error": r["error"],
                    "limits_hit": result.limits_hit, "host": hm.summary()})  # fmt: skip
    assert_cleaned(sb, eid, "pid-exhaustion")


# --------------------------------------------------------------- 10. output


def test_10_output_flooding(sb, tmp_path):
    limits = ResourceLimits(output_bytes=65536, timeout_s=30)
    with HostMonitor() as hm:
        result, eid, ws = run_hostile(sb, tmp_path, "output_flood", limits=limits)
    assert result.status is ExecutionStatus.LIMIT_EXCEEDED and LimitHit.OUTPUT in result.limits_hit
    assert len(result.stdout.encode()) <= 65536 and len(result.stderr.encode()) <= 65536
    assert result.stdout_truncated or result.stderr_truncated
    assert any(c["args"][0] == "kill" for c in sb.runner.for_exec(eid))
    assert hm.ws_after - hm.ws_before < 64 * MiB
    log("10 output", {"limit_per_stream": 65536, "stdout_bytes": len(result.stdout.encode()),
                      "stderr_bytes": len(result.stderr.encode()), "truncated": [result.stdout_truncated, result.stderr_truncated],
                      "duration_s": round(result.duration_s, 2), "host": hm.summary()})  # fmt: skip
    assert_cleaned(sb, eid, "output-overflow")


# ------------------------------------------------- 11. malformed paths/archives


def _member(name, kind=tarfile.REGTYPE, data=b"x", **kw):
    info = tarfile.TarInfo(name)
    info.type = kind
    for k, v in kw.items():
        setattr(info, k, v)
    if kind == tarfile.REGTYPE:
        info.size = len(data)
    return info, (data if kind == tarfile.REGTYPE else None)


def _archive(*members, fmt=tarfile.PAX_FORMAT):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


CRAFTED = {
    "dotdot": lambda: _archive(_member("../x")),
    "absolute": lambda: _archive(_member("/etc/evil")),
    "symlink": lambda: _archive(_member("link", tarfile.SYMTYPE, linkname="/etc/passwd")),
    "symlink_dir_then_write": lambda: _archive(_member("d", tarfile.SYMTYPE, linkname="/"), _member("d/evil")),
    "hardlink": lambda: _archive(_member("h", tarfile.LNKTYPE, linkname="/etc/passwd")),
    "char_device": lambda: _archive(_member("c", tarfile.CHRTYPE, devmajor=1, devminor=3)),
    "block_device": lambda: _archive(_member("b", tarfile.BLKTYPE, devmajor=8, devminor=0)),
    "fifo": lambda: _archive(_member("f", tarfile.FIFOTYPE)),
    "directory_entry": lambda: _archive(_member("dir", tarfile.DIRTYPE)),
    "duplicate": lambda: _archive(_member("a.py", data=b"1"), _member("a.py", data=b"2")),
    "file_dir_conflict": lambda: _archive(_member("a"), _member("a/b")),
    "pax_path_override": lambda: _archive(_member("innocent.py", pax_headers={"path": "../../pax-evil"})),
    "gnu_longname_traversal": lambda: _archive(_member("../" * 40 + "x"), fmt=tarfile.GNU_FORMAT),
    "dot_component": lambda: _archive(_member("./a.py")),
}


def _crafted_run(sb, tmp_path, monkeypatch, archive, *, truncate=False):
    monkeypatch.setattr(docker_mod, "inspect_archive", lambda data: [])  # bypass host checks: test af-entry itself
    monkeypatch.setattr(docker_mod, "stage_workspace", lambda w, max_bytes: StagedArchive(archive, ("x",), len(archive), ""))
    if truncate:
        real = docker_mod.build_create_argv
        monkeypatch.setattr(docker_mod, "build_create_argv", lambda **kw: real(**{**kw, "archive_bytes": kw["archive_bytes"] + 512}))
    return run_hostile(sb, tmp_path, "list_work")


@pytest.mark.parametrize("case", sorted(CRAFTED))
def test_11a_malformed_archives_rejected_by_af_entry(sb, tmp_path, canary, monkeypatch, case):
    before = canary.snapshot()
    with pytest.raises(SandboxInfrastructureError) as exc:
        _crafted_run(sb, tmp_path, monkeypatch, CRAFTED[case]())
    assert "exit 120" in str(exc.value), str(exc.value)
    eid = next(c["args"] for c in reversed(sb.runner.calls) if c["args"][0] == "create")[4]
    assert canary.snapshot() == before
    log(f"11a {case}", {"af_entry": str(exc.value).split("af-entry: ", 1)[-1][:160]})
    assert_cleaned(sb, eid, f"af-entry-rejection:{case}")


def test_11b_truncated_archive_stream_rejected(sb, tmp_path, monkeypatch):
    with pytest.raises(SandboxInfrastructureError, match="stdin ended"):
        _crafted_run(sb, tmp_path, monkeypatch, _archive(_member("a.py")), truncate=True)


def test_11c_unicode_lookalikes_stay_inside_work(sb, tmp_path, monkeypatch):
    probe = (HOSTILE / "list_work.hpy").read_bytes()
    names = ["\uff0e\uff0e\uff0fx", "cafe\u0301.py", "caf\u00e9.py", "\u202eevil.py", "\u2025/x"]
    archive = _archive(_member("probe.py", data=probe), *[_member(n) for n in names])
    result, eid, ws = _crafted_run(sb, tmp_path, monkeypatch, archive)
    r = report(result)
    assert r["outside"] == []
    assert set(r["files"]) == {"probe.py", *names}  # extracted, but only inside /work
    log("11c unicode", {"af_entry_layer": "accepted and contained in /work", "files": r["files"],
                        "host_staging_layer": "rejects non-NFC and format characters (Tier-0 tests)"})  # fmt: skip
    assert_cleaned(sb, eid, "success-unicode")


def test_11d_host_side_attacks_rejected_before_any_container(sb, tmp_path):
    import _winapi

    cases = {}
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    setups = {
        "hardlink": lambda p: os.link(outside / "secret.txt", p / "alias.txt"),
        "junction": lambda p: _winapi.CreateJunction(str(outside), str(p / "evil")),
        "nfd_name": lambda p: (p / "cafe\u0301.py").write_text("x", encoding="utf-8"),
        "bidi_override": lambda p: (p / "evil\u202egnp.py").write_text("x", encoding="utf-8"),
        "oversize": lambda p: (p / "big.bin").write_bytes(b"x" * (ResourceLimits().max_input_bytes + 1)),
    }
    for case, setup in setups.items():
        creates_before = sum(1 for c in sb.runner.calls if c["args"][0] == "create")
        ws = Workspace.create(tmp_path / f"host-{case}")
        (ws.project_dir / "probe.py").write_text("print('must not run')\n", encoding="utf-8")
        setup(ws.project_dir)
        request = ExecutionRequest(
            execution_id=f"af-adv-{secrets.token_hex(6)}", workspace=ws, command=("python", "probe.py"),
            env=dict(DEFAULT_SANDBOX_ENV), env_allowlist=DEFAULT_ENV_ALLOWLIST, trust=CodeTrust.UNTRUSTED,
        )  # fmt: skip
        require_container_isolation(sb.manager)
        with pytest.raises(StagingRejected) as exc:
            sb.manager.run(request)
        assert sum(1 for c in sb.runner.calls if c["args"][0] == "create") == creates_before
        cases[case] = str(exc.value)[:140]
    log("11d host-side", cases)


# ------------------------------------------------------------ 12. concurrency


def test_12_concurrent_run_isolation(sb, tmp_path):
    tokens = [f"TOKEN-A-{secrets.token_hex(8)}", f"TOKEN-B-{secrets.token_hex(8)}"]
    results, spans, errors = {}, {}, []

    def go(token, sub):
        try:
            t0 = time.monotonic()
            results[token] = run_hostile(sb, tmp_path / sub, "concurrency", (token,))
            spans[token] = (t0, time.monotonic())
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    threads = [threading.Thread(target=go, args=(t, s)) for t, s in zip(tokens, ("a", "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    (ra, ea, _), (rb, eb, _) = results[tokens[0]], results[tokens[1]]
    assert ea != eb
    # Container lifetime = from issuing `docker start` until that attached call returned (container exited).
    a_start, a_end = next((c["t0"], c["t"]) for c in sb.runner.for_exec(ea) if c["args"][0] == "start")
    b_start, b_end = next((c["t0"], c["t"]) for c in sb.runner.for_exec(eb) if c["args"][0] == "start")
    overlap = min(a_end, b_end) - max(a_start, b_start)
    assert overlap > 2.0, f"executions did not run concurrently (overlap {overlap:.2f}s)"
    for (mine, other), res in zip(((tokens[0], tokens[1]), (tokens[1], tokens[0])), (ra, rb)):
        r = report(res)
        assert r["token"] == mine and mine in res.stdout and other not in res.stdout + res.stderr
        assert r["work"]["mine.txt"] == mine and r["tmp"]["mine.txt"] == mine
    log("12 concurrency", {"exec_ids": [ea, eb], "running_overlap_s": round(overlap, 2),
                           "procs_seen_by_A": report(ra)["procs"]})  # fmt: skip
    assert_cleaned(sb, ea, "concurrent-A")
    assert_cleaned(sb, eb, "concurrent-B")


# ---------------------------------------------------------- 13. cleanup matrix


def test_13a_cleanup_after_success_and_ordinary_failure(sb, tmp_path):
    ok, eid, _ = run_hostile(sb, tmp_path, "list_work")
    assert ok.exit_code == 0
    assert_cleaned(sb, eid, "success")
    (tmp_path / "f").mkdir()
    fail, eid2, _ = run_hostile(sb, tmp_path / "f", "list_work", extra={"probe.py": b"import sys\nsys.exit(1)\n"})
    assert fail.status is ExecutionStatus.COMPLETED and fail.exit_code == 1
    assert_cleaned(sb, eid2, "ordinary-failure")


def test_13b_cleanup_after_orchestrator_interruption(sb, tmp_path):
    runner = RecordingRunner(SubprocessDockerRunner(), interrupt_project_start=True)
    backend = DockerBackend(sb.config, runner=runner)
    manager = SandboxManager([backend])
    with pytest.raises(KeyboardInterrupt):
        run_hostile(sb, tmp_path, "infinite", manager=manager)
    eid = next(c["args"] for c in runner.calls if c["args"][0] == "start" and RecordingRunner._is_project(c["args"]))[-1]
    assert_cleaned(sb, eid, "orchestrator-interruption", runner=runner)


def test_13c_abandoned_container_removed_by_janitor(sb, tmp_path):
    runner = RecordingRunner(SubprocessDockerRunner())
    crashed = DockerBackend(sb.config, runner=runner, wall_clock=lambda: time.time() - 100_000)  # deadline long past
    assert crashed.probe().available
    ws = Workspace.create(tmp_path / "abandoned")
    (ws.project_dir / "probe.py").write_text("pass\n", encoding="utf-8")
    request = ExecutionRequest(execution_id=f"af-adv-{secrets.token_hex(6)}", workspace=ws, command=("python", "probe.py"),
                               env=dict(DEFAULT_SANDBOX_ENV), env_allowlist=DEFAULT_ENV_ALLOWLIST, trust=CodeTrust.UNTRUSTED)
    crashed.prepare(request)  # the "process" dies here: container created, never started or destroyed
    ok, present = sb.backend._containers_with_label(f"{LABEL_EXEC}={request.execution_id}")
    assert ok and len(present) == 1
    removed = sb.backend.janitor()  # a fresh orchestrator's janitor
    assert present[0] in removed
    ok, left = sb.backend._containers_with_label(f"{LABEL_EXEC}={request.execution_id}")
    assert ok and left == []
    CLEANUP["abandoned-janitor"] = request.execution_id
    log("13c janitor", {"removed": removed})


def test_13z_cleanup_matrix_complete(sb):
    required = {"success", "ordinary-failure", "timeout", "children-timeout", "oom", "disk-exhaustion",
                "pid-exhaustion", "output-overflow", "orchestrator-interruption", "abandoned-janitor"}  # fmt: skip
    afentry = [k for k in CLEANUP if k.startswith("af-entry-rejection:")]
    missing = required - set(CLEANUP)
    assert not missing, f"cleanup not proven for: {sorted(missing)}"
    assert len(afentry) == len(CRAFTED)
    ok, left = sb.backend._containers_with_label(LABEL_EXEC)
    assert ok and left == []
    log("13 cleanup matrix", {"scenarios_verified": sorted(CLEANUP), "agent_factory_containers_remaining": len(left)})


# ------------------------------------------------------------ 14. fail closed


class SimulatedRealModel(FakeProvider):
    name = "simulated-real-model"
    produces_trusted_code = False


def _blocked_run(tmp_path, backend):
    h = Harness(tmp_path / f"fc-{secrets.token_hex(3)}")
    provider = SimulatedRealModel({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    manager = SandboxManager([backend])
    require_container_isolation(manager)
    state = h.executor(provider, sandbox=manager).start(h.template.task, run_id="fail-closed")
    return state, provider, h, manager


def _assert_blocked_no_retry(state, provider):
    assert state.status is RunStatus.BLOCKED, (state.status, state.error)
    assert "tester" not in state.node_visits
    assert not any(k.startswith("tester:") for k in state.edge_traversals)
    assert len(provider.calls_for("developer")) == 1


@pytest.mark.parametrize("case", ["docker_unavailable", "remote_docker_host", "ssh_docker_host", "image_missing",
                                  "stale_lock", "self_test_failure"])  # fmt: skip
def test_14_fail_closed(sb, tmp_path, monkeypatch, case):
    runner = RecordingRunner(SubprocessDockerRunner())
    config = sb.config
    if case == "docker_unavailable":
        runner = RecordingRunner(SubprocessDockerRunner("docker-af-not-installed"))
    elif case == "remote_docker_host":
        monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:9")
    elif case == "ssh_docker_host":
        monkeypatch.setenv("DOCKER_HOST", "ssh://nobody@af-check.invalid")
    elif case == "image_missing":
        config = DockerConfig(image_id="sha256:" + "0" * 64)
    elif case == "stale_lock":
        lock = tmp_path / "image.lock.json"
        save_image_record(ImageRecord(**{**sb.record.__dict__, "af_entry_sha256": "0" * 64}), lock)
        config = docker_config_from_lock(lock)
    elif case == "self_test_failure":
        config = DockerConfig(image_id=sb.record.base_image_id)  # real image without af-entry: self-test cannot pass
    state, provider, h, _ = _blocked_run(tmp_path, DockerBackend(config, runner=runner))
    _assert_blocked_no_retry(state, provider)
    assert runner.project_starts() == []  # no untrusted project process executed
    ok, left = sb.backend._containers_with_label(LABEL_EXEC)
    assert ok and left == []
    log(f"14 {case}", {"status": state.status, "blocked": state.blocked["message"][:200],
                       "docker_subcommands": sorted({c["args"][0] for c in runner.calls})})  # fmt: skip


def test_14_cleanup_verification_failure_blocks_further_execution(sb, tmp_path):
    runner = RecordingRunner(SubprocessDockerRunner(), drop_rm_for_project=True)
    backend = DockerBackend(sb.config, runner=runner)
    state, provider, h, manager = _blocked_run(tmp_path, backend)
    _assert_blocked_no_retry(state, provider)
    assert state.blocked["type"] == "SandboxCleanupError"
    first_starts = len(runner.project_starts())
    assert first_starts == 1  # the project ran once; its cleanup could not be verified
    leftover_exec = runner.project_starts()[0]["args"][-1]
    # Resume with the same (now unhealthy) manager: nothing more may run.
    state2 = h.executor(provider, sandbox=manager).resume("fail-closed")
    _assert_blocked_no_retry(state2, provider)
    assert len(runner.project_starts()) == first_starts
    assert "unhealthy" in state2.blocked["message"]
    # Remove the deliberately leaked container for real, then verify.
    sb.runner.inner(["rm", "--force", "--volumes", leftover_exec], host=sb.backend._endpoint, stdin=None,
                    timeout_s=60, output_limit=65536, on_kill=None)  # fmt: skip
    ok, left = sb.backend._containers_with_label(LABEL_EXEC)
    assert ok and left == []
    log("14 cleanup-verification-failure", {"first": state.blocked["message"][:200], "resume": state2.blocked["message"][:200],
                                            "project_starts": first_starts})  # fmt: skip


# ------------------------------------------------------------------- final


def test_14_default_routing_blocks_when_docker_unavailable(sb, tmp_path, monkeypatch):
    """build_executor() with no sandbox argument: Docker unreachable -> BLOCKED, never local."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:9")
    h = Harness(tmp_path / "default-down")
    provider = SimulatedRealModel({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    executor = h.executor(provider)  # default SandboxManager
    require_container_isolation(executor.tools.get("run_tests")._sandbox.__self__)
    state = executor.start(h.template.task, run_id="default-down")
    _assert_blocked_no_retry(state, provider)
    assert "local-process: not a sandbox" in state.blocked["message"]
    log("14 default routing, docker unreachable", {"status": state.status, "blocked": state.blocked["message"][:300]})


# ---------------------------------------------------------------- 15. CPU


def _cpu_progress(result):
    limit = next((ln.split()[1:] for ln in result.stdout.splitlines() if ln.startswith("LIMIT ")), None)
    used = [float(ln.split()[1]) for ln in result.stdout.splitlines() if ln.startswith("cpu ")]
    return limit, (max(used) if used else 0.0)


def test_15a_cpus_quota_constrains_cpu_availability(sb, tmp_path):
    runs = {}
    with HostMonitor() as hm:
        for cpus in (0.5, 1.0):
            sub = tmp_path / f"cpus-{cpus}"
            sub.mkdir()
            limits = ResourceLimits(cpus=cpus, timeout_s=60)
            result, eid, ws = run_hostile(sb, sub, "cpu_spinners", ("4", "3"), limits=limits)
            assert result.status is ExecutionStatus.COMPLETED and result.exit_code == 0, result.stderr[-500:]
            r = report(result)
            r["ratio"] = r["cgroup_cpu_s"] / r["wall_s"]
            runs[cpus] = r
            assert_cleaned(sb, eid, f"cpu-quota-{cpus}")
    assert runs[0.5]["cpu_max"] == "50000 100000" and runs[1.0]["cpu_max"] == "100000 100000"
    assert runs[0.5]["ratio"] <= 0.5 * 1.25, runs[0.5]  # 4 busy processes get at most ~0.5 CPU
    assert 0.75 <= runs[1.0]["ratio"] <= 1.0 * 1.25, runs[1.0]
    assert runs[1.0]["ratio"] / runs[0.5]["ratio"] >= 1.5
    assert all(r["nr_throttled"] and r["nr_throttled"] > 0 for r in runs.values())
    assert hm.max_gap < 2.0
    log("15a --cpus", {str(c): {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}
                       for c, r in runs.items()} | {"host": hm.summary()})  # fmt: skip


def test_15b_cpu_time_limit_terminates_cpu_bound_process(sb, tmp_path):
    limits = ResourceLimits(cpus=1.0, cpu_time_s=2, timeout_s=30)
    with HostMonitor() as hm:
        result, eid, ws = run_hostile(sb, tmp_path, "cpu_burn", limits=limits)
    limit, used = _cpu_progress(result)
    state = json.loads(next(c for c in sb.runner.for_exec(eid) if c["args"][:2] == ["container", "inspect"])["stdout"])
    log("15b --ulimit cpu", {"rlimit_cpu_soft_hard": limit, "cpu_used_last_report_s": used, "exit": result.exit_code,
                             "OOMKilled": state["OOMKilled"], "timed_out": result.timed_out, "status": result.status,
                             "limits_hit": result.limits_hit, "duration_s": round(result.duration_s, 2),
                             "host": hm.summary()})  # fmt: skip
    assert not result.timed_out and state["OOMKilled"] is False
    assert limit is not None and limit[0] == "2"
    assert result.exit_code == 128 + 24, "expected SIGXCPU at the soft CPU-time limit"
    assert result.status is ExecutionStatus.LIMIT_EXCEEDED and LimitHit.CPU in result.limits_hit
    assert 1.6 <= used <= 2.05  # stopped at the configured CPU-time limit
    assert hm.max_gap < 2.0
    assert_cleaned(sb, eid, "cpu-time-limit")


def test_15c_ignoring_sigxcpu_still_stops_at_the_hard_limit(sb, tmp_path):
    limits = ResourceLimits(cpus=1.0, cpu_time_s=2, timeout_s=30)
    result, eid, ws = run_hostile(sb, tmp_path, "cpu_burn", ("ignore-sigxcpu",), limits=limits)
    limit, used = _cpu_progress(result)
    state = json.loads(next(c for c in sb.runner.for_exec(eid) if c["args"][:2] == ["container", "inspect"])["stdout"])
    log("15c SIGXCPU ignored", {"rlimit_cpu_soft_hard": limit, "cpu_used_last_report_s": used, "exit": result.exit_code,
                                "OOMKilled": state["OOMKilled"], "timed_out": result.timed_out, "status": result.status,
                                "limits_hit": result.limits_hit})  # fmt: skip
    assert not result.timed_out and state["OOMKilled"] is False
    hard = int(limit[1])
    assert hard > 2, "hard CPU limit must exceed the soft limit so SIGXCPU is delivered first"
    assert result.exit_code == 128 + 9  # SIGKILL from the kernel at the hard limit
    assert result.status is ExecutionStatus.LIMIT_EXCEEDED and LimitHit.CPU in result.limits_hit
    assert hard - 0.4 <= used <= hard + 0.05
    assert_cleaned(sb, eid, "cpu-hard-limit")


def test_zz_zero_agent_factory_containers_remain(sb):
    for scenario in ("cpu-quota-0.5", "cpu-quota-1.0", "cpu-time-limit", "cpu-hard-limit"):
        assert scenario in CLEANUP, f"cleanup not proven for {scenario}"
    ok, left = sb.backend._containers_with_label(LABEL_EXEC)
    assert ok and left == []

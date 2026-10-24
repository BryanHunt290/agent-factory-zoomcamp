"""Stage E live Docker smoke tests (marker: docker_live).

Run ONLY with:  py -3.12 -m pytest -m docker_live --run-docker-live -v
Without --run-docker-live these are skipped. With it, any skip or an
unavailable Docker/image is a FAILURE.

Scope: the minimum live checks needed to validate assumptions Tier 0 could
not (context, image identity, --pull never, uid, read-only root, caps,
network none, tmpfs, copy-in/af-entry, capture, inspect timestamps,
cleanup, --log-driver none with attach, --ulimit fsize units). This is NOT
the adversarial acceptance phase: no escape, exhaustion, or stress tests.
"""

import hashlib
import json
import re
import secrets
import statistics
import time
from datetime import datetime

import pytest
from conftest import Harness

from agent_factory.demo import APPROVE, DEV_BUGGY, DEV_FIX, PLAN
from agent_factory.providers import FakeProvider
from agent_factory.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_SANDBOX_ENV,
    CodeTrust,
    ExecutionRequest,
    ExecutionStatus,
    LimitHit,
    ResourceLimits,
    SandboxInfrastructureError,
    SandboxManager,
    run_sandboxed,
)
from agent_factory.sandbox import docker as docker_mod
from agent_factory.sandbox.config import docker_config_from_lock, load_image_record
from agent_factory.sandbox.docker import LABEL_EXEC, DockerBackend, endpoint_problem
from agent_factory.sandbox.staging import StagedArchive, _build_tar
from agent_factory.state import RunStatus
from agent_factory.workspace import Workspace

pytestmark = pytest.mark.docker_live

MiB = 1024 * 1024
EXPECTED_ENV = set(DEFAULT_SANDBOX_ENV) | {"HOME", "PATH", "HOSTNAME"}


@pytest.fixture(scope="module")
def backend():
    config = docker_config_from_lock()
    if config.problems():
        pytest.fail("sandbox image not usable: " + "; ".join(config.problems()))
    b = DockerBackend(config)
    status = b.self_test()  # live isolation self-test (uid, net, ro root, caps, env)
    if not status.available:
        pytest.fail("Docker sandbox unavailable: " + "; ".join(status.reasons))
    yield b
    ok, leftovers = b._containers_with_label(LABEL_EXEC)
    assert ok and leftovers == [], f"Agent Factory containers left behind: {leftovers}"


@pytest.fixture(autouse=True)
def no_leftovers(backend):
    ok, before = backend._containers_with_label(LABEL_EXEC)
    assert ok and before == []
    yield
    ok, after = backend._containers_with_label(LABEL_EXEC)
    assert ok and after == [], f"leftover containers: {after}"


def _ws(tmp_path, files):
    ws = Workspace.create(tmp_path / f"ws-{secrets.token_hex(3)}")
    for rel, content in files.items():
        path = ws.project_dir.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return ws


def _req(ws, command=("python", "probe.py"), **kw):
    return ExecutionRequest(
        execution_id=f"af-live-{secrets.token_hex(6)}",
        workspace=ws,
        command=command,
        env=dict(DEFAULT_SANDBOX_ENV),
        env_allowlist=DEFAULT_ENV_ALLOWLIST,
        trust=CodeTrust.UNTRUSTED,
        run_id="live",
        **kw,
    )


def _report(stdout):
    line = next(ln for ln in stdout.splitlines() if ln.startswith("RESULT "))
    return json.loads(line[len("RESULT ") :])


# --------------------------------------------------------------- identity


def test_context_is_local_and_image_identity_matches(backend):
    info = backend.info()
    assert endpoint_problem(info.runtime["endpoint"]) is None
    record = load_image_record()
    assert info.runtime["image_id"] == record.image_id
    out = backend._docker(["image", "inspect", "--format", "{{json .}}", record.image_id])
    image = json.loads(out.stdout)
    assert image["Id"] == record.image_id
    assert image["Config"]["User"] == "10001:10001"
    assert all(e.startswith("PATH=") for e in image["Config"].get("Env") or [])
    assert image["Config"]["Labels"]["org.agent-factory.sandbox.base"] == record.base_image
    assert image["Config"]["Labels"]["org.agent-factory.sandbox.af-entry-sha256"] == record.af_entry_sha256


def test_pull_never_is_honoured(backend):
    name = f"af-pullcheck-{secrets.token_hex(4)}"
    out = backend._docker(
        ["create", "--pull", "never", "--name", name, "--label", f"{LABEL_EXEC}={name}",
         "--network", "none", "agent-factory.invalid/never-pulled:none", "true"]
    )  # fmt: skip
    text = (out.stdout + out.stderr).decode(errors="replace").lower()
    assert out.exit_code != 0
    assert "no such image" in text or "not found" in text, text
    assert "pulling" not in text and "unable to find image" not in text, text
    ok, left = backend._containers_with_label(f"{LABEL_EXEC}={name}")
    assert ok and left == []


# ------------------------------------------------------- runtime isolation

ISOLATION_PROBE = r"""
import json, os, socket, sys
r = {"ids": [os.getuid(), os.geteuid(), os.getgid(), os.getegid()], "groups": sorted(os.getgroups())}
st = dict(l.split(":", 1) for l in open("/proc/self/status") if ":" in l)
r["caps"] = {k: int(st[k].strip(), 16) for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")}
r["no_new_privs"] = st["NoNewPrivs"].strip()
r["net"] = sorted(os.listdir("/sys/class/net"))
r["mounts"] = {}
for line in open("/proc/mounts"):
    dev, mnt, fstype, opts = line.split()[:4]
    if mnt in ("/", "/work", "/tmp"):
        r["mounts"][mnt] = [fstype, opts.split(",")]
def write(path):
    try:
        open(path, "w").close()
        return "written"
    except OSError as e:
        return e.errno
r["write"] = {p: write(p) for p in ("/af-probe", "/usr/af-probe", "/etc/af-probe", "/work/ok", "/tmp/ok")}
s = socket.socket(); s.settimeout(3)
try:
    s.connect(("192.0.2.1", 80)); r["connect"] = "connected"
except OSError as e:
    r["connect"] = e.errno
try:
    socket.getaddrinfo("af-dns-check.invalid", 80); r["dns"] = "resolved"
except OSError as e:
    r["dns"] = type(e).__name__
r["env"] = sorted(os.environ)
r["stdin"] = sys.stdin.read()
r["cwd"] = os.getcwd()
print("STDERR-MARKER", file=sys.stderr)
print("RESULT " + json.dumps(r))
"""


def test_runtime_isolation_facts(backend, tmp_path):
    result = run_sandboxed(backend, _req(_ws(tmp_path, {"probe.py": ISOLATION_PROBE})))
    assert result.status is ExecutionStatus.COMPLETED and result.exit_code == 0, result.stderr
    r = _report(result.stdout)
    assert r["ids"] == [10001, 10001, 10001, 10001] and set(r["groups"]) <= {10001}
    assert all(v == 0 for v in r["caps"].values()), r["caps"]
    assert r["no_new_privs"] == "1"
    assert r["net"] == ["lo"]
    assert r["connect"] != "connected" and r["dns"] != "resolved"
    root_fs, root_opts = r["mounts"]["/"]
    assert "ro" in root_opts
    for mnt in ("/work", "/tmp"):
        fstype, opts = r["mounts"][mnt]
        assert fstype == "tmpfs" and {"rw", "nosuid", "nodev", "noexec"} <= set(opts), (mnt, opts)
        assert "uid=10001" in opts and "gid=10001" in opts and "mode=700" in opts
    assert r["write"]["/af-probe"] == 30 and r["write"]["/usr/af-probe"] == 30 and r["write"]["/etc/af-probe"] == 30
    assert r["write"]["/work/ok"] == "written" and r["write"]["/tmp/ok"] == "written"
    assert set(r["env"]) == EXPECTED_ENV, r["env"]
    assert r["stdin"] == ""  # staging channel closed before project code ran
    assert r["cwd"] == "/work"
    assert "STDERR-MARKER" in result.stderr and "STDERR-MARKER" not in result.stdout
    assert result.cleanup_verified


TMPFS_PROBE = r"""
import json, os, stat, subprocess
r = {}
v = os.statvfs("/work"); r["work_bytes"] = v.f_blocks * v.f_frsize
with open("/work/blob", "wb") as f:
    f.write(b"x" * (1024 * 1024))
r["blob"] = os.path.getsize("/work/blob")
with open("/work/x.sh", "w") as f:
    f.write("#!/bin/sh\necho ran\n")
os.chmod("/work/x.sh", 0o755)
try:
    subprocess.run(["/work/x.sh"], check=True, capture_output=True); r["exec"] = "ran"
except PermissionError as e:
    r["exec"] = e.errno
s = os.stat("/work/probe.py"); r["staged"] = [s.st_uid, oct(stat.S_IMODE(s.st_mode))]
print("RESULT " + json.dumps(r))
"""


def test_tmpfs_work_size_options_and_noexec(backend, tmp_path):
    limits = ResourceLimits(disk_mb=16)
    result = run_sandboxed(backend, _req(_ws(tmp_path, {"probe.py": TMPFS_PROBE}), limits=limits))
    assert result.exit_code == 0, result.stderr
    r = _report(result.stdout)
    assert r["work_bytes"] == 16 * MiB
    assert r["blob"] == MiB
    assert r["exec"] == 13  # EACCES: /work is mounted noexec
    assert r["staged"] == [10001, "0o644"]


# -------------------------------------------------------- copy-in protocol

ROUNDTRIP_PROBE = r"""
import hashlib, json, os, sys
files = {}
for root, dirs, names in os.walk("/work"):
    for n in names:
        p = os.path.join(root, n)
        files[os.path.relpath(p, "/work")] = hashlib.sha256(open(p, "rb").read()).hexdigest()
print("RESULT " + json.dumps({"files": files, "argv": sys.argv[1:]}))
"""


def test_copy_in_round_trip_and_argv_without_shell(backend, tmp_path):
    blob = bytes((i * 7919) % 256 for i in range(200_000))
    files = {
        "probe.py": ROUNDTRIP_PROBE,
        "pkg/__init__.py": "",
        "pkg/data.bin": blob,
        "dir with space/ünïcode.txt": "héllo\n",
        "empty.txt": b"",
    }
    ws = _ws(tmp_path, files)
    tricky = ["; echo pwned", "$(id)", "*", "a b", "--", "`whoami`"]
    result = run_sandboxed(backend, _req(ws, command=("python", "probe.py", *tricky)))
    assert result.exit_code == 0, result.stderr
    r = _report(result.stdout)
    expected = {rel: hashlib.sha256(c if isinstance(c, bytes) else c.encode()).hexdigest() for rel, c in files.items()}
    assert r["files"] == expected
    assert r["argv"] == tricky  # passed verbatim: no shell expansion
    assert "pwned" not in result.stdout.replace("; echo pwned", "")


def _bad_archive_run(backend, tmp_path, monkeypatch, archive=None, wrong_digest=False):
    ws = _ws(tmp_path, {"probe.py": "print('should not run')\n"})
    if archive is not None:
        monkeypatch.setattr(docker_mod, "inspect_archive", lambda data: [])  # bypass host check to test af-entry
        monkeypatch.setattr(
            docker_mod, "stage_workspace", lambda w, max_bytes: StagedArchive(archive, ("x",), len(archive), "")
        )
    if wrong_digest:
        real = docker_mod.build_create_argv
        monkeypatch.setattr(docker_mod, "build_create_argv", lambda **kw: real(**{**kw, "archive_sha256": "0" * 64}))
    with pytest.raises(SandboxInfrastructureError) as exc:
        run_sandboxed(backend, _req(ws))
    return str(exc.value)


def test_af_entry_rejects_digest_mismatch_live(backend, tmp_path, monkeypatch):
    message = _bad_archive_run(backend, tmp_path, monkeypatch, wrong_digest=True)
    assert "exit 120" in message and "sha256 mismatch" in message


@pytest.mark.parametrize(
    "member, expect",
    [("traversal", "escapes|normalised"), ("symlink", "not allowed")],
)
def test_af_entry_rejects_unsafe_members_live(backend, tmp_path, monkeypatch, member, expect):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("../af-evil.py" if member == "traversal" else "link")
        if member == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
        else:
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    message = _bad_archive_run(backend, tmp_path, monkeypatch, archive=buf.getvalue())
    assert "exit 120" in message and re.search(expect, message), message


# ------------------------------------------- container config, timestamps


def test_container_config_log_driver_and_timestamps(backend, tmp_path):
    ws = _ws(tmp_path, {"probe.py": "import sys\nprint('out-line')\nprint('err-line', file=sys.stderr)\n"})
    handle = backend.prepare(_req(ws))
    try:
        backend.execute(handle)
        out = backend._docker(["container", "inspect", "--format", "{{json .}}", handle.data["name"]])
        c = json.loads(out.stdout)
        result = backend.collect_result(handle)
    finally:
        report = backend.destroy(handle)
    assert report.verified
    hc = c["HostConfig"]
    assert hc["LogConfig"]["Type"] == "none"
    assert result.stdout.strip() == "out-line" and result.stderr.strip() == "err-line"  # attach works with log driver none
    assert hc["ReadonlyRootfs"] is True and hc["NetworkMode"] == "none" and hc["Privileged"] is False
    assert hc["CapDrop"] == ["ALL"] and not hc.get("CapAdd")
    assert not hc.get("Binds") and not c.get("Mounts") and not hc.get("Devices")
    assert hc["IpcMode"] == "private" and hc.get("PidMode", "") == ""
    assert hc["SecurityOpt"] == ["no-new-privileges:true"]
    assert hc["Memory"] == 512 * MiB and hc["MemorySwap"] == 512 * MiB and hc["PidsLimit"] == 128
    assert hc["NanoCpus"] == 1_000_000_000 and hc["Init"] is True
    assert {u["Name"] for u in hc["Ulimits"]} == {"cpu", "fsize", "nofile"}
    assert set(hc["Tmpfs"]) == {"/work", "/tmp"}
    assert c["Config"]["User"] == "10001:10001" and c["Config"]["OpenStdin"] is True
    print(f"\n[live] Config.StdinOnce={c['Config']['StdinOnce']} (af-entry reads an exact byte count, so either is fine)")
    for ts in (c["State"]["StartedAt"], c["State"]["FinishedAt"]):
        m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", ts)
        assert m, ts
        datetime.fromisoformat(m.group(1) + "+00:00")
    assert result.started_at and result.finished_at and result.duration_s > 0


# ------------------------------------------------------- staging headroom

HEADROOM_PROBE = r"""
import json, os, sys
v = os.statvfs("/work")
free_before = v.f_bavail * v.f_frsize
want = int(sys.argv[1])
with open("/work/scratch.bin", "wb") as f:
    f.write(b"y" * want)
print("RESULT " + json.dumps({"total": v.f_blocks * v.f_frsize, "free_before": free_before,
                              "scratch": os.path.getsize("/work/scratch.bin"),
                              "staged_blob": os.path.getsize("/work/blob.bin")}))
"""


def test_staging_at_input_maximum_keeps_writable_headroom(backend, tmp_path):
    from agent_factory.sandbox.staging import tmpfs_footprint

    limits = ResourceLimits(disk_mb=8)  # /work 8 MiB: staged input <= 4 MiB, >= 4 MiB headroom
    probe_fp = tmpfs_footprint(len(HEADROOM_PROBE.encode()))
    ws = _ws(tmp_path, {"probe.py": HEADROOM_PROBE, "blob.bin": b"s" * (limits.max_input_bytes - probe_fp)})
    scratch = limits.work_headroom_bytes - 256 * 1024  # nearly all of the reserved headroom
    result = run_sandboxed(backend, _req(ws, command=("python", "probe.py", str(scratch)), limits=limits))
    assert result.exit_code == 0, result.stderr
    r = _report(result.stdout)
    assert r["total"] == 8 * MiB
    assert r["staged_blob"] == limits.max_input_bytes - probe_fp
    assert r["free_before"] >= limits.work_headroom_bytes - 64 * 1024
    assert r["scratch"] == scratch
    assert result.cleanup_verified


def test_oversized_input_is_rejected_before_container_live(backend, tmp_path):
    from agent_factory.sandbox import StagingRejected

    limits = ResourceLimits(disk_mb=8)
    ws = _ws(tmp_path, {"probe.py": "print('must not run')\n", "blob.bin": b"s" * limits.max_input_bytes})
    with pytest.raises(StagingRejected, match="headroom"):
        run_sandboxed(backend, _req(ws, limits=limits))
    ok, left = backend._containers_with_label(LABEL_EXEC)
    assert ok and left == []


# ------------------------------------------------------ ulimit fsize units

FSIZE_PROBE = r"""
import errno, json
written, err = 0, None
try:
    with open("/tmp/big", "wb", buffering=0) as f:
        for _ in range(48):
            written += f.write(b"x" * 65536)
except OSError as e:
    err = e.errno
print("RESULT " + json.dumps({"written": written, "errno": err}))
"""


def test_ulimit_fsize_is_in_bytes(backend, tmp_path):
    limits = ResourceLimits(disk_mb=2)  # --ulimit fsize = 2 MiB (2097152)
    result = run_sandboxed(backend, _req(_ws(tmp_path, {"probe.py": FSIZE_PROBE}), limits=limits))
    r = _report(result.stdout)
    assert r["errno"] == 27, r  # EFBIG: Python ignores SIGXFSZ, so the write fails
    assert r["written"] == 2 * MiB, r
    assert result.cleanup_verified


# ------------------------------------------------------------ end to end


class SimulatedRealModel(FakeProvider):
    name = "simulated-real-model"
    produces_trusted_code = False


def test_untrusted_run_end_to_end_in_docker(backend, tmp_path):
    h = Harness(tmp_path / "e2e")
    provider = SimulatedRealModel({"planner": [PLAN], "developer": [DEV_BUGGY, DEV_FIX], "reviewer": [APPROVE]})
    state = h.executor(provider, sandbox=SandboxManager([backend])).start(h.template.task, run_id="live-e2e")
    assert state.status is RunStatus.COMPLETED, state.blocked or state.error
    assert [(x["node"], x["outcome"]) for x in state.history][2:5] == [
        ("tester", "fail"), ("developer", "implemented"), ("tester", "pass")
    ]
    sandbox = state.test_report["sandbox"]
    assert sandbox["backend"]["name"] == "docker" and sandbox["backend"]["isolation_level"] == "CONTAINER"
    assert sandbox["cleanup_verified"] and h.workspace.trust is CodeTrust.UNTRUSTED


def test_default_routing_executes_untrusted_code_in_docker(tmp_path):
    """build_executor() with no sandbox argument routes untrusted code to the approved Docker image."""
    h = Harness(tmp_path / "default")
    provider = SimulatedRealModel({"planner": [PLAN], "developer": [DEV_BUGGY, DEV_FIX], "reviewer": [APPROVE]})
    executor = h.executor(provider)  # default SandboxManager: [LocalProcessBackend, DockerBackend]
    state = executor.start(h.template.task, run_id="live-default")
    assert state.status is RunStatus.COMPLETED, state.blocked or state.error
    sandbox = state.test_report["sandbox"]
    assert sandbox["backend"]["name"] == "docker" and sandbox["backend"]["isolation_level"] == "CONTAINER"
    assert sandbox["backend"]["runtime"]["image_id"] == load_image_record().image_id
    assert h.workspace.trust is CodeTrust.UNTRUSTED


# ------------------------------------------------------------ measurement


def test_measure_overhead(backend, tmp_path):
    ws = _ws(tmp_path, {"probe.py": "pass\n"})
    phases = {"prepare(create)": [], "execute(start+copy-in)": [], "collect(inspect)": [], "destroy(rm+verify)": [], "total": []}
    for _ in range(5):
        t0 = time.monotonic()
        handle = backend.prepare(_req(ws, command=("python", "-c", "pass")))
        t1 = time.monotonic()
        backend.execute(handle)
        t2 = time.monotonic()
        result = backend.collect_result(handle)
        t3 = time.monotonic()
        assert backend.destroy(handle).verified and result.exit_code == 0
        t4 = time.monotonic()
        for key, value in zip(phases, (t1 - t0, t2 - t1, t3 - t2, t4 - t3, t4 - t0)):
            value_list = phases[key]
            value_list.append(value)
    print("\n[live] container overhead over 5 runs of `python -c pass` (seconds):")
    for key, values in phases.items():
        print(f"[live]   {key:<24} median {statistics.median(values):.3f}  min {min(values):.3f}  max {max(values):.3f}")
    assert statistics.median(phases["total"]) < 30


def test_zz_no_agent_factory_containers_remain(backend):
    ok, leftovers = backend._containers_with_label(LABEL_EXEC)
    assert ok and leftovers == []

"""Stage D Tier-0: DockerBackend logic against an in-memory fake CLI. No Docker, no containers."""

import json
import random

import pytest
from conftest import Harness
from docker_fakes import IMAGE_ID, LOCAL_PIPE, FakeDocker, StartBehaviour

from agent_factory.demo import APPROVE, DEV_POLISH, PLAN
from agent_factory.providers import FakeProvider
from agent_factory.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_SANDBOX_ENV,
    CodeTrust,
    ExecutionRequest,
    ExecutionStatus,
    LimitHit,
    ResourceLimits,
    SandboxCleanupError,
    SandboxInfrastructureError,
    SandboxManager,
    SandboxUnavailable,
    StagingRejected,
    run_sandboxed,
)
from agent_factory.sandbox.docker import (
    ALLOWED_SUBCOMMANDS,
    LABEL_EXEC,
    PROCESS_TOKEN,
    DockerBackend,
    DockerConfig,
    build_create_argv,
    check_hardened_argv,
    check_self_test,
    endpoint_from_context,
    endpoint_problem,
    map_container_state,
)
from agent_factory.state import RunStatus
from agent_factory.workspace import Workspace

CONFIG = DockerConfig(image_id=IMAGE_ID)
NOW = 1_800_000_000.0


@pytest.fixture
def ws(tmp_path):
    return Workspace.create(tmp_path / "ws", scaffold={"tests/__init__.py": "", "tests/test_x.py": "pass\n"})


def _backend(fake=None, config=CONFIG):
    fake = fake or FakeDocker()
    return DockerBackend(config, runner=fake, wall_clock=lambda: NOW), fake


def _ready(fake=None):
    backend, fake = _backend(fake)
    assert backend.probe().available
    return backend, fake


def _req(ws, execution_id="af-dock-0001", **kw):
    return ExecutionRequest(
        execution_id=execution_id,
        workspace=ws,
        command=("python", "-m", "unittest"),
        env=dict(DEFAULT_SANDBOX_ENV),
        env_allowlist=DEFAULT_ENV_ALLOWLIST,
        trust=CodeTrust.UNTRUSTED,
        run_id="run-1",
        **kw,
    )


def _argv(**over):
    kw = dict(
        name="af-golden-0001",
        labels={LABEL_EXEC: "af-golden-0001", "agent-factory.run": "run-1", "agent-factory.owner": "tok", "agent-factory.deadline": "123"},
        limits=ResourceLimits(),
        env=dict(DEFAULT_SANDBOX_ENV),
        command=("python", "-m", "unittest"),
        working_dir=".",
        config=CONFIG,
        archive_bytes=10240,
        archive_sha256="ab" * 32,
    )
    kw.update(over)
    return build_create_argv(**kw)


# ------------------------------------------------------------ argv: golden


GOLDEN = [
    "create", "--pull", "never", "--name", "af-golden-0001",
    "--label", "agent-factory.deadline=123", "--label", "agent-factory.exec=af-golden-0001",
    "--label", "agent-factory.owner=tok", "--label", "agent-factory.run=run-1",
    "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
    "--user", "10001:10001", "--init", "--ipc", "private", "--log-driver", "none", "--hostname", "sandbox",
    "--pids-limit", "128", "--cpus", "1", "--memory", "512m", "--memory-swap", "512m",
    "--ulimit", "cpu=125:127", "--ulimit", "fsize=268435456:268435456", "--ulimit", "nofile=256:256",
    "--tmpfs", "/work:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=256m",
    "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=64m",
    "--env", "HOME=/tmp", "--env", "LANG=C.UTF-8", "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
    "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONHASHSEED=0", "--env", "PYTHONIOENCODING=utf-8",
    "--env", "PYTHONUNBUFFERED=1",
    "--interactive", IMAGE_ID, "af-entry", "--workdir", ".", "--archive-bytes", "10240",
    "--archive-sha256", "ab" * 32, "--", "python", "-m", "unittest",
]  # fmt: skip


def test_create_argv_matches_golden():
    assert _argv() == GOLDEN
    assert check_hardened_argv(GOLDEN, CONFIG) == []


def test_generated_argv_always_passes_hardening():
    rng = random.Random(1234)
    for _ in range(200):
        limits = ResourceLimits(
            timeout_s=rng.uniform(0.5, 600),
            cpus=rng.choice([0.5, 1, 2, 4]),
            memory_mb=rng.randint(64, 4096),
            disk_mb=rng.randint(1, 2048),
            pids=rng.randint(8, 1024),
        )
        env = {k: v for k, v in DEFAULT_SANDBOX_ENV.items() if rng.random() < 0.7}
        command = ("python", *[rng.choice(["-m", "unittest", "--privileged", "-v", "/:/host"]) for _ in range(rng.randint(0, 5))])
        args = _argv(limits=limits, env=env, command=command, working_dir=rng.choice([".", "pkg", "a/b"]))
        assert check_hardened_argv(args, CONFIG) == [], args


def _inject(extra, before_image=True):
    args = list(GOLDEN)
    idx = args.index(IMAGE_ID)
    return args[:idx] + list(extra) + args[idx:]


@pytest.mark.parametrize(
    "extra",
    [
        ["-v", "C:\\:/host"],
        ["--volume", "/:/host"],
        ["--mount", "type=bind,src=/,dst=/host"],
        ["--volumes-from", "other"],
        ["--privileged"],
        ["--network", "host"],
        ["--network", "bridge"],
        ["--net", "host"],
        ["--pid", "host"],
        ["--ipc", "host"],
        ["--uts", "host"],
        ["--userns", "host"],
        ["--cap-add", "SYS_ADMIN"],
        ["--device", "/dev/kvm"],
        ["--security-opt", "seccomp=unconfined"],
        ["--security-opt", "apparmor=unconfined"],
        ["--env", "OPENAI_API_KEY=sk-x"],
        ["--env", "BARE"],
        ["-e", "X=1"],
        ["--env-file", "C:/Users/me/.env"],
        ["--publish", "80:80"],
        ["--gpus", "all"],
        ["--runtime", "runc"],
        ["--sysctl", "net.ipv4.ip_forward=1"],
        ["--add-host", "host.docker.internal:host-gateway"],
        ["--tmpfs", "/data:rw,exec"],
        ["--label", "evil=1"],
        ["--user", "0:0"],
        ["--name", "second"],
        ["--env", "DOCKER_HOST=npipe:////./pipe/docker_engine"],
        ["--hostname", "/var/run/docker.sock"],
        ["--env", "AGENT_FACTORY_AUTH_ISSUER=https://issuer.example.test"],
        ["--env", "OIDC_CLIENT_ID=agent-factory"],
        ["--env", "JWKS_URL=https://issuer.example.test/jwks.json"],
        ["--env", "DEPLOY_CONFIG=eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"],  # a token under an innocent name
    ],
)
def test_hardening_rejects_dangerous_options(extra):
    assert check_hardened_argv(_inject(extra), CONFIG) != []


@pytest.mark.parametrize(
    "flag, value",
    [("--read-only", None), ("--network", "none"), ("--pull", "never"), ("--cap-drop", "ALL"), ("--init", None), ("--log-driver", "none")],
)
def test_hardening_requires_protective_options(flag, value):
    args = list(GOLDEN)
    i = args.index(flag)
    del args[i : i + (1 if value is None else 2)]
    assert any(flag in p for p in check_hardened_argv(args, CONFIG))


@pytest.mark.parametrize("user", ["root", "0:0", "0:10001", "10001", "1000:0"])
def test_root_or_malformed_user_is_rejected(user):
    assert DockerConfig(image_id=IMAGE_ID, user=user).problems()
    args = [a if a != "10001:10001" else user for a in GOLDEN]
    assert check_hardened_argv(args, CONFIG) != []


def test_memory_swap_must_equal_memory():
    args = [a if a != "512m" else "1024m" for a in GOLDEN]
    args[args.index("--memory") + 1] = "512m"
    assert any("swap" in p for p in check_hardened_argv(args, CONFIG))


# ------------------------------------------------------------ endpoints


@pytest.mark.parametrize("host", [LOCAL_PIPE, "npipe:////./pipe/docker_engine", "unix:///var/run/docker.sock"])
def test_local_endpoints_are_accepted(host):
    assert endpoint_problem(host) is None


@pytest.mark.parametrize(
    "host",
    ["tcp://10.0.0.5:2376", "tcp://localhost:2375", "ssh://user@build-box", "npipe:////buildserver/pipe/docker_engine",
     "https://docker.example.com", "fd://", "", None, "npipe:////./pipe/../x"],
)  # fmt: skip
def test_remote_or_odd_endpoints_are_rejected(host):
    assert endpoint_problem(host) is not None


def test_endpoint_parsing_from_context():
    assert endpoint_from_context(json.dumps([{"Endpoints": {"docker": {"Host": LOCAL_PIPE}}}])) == LOCAL_PIPE
    assert endpoint_from_context("garbage") is None


# ---------------------------------------------------------------- probe


def test_probe_available_and_pins_local_endpoint():
    backend, fake = _backend()
    status = backend.probe()
    assert status.available and status.info.runtime["endpoint"] == LOCAL_PIPE
    hosts = {tuple(args[:2]): host for host, args, _ in fake.calls}
    assert hosts[("version", "--format")] is None and hosts[("image", "inspect")] == LOCAL_PIPE


@pytest.mark.parametrize(
    "fake_kw, config, reason",
    [
        ({"engine_up": False}, CONFIG, "not reachable"),
        ({"server_os": "windows"}, CONFIG, "Linux engine"),
        ({"context_host": "tcp://10.1.2.3:2376"}, CONFIG, "remote engines are refused"),
        ({"context_host": "ssh://me@box"}, CONFIG, "remote engines are refused"),
        ({"image_present": False}, CONFIG, "never pulled"),
        ({"image_id": "sha256:" + "cd" * 32}, CONFIG, "mismatch"),
        ({}, DockerConfig(image_id=None), "no sandbox image id configured"),
        ({}, DockerConfig(image_id="python:3.12-slim"), "no sandbox image id configured"),
    ],
)
def test_probe_unavailable_cases(fake_kw, config, reason):
    backend, fake = _backend(FakeDocker(**fake_kw), config)
    status = backend.probe()
    assert not status.available and reason in " ".join(status.reasons)
    assert all(sub in ALLOWED_SUBCOMMANDS for sub in fake.subcommands())
    with pytest.raises(SandboxUnavailable):
        backend._docker(["ps", "--all"])  # nothing pinned after a failed probe


def test_remote_docker_host_env_is_refused(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://203.0.113.9:2375")
    backend, fake = _backend()
    status = backend.probe()
    assert not status.available and "DOCKER_HOST" in status.reasons[0]
    assert fake.calls == []  # refused before contacting anything


@pytest.mark.parametrize(
    "args",
    [["pull", "python:3.12-slim"], ["build", "."], ["run", "alpine"], ["exec", "x", "sh"], ["cp", "x:/a", "."],
     ["login"], ["push", "img"], ["image", "pull", "x"], ["image", "build", "."], ["container", "run", "x"],
     ["context", "use", "remote"], ["volume", "create"], ["network", "connect", "bridge", "x"]],
)  # fmt: skip
def test_backend_refuses_non_allowlisted_subcommands(args):
    backend, fake = _ready()
    before = len(fake.calls)
    with pytest.raises(SandboxInfrastructureError, match="refusing"):
        backend._docker(args)
    assert len(fake.calls) == before


# ------------------------------------------------------------ lifecycle


def test_full_lifecycle_copy_in_and_verified_cleanup(ws):
    backend, fake = _ready()
    result = run_sandboxed(backend, _req(ws))
    assert result.status is ExecutionStatus.COMPLETED and result.exit_code == 0
    assert result.cleanup_verified and result.backend.isolation_level.name == "CONTAINER"
    lifecycle = [s for s in fake.subcommands() if s not in {("version",), ("context", "inspect"), ("image", "inspect")}]
    assert lifecycle == [("create",), ("start",), ("container", "inspect"), ("rm",), ("ps",)]
    assert fake.containers == {}
    # Copy-in: the archive went to `start` on stdin, and no host path appears in any docker argv.
    start_stdin = next(stdin for _, args, stdin in fake.calls if args[0] == "start")
    assert start_stdin and start_stdin[:4] != b"" and len(start_stdin) % 512 == 0
    all_args = " ".join(" ".join(a) for _, a, _ in fake.calls)
    assert str(ws.root) not in all_args and str(ws.project_dir) not in all_args
    for _, args, stdin in fake.calls:
        assert stdin is None or args[0] == "start"


def test_each_execution_gets_unique_name_and_labels(ws):
    backend, fake = _ready()
    run_sandboxed(backend, _req(ws, "af-dock-0001"))
    run_sandboxed(backend, _req(ws, "af-dock-0002"))
    creates = [a for _, a, _ in fake.calls if a[0] == "create"]
    names = [a[a.index("--name") + 1] for a in creates]
    exec_labels = [next(v for v in a if v.startswith(f"{LABEL_EXEC}=")) for a in creates]
    assert len(set(names)) == 2
    assert exec_labels == [f"{LABEL_EXEC}=af-dock-0001", f"{LABEL_EXEC}=af-dock-0002"]


def test_host_secrets_never_reach_container_args(ws, monkeypatch):
    monkeypatch.setenv("AF_CANARY_SECRET", "canary-5678")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak")
    backend, fake = _ready()
    run_sandboxed(backend, _req(ws))
    create = next(a for _, a, _ in fake.calls if a[0] == "create")
    joined = " ".join(create)
    assert "canary-5678" not in joined and "aws-should-not-leak" not in joined
    env_names = sorted(create[i + 1].split("=")[0] for i, t in enumerate(create) if t == "--env")
    assert env_names == sorted(set(DEFAULT_SANDBOX_ENV) | {"HOME", "PATH"})


def test_docker_cli_env_strips_docker_overrides(monkeypatch):
    from agent_factory.sandbox.docker import docker_cli_env

    monkeypatch.setenv("DOCKER_HOST", "tcp://evil:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    assert not any(k.upper().startswith("DOCKER_") for k in docker_cli_env(pinned=True))


def test_unsafe_workspace_is_rejected_before_any_container_exists(ws):
    import os

    os.link(ws.project_dir / "tests" / "test_x.py", ws.project_dir / "alias.py")
    backend, fake = _ready()
    with pytest.raises(StagingRejected):
        run_sandboxed(backend, _req(ws))
    assert ("create",) not in fake.subcommands()


def test_unprobed_backend_refuses_to_prepare(ws):
    backend, fake = _backend()
    with pytest.raises(SandboxUnavailable):
        run_sandboxed(backend, _req(ws))
    assert fake.calls == []


# ----------------------------------------------------- limits and mapping


@pytest.mark.parametrize(
    "behaviour, status, hit",
    [
        (StartBehaviour(timeout=True), ExecutionStatus.TIMED_OUT, LimitHit.TIME),
        (StartBehaviour(oom=True), ExecutionStatus.LIMIT_EXCEEDED, LimitHit.MEMORY),
        (StartBehaviour(overflow=True), ExecutionStatus.LIMIT_EXCEEDED, LimitHit.OUTPUT),
        (StartBehaviour(exit_code=152, stderr=""), ExecutionStatus.LIMIT_EXCEEDED, LimitHit.CPU),
        (StartBehaviour(exit_code=1, stderr="OSError: [Errno 28] No space left on device"), ExecutionStatus.LIMIT_EXCEEDED, LimitHit.DISK),
        (StartBehaviour(exit_code=1, stderr="BlockingIOError: [Errno 11] Resource temporarily unavailable"), ExecutionStatus.LIMIT_EXCEEDED, LimitHit.PIDS),
    ],
)
def test_limit_outcomes_are_mapped_and_cleaned_up(ws, behaviour, status, hit):
    backend, fake = _ready(FakeDocker(start=behaviour))
    result = run_sandboxed(backend, _req(ws))
    assert result.status is status and hit in result.limits_hit
    assert result.cleanup_verified and fake.containers == {}
    if behaviour.timeout or behaviour.overflow:
        assert ("kill",) in fake.subcommands()


@pytest.mark.parametrize(
    "state, message",
    [
        ({"Error": "OCI runtime create failed", "StartedAt": "2026-01-01T00:00:00Z"}, "container error"),
        ({"StartedAt": "0001-01-01T00:00:00Z", "Error": ""}, "never started"),
        ({"StartedAt": "2026-01-01T00:00:00Z", "Running": True}, "still running"),
    ],
)
def test_infrastructure_states_raise(state, message):
    with pytest.raises(SandboxInfrastructureError, match=message):
        map_container_state(state, timed_out=False, output_exceeded=False, stderr="")


def test_container_error_is_infrastructure_and_still_cleaned(ws):
    backend, fake = _ready(FakeDocker(start=StartBehaviour(error="OCI runtime create failed")))
    with pytest.raises(SandboxInfrastructureError):
        run_sandboxed(backend, _req(ws))
    assert fake.containers == {}


# ---------------------------------------------------------------- cleanup


def test_rm_failure_is_unverified_cleanup(ws):
    backend, fake = _ready(FakeDocker(sticky=True))
    with pytest.raises(SandboxCleanupError) as exc:
        run_sandboxed(backend, _req(ws))
    assert exc.value.report.leftovers


def test_verification_failure_is_unverified_cleanup(ws):
    fake = FakeDocker()
    backend, _ = _ready(fake)
    fake.fail.add("ps")
    with pytest.raises(SandboxCleanupError, match="verify"):
        run_sandboxed(backend, _req(ws))


def test_failed_create_is_cleaned_up_then_blocks(ws):
    backend, fake = _ready(FakeDocker(fail={"create"}, partial_create=True))
    with pytest.raises(SandboxInfrastructureError, match="create failed"):
        run_sandboxed(backend, _req(ws))
    assert fake.containers == {}
    assert ("start",) not in fake.subcommands()


def test_failed_create_with_unremovable_leftover_is_cleanup_error(ws):
    backend, fake = _ready(FakeDocker(fail={"create"}, partial_create=True, sticky=True))
    with pytest.raises(SandboxCleanupError):
        run_sandboxed(backend, _req(ws))


def test_cleanup_failure_makes_manager_block_further_execution(ws):
    fake = FakeDocker()
    backend, _ = _backend(fake)
    manager = SandboxManager([backend])
    manager.run(_req(ws, "af-dock-0001"))  # self-test + one good execution
    fake.sticky = True
    with pytest.raises(SandboxCleanupError):
        manager.run(_req(ws, "af-dock-0002"))
    fake.sticky = False
    creates_before = sum(1 for s in fake.subcommands() if s == ("create",))
    with pytest.raises(SandboxUnavailable, match="unhealthy"):
        manager.run(_req(ws, "af-dock-0003"))
    assert sum(1 for s in fake.subcommands() if s == ("create",)) == creates_before


def test_janitor_removes_expired_and_own_orphans_only():
    backend, fake = _ready()
    fake.add_foreign_container(exec_id="af-expired-01", owner="other-proc", deadline=NOW - 1)
    fake.add_foreign_container(exec_id="af-live-0001", owner="other-proc", deadline=NOW + 600)
    fake.add_foreign_container(exec_id="af-orphan-01", owner=PROCESS_TOKEN, deadline=NOW + 600)
    fake.add_foreign_container(exec_id="af-badlabel1", owner="other-proc", deadline="not-a-number")
    removed = backend.janitor()
    assert len(removed) == 3
    remaining = {r["labels"][LABEL_EXEC] for r in fake.containers.values()}
    assert remaining == {"af-live-0001"}


def test_janitor_failure_raises_cleanup_error():
    backend, fake = _ready()
    fake.add_foreign_container(exec_id="af-expired-01", owner="x", deadline=NOW - 1)
    fake.sticky = True
    with pytest.raises(SandboxCleanupError):
        backend.janitor()


# -------------------------------------------------------------- self-test


def test_self_test_passes_and_cleans_up():
    backend, fake = _backend()
    assert backend.self_test().available
    assert fake.containers == {}
    create = next(a for _, a, _ in fake.calls if a[0] == "create")
    assert check_hardened_argv(create, CONFIG) == []


@pytest.mark.parametrize(
    "override, message",
    [
        ({"uid": 0, "gid": 0}, "uid"),
        ({"net": ["eth0", "lo"]}, "network interfaces"),
        ({"net": None}, "network interfaces"),
        ({"root_writable": True}, "writable"),
        ({"cap_eff": "00000000a80425fb"}, "capabilities"),
        ({"env": ["HOME", "PATH", "OPENAI_API_KEY"]}, "unexpected environment"),
    ],
)
def test_self_test_detects_broken_isolation(override, message):
    report = {**FakeDocker().self_test_report, **override}
    backend, fake = _backend(FakeDocker(self_test_report=report))
    status = backend.self_test()
    assert not status.available and message in " ".join(status.reasons)
    assert fake.containers == {}
    with pytest.raises(SandboxUnavailable):
        backend._docker(["ps"])  # endpoint unpinned: nothing more can run


def test_check_self_test_requires_a_report():
    assert check_self_test("hello", CONFIG) == ["self-test produced no report"]


# ------------------------------------------------- executor integration


class SimulatedRealModel(FakeProvider):
    name = "simulated-real-model"
    produces_trusted_code = False


def test_untrusted_run_completes_through_docker_backend_logic(tmp_path):
    h = Harness(tmp_path / "ws")
    backend, fake = _backend()
    provider = SimulatedRealModel({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    state = h.executor(provider, sandbox=SandboxManager([backend])).start(h.template.task, run_id="run-1")
    assert state.status is RunStatus.COMPLETED
    assert state.test_report["sandbox"]["backend"]["name"] == "docker"
    assert fake.containers == {}
    create = [a for _, a, _ in fake.calls if a[0] == "create"]
    assert len(create) == 2  # self-test + the tester's execution
    assert all(check_hardened_argv(a, CONFIG) == [] for a in create)


def test_untrusted_run_blocks_when_docker_is_down(tmp_path):
    h = Harness(tmp_path / "ws")
    backend, fake = _backend(FakeDocker(engine_up=False))
    provider = SimulatedRealModel({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    state = h.executor(provider, sandbox=SandboxManager([backend])).start(h.template.task, run_id="run-1")
    assert state.status is RunStatus.BLOCKED and "not reachable" in state.blocked["message"]
    assert ("create",) not in fake.subcommands()


def test_oversized_input_is_rejected_before_any_container(ws):
    limits = ResourceLimits(disk_mb=1)  # staged input may use at most 512 KiB of /work
    (ws.project_dir / "big.bin").write_bytes(b"x" * (limits.max_input_bytes + 1))
    backend, fake = _ready()
    with pytest.raises(StagingRejected, match="headroom"):
        run_sandboxed(backend, _req(ws, limits=limits))
    assert ("create",) not in fake.subcommands() and fake.containers == {}



# ------------------------------------------------------------- CPU limits (v0.2 pre-release)


def test_cpu_ulimit_hard_exceeds_soft_so_sigxcpu_comes_first():
    args = _argv(limits=ResourceLimits(cpu_time_s=3))
    assert "cpu=3:5" in args  # soft 3 s (SIGXCPU), hard 5 s (SIGKILL)
    assert check_hardened_argv(args, CONFIG) == []


@pytest.mark.parametrize("bad", ["cpu=5:3", "cpu=x:y", "cpu=5", "fsize=-1:-1"])
def test_malformed_ulimits_are_rejected(bad):
    args = list(GOLDEN)
    args[args.index("cpu=125:127")] = bad if bad.startswith("cpu") else "cpu=125:127"
    if not bad.startswith("cpu"):
        args[args.index("fsize=268435456:268435456")] = bad
    assert any("--ulimit" in p for p in check_hardened_argv(args, CONFIG))


@pytest.mark.parametrize(
    "state, kw, status, hits",
    [
        ({"ExitCode": 152}, {}, ExecutionStatus.LIMIT_EXCEEDED, (LimitHit.CPU,)),
        ({"ExitCode": 137}, {}, ExecutionStatus.LIMIT_EXCEEDED, (LimitHit.CPU,)),  # hard limit, SIGXCPU ignored
        ({"ExitCode": 137}, {"timed_out": True}, ExecutionStatus.TIMED_OUT, (LimitHit.TIME,)),
        ({"ExitCode": 137, "OOMKilled": True}, {}, ExecutionStatus.LIMIT_EXCEEDED, (LimitHit.MEMORY,)),
        ({"ExitCode": 137}, {"output_exceeded": True}, ExecutionStatus.LIMIT_EXCEEDED, (LimitHit.OUTPUT,)),
        ({"ExitCode": 1}, {}, ExecutionStatus.COMPLETED, ()),
    ],
)
def test_cpu_and_sigkill_mapping(state, kw, status, hits):
    full = {"StartedAt": "2026-10-01T00:00:00Z", "Error": "", **state}
    args = {"timed_out": False, "output_exceeded": False, "stderr": "", **kw}
    got_status, got_hits, _ = map_container_state(full, **args)
    assert (got_status, got_hits) == (status, hits)

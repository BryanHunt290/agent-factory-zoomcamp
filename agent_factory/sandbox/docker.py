"""DockerBackend: container isolation for untrusted code (Docker Desktop / Linux engine).

Security properties enforced here (see docs/design/v0.2-sandbox.md):

* copy-in only: the workspace is staged into a tar and streamed on stdin;
  nothing from the host is mounted;
* the ``docker create`` argv is built by one pure function and re-checked
  by an allowlist-based validator before every create;
* only a fixed set of Docker subcommands can ever be issued; ``pull``,
  ``build``, ``run``, ``exec``, ``cp``, ``login`` and ``push`` are refused,
  and every create passes ``--pull never``;
* the engine endpoint must be a local named pipe or unix socket; once
  validated it is pinned with ``--host`` and ``DOCKER_*`` variables are
  stripped from the CLI environment;
* deadlines use the monotonic clock (only the janitor's cross-process
  ``deadline`` label uses wall-clock time);
* cleanup is ``rm --force`` followed by verification that no container with
  the execution label remains.

All Docker CLI invocations go through an injectable ``runner`` so the whole
backend can be exercised without Docker (Tier-0 tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..util import timestamp, utc_now
from .base import SandboxBackend
from .process import ProcessOutcome, decode, run_capped
from .staging import _build_tar, inspect_archive, stage_workspace
from .types import (
    BackendInfo,
    BackendStatus,
    CleanupReport,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    IsolationLevel,
    LimitHit,
    MiB,
    ResourceLimits,
    SandboxCleanupError,
    SandboxHandle,
    SandboxInfrastructureError,
    SandboxUnavailable,
)
from .validation import (
    CREDENTIAL_URL_RE,
    DEFAULT_SANDBOX_ENV,
    ENV_NAME_RE,
    JWT_VALUE_RE,
    SECRET_NAME_RE,
    validate_request,
)

SANDBOX_UID = 10001
DEFAULT_IMAGE_REF = "agent-factory/sandbox-python:3.12"
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")

LABEL_EXEC = "agent-factory.exec"
LABEL_RUN = "agent-factory.run"
LABEL_OWNER = "agent-factory.owner"
LABEL_DEADLINE = "agent-factory.deadline"

#: Identifies containers created by this orchestrator process (for the janitor).
PROCESS_TOKEN = f"{os.getpid()}-{secrets.token_hex(4)}"

#: Seconds between the RLIMIT_CPU soft limit (SIGXCPU, exit 152) and the hard
#: limit (SIGKILL). With soft == hard the kernel sends SIGKILL first, so a CPU
#: limit hit would be indistinguishable from any other SIGKILL.
CPU_HARD_GRACE_S = 2

#: Fixed container environment added by the backend (never from the host).
FIXED_CONTAINER_ENV: Mapping[str, str] = {"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin"}

#: The only Docker subcommands this backend may issue.
ALLOWED_SUBCOMMANDS = frozenset(
    {
        ("version",),
        ("context", "inspect"),
        ("image", "inspect"),
        ("create",),
        ("start",),
        ("kill",),
        ("container", "inspect"),
        ("rm",),
        ("ps",),
    }
)

_LOCAL_NPIPE = re.compile(r"^npipe:////\./pipe/[A-Za-z0-9_.-]+$")
_LOCAL_UNIX = re.compile(r"^unix:///[A-Za-z0-9_./-]+$")
_ENGINE_REFERENCES = ("docker.sock", "docker_engine", "dockerdesktop", "/var/run/docker", "npipe:")


# ------------------------------------------------------------------ config


@dataclass(frozen=True)
class DockerConfig:
    #: Exact local image id (``sha256:...``). Required; images are never pulled.
    image_id: str | None = None
    image_ref: str = DEFAULT_IMAGE_REF
    user: str = f"{SANDBOX_UID}:{SANDBOX_UID}"
    tmp_mb: int = 64
    nofile: int = 256
    cli_timeout_s: float = 60.0
    #: Added to the janitor deadline label beyond the execution timeout.
    deadline_slack_s: float = 300.0
    #: Set when the configuration was deliberately made unusable (fail closed).
    unavailable_reason: str | None = None

    def problems(self) -> list[str]:
        out = []
        if self.unavailable_reason:
            return [self.unavailable_reason]
        if not self.image_id or not IMAGE_ID_RE.match(self.image_id):
            out.append(
                "no sandbox image id configured (expected sha256:<64 hex>); images are built only "
                "by an explicit human-run command and are never pulled automatically"
            )
        if not _is_non_root_user(self.user):
            out.append(f"container user {self.user!r} must be a numeric non-root uid:gid")
        return out


def _is_non_root_user(user: str) -> bool:
    m = re.fullmatch(r"(\d+):(\d+)", user or "")
    return bool(m) and int(m.group(1)) != 0 and int(m.group(2)) != 0


# --------------------------------------------------------------- endpoints


def endpoint_problem(host: str | None) -> str | None:
    """None if ``host`` is a local Docker endpoint; otherwise why it is rejected."""
    if not host:
        return "no Docker endpoint"
    if _LOCAL_NPIPE.match(host) or _LOCAL_UNIX.match(host):
        return None
    return f"Docker endpoint {host!r} is not a local named pipe or unix socket (remote engines are refused)"


def endpoint_from_context(inspect_output: str) -> str | None:
    try:
        data = json.loads(inspect_output)
        entry = data[0] if isinstance(data, list) else data
        return entry["Endpoints"]["docker"]["Host"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None


# ------------------------------------------------------------ argv builder


def build_create_argv(
    *,
    name: str,
    labels: Mapping[str, str],
    limits: ResourceLimits,
    env: Mapping[str, str],
    command: Sequence[str],
    working_dir: str,
    config: DockerConfig,
    archive_bytes: int,
    archive_sha256: str,
) -> list[str]:
    """The complete ``docker create`` argument list (without ``docker --host X``).

    The container runs ``af-entry`` (copy-in protocol v1, see
    ``sandbox_image/af-entry``), which reads exactly ``archive_bytes`` from
    stdin, verifies ``archive_sha256``, extracts into /work and execs the argv.
    """
    uid, gid = config.user.split(":")
    tmpfs_opts = f"rw,noexec,nosuid,nodev,uid={uid},gid={gid},mode=0700"
    args = ["create", "--pull", "never", "--name", name]
    for key in sorted(labels):
        args += ["--label", f"{key}={labels[key]}"]
    args += [
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--user", config.user,
        "--init",
        "--ipc", "private",
        "--log-driver", "none",
        "--hostname", "sandbox",
        "--pids-limit", str(limits.pids),
        "--cpus", f"{limits.cpus:g}",
        "--memory", f"{limits.memory_mb}m",
        "--memory-swap", f"{limits.memory_mb}m",
        "--ulimit", f"cpu={limits.cpu_seconds}:{limits.cpu_seconds + CPU_HARD_GRACE_S}",
        "--ulimit", f"fsize={limits.disk_bytes}:{limits.disk_bytes}",
        "--ulimit", f"nofile={config.nofile}:{config.nofile}",
        "--tmpfs", f"/work:{tmpfs_opts},size={limits.disk_mb}m",
        "--tmpfs", f"/tmp:{tmpfs_opts},size={config.tmp_mb}m",
    ]  # fmt: skip
    merged = {**env, **FIXED_CONTAINER_ENV}
    for key in sorted(merged):
        args += ["--env", f"{key}={merged[key]}"]
    args += [
        "--interactive",
        config.image_id or "",
        "af-entry",
        "--workdir", working_dir,
        "--archive-bytes", str(archive_bytes),
        "--archive-sha256", archive_sha256,
        "--",
        *command,
    ]  # fmt: skip
    return args


# Options the validator understands. Anything else is rejected (allowlist).
_VALUE_OPTIONS = frozenset(
    {
        "--pull", "--name", "--label", "--network", "--cap-drop", "--security-opt", "--user", "--ipc",
        "--log-driver", "--hostname", "--pids-limit", "--cpus", "--memory", "--memory-swap", "--ulimit",
        "--tmpfs", "--env",
    }
)  # fmt: skip
_FLAG_OPTIONS = frozenset({"--read-only", "--init", "--interactive"})
_REQUIRED_VALUES = {
    "--pull": {"never"},
    "--network": {"none"},
    "--cap-drop": {"ALL"},
    "--security-opt": {"no-new-privileges:true"},
    "--ipc": {"private"},
    "--log-driver": {"none"},
}
_TMPFS_RE = re.compile(r"^(/work|/tmp):rw,noexec,nosuid,nodev,uid=\d+,gid=\d+,mode=0700,size=\d+m$")


def check_hardened_argv(args: Sequence[str], config: DockerConfig) -> list[str]:
    """Problems with a ``docker create`` argv. Empty list means it is acceptable."""
    problems: list[str] = []
    if not args or args[0] != "create":
        return ["argv must start with 'create'"]
    image = config.image_id or ""
    if not IMAGE_ID_RE.match(image) or image not in args:
        return ["argv must reference the configured image id"]
    options = list(args[1 : list(args).index(image)])

    seen: dict[str, list[str]] = {}
    i = 0
    while i < len(options):
        tok = options[i]
        if tok in _FLAG_OPTIONS:
            seen.setdefault(tok, []).append("")
            i += 1
        elif tok in _VALUE_OPTIONS and i + 1 < len(options):
            seen.setdefault(tok, []).append(options[i + 1])
            i += 2
        else:
            problems.append(f"option not allowed: {tok!r}")
            i += 1

    for flag in _FLAG_OPTIONS:
        if flag not in seen:
            problems.append(f"required option missing: {flag}")
    for flag, allowed in _REQUIRED_VALUES.items():
        values = seen.get(flag, [])
        if not values:
            problems.append(f"required option missing: {flag}")
        for value in values:
            if value not in allowed:
                problems.append(f"{flag} {value!r} not permitted")
    for flag in ("--name", "--user", "--pids-limit", "--cpus", "--memory", "--memory-swap", "--hostname"):
        if len(seen.get(flag, [])) != 1:
            problems.append(f"{flag} must appear exactly once")
    for user in seen.get("--user", []):
        if not _is_non_root_user(user):
            problems.append(f"--user {user!r} must be a numeric non-root uid:gid")
    if seen.get("--memory") != seen.get("--memory-swap"):
        problems.append("--memory-swap must equal --memory (no swap)")
    ulimits = {v.split("=", 1)[0] for v in seen.get("--ulimit", [])}
    for value in seen.get("--ulimit", []):
        soft, _, hard = value.partition("=")[2].partition(":")
        if not (soft.isdigit() and hard.isdigit()) or int(hard) < int(soft):
            problems.append(f"--ulimit {value!r} must be name=soft:hard with hard >= soft")
    for required in ("cpu", "fsize", "nofile"):
        if required not in ulimits:
            problems.append(f"required --ulimit {required} missing")
    tmpfs = seen.get("--tmpfs", [])
    if sorted(t.split(":", 1)[0] for t in tmpfs) != ["/tmp", "/work"]:
        problems.append("exactly two tmpfs mounts (/work, /tmp) are required")
    for t in tmpfs:
        if not _TMPFS_RE.match(t):
            problems.append(f"tmpfs {t!r} must be rw,noexec,nosuid,nodev with uid/gid/mode/size")
    for value in seen.get("--env", []):
        key = value.split("=", 1)[0]
        if "=" not in value or not ENV_NAME_RE.match(key):
            problems.append(f"--env {value!r} must be NAME=value")
        elif SECRET_NAME_RE.search(key):
            problems.append(f"--env {key} looks like a credential")
        elif CREDENTIAL_URL_RE.search(value.split("=", 1)[1]):
            problems.append(f"--env {key} holds a URL with embedded credentials")
        elif JWT_VALUE_RE.search(value.split("=", 1)[1]):
            problems.append(f"--env {key} holds what looks like an access token")
    for value in seen.get("--label", []):
        if not value.startswith("agent-factory."):
            problems.append(f"unexpected label {value!r}")
    for tok in options:
        if any(ref in tok.lower() for ref in _ENGINE_REFERENCES):
            problems.append(f"Docker engine reference not allowed: {tok!r}")
    return problems


# ------------------------------------------------------------- inspect map

#: Exit codes reserved by af-entry (staging, workdir, exec, usage). Untrusted
#: code could imitate them, but that only turns its own failure into BLOCKED.
AF_ENTRY_EXIT_CODES = frozenset({120, 121, 122, 123})
_PIDS_MARKERS = ("Resource temporarily unavailable", "can't start new thread", "BlockingIOError: [Errno 11]")
_DISK_MARKERS = ("No space left on device", "File too large")
_NEVER_STARTED = "0001-01-01T00:00:00Z"


def map_container_state(
    state: Mapping[str, Any], *, timed_out: bool, output_exceeded: bool, stderr: str
) -> tuple[ExecutionStatus, tuple[LimitHit, ...], int | None]:
    """Map ``docker container inspect`` State onto status and limits hit.

    Infrastructure conditions raise SandboxInfrastructureError. Limit hits
    inferred from stderr can be faked by the code itself, but that only
    changes how its own failure is described.
    """
    if state.get("Error"):
        raise SandboxInfrastructureError(f"container error: {state['Error']}")
    if not state.get("StartedAt") or state.get("StartedAt", "").startswith(_NEVER_STARTED[:10]):
        raise SandboxInfrastructureError("container never started")
    if state.get("Running"):
        raise SandboxInfrastructureError("container still running after execution ended")
    exit_code = state.get("ExitCode")
    if exit_code in AF_ENTRY_EXIT_CODES and stderr.startswith("af-entry: "):
        raise SandboxInfrastructureError(f"copy-in failed (exit {exit_code}): {stderr.strip()[:300]}")
    hits: list[LimitHit] = []
    if timed_out:
        hits.append(LimitHit.TIME)
    if state.get("OOMKilled"):
        hits.append(LimitHit.MEMORY)
    if output_exceeded:
        hits.append(LimitHit.OUTPUT)
    if exit_code == 128 + 24:  # SIGXCPU: the RLIMIT_CPU soft limit
        hits.append(LimitHit.CPU)
    elif exit_code == 128 + 9 and not (timed_out or output_exceeded or state.get("OOMKilled")):
        # A SIGKILL the orchestrator did not send and the OOM killer did not
        # cause: the RLIMIT_CPU hard limit (the process ignored SIGXCPU). A
        # payload that SIGKILLs itself is labelled the same way; that only
        # changes how its own failure is described.
        hits.append(LimitHit.CPU)
    if exit_code == 128 + 25 or any(m in stderr for m in _DISK_MARKERS):  # SIGXFSZ / ENOSPC
        hits.append(LimitHit.DISK)
    if any(m in stderr for m in _PIDS_MARKERS):
        hits.append(LimitHit.PIDS)
    if timed_out:
        status = ExecutionStatus.TIMED_OUT
    elif hits:
        status = ExecutionStatus.LIMIT_EXCEEDED
    else:
        status = ExecutionStatus.COMPLETED
    return status, tuple(dict.fromkeys(hits)), exit_code


# ------------------------------------------------------------- self-test

SELF_TEST_MARKER = "AF-SELFTEST "
SELF_TEST_SCRIPT = (
    "import json,os\n"
    "r={'uid':os.getuid(),'gid':os.getgid()}\n"
    "try:r['net']=sorted(os.listdir('/sys/class/net'))\n"
    "except OSError:r['net']=None\n"
    "try:\n open('/af-selftest','w');r['root_writable']=True\n"
    "except OSError:r['root_writable']=False\n"
    "r['cap_eff']=[l.split()[1] for l in open('/proc/self/status') if l.startswith('CapEff:')][0]\n"
    "r['env']=sorted(os.environ)\n"
    "print('" + SELF_TEST_MARKER + "'+json.dumps(r))\n"
)
_SELF_TEST_ENV_OK = frozenset(DEFAULT_SANDBOX_ENV) | frozenset(FIXED_CONTAINER_ENV) | {"HOSTNAME"}


def check_self_test(stdout: str, config: DockerConfig) -> list[str]:
    line = next((ln for ln in stdout.splitlines() if ln.startswith(SELF_TEST_MARKER)), None)
    if line is None:
        return ["self-test produced no report"]
    try:
        r = json.loads(line[len(SELF_TEST_MARKER) :])
    except ValueError:
        return ["self-test report is not JSON"]
    problems = []
    uid, gid = (int(x) for x in config.user.split(":"))
    if r.get("uid") != uid or r.get("gid") != gid or r.get("uid") == 0:
        problems.append(f"runs as uid/gid {r.get('uid')}/{r.get('gid')}, expected {uid}/{gid}")
    if r.get("net") != ["lo"]:
        problems.append(f"network interfaces {r.get('net')!r}, expected only ['lo']")
    if r.get("root_writable") is not False:
        problems.append("root filesystem is writable")
    try:
        if int(str(r.get("cap_eff")), 16) != 0:
            problems.append(f"effective capabilities {r.get('cap_eff')} are not empty")
    except ValueError:
        problems.append("could not read effective capabilities")
    extra = sorted(set(r.get("env") or []) - _SELF_TEST_ENV_OK)
    if extra:
        problems.append(f"unexpected environment variables in container: {extra}")
    return problems


# ----------------------------------------------------------------- runner


class DockerRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        host: str | None,
        stdin: bytes | None,
        timeout_s: float,
        output_limit: int,
        on_kill: Callable[[], None] | None,
    ) -> ProcessOutcome: ...


def docker_cli_env(pinned: bool) -> dict[str, str]:
    """Environment for the (trusted) docker CLI. Pinned calls drop all DOCKER_* overrides."""
    env = dict(os.environ)
    if pinned:
        env = {k: v for k, v in env.items() if not k.upper().startswith("DOCKER_")}
    return env


class SubprocessDockerRunner:
    """Runs the real docker CLI. Not used by Tier-0 tests."""

    def __init__(self, executable: str = "docker") -> None:
        self.executable = executable

    def __call__(self, args, *, host, stdin, timeout_s, output_limit, on_kill):
        argv = [self.executable, *(["--host", host] if host else []), *args]
        return run_capped(
            argv,
            timeout_s=timeout_s,
            output_limit=output_limit,
            env=docker_cli_env(pinned=host is not None),
            stdin=stdin,
            on_kill=on_kill,
        )


# ---------------------------------------------------------------- backend


class DockerBackend(SandboxBackend):
    name = "docker"
    isolation_level = IsolationLevel.CONTAINER
    is_security_sandbox = True

    def __init__(
        self,
        config: DockerConfig,
        runner: DockerRunner | None = None,
        *,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._runner = runner or SubprocessDockerRunner()
        self._wall_clock = wall_clock
        self._endpoint: str | None = None
        self._server_version = "unknown"
        self._active: set[str] = set()

    # ----------------------------------------------------------- CLI access

    def _docker(
        self,
        args: Sequence[str],
        *,
        pinned: bool = True,
        stdin: bytes | None = None,
        timeout_s: float | None = None,
        output_limit: int = MiB,
        on_kill: Callable[[], None] | None = None,
    ) -> ProcessOutcome:
        args = list(args)
        key = tuple(args[:2]) if args and args[0] in ("context", "image", "container") else tuple(args[:1])
        if key not in ALLOWED_SUBCOMMANDS:
            raise SandboxInfrastructureError(f"refusing to issue docker subcommand {' '.join(key)!r}")
        host = None
        if pinned:
            if self._endpoint is None:
                raise SandboxUnavailable("docker backend has not been probed")
            host = self._endpoint
        return self._runner(
            args,
            host=host,
            stdin=stdin,
            timeout_s=timeout_s or self.config.cli_timeout_s,
            output_limit=output_limit,
            on_kill=on_kill,
        )

    def info(self) -> BackendInfo:
        return BackendInfo(
            name=self.name,
            version=self._server_version,
            isolation_level=self.isolation_level,
            enforced_limits=tuple(LimitHit),
            runtime={
                "endpoint": self._endpoint or "",
                "image_id": self.config.image_id or "",
                "user": self.config.user,
            },
        )

    # --------------------------------------------------------- availability

    def probe(self) -> BackendStatus:
        self._endpoint = None
        problems = self.config.problems()
        if problems:
            return BackendStatus(False, tuple(problems))
        env_host = os.environ.get("DOCKER_HOST")
        if env_host and endpoint_problem(env_host):
            return BackendStatus(False, (f"DOCKER_HOST: {endpoint_problem(env_host)}",))

        version = self._docker(["version", "--format", "{{json .}}"], pinned=False)
        try:
            data = json.loads(decode(version.stdout)) if version.exit_code == 0 else {}
        except ValueError:
            data = {}
        server = data.get("Server") if isinstance(data, dict) else None
        if version.error or not server:
            detail = version.error or decode(version.stderr).strip()[:200]
            return BackendStatus(False, (f"docker engine not reachable: {detail}",))
        if str(server.get("Os", "")).lower() != "linux":
            return BackendStatus(False, (f"docker engine OS is {server.get('Os')!r}; a Linux engine is required",))

        endpoint = env_host
        if not endpoint:
            ctx = self._docker(["context", "inspect"], pinned=False)
            endpoint = endpoint_from_context(decode(ctx.stdout)) if ctx.exit_code == 0 else None
        problem = endpoint_problem(endpoint)
        if problem:
            return BackendStatus(False, (problem,))

        self._endpoint = endpoint
        image = self._docker(["image", "inspect", "--format", "{{json .}}", self.config.image_id])
        try:
            img = json.loads(decode(image.stdout)) if image.exit_code == 0 else None
        except ValueError:
            img = None
        reason = None
        if not isinstance(img, dict):
            reason = (
                f"sandbox image {self.config.image_id} is not present locally; images are never pulled "
                "automatically and must be built by an explicit human-run command"
            )
        elif img.get("Id") != self.config.image_id:
            reason = f"image id mismatch: found {img.get('Id')!r}"
        elif str(img.get("Os", "")).lower() != "linux":
            reason = f"image OS {img.get('Os')!r} is not linux"
        if reason:
            self._endpoint = None
            return BackendStatus(False, (reason,))
        self._server_version = str(server.get("Version", "unknown"))
        return BackendStatus(True, (), self.info())

    def self_test(self) -> BackendStatus:
        status = self.probe()
        if not status.available:
            return status
        limits = ResourceLimits(timeout_s=30, memory_mb=128, disk_mb=8, pids=32, output_bytes=65536)
        handle = self._prepare_staged(
            execution_id=f"af-selftest-{secrets.token_hex(6)}",
            run_id="selftest",
            archive=_build_tar([]),
            command=("python", "-c", SELF_TEST_SCRIPT),
            working_dir=".",
            limits=limits,
            env=dict(DEFAULT_SANDBOX_ENV),
            request=None,
        )
        try:
            self.execute(handle)
            result = self.collect_result(handle)
        except SandboxInfrastructureError as exc:
            result = None
            failure = str(exc)
        finally:
            report = self.destroy(handle)
        if not report.verified:
            raise SandboxCleanupError(f"docker self-test cleanup not verified: {report.detail}", report)
        if result is None:
            return BackendStatus(False, (f"isolation self-test could not run: {failure}",))
        if result.status is not ExecutionStatus.COMPLETED or result.exit_code != 0:
            return BackendStatus(False, (f"isolation self-test failed to run ({result.status}, exit {result.exit_code})",))
        problems = check_self_test(result.stdout, self.config)
        if problems:
            self._endpoint = None
            return BackendStatus(False, tuple(f"isolation self-test: {p}" for p in problems))
        return status

    # ------------------------------------------------------------ lifecycle

    def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        validate_request(request)
        if self._endpoint is None:
            raise SandboxUnavailable("docker backend has not passed probe/self-test")
        staged = stage_workspace(request.workspace, max_bytes=request.limits.max_input_bytes)
        return self._prepare_staged(
            execution_id=request.execution_id,
            run_id=request.run_id,
            archive=staged.data,
            command=request.command,
            working_dir=request.working_dir,
            limits=request.limits,
            env=dict(request.env),
            request=request,
            staged_files=len(staged.files),
        )

    def _prepare_staged(
        self,
        *,
        execution_id: str,
        run_id: str,
        archive: bytes,
        command: Sequence[str],
        working_dir: str,
        limits: ResourceLimits,
        env: Mapping[str, str],
        request: ExecutionRequest | None,
        staged_files: int = 0,
    ) -> SandboxHandle:
        archive_problems = inspect_archive(archive)
        if archive_problems:
            raise SandboxInfrastructureError(f"staged archive failed inspection: {archive_problems[:5]}")
        name = execution_id if execution_id.startswith("af-") else f"af-{execution_id}"
        deadline = int(self._wall_clock() + limits.timeout_s + self.config.deadline_slack_s)
        labels = {
            LABEL_EXEC: execution_id,
            LABEL_RUN: run_id or "none",
            LABEL_OWNER: PROCESS_TOKEN,
            LABEL_DEADLINE: str(deadline),
        }
        args = build_create_argv(
            name=name,
            labels=labels,
            limits=limits,
            env=env,
            command=command,
            working_dir=working_dir,
            config=self.config,
            archive_bytes=len(archive),
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
        problems = check_hardened_argv(args, self.config)
        if problems:
            raise SandboxInfrastructureError(f"refusing to create container; hardening check failed: {problems}")

        handle = SandboxHandle(
            execution_id,
            self.name,
            request,
            {"name": name, "archive": archive, "limits": limits, "staged_files": staged_files, "create_args": args},
        )
        self._active.add(execution_id)
        out = self._docker(args)
        container_id = decode(out.stdout).strip()
        if out.exit_code == 0 and CONTAINER_ID_RE.match(container_id):
            handle.data["container_id"] = container_id
            return handle
        report = self.destroy(handle)
        if not report.verified:
            raise SandboxCleanupError(f"docker create failed and cleanup is unverified: {report.detail}", report)
        raise SandboxInfrastructureError(f"docker create failed: {out.error or decode(out.stderr).strip()[:300]}")

    def _kill(self, handle: SandboxHandle) -> None:
        out = self._docker(["kill", "--signal", "KILL", handle.data["name"]])
        handle.data["kill_ok"] = out.exit_code == 0

    def execute(self, handle: SandboxHandle) -> None:
        limits: ResourceLimits = handle.data["limits"]
        handle.data["started_at"] = timestamp(utc_now)
        t0 = time.monotonic()
        outcome = self._docker(
            ["start", "--attach", "--interactive", handle.data["name"]],
            stdin=handle.data["archive"],
            timeout_s=limits.timeout_s,
            output_limit=limits.output_bytes,
            on_kill=lambda: self._kill(handle),
        )
        handle.data["duration_s"] = time.monotonic() - t0
        handle.data["finished_at"] = timestamp(utc_now)
        handle.data["outcome"] = outcome
        if outcome.error:
            raise SandboxInfrastructureError(f"docker CLI could not be started: {outcome.error}")

    def collect_result(self, handle: SandboxHandle) -> ExecutionResult:
        outcome: ProcessOutcome | None = handle.data.get("outcome")
        if outcome is None:
            raise SandboxInfrastructureError("execute() did not run")
        out = self._docker(["container", "inspect", "--format", "{{json .State}}", handle.data["name"]])
        try:
            state = json.loads(decode(out.stdout)) if out.exit_code == 0 else None
        except ValueError:
            state = None
        if not isinstance(state, dict):
            raise SandboxInfrastructureError(f"could not inspect container: {decode(out.stderr).strip()[:300]}")
        stderr = decode(outcome.stderr)
        status, hits, exit_code = map_container_state(
            state, timed_out=outcome.timed_out, output_exceeded=outcome.output_exceeded, stderr=stderr
        )
        return ExecutionResult(
            execution_id=handle.execution_id,
            status=status,
            exit_code=exit_code,
            stdout=decode(outcome.stdout),
            stderr=stderr,
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
            timed_out=outcome.timed_out,
            limits_hit=hits,
            duration_s=handle.data.get("duration_s", outcome.duration_s),
            started_at=handle.data.get("started_at", ""),
            finished_at=handle.data.get("finished_at", ""),
            backend=self.info(),
        )

    def _containers_with_label(self, label_filter: str) -> tuple[bool, list[str]]:
        out = self._docker(["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={label_filter}"])
        if out.exit_code != 0 or out.error:
            return False, []
        return True, decode(out.stdout).split()

    def destroy(self, handle: SandboxHandle) -> CleanupReport:
        name = handle.data["name"]
        rm = self._docker(["rm", "--force", "--volumes", name])
        ok, leftovers = self._containers_with_label(f"{LABEL_EXEC}={handle.execution_id}")
        handle.destroyed = True
        if not ok:
            return CleanupReport(False, "could not list containers to verify removal")
        if leftovers:
            detail = decode(rm.stderr).strip()[:200]
            return CleanupReport(False, f"container still present after rm --force: {detail}", tuple(leftovers))
        self._active.discard(handle.execution_id)
        return CleanupReport(True, "container removed and verified absent")

    def janitor(self) -> list[str]:
        """Remove expired containers (any owner) and this process's orphans; verify."""
        if self._endpoint is None:
            status = self.probe()
            if not status.available:
                raise SandboxUnavailable("docker: " + "; ".join(status.reasons))
        fmt = f'{{{{.ID}}}}\t{{{{.Label "{LABEL_EXEC}"}}}}\t{{{{.Label "{LABEL_OWNER}"}}}}\t{{{{.Label "{LABEL_DEADLINE}"}}}}'
        out = self._docker(["ps", "--all", "--no-trunc", "--filter", f"label={LABEL_EXEC}", "--format", fmt])
        if out.exit_code != 0 or out.error:
            raise SandboxCleanupError("janitor could not list sandbox containers")
        now = self._wall_clock()
        stale: list[str] = []
        for line in decode(out.stdout).splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            cid, exec_id, owner, deadline = parts
            try:
                expired = float(deadline) < now
            except ValueError:
                expired = True  # malformed label: treat as stale
            orphan = owner == PROCESS_TOKEN and exec_id not in self._active
            if expired or orphan:
                stale.append(cid)
        for cid in stale:
            self._docker(["rm", "--force", "--volumes", cid])
        if stale:
            ok, remaining = self._containers_with_label(LABEL_EXEC)
            still = sorted(set(stale) & set(remaining))
            if not ok or still:
                raise SandboxCleanupError(f"janitor could not remove stale sandbox containers: {still or 'unverified'}")
        return stale

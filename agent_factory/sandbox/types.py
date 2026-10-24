"""Sandbox data model: requests, limits, results, and the error hierarchy.

Error semantics (how the executor reacts):

* :class:`SandboxBlocked` and subclasses: infrastructure or policy prevented
  execution, or cleanup could not be verified. Nothing ran (or what ran was
  discarded). The run moves to the resumable ``blocked`` state and no
  developer repair retry is consumed.
* :class:`RequestRejected`: the request itself is invalid (orchestrator or
  template misconfiguration, or unsafe workspace content). Nothing ran. The
  run fails.
* Limits hit by the executed code (time, memory, ...) are not errors. They
  are reported in :class:`ExecutionResult` and treated as test failures.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..workspace import Workspace


class IsolationLevel(IntEnum):
    NONE = 0  # ordinary local process: NOT a security sandbox
    PROCESS = 1  # OS process sandbox (e.g. AppContainer); none implemented
    CONTAINER = 2  # namespaces/cgroups container (DockerBackend)
    VM = 3  # hardware virtualization; none implemented


class NetworkPolicy(StrEnum):
    NONE = "none"  # the only value accepted in v0.2


class CodeTrust(StrEnum):
    #: Content authored by repository maintainers (scripted FakeProvider output,
    #: template scaffolds). May run with isolation level NONE.
    TRUSTED_FIXTURE = "trusted_fixture"
    #: Anything produced by a real model. Requires isolation level CONTAINER+.
    UNTRUSTED = "untrusted"


REQUIRED_ISOLATION: Mapping[CodeTrust, IsolationLevel] = MappingProxyType(
    {
        CodeTrust.TRUSTED_FIXTURE: IsolationLevel.NONE,
        CodeTrust.UNTRUSTED: IsolationLevel.CONTAINER,
    }
)


def required_isolation(trust: CodeTrust) -> IsolationLevel:
    """Isolation needed for ``trust``; anything unrecognised needs CONTAINER."""
    try:
        return REQUIRED_ISOLATION[CodeTrust(trust)]
    except (KeyError, ValueError):
        return IsolationLevel.CONTAINER


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"  # the process exited on its own (any exit code)
    TIMED_OUT = "timed_out"
    LIMIT_EXCEEDED = "limit_exceeded"


class LimitHit(StrEnum):
    TIME = "time"
    CPU = "cpu"
    MEMORY = "memory"
    DISK = "disk"
    PIDS = "pids"
    OUTPUT = "output"


MiB = 1024 * 1024

# Hard bounds on what any request may ask for.
_BOUNDS = {
    "timeout_s": (0.1, 3600.0),
    "cpus": (0.1, 16.0),
    "memory_mb": (32, 16384),
    "disk_mb": (1, 10240),
    "pids": (8, 4096),
    "output_bytes": (1024, 16 * MiB),
}


@dataclass(frozen=True)
class ResourceLimits:
    timeout_s: float = 120.0
    cpus: float = 1.0
    memory_mb: int = 512
    disk_mb: int = 256
    pids: int = 128
    output_bytes: int = 1_000_000  # per stream
    #: Per-process CPU-time budget in seconds (RLIMIT_CPU). None means
    #: ceil(timeout_s) + 5: a backstop behind the wall-clock deadline.
    cpu_time_s: int | None = None

    def __post_init__(self) -> None:
        problems = []
        for name, (lo, hi) in _BOUNDS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (lo <= value <= hi):
                problems.append(f"{name}={value!r} outside [{lo}, {hi}]")
        cpu_time = self.cpu_time_s
        if cpu_time is not None and (isinstance(cpu_time, bool) or not isinstance(cpu_time, int) or not 1 <= cpu_time <= 86400):
            problems.append(f"cpu_time_s={cpu_time!r} outside [1, 86400]")
        if problems:
            raise ValueError("invalid resource limits: " + "; ".join(problems))

    @property
    def cpu_seconds(self) -> int:
        """Per-process CPU-time budget (RLIMIT_CPU soft limit)."""
        if self.cpu_time_s is not None:
            return self.cpu_time_s
        return math.ceil(self.timeout_s) + 5

    @property
    def disk_bytes(self) -> int:
        """Writable /work capacity (tmpfs size)."""
        return self.disk_mb * MiB

    @property
    def max_input_bytes(self) -> int:
        """Largest staged project footprint (tmpfs pages) allowed into /work.

        At most half of /work, so at least half always remains writable for
        test temp files, runtime files, reports and scratch data; and at most a
        quarter of the memory limit, because af-entry buffers the archive in
        memory and tmpfs pages are charged to the container's memory cgroup.
        """
        return min(self.disk_bytes // 2, (self.memory_mb * MiB) // 4)

    @property
    def work_headroom_bytes(self) -> int:
        """Writable /work capacity guaranteed to remain after staging."""
        return self.disk_bytes - self.max_input_bytes

    def to_dict(self) -> dict[str, Any]:
        return {**{name: getattr(self, name) for name in _BOUNDS}, "cpu_seconds": self.cpu_seconds}


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    workspace: Workspace
    command: tuple[str, ...]
    run_id: str = ""
    working_dir: str = "."
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    network: NetworkPolicy = NetworkPolicy.NONE
    env: Mapping[str, str] = field(default_factory=dict)
    env_allowlist: frozenset[str] = frozenset()
    trust: CodeTrust = CodeTrust.UNTRUSTED

    def summary(self) -> dict[str, Any]:
        """Loggable description. Env values are omitted on purpose."""
        return {
            "execution_id": self.execution_id,
            "command": list(self.command),
            "working_dir": self.working_dir,
            "limits": self.limits.to_dict(),
            "network": str(self.network),
            "env_names": sorted(self.env),
            "trust": str(self.trust),
        }


@dataclass(frozen=True)
class BackendInfo:
    name: str
    version: str
    isolation_level: IsolationLevel
    #: Which limits this backend actually enforces.
    enforced_limits: tuple[LimitHit, ...]
    runtime: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "isolation_level": self.isolation_level.name,
            "enforced_limits": [str(x) for x in self.enforced_limits],
            "runtime": dict(self.runtime),
        }


@dataclass(frozen=True)
class BackendStatus:
    available: bool
    reasons: tuple[str, ...] = ()
    info: BackendInfo | None = None


@dataclass(frozen=True)
class CleanupReport:
    verified: bool
    detail: str = ""
    leftovers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    limits_hit: tuple[LimitHit, ...]
    duration_s: float
    started_at: str
    finished_at: str
    backend: BackendInfo
    cleanup_verified: bool = False
    detail: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": str(self.status),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "limits_hit": [str(x) for x in self.limits_hit],
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "duration_s": round(self.duration_s, 3),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cleanup_verified": self.cleanup_verified,
            "backend": self.backend.to_dict(),
            "detail": self.detail,
        }


@dataclass
class SandboxHandle:
    """Backend-owned state for one execution, from prepare() to destroy()."""

    execution_id: str
    backend_name: str
    request: ExecutionRequest | None
    data: dict[str, Any] = field(default_factory=dict)
    destroyed: bool = False


# ------------------------------------------------------------------ errors


class SandboxError(RuntimeError):
    pass


class SandboxBlocked(SandboxError):
    """Execution could not happen safely; the run must block, not fail a test."""


class SandboxUnavailable(SandboxBlocked):
    """No qualifying backend is available or healthy, or one failed its self-test."""


class ExecutionDenied(SandboxBlocked):
    """Policy forbids this execution on the available backends (e.g. untrusted code, no container)."""


class SandboxInfrastructureError(SandboxBlocked):
    """The backend failed while preparing/executing (daemon error, missing interpreter, ...)."""


class SandboxCleanupError(SandboxBlocked):
    """Cleanup could not be verified. The backend is unhealthy until recovered."""

    def __init__(self, message: str, report: CleanupReport | None = None) -> None:
        super().__init__(message)
        self.report = report


class RequestRejected(SandboxError):
    """The request is invalid; nothing was executed."""


class StagingRejected(RequestRejected):
    """Workspace content cannot be safely copied into a sandbox."""

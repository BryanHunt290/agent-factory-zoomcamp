"""FakeSandboxBackend: a deterministic test double. It executes nothing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .base import SandboxBackend
from .types import (
    BackendInfo,
    BackendStatus,
    CleanupReport,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    IsolationLevel,
    LimitHit,
    SandboxHandle,
    SandboxInfrastructureError,
)
from .validation import validate_request


@dataclass(frozen=True)
class FakeOutcome:
    status: ExecutionStatus = ExecutionStatus.COMPLETED
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    limits_hit: tuple[LimitHit, ...] = ()
    duration_s: float = 0.01


@dataclass
class FakeSandboxBackend(SandboxBackend):
    """Scripted backend. ``fail_at`` names a lifecycle stage that raises."""

    level: IsolationLevel = IsolationLevel.CONTAINER
    available: bool = True
    self_test_passes: bool = True
    outcome: FakeOutcome | Callable[[ExecutionRequest], FakeOutcome] = field(default_factory=FakeOutcome)
    fail_at: str | None = None  # "prepare" | "execute" | "collect" | "destroy"
    cleanup_verified: bool = True
    janitor_ok: bool = True
    label: str = "fake"
    calls: list[str] = field(default_factory=list)
    requests: list[ExecutionRequest] = field(default_factory=list)

    name = "fake"  # type: ignore[assignment]
    is_test_double = True
    is_security_sandbox = True  # pretends to be one; only usable when test doubles are allowed

    @property
    def isolation_level(self) -> IsolationLevel:  # type: ignore[override]
        return self.level

    def info(self) -> BackendInfo:
        return BackendInfo(self.label, "0", self.level, tuple(LimitHit))

    def probe(self) -> BackendStatus:
        self.calls.append("probe")
        if not self.available:
            return BackendStatus(False, (f"{self.label} unavailable",))
        return BackendStatus(True, (), self.info())

    def self_test(self) -> BackendStatus:
        status = self.probe()
        self.calls.append("self_test")
        if status.available and not self.self_test_passes:
            return BackendStatus(False, (f"{self.label} isolation self-test failed",))
        return status

    def janitor(self) -> list[str]:
        self.calls.append("janitor")
        if not self.janitor_ok:
            from .types import SandboxCleanupError

            raise SandboxCleanupError(f"{self.label}: janitor could not remove leftovers")
        return []

    def _maybe_fail(self, stage: str) -> None:
        self.calls.append(stage)
        if self.fail_at == stage:
            raise SandboxInfrastructureError(f"{self.label}: simulated failure in {stage}")

    def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        validate_request(request)
        self.requests.append(request)
        self._maybe_fail("prepare")
        return SandboxHandle(request.execution_id, self.label, request)

    def execute(self, handle: SandboxHandle) -> None:
        self._maybe_fail("execute")

    def collect_result(self, handle: SandboxHandle) -> ExecutionResult:
        self._maybe_fail("collect")
        o = self.outcome(handle.request) if callable(self.outcome) else self.outcome
        return ExecutionResult(
            execution_id=handle.execution_id,
            status=o.status,
            exit_code=o.exit_code,
            stdout=o.stdout,
            stderr=o.stderr,
            stdout_truncated=LimitHit.OUTPUT in o.limits_hit,
            stderr_truncated=False,
            timed_out=o.status == ExecutionStatus.TIMED_OUT,
            limits_hit=o.limits_hit,
            duration_s=o.duration_s,
            started_at="1970-01-01T00:00:00.000000Z",
            finished_at="1970-01-01T00:00:00.010000Z",
            backend=self.info(),
        )

    def destroy(self, handle: SandboxHandle) -> CleanupReport:
        self._maybe_fail("destroy")
        return CleanupReport(self.cleanup_verified, "fake cleanup" if self.cleanup_verified else "fake leftover")

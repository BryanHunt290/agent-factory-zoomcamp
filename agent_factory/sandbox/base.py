"""The SandboxBackend contract and the lifecycle driver."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import ClassVar

from .types import (
    BackendStatus,
    CleanupReport,
    ExecutionRequest,
    ExecutionResult,
    IsolationLevel,
    SandboxCleanupError,
    SandboxHandle,
)


class SandboxBackend(ABC):
    name: ClassVar[str]
    isolation_level: ClassVar[IsolationLevel]
    #: True only for backends that are a security boundary for untrusted code.
    is_security_sandbox: ClassVar[bool] = False
    #: Test doubles are refused by SandboxManager unless explicitly allowed.
    is_test_double: ClassVar[bool] = False

    @abstractmethod
    def probe(self) -> BackendStatus:
        """Report availability. Must not create anything or touch the network."""

    def self_test(self) -> BackendStatus:
        """Prove the isolation actually holds. Defaults to probe()."""
        return self.probe()

    @abstractmethod
    def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        """Validate, stage, and create the isolated environment without starting it.

        On failure, anything partially created must be cleaned up; if that
        cleanup cannot be verified, raise SandboxCleanupError.
        """

    @abstractmethod
    def execute(self, handle: SandboxHandle) -> None:
        """Run to completion, deadline, or limit. Must enforce the deadline itself."""

    @abstractmethod
    def collect_result(self, handle: SandboxHandle) -> ExecutionResult: ...

    @abstractmethod
    def destroy(self, handle: SandboxHandle) -> CleanupReport:
        """Tear down everything for ``handle`` and verify it is gone. Idempotent."""

    def janitor(self) -> list[str]:
        """Remove leftovers from earlier crashed executions. Raise SandboxCleanupError if any remain."""
        return []


def _safe_destroy(backend: SandboxBackend, handle: SandboxHandle) -> CleanupReport:
    try:
        report = backend.destroy(handle)
    except Exception as exc:  # destroy must never mask the cleanup question
        return CleanupReport(False, f"destroy raised {type(exc).__name__}: {exc}")
    handle.destroyed = True
    return report


def run_sandboxed(backend: SandboxBackend, request: ExecutionRequest) -> ExecutionResult:
    """prepare -> execute -> collect_result, then destroy (always) and verify cleanup.

    An unverified cleanup always raises SandboxCleanupError, even if the
    execution itself also failed, because it is the more severe condition.
    """
    handle = backend.prepare(request)
    try:
        backend.execute(handle)
        result = backend.collect_result(handle)
    except BaseException as exc:
        report = _safe_destroy(backend, handle)
        if not report.verified:
            raise SandboxCleanupError(f"{backend.name}: cleanup not verified: {report.detail}", report) from exc
        raise
    report = _safe_destroy(backend, handle)
    if not report.verified:
        raise SandboxCleanupError(f"{backend.name}: cleanup not verified: {report.detail}", report)
    return replace(result, cleanup_verified=True)

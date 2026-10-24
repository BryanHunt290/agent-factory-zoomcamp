"""Execution sandbox layer for running (potentially untrusted) project code.

See docs/design/v0.2-sandbox.md. Only backends with ``is_security_sandbox``
and isolation level CONTAINER or higher may run untrusted code.
LocalProcessBackend is NOT a sandbox.
"""

from .base import SandboxBackend, run_sandboxed
from .docker import DockerBackend, DockerConfig
from .fake import FakeOutcome, FakeSandboxBackend
from .local import LocalProcessBackend
from .manager import SandboxManager
from .types import (
    BackendInfo,
    BackendStatus,
    CleanupReport,
    CodeTrust,
    ExecutionDenied,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    IsolationLevel,
    LimitHit,
    NetworkPolicy,
    RequestRejected,
    ResourceLimits,
    SandboxBlocked,
    SandboxCleanupError,
    SandboxError,
    SandboxHandle,
    SandboxInfrastructureError,
    SandboxUnavailable,
    StagingRejected,
    required_isolation,
)
from .validation import DEFAULT_ENV_ALLOWLIST, DEFAULT_SANDBOX_ENV, validate_request

__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_SANDBOX_ENV",
    "BackendInfo",
    "BackendStatus",
    "CleanupReport",
    "CodeTrust",
    "DockerBackend",
    "DockerConfig",
    "ExecutionDenied",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionStatus",
    "FakeOutcome",
    "FakeSandboxBackend",
    "IsolationLevel",
    "LimitHit",
    "LocalProcessBackend",
    "NetworkPolicy",
    "RequestRejected",
    "ResourceLimits",
    "SandboxBackend",
    "SandboxBlocked",
    "SandboxCleanupError",
    "SandboxError",
    "SandboxHandle",
    "SandboxInfrastructureError",
    "SandboxManager",
    "SandboxUnavailable",
    "StagingRejected",
    "required_isolation",
    "run_sandboxed",
    "validate_request",
]

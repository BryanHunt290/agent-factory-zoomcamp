"""LocalProcessBackend: runs a command as an ordinary local subprocess.

    *** THIS IS NOT A SANDBOX. ***

It provides no filesystem, network, memory, CPU, disk, or process-tree
isolation; the process runs with the user's full rights. It exists only so
deterministic, maintainer-authored fixtures (FakeProvider scripts, template
scaffolds) can be tested on machines without Docker.

It refuses any request whose trust is not ``TRUSTED_FIXTURE``, and
SandboxManager never selects it for untrusted code. It must never be used
for code produced by a real model.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from ..util import timestamp, utc_now
from .base import SandboxBackend
from .process import ProcessOutcome, decode, run_capped
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
    SandboxHandle,
    SandboxInfrastructureError,
)
from .validation import validate_request

#: Host variables passed through so a local Python can start (v0.1 behaviour).
HOST_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG")


def scrubbed_env() -> dict[str, str]:
    """Minimal host environment for trusted local fixtures: no keys, tokens, or proxies."""
    env = {k: os.environ[k] for k in HOST_ENV_ALLOWLIST if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@dataclass
class _LocalRun:
    argv: list[str]
    cwd: str
    env: dict[str, str]
    outcome: ProcessOutcome | None = None
    started_at: str = ""
    finished_at: str = ""


class LocalProcessBackend(SandboxBackend):
    name = "local-process"
    isolation_level = IsolationLevel.NONE
    is_security_sandbox = False
    #: Only TIME and OUTPUT are enforced; nothing else is.
    ENFORCED_LIMITS = (LimitHit.TIME, LimitHit.OUTPUT)

    def __init__(self, python: str = sys.executable) -> None:
        self.python = python

    def info(self) -> BackendInfo:
        return BackendInfo(
            name=self.name,
            version=f"python {sys.version.split()[0]}",
            isolation_level=self.isolation_level,
            enforced_limits=self.ENFORCED_LIMITS,
            runtime={"warning": "not a sandbox; trusted fixtures only"},
        )

    def probe(self) -> BackendStatus:
        if not os.path.isfile(self.python):
            return BackendStatus(False, (f"python interpreter not found: {self.python}",))
        return BackendStatus(True, (), self.info())

    def prepare(self, request: ExecutionRequest) -> SandboxHandle:
        validate_request(request)
        # Defence in depth: refuse untrusted code even if a caller bypasses
        # SandboxManager. This backend is not a security boundary.
        if request.trust != CodeTrust.TRUSTED_FIXTURE:
            raise ExecutionDenied(
                "LocalProcessBackend is not a sandbox and refuses untrusted code "
                f"(trust={request.trust!s}); a CONTAINER-level backend is required"
            )
        cwd = request.workspace.resolve(request.working_dir) if request.working_dir != "." else request.workspace.project_dir
        argv = [self.python, *request.command[1:]]
        env = {**scrubbed_env(), **request.env}
        return SandboxHandle(request.execution_id, self.name, request, {"run": _LocalRun(argv, str(cwd), env)})

    def execute(self, handle: SandboxHandle) -> None:
        run: _LocalRun = handle.data["run"]
        limits = handle.request.limits
        run.started_at = timestamp(utc_now)
        run.outcome = run_capped(
            run.argv, timeout_s=limits.timeout_s, output_limit=limits.output_bytes, cwd=run.cwd, env=run.env
        )
        run.finished_at = timestamp(utc_now)

    def collect_result(self, handle: SandboxHandle) -> ExecutionResult:
        run: _LocalRun = handle.data["run"]
        outcome = run.outcome
        if outcome is None:
            raise SandboxInfrastructureError("execute() did not run")
        if outcome.error is not None:
            raise SandboxInfrastructureError(f"could not start {run.argv[0]}: {outcome.error}")
        if outcome.timed_out:
            status, hits = ExecutionStatus.TIMED_OUT, (LimitHit.TIME,)
        elif outcome.output_exceeded:
            status, hits = ExecutionStatus.LIMIT_EXCEEDED, (LimitHit.OUTPUT,)
        else:
            status, hits = ExecutionStatus.COMPLETED, ()
        return ExecutionResult(
            execution_id=handle.execution_id,
            status=status,
            exit_code=outcome.exit_code,
            stdout=decode(outcome.stdout),
            stderr=decode(outcome.stderr),
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
            timed_out=outcome.timed_out,
            limits_hit=hits,
            duration_s=outcome.duration_s,
            started_at=run.started_at,
            finished_at=run.finished_at,
            backend=self.info(),
        )

    def destroy(self, handle: SandboxHandle) -> CleanupReport:
        # run_capped() always waits for (or kills) the direct child before
        # returning. Grandchildren are NOT tracked: acceptable only because
        # this backend runs maintainer-authored fixtures exclusively.
        run: _LocalRun = handle.data["run"]
        if run.outcome is not None and run.outcome.exit_code is None and run.outcome.error is None:
            return CleanupReport(False, "local process did not exit")
        return CleanupReport(True, "local process exited (child processes are not tracked)")

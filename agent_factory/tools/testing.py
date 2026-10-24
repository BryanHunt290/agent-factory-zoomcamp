"""Test execution through the sandbox layer.

The command is fixed by the project template, never chosen by a model. It
is backend-neutral (``python -m ...``); each sandbox backend resolves the
interpreter. ``RunTestsTool`` never starts a process itself. It builds an
:class:`ExecutionRequest` and hands it to the sandbox layer.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Sequence
from typing import Any

from ..safety.execution import EffectClass, ExecutionSemantics
from ..safety.permissions import ActionCategory
from ..sandbox.local import scrubbed_env  # noqa: F401  (re-exported for v0.1 callers)
from ..sandbox.manager import SandboxManager
from ..sandbox.types import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    ResourceLimits,
)
from ..sandbox.validation import DEFAULT_ENV_ALLOWLIST, DEFAULT_SANDBOX_ENV
from ..workspace import Workspace
from .base import Tool, ToolContext, ToolError, ToolResult

UNITTEST_COMMAND = ("python", "-m", "unittest", "discover", "-s", "tests", "-t", ".")

_FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)", re.MULTILINE)
_RAN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)

SandboxRunner = Callable[[ExecutionRequest], ExecutionResult]


def new_execution_id() -> str:
    return f"af-{secrets.token_hex(8)}"


def parse_unittest_output(output: str) -> tuple[int, list[str]]:
    ran = _RAN.search(output)
    tests_run = int(ran.group(1)) if ran else 0
    failures = sorted(
        {
            # Python 3.11+ prints "FAIL: test_x (pkg.mod.Class.test_x)"; older
            # versions omit the method from the parenthesised id.
            ident if ident.endswith("." + name) else f"{ident}.{name}"
            for name, ident in _FAILED_TEST.findall(output)
        }
    )
    return tests_run, failures


def report_from_result(result: ExecutionResult) -> dict[str, Any]:
    """Map a sandbox result onto the v0.1 test-report fields plus a ``sandbox`` block."""
    output = result.stdout + result.stderr
    tests_run, failures = parse_unittest_output(output)
    notes = []
    if result.status is ExecutionStatus.TIMED_OUT:
        notes.append(f"[sandbox] timed out after {result.duration_s:.1f}s; process killed")
    elif result.status is ExecutionStatus.LIMIT_EXCEEDED:
        notes.append(f"[sandbox] resource limit exceeded: {', '.join(map(str, result.limits_hit))}")
    if result.stdout_truncated or result.stderr_truncated:
        notes.append("[sandbox] output truncated")
    if notes:
        output = output + "\n" + "\n".join(notes)
    # Passing requires a normal exit, exit code 0, and at least one test.
    passed = result.status is ExecutionStatus.COMPLETED and result.exit_code == 0 and tests_run > 0
    return {
        "passed": passed,
        "returncode": result.exit_code,
        "tests_run": tests_run,
        "failures": failures,
        "output": output[-4000:],
        "sandbox": result.summary(),
    }


class RunTestsTool(Tool):
    name = "run_tests"
    category = ActionCategory.EXECUTE_SANDBOXED
    description = "Run the project's configured test command in the sandbox layer."
    execution = ExecutionSemantics(
        effect=EffectClass.READ_ONLY, max_attempts=3, replayable_result=False,
        rationale=(
            "observes the project by running its tests. In the Docker sandbox nothing persists (copy-in "
            "only). The trusted local backend runs maintainer-written fixtures in the workspace, which may "
            "write caches; it is never used for model-generated code"
        ),
    )
    wants_context = True

    def __init__(
        self,
        command: Sequence[str] = UNITTEST_COMMAND,
        limits: ResourceLimits | None = None,
        sandbox: SandboxRunner | None = None,
    ) -> None:
        if not command:
            raise ValueError("test command must be non-empty")
        self.command = tuple(command)
        self.limits = limits or ResourceLimits()
        # Default: trusted fixtures only; untrusted code is BLOCKED.
        self._sandbox = sandbox or SandboxManager.default().run

    def run(self, workspace: Workspace, *, context: ToolContext | None = None, **params: Any) -> ToolResult:
        if params:
            raise ToolError(f"run_tests takes no parameters (got {sorted(params)})")
        request = ExecutionRequest(
            execution_id=new_execution_id(),
            run_id=context.run_id if context else "",
            workspace=workspace,
            command=self.command,
            limits=self.limits,
            env=dict(DEFAULT_SANDBOX_ENV),
            env_allowlist=DEFAULT_ENV_ALLOWLIST,
            # Trust comes from the workspace record, which the executor downgrades
            # to UNTRUSTED whenever an untrusted provider is involved.
            trust=workspace.trust,
        )
        result = self._sandbox(request)
        report = report_from_result(result)
        summary = f"{report['tests_run']} tests, " + (
            "passed" if report["passed"] else f"FAILED ({len(report['failures'])} failing)"
        )
        if result.status is not ExecutionStatus.COMPLETED:
            summary += f" [{result.status}]"
        return ToolResult(data=report, summary=summary, ok=report["passed"])

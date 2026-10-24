"""CoreToolPack and GitToolPack: the existing local tools, registered as capabilities (docs/capabilities.md).

The tools themselves are unchanged (``filesystem.py``, ``testing.py``, ``git.py``): the packs only wrap
them with capability definitions. Workspace confinement, protected paths, the sandbox routing, and the
workspace git confinement (``Workspace._git``) all stay where they were, inside the tools and the
workspace. Both packs are local: always enabled, always available.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..capabilities.model import ApprovalRule, Capability, CostImpact, Risk
from ..capabilities.packs import ToolPack
from ..safety.execution import EffectClass
from ..safety.permissions import ActionCategory
from ..sandbox.types import ResourceLimits
from .base import Tool
from .filesystem import DeleteFileTool, ListFilesTool, ReadFileTool, WriteFileTool
from .git import GitCommitTool
from .testing import UNITTEST_COMMAND, RunTestsTool, SandboxRunner

_PATH = {"type": "string", "minLength": 1, "maxLength": 300}
_NO_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
_CAP_TESTS = "tests/test_capabilities.py::"


def _core(capability_id: str, tool: str, category: ActionCategory, effect: EffectClass, description: str, *,
          mutates: bool, approval: ApprovalRule = ApprovalRule.POLICY, risk: Risk = Risk.LOW,
          input_schema: dict | None = None, output_schema: dict | None = None,
          sandbox: bool = False, tests: Sequence[str] = (), tags: Sequence[str] = ()) -> Capability:  # fmt: skip
    return Capability(
        capability_id=capability_id, version=1, pack=capability_id.split(".")[0], description=description,
        implemented_by=tool, category=category, effect=effect, approval=approval, cost=CostImpact.NONE, risk=risk,
        mutates=mutates, sandbox_compatible=sandbox, input_schema=input_schema or _NO_INPUT,
        output_schema=output_schema or {"type": "object"},
        required_permissions=(f"tool:{tool}", f"category:{category.value}"), tests=tuple(tests), tags=tuple(tags),
        documentation=("docs/capabilities.md",),
    )  # fmt: skip


class CoreToolPack(ToolPack):
    name = "core"

    def __init__(self, test_command: Sequence[str] = UNITTEST_COMMAND, limits: ResourceLimits | None = None,
                 sandbox: SandboxRunner | None = None) -> None:  # fmt: skip
        self._tools: tuple[Tool, ...] = (
            ListFilesTool(), ReadFileTool(), WriteFileTool(), DeleteFileTool(),
            RunTestsTool(test_command, limits=limits, sandbox=sandbox),
        )  # fmt: skip

    @property
    def enabled(self) -> bool:
        return True

    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            _core("core.files.list", "list_files", ActionCategory.READ_WORKSPACE, EffectClass.READ_ONLY,
                  "List the project's files as relative paths.", mutates=False,
                  output_schema={"type": "array", "items": {"type": "string"}},
                  tests=(_CAP_TESTS + "test_core_and_git_packs_keep_every_tool_exactly_as_before",)),
            _core("core.files.read", "read_file", ActionCategory.READ_WORKSPACE, EffectClass.READ_ONLY,
                  "Read one UTF-8 text file inside the project.", mutates=False,
                  input_schema={"type": "object", "properties": {"path": _PATH}, "required": ["path"],
                                "additionalProperties": False},
                  output_schema={"type": "string"},
                  tests=("tests/test_tool_semantics.py::test_read_tools_do_not_record_content_for_replay",
                         _CAP_TESTS + "test_core_and_git_packs_keep_every_tool_exactly_as_before")),
            _core("core.files.write", "write_file", ActionCategory.WRITE_WORKSPACE, EffectClass.IDEMPOTENT,
                  "Create or overwrite one UTF-8 text file inside the project; protected paths are refused.",
                  mutates=True,
                  input_schema={"type": "object", "properties": {"path": _PATH, "content": {"type": "string",
                                "maxLength": 200_000}}, "required": ["path", "content"], "additionalProperties": False},
                  output_schema={"type": "string"},
                  tests=(_CAP_TESTS + "test_core_and_git_packs_keep_every_tool_exactly_as_before",)),
            _core("core.files.delete", "delete_file", ActionCategory.DESTRUCTIVE_DELETE, EffectClass.IDEMPOTENT,
                  "Delete one file inside the project. Every deletion waits for a person's approval.",
                  mutates=True, approval=ApprovalRule.ALWAYS, risk=Risk.MEDIUM,
                  input_schema={"type": "object", "properties": {"path": _PATH}, "required": ["path"],
                                "additionalProperties": False},
                  output_schema={"type": "boolean"},
                  tests=(_CAP_TESTS + "test_a_dangerous_capability_pauses_for_approval_then_runs_only_through_the_action_guard",)),
            _core("core.tests.run", "run_tests", ActionCategory.EXECUTE_SANDBOXED, EffectClass.READ_ONLY,
                  "Run the project's configured test command in the isolated sandbox, without network.",
                  mutates=False, sandbox=True,
                  output_schema={"type": "object"},
                  tests=("tests/test_sandbox_default.py::test_untrusted_code_never_falls_back_to_local",
                         "tests/test_sandbox_default.py::test_untrusted_code_runs_in_docker_by_default")),
        )  # fmt: skip


class GitToolPack(ToolPack):
    """Workspace git only: every operation goes through ``Workspace._git``, pinned to ``<workspace>/project/.git``."""

    name = "git"

    def __init__(self) -> None:
        self._tools: tuple[Tool, ...] = (GitCommitTool(),)

    @property
    def enabled(self) -> bool:
        return True

    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            _core("git.commit", "git_commit", ActionCategory.WRITE_WORKSPACE, EffectClass.IDEMPOTENT,
                  "Commit the project's changes to the workspace's own local repository; no remotes, no push.",
                  mutates=True,
                  input_schema={"type": "object", "properties": {"message": {"type": "string", "minLength": 1,
                                "maxLength": 2000}}, "required": ["message"], "additionalProperties": False},
                  output_schema={"type": "string"},
                  tests=("tests/test_workspace_git.py::test_inherited_git_variables_cannot_redirect_workspace_git",
                         "tests/test_workspace_git.py::test_a_workspace_damaged_concurrently_never_redirects_git_to_the_parent",
                         "tests/test_workspace_git.py::test_a_deleted_repository_refuses_every_git_operation")),
        )  # fmt: skip


def local_packs(test_command: Sequence[str] = UNITTEST_COMMAND, limits: ResourceLimits | None = None,
                sandbox: SandboxRunner | None = None) -> tuple[ToolPack, ...]:  # fmt: skip
    """The packs every installation has: the same six tools as before, now described as capabilities."""
    return (CoreToolPack(test_command, limits, sandbox), GitToolPack())


__all__ = ["CoreToolPack", "GitToolPack", "local_packs"]

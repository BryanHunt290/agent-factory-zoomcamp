from collections.abc import Sequence

from ..sandbox.types import ResourceLimits

from .base import Tool, ToolError, ToolRegistry, ToolResult
from .filesystem import DeleteFileTool, ListFilesTool, ReadFileTool, WriteFileTool
from .gateway import ToolGateway
from .git import GitCommitTool
from .testing import UNITTEST_COMMAND, RunTestsTool, SandboxRunner


def default_registry(
    test_command: Sequence[str] = UNITTEST_COMMAND,
    limits: ResourceLimits | None = None,
    sandbox: SandboxRunner | None = None,
) -> ToolRegistry:
    """The local tools, as registered by the Core and Git tool packs (docs/capabilities.md)."""
    from .packs import local_packs  # imported here: the packs import the capability model, which imports .base

    return ToolRegistry([tool for pack in local_packs(test_command, limits, sandbox) for tool in pack.tools()])


__all__ = [
    "DeleteFileTool",
    "GitCommitTool",
    "ListFilesTool",
    "ReadFileTool",
    "RunTestsTool",
    "Tool",
    "ToolError",
    "ToolGateway",
    "ToolRegistry",
    "ToolResult",
    "UNITTEST_COMMAND",
    "WriteFileTool",
    "default_registry",
]

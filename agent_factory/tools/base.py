"""Tool interface and registry.

A tool is a single capability (read a file, run tests, ...) with a declared
:class:`ActionCategory`. Tools never decide whether they may run; the
:class:`~agent_factory.tools.gateway.ToolGateway` does that before calling them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ..safety.execution import ExecutionSemantics, check_tool_semantics
from ..safety.permissions import ActionCategory
from ..util import sha256_hex

if TYPE_CHECKING:
    from ..workspace import Workspace


class ToolError(RuntimeError):
    """A definitive failure: the tool knows the action did not take effect (or failed definitively)."""


class NotDispatched(ToolError):
    """The request provably never left the process (validation, connection refused before sending).
    Retrying is safe for every execution class."""


class OutcomeUnknown(Exception):  # noqa: N818 - a reported state, not a failure
    """The request was sent, but whether it took effect is unknown (a timeout after sending, a
    reset, an ambiguous error, a partial batch). Never a ToolError: an agent must not treat it as a
    definitive failure. The execution guard handles it according to the tool's class."""


@dataclass
class ToolResult:
    data: Any = None
    summary: str = ""
    ok: bool = True
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    """Who is calling. Passed only to tools that set ``wants_context``.

    For an approval-gated action, ``execution_id`` and ``attempt`` identify
    the durable execution record, and ``idempotency_key`` is set for keyed
    idempotent tools: the same key on every retry of the same execution.
    """

    run_id: str
    node: str
    agent: str
    visit: int
    execution_id: str | None = None
    attempt: int | None = None
    idempotency_key: str | None = None


class Tool(ABC):
    name: ClassVar[str]
    category: ClassVar[ActionCategory]
    description: ClassVar[str] = ""
    #: If True the gateway passes ``context=ToolContext(...)`` to run().
    wants_context: ClassVar[bool] = False
    #: What may happen when the action is repeated (agent_factory.safety.execution). ``None``
    #: means undeclared: treated as non-idempotent, and refused for dangerous categories.
    execution: ClassVar[ExecutionSemantics | None] = None

    @abstractmethod
    def run(self, workspace: Workspace, **params: Any) -> ToolResult: ...

    def summarize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Params as recorded in logs and approval requests: long strings are hashed."""
        out: dict[str, Any] = {}
        for key, value in sorted(params.items()):
            if isinstance(value, str) and len(value) > 200:
                out[key] = {"sha256": sha256_hex(value), "chars": len(value)}
            else:
                out[key] = value
        return out


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        check_tool_semantics(tool)  # a dangerous tool without declared semantics is refused here
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

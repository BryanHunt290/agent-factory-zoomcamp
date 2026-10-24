"""Agent interface.

An agent is a pure-ish function of (state snapshot, tools, provider) that
returns an :class:`AgentResult`: an outcome label (which selects the next
graph edge) and updates to the state fields it owns. Agents must be
idempotent per visit: a node paused for approval, or interrupted mid-way,
is re-executed from the start.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ..providers.base import ModelProvider, ModelRequest
from ..state import ProjectState
from ..tools.gateway import ToolGateway
from .schema import validate

if TYPE_CHECKING:
    from ..providers.gateway import ModelGateway
    from .prompts import PromptTemplate


class AgentOutputError(RuntimeError):
    """The model (or the agent) produced output that violates its contract."""


@dataclass
class AgentContext:
    run_id: str
    node: str
    visit: int
    state: ProjectState
    tools: ToolGateway
    provider: ModelProvider
    record_action: Callable[[dict[str, Any]], None] = lambda entry: None
    #: The run's Model Gateway for this visit: budgets, the request guard, retries, usage, audit.
    models: ModelGateway | None = None
    #: Host locations that must never appear in model context (replaced by placeholders).
    host_paths: tuple[str, ...] = ()


@dataclass
class AgentResult:
    outcome: str
    updates: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


class Agent(ABC):
    name: ClassVar[str]
    #: State fields this agent may update.
    writes: ClassVar[frozenset[str]] = frozenset()

    @abstractmethod
    def run(self, ctx: AgentContext) -> AgentResult: ...

    def ask(
        self, ctx: AgentContext, template: PromptTemplate, inputs: dict[str, Any], *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """A schema-valid answer from the model, through the run's Model Gateway.

        ``inputs`` is project data (untrusted); ``context`` records which files
        it contains, for the audit trail. Without a gateway (a context built by
        hand) the provider is called directly, with the same schema check.
        """
        if ctx.models is not None:
            return ctx.models.ask(template, inputs, context=context)
        return self.ask_model(ctx, purpose=template.purpose, instructions=template.text, inputs=inputs, schema=template.output_schema)

    def ask_model(
        self,
        ctx: AgentContext,
        *,
        purpose: str,
        instructions: str,
        inputs: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        request = ModelRequest(
            run_id=ctx.run_id,
            agent=self.name,
            purpose=purpose,
            instructions=instructions,
            inputs=inputs,
            output_schema=schema,
            attempt=ctx.visit,
        )
        response = ctx.provider.complete(request)
        errors = validate(response.content, schema)
        ctx.record_action(
            {
                "kind": "model_call",
                "provider": response.provider,
                "model": response.model,
                "purpose": purpose,
                "attempt": ctx.visit,
                "result": "invalid_output" if errors else "ok",
                "usage": response.usage,
            }
        )
        if errors:
            raise AgentOutputError(f"{self.name}: model output failed validation: {errors}")
        return response.content

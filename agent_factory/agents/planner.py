from __future__ import annotations

from ..graph.standard import Outcome
from ..providers.context import ContextBuilder
from .base import Agent, AgentContext, AgentResult
from .prompts import PLAN_SCHEMA, PLANNER_V2

__all__ = ["PLAN_SCHEMA", "PlannerAgent"]


class PlannerAgent(Agent):
    """Context: the task and the names of the existing files. No file contents."""

    name = "planner"
    writes = frozenset({"plan"})

    def run(self, ctx: AgentContext) -> AgentResult:
        context = ContextBuilder(host_paths=ctx.host_paths)
        task = ctx.state.task
        files = context.paths(ctx.tools.call("list_files").data)
        plan = self.ask(
            ctx,
            PLANNER_V2,
            {
                "task": {"title": context.text(task.title), "goal": context.text(task.goal),
                         "requirements": [context.text(r) for r in task.requirements]},  # fmt: skip
                "existing_files": files,
            },
            context=context.manifest(),
        )
        return AgentResult(Outcome.PLANNED, {"plan": plan}, summary=plan["summary"])

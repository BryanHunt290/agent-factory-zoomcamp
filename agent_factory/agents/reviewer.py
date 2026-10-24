from __future__ import annotations

from dataclasses import asdict

from ..graph.standard import Outcome
from ..providers.context import ContextBuilder, withheld
from .base import Agent, AgentContext, AgentResult
from .prompts import REVIEW_SCHEMA, REVIEWER_V2

__all__ = ["REVIEW_SCHEMA", "ReviewerAgent"]


class ReviewerAgent(Agent):
    """Context: the task, the plan, what was implemented, the test evidence, and the changed/planned files."""

    name = "reviewer"
    writes = frozenset({"review"})

    def run(self, ctx: AgentContext) -> AgentResult:
        state = ctx.state
        report = state.test_report or {}
        if not report.get("passed"):
            # Never approve untested code, whatever a model might say.
            review = {
                "iteration": ctx.visit,
                "verdict": Outcome.CHANGES_REQUIRED,
                "summary": "tests have not passed",
                "comments": ["Tests must pass before review."],
            }
            return AgentResult(Outcome.CHANGES_REQUIRED, {"review": review}, summary=review["summary"])

        context = ContextBuilder(host_paths=ctx.host_paths)
        existing = set(ctx.tools.call("list_files").data)
        impl = state.implementation or {}
        paths = sorted(set(impl.get("files_written", [])) | set((state.plan or {}).get("files", [])))
        readable = [p for p in paths if isinstance(p, str) and p in existing]
        files = context.files({p: ctx.tools.call("read_file", path=p).data for p in readable if not withheld(p)})
        context.withheld.extend(p for p in readable if withheld(p))

        verdict = self.ask(
            ctx,
            REVIEWER_V2,
            {
                "task": asdict(state.task),
                "plan": state.plan,
                "implementation": impl,
                "test_report": {k: report.get(k) for k in ("passed", "tests_run", "failures")},
                "files": files,
            },
            context=context.manifest(),
        )
        review = {"iteration": ctx.visit, **verdict}
        return AgentResult(verdict["verdict"], {"review": review}, summary=verdict["summary"])

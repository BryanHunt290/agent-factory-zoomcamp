from __future__ import annotations

from ..graph.standard import Outcome
from .base import Agent, AgentContext, AgentResult


class TesterAgent(Agent):
    """Runs the project's test command. The verdict comes from the tests, not a model."""

    name = "tester"
    writes = frozenset({"test_report"})

    def run(self, ctx: AgentContext) -> AgentResult:
        result = ctx.tools.call("run_tests")
        report = dict(result.data)
        report["iteration"] = ctx.visit
        outcome = Outcome.PASS if report["passed"] else Outcome.FAIL
        return AgentResult(outcome, {"test_report": report}, summary=result.summary)

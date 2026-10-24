from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..graph.standard import Outcome
from ..providers.context import ContextBuilder, withheld
from ..safety.approval import PermissionDenied
from ..state import ProjectState
from .base import Agent, AgentContext, AgentResult
from .prompts import DEVELOPER_SCHEMA, DEVELOPER_V2

__all__ = ["DEVELOPER_SCHEMA", "DeveloperAgent", "feedback_for"]

#: How much of the test output a repair visit sees (its tail, where failures are reported).
FEEDBACK_OUTPUT_CHARS = 1500


def feedback_for(state: ProjectState, context: ContextBuilder | None = None) -> dict[str, Any] | None:
    """Structured feedback from whichever node sent work back to the developer."""
    last = state.last_history()
    if last is None:
        return None
    if last["node"] == "tester" and last["outcome"] == Outcome.FAIL and state.test_report:
        output = state.test_report.get("output", "")
        output = context.text(output, limit=FEEDBACK_OUTPUT_CHARS, tail=True) if context else output[-FEEDBACK_OUTPUT_CHARS:]
        return {"source": "tester", "failures": state.test_report.get("failures", []), "output": output}
    if last["node"] == "reviewer" and last["outcome"] == Outcome.CHANGES_REQUIRED and state.review:
        return {"source": "reviewer", "comments": state.review.get("comments", [])}
    return None


class DeveloperAgent(Agent):
    """Context: the task, the plan, the feedback to fix, and the current contents of the planned files only.

    The answer is data. Every file it names is written, and every deletion
    requested, only through the Tool Gateway: the workspace refuses paths
    outside the project and protected paths, and deletion needs a human.
    """

    name = "developer"
    writes = frozenset({"implementation"})

    def run(self, ctx: AgentContext) -> AgentResult:
        state = ctx.state
        context = ContextBuilder(host_paths=ctx.host_paths)
        existing = set(ctx.tools.call("list_files").data)
        wanted = [p for p in (state.plan or {}).get("files", []) if isinstance(p, str)]
        readable = [p for p in wanted if p in existing]
        current_files = context.files({p: ctx.tools.call("read_file", path=p).data for p in readable if not withheld(p)})
        for p in readable:
            if withheld(p):
                context.withheld.append(p)
        feedback = feedback_for(state, context)

        change = self.ask(
            ctx,
            DEVELOPER_V2,
            {
                "task": asdict(state.task),
                "plan": state.plan,
                "iteration": ctx.visit,
                "feedback": feedback,
                "current_files": current_files,
            },
            context=context.manifest(),
        )

        # Deletions first: if one needs approval the run pauses before any
        # writes happen, and a rejected deletion is skipped, not fatal.
        deleted, rejected = [], []
        for path in change.get("delete", []):
            try:
                ctx.tools.call("delete_file", path=path)
                deleted.append(path)
            except PermissionDenied:
                rejected.append(path)

        written = []
        for path, content in sorted(change["files"].items()):
            ctx.tools.call("write_file", path=path, content=content)
            written.append(path)

        commit = ctx.tools.call(
            "git_commit", message=f"developer iteration {ctx.visit}: {change['summary']}"
        ).data

        implementation = {
            "iteration": ctx.visit,
            "summary": change["summary"],
            "responding_to": feedback["source"] if feedback else None,
            "files_written": written,
            "files_deleted": deleted,
            "deletions_rejected": rejected,
            "commit": commit,
        }
        return AgentResult(Outcome.IMPLEMENTED, {"implementation": implementation}, summary=change["summary"])


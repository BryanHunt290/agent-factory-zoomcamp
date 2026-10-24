"""Local git snapshot tool (no remotes, no push)."""

from __future__ import annotations

from ..safety.execution import EffectClass, ExecutionSemantics, Idempotency
from ..safety.permissions import ActionCategory
from ..workspace import Workspace, WorkspaceError
from .base import Tool, ToolError, ToolResult


class GitCommitTool(Tool):
    name = "git_commit"
    category = ActionCategory.WRITE_WORKSPACE
    description = "Commit all project changes to the workspace's local git repository."
    execution = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.NATURAL, max_attempts=3,
        rationale=(
            "commits the working tree locally: repeating it on an unchanged tree commits nothing. "
            "After further changes a repeat makes a new commit, a different effect"
        ),
    )

    def run(self, workspace: Workspace, *, message: str) -> ToolResult:
        if not workspace.is_git and not workspace.expects_git:
            return ToolResult(data=None, summary="workspace is not a git repository; skipped")
        try:
            # Refuses unless project/.git is exactly this workspace's own repository.
            sha = workspace.commit_all(message)
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(data=sha, summary=f"committed {sha[:10]}" if sha else "nothing to commit")

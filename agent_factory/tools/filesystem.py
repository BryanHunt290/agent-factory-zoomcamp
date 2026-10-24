"""Workspace file tools. All paths are confined by :meth:`Workspace.resolve`."""

from __future__ import annotations

from typing import Any

from ..safety.execution import EffectClass, ExecutionSemantics, Idempotency
from ..safety.permissions import ActionCategory
from ..workspace import Workspace, WorkspaceError
from .base import Tool, ToolError, ToolResult


class ListFilesTool(Tool):
    name = "list_files"
    category = ActionCategory.READ_WORKSPACE
    description = "List project files (relative POSIX paths)."
    execution = ExecutionSemantics(
        effect=EffectClass.READ_ONLY, max_attempts=3, rationale="lists files; changes nothing"
    )

    def run(self, workspace: Workspace, **params: Any) -> ToolResult:
        files = workspace.list_files()
        return ToolResult(data=files, summary=f"{len(files)} files")


class ReadFileTool(Tool):
    name = "read_file"
    category = ActionCategory.READ_WORKSPACE
    description = "Read a UTF-8 text file from the project."
    execution = ExecutionSemantics(
        effect=EffectClass.READ_ONLY, max_attempts=3, replayable_result=False,
        rationale="reads one file; changes nothing (the content is not recorded for replay)",
    )

    def run(self, workspace: Workspace, *, path: str) -> ToolResult:
        try:
            content = workspace.read_text(path)
        except FileNotFoundError:
            raise ToolError(f"no such file: {path}") from None
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(data=content, summary=f"read {path} ({len(content)} chars)")


class WriteFileTool(Tool):
    name = "write_file"
    category = ActionCategory.WRITE_WORKSPACE
    description = "Create or overwrite a UTF-8 text file in the project."
    execution = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.NATURAL, max_attempts=3,
        rationale=(
            "overwrites one file with the given content: repeating the same write leaves the same file. "
            "Not harmless in general: replayed after a later write to the same path, it would revert it"
        ),
    )

    def run(self, workspace: Workspace, *, path: str, content: str) -> ToolResult:
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        try:
            workspace.write_text(path, content)
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(data=path, summary=f"wrote {path} ({len(content)} chars)")


class DeleteFileTool(Tool):
    name = "delete_file"
    category = ActionCategory.DESTRUCTIVE_DELETE
    description = "Delete a file from the project (requires human approval)."
    execution = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.NATURAL, max_attempts=3,
        rationale=(
            "makes one path absent: a repeat finds nothing to delete and reports 'did not exist'. "
            "The effect is local and synchronous, so a stopped process leaves nothing in flight"
        ),
    )

    def run(self, workspace: Workspace, *, path: str) -> ToolResult:
        try:
            existed = workspace.delete(path)
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(data=existed, summary=f"deleted {path}" if existed else f"{path} did not exist")

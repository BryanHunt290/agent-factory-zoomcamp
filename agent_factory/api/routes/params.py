"""Path and query parameters, declared once with the contract's constraints.

FastAPI validates them before a handler runs. An invalid path ID becomes 404
(nothing can exist at it); an invalid query value becomes 422
``validation_failed`` (handlers.py). Cursors are checked by the cursor codec,
so every unusable cursor gets the same 400 ``invalid_cursor``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path, Query
from pydantic import AfterValidator, Field

from ..schemas.common import (
    APPROVAL_ID_PATTERN,
    PROJECT_ID_PATTERN,
    RUN_ID_PATTERN,
    MilestoneCategory,
    MilestoneStatus,
    ProjectId,
    Timestamp,
)


#: Every product path starts with the API version (contract rule: breaking changes go to /api/v2).
PREFIX = "/api/v1"


def _unique(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("each value may appear only once")
    return values


ProjectIdPath = Annotated[str, Path(pattern=PROJECT_ID_PATTERN, description="The project's stable ID.")]
RunIdPath = Annotated[str, Path(pattern=RUN_ID_PATTERN, description="The run's ID, unique within its project.")]
ApprovalIdPath = Annotated[str, Path(pattern=APPROVAL_ID_PATTERN, description="The approval request's ID.")]

Limit = Annotated[int, Query(ge=1, le=100, description="Maximum number of items on the page.")]
CursorParam = Annotated[str | None, Query(description="The next_cursor of the previous page.")]
ActivitySince = Annotated[Timestamp | None, Query(description="Count milestones reached at or after this time.")]
Since = Annotated[Timestamp | None, Query(description="Milestones reached at or after this time.")]
Until = Annotated[Timestamp | None, Query(description="Milestones reached before this time.")]
StatusFilter = Annotated[
    Annotated[list[MilestoneStatus], Field(max_length=5), AfterValidator(_unique)] | None,
    Query(description="Only milestones in these statuses (repeatable)."),
]
CategoryFilter = Annotated[
    Annotated[list[MilestoneCategory], Field(max_length=4), AfterValidator(_unique)] | None,
    Query(description="Only milestones in these categories (repeatable)."),
]
ProjectIdFilter = Annotated[ProjectId | None, Query(description="Only this project's approvals.")]

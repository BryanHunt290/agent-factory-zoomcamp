"""Milestones (contract schemas: Milestone, MilestonePage, StepRef, MilestoneSource)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from .common import (
    ApiModel,
    ApprovalId,
    Cursor,
    MilestoneCategory,
    MilestoneId,
    MilestoneStatus,
    MilestoneType,
    ProjectId,
    RunId,
    Timestamp,
)

Short = Annotated[str, StringConstraints(max_length=64)]


class StepRef(ApiModel):
    node: Short
    outcome: Short


class MilestoneSource(ApiModel):
    node: Short | None
    agent: Short | None


class Milestone(ApiModel):
    id: MilestoneId
    project_id: ProjectId
    run_id: RunId
    sequence: Annotated[int, Field(ge=1)]
    type: MilestoneType
    category: MilestoneCategory
    title: Annotated[str, StringConstraints(max_length=120)]
    summary: Annotated[str, StringConstraints(max_length=1000)]
    status: MilestoneStatus
    occurred_at: Timestamp
    source: MilestoneSource
    follows: StepRef | None
    resolves_milestone_id: MilestoneId | None
    approval_id: ApprovalId | None


class MilestonePage(ApiModel):
    items: list[Milestone]
    next_cursor: Cursor | None

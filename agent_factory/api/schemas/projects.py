"""Projects and templates (contract schemas: Project, ProjectSummary, Template, ...)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from .common import (
    ApiModel,
    Cursor,
    MilestoneCategory,
    MilestoneType,
    ProjectDescription,
    ProjectId,
    ProjectName,
    ProjectStatus,
    RequestModel,
    RunId,
    RunStatus,
    TemplateName,
    Timestamp,
)


class MilestoneTypeDefinition(ApiModel):
    type: MilestoneType
    title: Annotated[str, StringConstraints(max_length=120)]
    category: MilestoneCategory
    description: Annotated[str, StringConstraints(max_length=500)]
    order: Annotated[int, Field(ge=1)] | None


class Template(ApiModel):
    name: TemplateName
    description: Annotated[str, StringConstraints(max_length=500)]
    milestone_types: list[MilestoneTypeDefinition]


class TemplateList(ApiModel):
    items: list[Template]


class RunSummary(ApiModel):
    run_id: RunId
    number: Annotated[int, Field(ge=1)]
    status: RunStatus
    updated_at: Timestamp


class Activity(ApiModel):
    since: Timestamp
    milestones: Annotated[int, Field(ge=0)]


class ProjectSummary(ApiModel):
    id: ProjectId
    name: Annotated[str, StringConstraints(max_length=200)]
    template: TemplateName | None
    status: ProjectStatus
    latest_run: RunSummary | None
    pending_approvals: Annotated[int, Field(ge=0)]
    activity: Activity | None


class Project(ApiModel):
    id: ProjectId
    name: Annotated[str, StringConstraints(max_length=200)]
    description: Annotated[str, StringConstraints(max_length=2000)]
    template: TemplateName | None
    created_at: Timestamp
    status: ProjectStatus
    latest_run: RunSummary | None
    pending_approvals: Annotated[int, Field(ge=0)]
    activity: Activity | None
    milestone_types: list[MilestoneTypeDefinition]


class ProjectSummaryPage(ApiModel):
    items: list[ProjectSummary]
    next_cursor: Cursor | None


class CreateProjectRequest(RequestModel):
    """The only fields a client sends. The ID, time, and status are the server's."""

    name: ProjectName
    template: TemplateName
    description: ProjectDescription = ""

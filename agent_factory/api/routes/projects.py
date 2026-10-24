"""/api/v1/projects and /api/v1/templates.

The template catalog needs ``projects:read``: it describes what kinds of project
exist and holds no project data, so a permission of its own would add nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ...domain.principals import Permission
from .. import schemas as api
from ..auth import require_permission
from ..dependencies import IdempotencyKeyDep, ProjectServiceDep, json_body
from .params import PREFIX, ActivitySince, CursorParam, Limit, ProjectIdPath

router = APIRouter()

CreateProjectBody = Annotated[api.CreateProjectRequest, Depends(json_body(api.CreateProjectRequest))]
READ = [Depends(require_permission(Permission.PROJECTS_READ))]


@router.get(
    PREFIX + "/projects",
    operation_id="listProjects",
    response_model=api.ProjectSummaryPage,
    tags=["projects"],
    dependencies=READ,
)
def list_projects(
    service: ProjectServiceDep, activity_since: ActivitySince = None, limit: Limit = 50, cursor: CursorParam = None
) -> api.ProjectSummaryPage:
    return service.list_projects(activity_since=activity_since, limit=limit, cursor=cursor)


@router.post(
    PREFIX + "/projects",
    operation_id="createProject",
    response_model=api.Project,
    status_code=201,
    tags=["projects"],
    dependencies=[Depends(require_permission(Permission.PROJECTS_CREATE))],
)
def create_project(
    response: Response, service: ProjectServiceDep, key: IdempotencyKeyDep, body: CreateProjectBody
) -> api.Project:
    result = service.create_project(key, body)
    response.status_code = result.status
    response.headers["Location"] = f"/api/v1/projects/{result.project.id}"
    if result.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return result.project


@router.get(
    PREFIX + "/projects/{project_id}",
    operation_id="getProject",
    response_model=api.Project,
    tags=["projects"],
    dependencies=READ,
)
def get_project(project_id: ProjectIdPath, service: ProjectServiceDep, activity_since: ActivitySince = None) -> api.Project:
    return service.get_project(project_id, activity_since=activity_since)


@router.get(
    PREFIX + "/templates",
    operation_id="listTemplates",
    response_model=api.TemplateList,
    tags=["templates"],
    dependencies=READ,
)
def list_templates(service: ProjectServiceDep) -> api.TemplateList:
    return service.list_templates()

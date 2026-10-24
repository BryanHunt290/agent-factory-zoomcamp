"""/api/v1/projects/{project_id}/runs and its milestones. GET only: workers own execution."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...domain.principals import Permission
from .. import schemas as api
from ..auth import require_permission
from ..dependencies import MilestoneServiceDep, RunServiceDep
from .params import PREFIX, CategoryFilter, CursorParam, Limit, ProjectIdPath, RunIdPath, Since, StatusFilter, Until

router = APIRouter()
RUNS = [Depends(require_permission(Permission.RUNS_READ))]
MILESTONES = [Depends(require_permission(Permission.MILESTONES_READ))]


@router.get(
    PREFIX + "/projects/{project_id}/runs", operation_id="listRuns", response_model=api.RunPage, tags=["runs"], dependencies=RUNS
)
def list_runs(
    project_id: ProjectIdPath, service: RunServiceDep, limit: Limit = 50, cursor: CursorParam = None
) -> api.RunPage:
    return service.list_runs(project_id, limit=limit, cursor=cursor)


@router.get(
    PREFIX + "/projects/{project_id}/runs/{run_id}",
    operation_id="getRun",
    response_model=api.Run,
    tags=["runs"],
    dependencies=RUNS,
)
def get_run(project_id: ProjectIdPath, run_id: RunIdPath, service: RunServiceDep) -> api.Run:
    return service.get_run(project_id, run_id)


@router.get(
    PREFIX + "/projects/{project_id}/milestones",
    operation_id="listProjectMilestones",
    response_model=api.MilestonePage,
    tags=["milestones"],
    dependencies=MILESTONES,
)
def list_project_milestones(
    project_id: ProjectIdPath,
    service: MilestoneServiceDep,
    since: Since = None,
    until: Until = None,
    status: StatusFilter = None,
    category: CategoryFilter = None,
    limit: Limit = 50,
    cursor: CursorParam = None,
) -> api.MilestonePage:
    return service.list_milestones(
        project_id, since=since, until=until, statuses=status, categories=category, limit=limit, cursor=cursor
    )


@router.get(
    PREFIX + "/projects/{project_id}/runs/{run_id}/milestones",
    operation_id="listRunMilestones",
    response_model=api.MilestonePage,
    tags=["milestones"],
    dependencies=MILESTONES,
)
def list_run_milestones(
    project_id: ProjectIdPath,
    run_id: RunIdPath,
    service: MilestoneServiceDep,
    since: Since = None,
    until: Until = None,
    status: StatusFilter = None,
    category: CategoryFilter = None,
    limit: Limit = 50,
    cursor: CursorParam = None,
) -> api.MilestonePage:
    return service.list_milestones(
        project_id,
        run_id=run_id,
        since=since,
        until=until,
        statuses=status,
        categories=category,
        limit=limit,
        cursor=cursor,
    )

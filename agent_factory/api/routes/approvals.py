"""/api/v1/approvals, /api/v1/attention, and /api/v1/projects/{project_id}/approvals/{approval_id}[/decision|/reconciliation]."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from ...domain.principals import Permission
from .. import schemas as api
from ..auth import AuthenticatedPrincipal, decision_quota, reconciliation_quota, require_permission
from ..dependencies import ApprovalServiceDep, json_body
from .params import PREFIX, ApprovalIdPath, CursorParam, Limit, ProjectIdFilter, ProjectIdPath

router = APIRouter()

DecisionBody = Annotated[api.ApprovalDecisionRequest, Depends(json_body(api.ApprovalDecisionRequest))]
ReconciliationBody = Annotated[api.ReconciliationRequest, Depends(json_body(api.ReconciliationRequest))]
READ = [Depends(require_permission(Permission.APPROVALS_READ))]
#: approvals:decide, then the per-principal decision quota (auth/policy.py).
DecidingPrincipal = Annotated[AuthenticatedPrincipal, Depends(decision_quota)]
#: actions:reconcile, then the same per-principal quota.
ReconcilingPrincipal = Annotated[AuthenticatedPrincipal, Depends(reconciliation_quota)]


@router.get(
    PREFIX + "/approvals",
    operation_id="listPendingApprovals",
    response_model=api.ApprovalRequestPage,
    tags=["approvals"],
    dependencies=READ,
)
def list_pending_approvals(
    service: ApprovalServiceDep,
    status: Annotated[Literal["pending"], Query(description="v1 supports only pending.")],
    project_id: ProjectIdFilter = None,
    limit: Limit = 50,
    cursor: CursorParam = None,
) -> api.ApprovalRequestPage:
    return service.list_pending(project_id=project_id, limit=limit, cursor=cursor)


@router.get(
    PREFIX + "/projects/{project_id}/approvals/{approval_id}",
    operation_id="getApproval",
    response_model=api.ApprovalRequest,
    tags=["approvals"],
    dependencies=READ,
)
def get_approval(project_id: ProjectIdPath, approval_id: ApprovalIdPath, service: ApprovalServiceDep) -> api.ApprovalRequest:
    return service.get_approval(project_id, approval_id)


@router.post(
    PREFIX + "/projects/{project_id}/approvals/{approval_id}/decision",
    operation_id="decideApproval",
    response_model=api.ApprovalRequest,
    status_code=201,
    tags=["approvals"],
)
def decide_approval(
    principal: DecidingPrincipal,
    project_id: ProjectIdPath,
    approval_id: ApprovalIdPath,
    response: Response,
    service: ApprovalServiceDep,
    body: DecisionBody,
) -> api.ApprovalRequest:
    """Record the authenticated principal's decision. 201 when recorded now, 200 for an identical repeat.

    Executes nothing: approval is not execution (docs/design/action-execution.md).
    """
    result = service.decide(project_id, approval_id, body, principal.actor())
    if result.recorded_now:
        response.headers["Location"] = f"/api/v1/projects/{project_id}/approvals/{approval_id}"
    else:
        response.status_code = 200
    return result.approval


@router.post(
    PREFIX + "/projects/{project_id}/approvals/{approval_id}/reconciliation",
    operation_id="reconcileAction",
    response_model=api.ApprovalRequest,
    status_code=201,
    tags=["approvals"],
)
def reconcile_action(
    principal: ReconcilingPrincipal,
    project_id: ProjectIdPath,
    approval_id: ApprovalIdPath,
    response: Response,
    service: ApprovalServiceDep,
    body: ReconciliationBody,
) -> api.ApprovalRequest:
    """Record the authenticated principal's answer about an uncertain action. 201 when recorded now, 200 for a repeat.

    Executes nothing: a worker applies the answer through the execution ledger (docs/design/action-execution.md).
    """
    result = service.reconcile(project_id, approval_id, body, principal.actor())
    if result.recorded_now:
        response.headers["Location"] = f"/api/v1/projects/{project_id}/approvals/{approval_id}"
    else:
        response.status_code = 200
    return result.approval


@router.get(
    PREFIX + "/attention",
    operation_id="listAttention",
    response_model=api.AttentionPage,
    tags=["approvals"],
    dependencies=READ,
)
def list_attention(
    service: ApprovalServiceDep,
    project_id: ProjectIdFilter = None,
    limit: Limit = 50,
    cursor: CursorParam = None,
) -> api.AttentionPage:
    return service.list_attention(project_id=project_id, limit=limit, cursor=cursor)

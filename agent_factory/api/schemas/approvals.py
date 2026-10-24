"""Approvals (contract schemas: ApprovalRequest, ApprovalDecision, Actor, ExactAction, ...).

And, since E5, what happened to an approved action (ActionExecution,
Reconciliation, TimelineEntry), the reconciliation request, and the attention
inbox (AttentionItem).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints

from .common import (
    ActionCategory,
    ActionFingerprint,
    ActorKind,
    ApiModel,
    ApprovalId,
    ApprovalStatus,
    AttentionKind,
    DecisionComment,
    DecisionId,
    DecisionOutcome,
    Cursor,
    ExecutionId,
    ExecutionState,
    NotificationFailure,
    NotificationState,
    ProjectId,
    ReconciliationChoice,
    RequestModel,
    RunId,
    TimelineKind,
    Timestamp,
    WorkerId,
)

AttemptNumber = Annotated[int, Field(ge=1)]


class ExactAction(ApiModel):
    tool: Annotated[str, StringConstraints(max_length=64)]
    category: ActionCategory
    #: Exactly as the engine will use them. Never redacted: the approver must see what they approve.
    params: dict[str, Any]


class Actor(ApiModel):
    kind: ActorKind
    id: Annotated[str, StringConstraints(max_length=200)]
    display_name: Annotated[str, StringConstraints(max_length=200)]
    verified: bool


class ApprovalDecision(ApiModel):
    id: DecisionId
    outcome: DecisionOutcome
    action_fingerprint: ActionFingerprint
    decided_at: Timestamp
    decided_by: Actor
    comment: Annotated[str, StringConstraints(max_length=1000)]


class WorkerRef(ApiModel):
    worker_id: WorkerId
    fencing_token: Annotated[int, Field(ge=1)]


class TimelineEntry(ApiModel):
    kind: TimelineKind
    at: Timestamp
    attempt: AttemptNumber | None
    actor: Actor | None
    worker: WorkerRef | None
    outcome: Annotated[str, StringConstraints(max_length=32)] | None
    until: Timestamp | None


class Reconciliation(ApiModel):
    attempt: AttemptNumber
    choice: ReconciliationChoice
    decided_at: Timestamp
    decided_by: Actor
    comment: Annotated[str, StringConstraints(max_length=1000)]
    applied: bool


class ActionExecution(ApiModel):
    id: ExecutionId
    state: ExecutionState
    attempt: Annotated[int, Field(ge=0)]
    retry_authorized_until: Timestamp | None
    reconciliation: Reconciliation | None


class NotificationStatus(ApiModel):
    """Whether people were told about one attention event. No recipient, address, or message text."""

    about: AttentionKind
    attempt: AttemptNumber | None
    state: NotificationState
    deliveries: Annotated[int, Field(ge=1)]
    sent: Annotated[int, Field(ge=0)]
    failed: Annotated[int, Field(ge=0)]
    created_at: Timestamp
    last_attempt_at: Timestamp | None
    sent_at: Timestamp | None
    outcome_unknown: bool
    failure: NotificationFailure | None


class ApprovalRequest(ApiModel):
    id: ApprovalId
    project_id: ProjectId
    project_name: Annotated[str, StringConstraints(max_length=200)]
    run_id: RunId
    node: Annotated[str, StringConstraints(max_length=64)]
    agent: Annotated[str, StringConstraints(max_length=64)]
    action: ExactAction
    action_fingerprint: ActionFingerprint
    reason: Annotated[str, StringConstraints(max_length=500)]
    risk: Annotated[str, StringConstraints(max_length=500)]
    status: ApprovalStatus
    requested_at: Timestamp
    expires_at: Timestamp | None
    decision: ApprovalDecision | None
    action_summary: Annotated[str, StringConstraints(max_length=300)]
    attention: AttentionKind | None
    execution: ActionExecution | None
    notifications: Annotated[list[NotificationStatus], Field(max_length=50)]
    timeline: Annotated[list[TimelineEntry], Field(max_length=200)]


class ApprovalRequestPage(ApiModel):
    items: list[ApprovalRequest]
    next_cursor: Cursor | None


class AttentionItem(ApiModel):
    kind: AttentionKind
    project_id: ProjectId
    project_name: Annotated[str, StringConstraints(max_length=200)]
    run_id: RunId
    approval_id: ApprovalId
    execution_id: ExecutionId | None
    attempt: AttemptNumber | None
    tool: Annotated[str, StringConstraints(max_length=64)]
    category: ActionCategory
    summary: Annotated[str, StringConstraints(max_length=300)]
    since: Timestamp
    expires_at: Timestamp | None


class AttentionPage(ApiModel):
    items: list[AttentionItem]
    next_cursor: Cursor | None


class ApprovalDecisionRequest(RequestModel):
    """The only fields a client sends. No time, actor, or verification field exists to send."""

    outcome: DecisionOutcome
    action_fingerprint: ActionFingerprint
    comment: DecisionComment = ""


class ReconciliationRequest(RequestModel):
    """The only fields a client sends: exactly which attempt of which action, and the answer. No time or actor."""

    execution_id: ExecutionId
    attempt: AttemptNumber
    action_fingerprint: ActionFingerprint
    choice: ReconciliationChoice
    comment: DecisionComment = ""

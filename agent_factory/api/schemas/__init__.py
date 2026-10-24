"""API models: the request and response shapes of openapi.yaml, as Pydantic models.

They are deliberately separate from the domain model (agent_factory.domain)
and from the database rows (agent_factory.persistence). Each class is named
after the contract schema it implements, and tests/test_api_conformance.py
checks that their fields match the checked-in openapi.yaml, which stays the
source of truth.
"""

from .approvals import (
    ActionExecution,
    Actor,
    ApprovalDecision,
    ApprovalDecisionRequest,
    ApprovalRequest,
    ApprovalRequestPage,
    AttentionItem,
    AttentionPage,
    ExactAction,
    NotificationStatus,
    Reconciliation,
    ReconciliationRequest,
    TimelineEntry,
    WorkerRef,
)
from .milestones import Milestone, MilestonePage, MilestoneSource, StepRef
from .problems import FieldError, Problem
from .projects import (
    Activity,
    CreateProjectRequest,
    MilestoneTypeDefinition,
    Project,
    ProjectSummary,
    ProjectSummaryPage,
    RunSummary,
    Template,
    TemplateList,
)
from .runs import ModelUsage, ModelUsageByModel, Run, RunPage

#: Contract schema name -> model, for the conformance tests.
MODELS = {
    model.__name__: model
    for model in (
        ActionExecution,
        Activity,
        Actor,
        ApprovalDecision,
        ApprovalDecisionRequest,
        ApprovalRequest,
        ApprovalRequestPage,
        AttentionItem,
        AttentionPage,
        CreateProjectRequest,
        ExactAction,
        FieldError,
        Milestone,
        MilestonePage,
        MilestoneSource,
        MilestoneTypeDefinition,
        ModelUsage,
        ModelUsageByModel,
        NotificationStatus,
        Problem,
        Project,
        ProjectSummary,
        ProjectSummaryPage,
        Reconciliation,
        ReconciliationRequest,
        Run,
        RunPage,
        RunSummary,
        StepRef,
        Template,
        TemplateList,
        TimelineEntry,
        WorkerRef,
    )
}

__all__ = [
    "MODELS",
    "ActionExecution",
    "Activity",
    "Actor",
    "ApprovalDecision",
    "ApprovalDecisionRequest",
    "ApprovalRequest",
    "ApprovalRequestPage",
    "AttentionItem",
    "AttentionPage",
    "CreateProjectRequest",
    "ExactAction",
    "FieldError",
    "Milestone",
    "MilestonePage",
    "MilestoneSource",
    "MilestoneTypeDefinition",
    "ModelUsage",
    "ModelUsageByModel",
    "NotificationStatus",
    "Problem",
    "Project",
    "ProjectSummary",
    "ProjectSummaryPage",
    "Reconciliation",
    "ReconciliationRequest",
    "Run",
    "RunPage",
    "RunSummary",
    "StepRef",
    "Template",
    "TemplateList",
    "TimelineEntry",
    "WorkerRef",
]

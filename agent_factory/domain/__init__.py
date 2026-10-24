"""Product domain model: projects, runs, milestones, approvals.

This package is the foundation for future storage, API, dashboard, mobile
approval, notifications, and overnight summaries. It has no database, API, or
UI dependencies. See docs/product-domain.md.
"""

from .approvals import (
    APPROVAL_TRANSITIONS,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    ExactAction,
    risk_for,
)
from .errors import (
    ApprovalBindingError,
    ApprovalExpiredError,
    ConflictError,
    DomainError,
    DomainValidationError,
    RecordNotFoundError,
    SchemaVersionError,
    StatusTransitionError,
)
from .idempotency import IdempotencyRecord, request_hash
from .identity import Actor, ActorKind
from .ids import decision_id, milestone_id, new_principal_id, new_project_id
from .milestones import (
    HUMAN_DECISION,
    MILESTONE_TRANSITIONS,
    RUN_FINISHED,
    Milestone,
    MilestoneCatalog,
    MilestoneCategory,
    MilestoneDefinition,
    MilestonePolicy,
    MilestoneSource,
    MilestoneStatus,
    NodeMilestoneRule,
    milestone_policy,
    standard_milestone_policy,
)
from .principals import ALL_PERMISSIONS, Permission, Principal, PrincipalStatus
from .project import RUN_TRANSITIONS, Project, Run
from .repositories import MilestoneQuery, ProductStore, in_memory_store
from .services import ApprovalService, DecisionResult

__all__ = [
    "ALL_PERMISSIONS",
    "APPROVAL_TRANSITIONS",
    "HUMAN_DECISION",
    "MILESTONE_TRANSITIONS",
    "RUN_FINISHED",
    "RUN_TRANSITIONS",
    "Actor",
    "ActorKind",
    "ApprovalBindingError",
    "ApprovalDecision",
    "ApprovalExpiredError",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalService",
    "ApprovalStatus",
    "ConflictError",
    "DecisionResult",
    "DomainError",
    "DomainValidationError",
    "ExactAction",
    "IdempotencyRecord",
    "Milestone",
    "MilestoneCatalog",
    "MilestoneCategory",
    "MilestoneDefinition",
    "MilestonePolicy",
    "MilestoneQuery",
    "MilestoneSource",
    "MilestoneStatus",
    "NodeMilestoneRule",
    "Permission",
    "Principal",
    "PrincipalStatus",
    "ProductStore",
    "Project",
    "RecordNotFoundError",
    "Run",
    "SchemaVersionError",
    "StatusTransitionError",
    "decision_id",
    "in_memory_store",
    "milestone_id",
    "milestone_policy",
    "new_principal_id",
    "new_project_id",
    "request_hash",
    "risk_for",
    "standard_milestone_policy",
]

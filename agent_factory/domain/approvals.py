"""Approval requests and decisions: the product-level record of exact-action approval.

The engine's v0.2 rule is unchanged: an approval applies to one exact action.
Here that rule becomes data:

* :class:`ExactAction` is the tool, category, and parameters of the proposed
  action. Its ``fingerprint`` is a SHA-256 over all three.
* :class:`ApprovalRequest` stores the action and its fingerprint. Its ID is the
  engine's own request ID (a hash of run, node, visit, tool, and parameters).
* :class:`ApprovalDecision` repeats the fingerprint the decider saw. A
  decision whose fingerprint differs from the request's is refused, so
  approving action A can never authorize action B.
* **Approvals expire** (:data:`APPROVAL_VALIDITY`). A request can be decided,
  and an approved action can begin, only before ``expires_at``: server time,
  and the expiry instant itself counts as expired. Expiry never renews; asking
  again is a new request, from a new visit of the workflow step.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..safety.permissions import ActionCategory
from .errors import DomainValidationError, StatusTransitionError
from .identity import Actor
from .ids import APPROVAL_ID_RE, DECISION_ID_RE, PROJECT_ID_RE, RUN_ID_RE, decision_id
from .serialization import (
    RecordMixin,
    canonical,
    check_enum,
    check_fields,
    check_json_object,
    check_optional_timestamp,
    check_pattern,
    check_text,
    check_timestamp,
    format_timestamp,
    open_envelope,
    parse_timestamp,
    seal,
)

FINGERPRINT_PREFIX = "sha256:"

#: How long after it was requested an approval can be decided, and an approved action can begin
#: (docs/product-domain.md#approval-expiry). Long enough for a request raised overnight to be decided
#: the next morning; short enough that a stale request cannot be approved days later.
APPROVAL_VALIDITY = timedelta(hours=12)


def approval_expiry(requested_at: str, validity: timedelta = APPROVAL_VALIDITY) -> str:
    """When a request made at ``requested_at`` expires."""
    return format_timestamp(parse_timestamp(check_timestamp(requested_at, "requested_at")) + validity)


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: The request outlived its validity window. Stale approvals must not be
    #: actionable, e.g. a link opened days later from a phone.
    EXPIRED = "expired"
    #: The request no longer applies, e.g. its run was cancelled.
    CANCELLED = "cancelled"


#: Only a pending request can change, and only once.
APPROVAL_TRANSITIONS: Mapping[ApprovalStatus, frozenset[ApprovalStatus]] = MappingProxyType(
    {
        ApprovalStatus.PENDING: frozenset(
            {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED}
        ),
        ApprovalStatus.APPROVED: frozenset(),
        ApprovalStatus.REJECTED: frozenset(),
        ApprovalStatus.EXPIRED: frozenset(),
        ApprovalStatus.CANCELLED: frozenset(),
    }
)


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def status(self) -> ApprovalStatus:
        return ApprovalStatus(self.value)


RISK_BY_CATEGORY: Mapping[str, str] = MappingProxyType(
    {
        ActionCategory.DESTRUCTIVE_DELETE.value: "Permanently deletes data; Agent Factory cannot undo it.",
        ActionCategory.SPEND_MONEY.value: "Spends money.",
        ActionCategory.PRODUCTION_DEPLOY.value: "Changes what real users run in production.",
        ActionCategory.CREDENTIAL_CHANGE.value: "Creates, changes, or revokes credentials or access.",
        ActionCategory.EXTERNAL_COMMUNICATION.value: "Sends information to people or services outside this machine.",
        ActionCategory.NETWORK_ACCESS.value: "Opens network connections from the project environment.",
    }
)


def risk_for(category: str) -> str:
    """Plain-language risk for an action category, shown with every approval request."""
    return RISK_BY_CATEGORY.get(category, "Requires human approval under the project's permission policy.")


@dataclass(frozen=True)
class ExactAction:
    """The exact proposed action. Every field is part of the fingerprint."""

    tool: str
    category: str
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_text(self.tool, "action.tool", max_len=64)
        object.__setattr__(self, "category", check_enum(ActionCategory, self.category, "action.category").value)
        object.__setattr__(self, "params", check_json_object(self.params, "action.params"))

    @property
    def fingerprint(self) -> str:
        material = canonical({"tool": self.tool, "category": self.category, "params": self.params})
        return FINGERPRINT_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "category": self.category, "params": self.params}

    @classmethod
    def from_dict(cls, data: Any) -> ExactAction:
        if not isinstance(data, dict):
            raise DomainValidationError("action: expected an object")
        check_fields(data, required=("tool", "category", "params"), where="action")
        return cls(**data)


def _check_fingerprint(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.startswith(FINGERPRINT_PREFIX) or len(value) != len(FINGERPRINT_PREFIX) + 64:
        raise DomainValidationError(f"{field_name}: expected sha256:<64 hex>, got {value!r}")
    return value


_REQUEST_IDENTITY = (
    "id", "project_id", "run_id", "node", "agent", "action", "action_fingerprint", "reason", "risk",
    "requested_at", "context", "expires_at",
)  # fmt: skip


@dataclass(frozen=True)
class ApprovalRequest(RecordMixin):
    """A protected action waiting for, or having received, a human decision."""

    RECORD_TYPE = "approval_request"
    SCHEMA_VERSION = 1

    id: str
    project_id: str
    run_id: str
    node: str
    agent: str
    action: ExactAction
    action_fingerprint: str
    reason: str
    risk: str
    requested_at: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    context: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None
    updated_at: str | None = None  # when the status last changed

    def __post_init__(self) -> None:
        check_pattern(self.id, APPROVAL_ID_RE, "id")
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.run_id, RUN_ID_RE, "run_id")
        check_text(self.node, "node", max_len=64)
        check_text(self.agent, "agent", max_len=64)
        if not isinstance(self.action, ExactAction):
            raise DomainValidationError("action: expected an ExactAction")
        _check_fingerprint(self.action_fingerprint, "action_fingerprint")
        if self.action_fingerprint != self.action.fingerprint:
            raise DomainValidationError("action_fingerprint: does not match the action")
        check_text(self.reason, "reason", max_len=500)
        check_text(self.risk, "risk", max_len=500)
        object.__setattr__(self, "requested_at", check_timestamp(self.requested_at, "requested_at"))
        object.__setattr__(self, "status", check_enum(ApprovalStatus, self.status, "status"))
        object.__setattr__(self, "context", check_json_object(self.context, "context"))
        object.__setattr__(self, "expires_at", check_optional_timestamp(self.expires_at, "expires_at"))
        if self.expires_at and parse_timestamp(self.expires_at) <= parse_timestamp(self.requested_at):
            raise DomainValidationError("expires_at: must be later than requested_at")
        if self.status is ApprovalStatus.PENDING and self.expires_at is None:
            raise DomainValidationError("expires_at: a pending request always expires; approvals are never permanent")
        object.__setattr__(self, "updated_at", check_optional_timestamp(self.updated_at, "updated_at"))
        if self.status is ApprovalStatus.PENDING and self.updated_at is not None:
            raise DomainValidationError("updated_at: a pending request has not changed status yet")
        if self.status is not ApprovalStatus.PENDING and self.updated_at is None:
            raise DomainValidationError("updated_at: required once a request has a final status")

    def is_expired(self, at: str) -> bool:
        """At or after ``expires_at``: the expiry instant itself belongs to expiry."""
        return self.expires_at is not None and parse_timestamp(at) >= parse_timestamp(self.expires_at)

    def transition(self, status: ApprovalStatus, *, at: str) -> ApprovalRequest:
        status = check_enum(ApprovalStatus, status, "status")
        if status not in APPROVAL_TRANSITIONS[self.status]:
            raise StatusTransitionError(f"approval {self.id}: {self.status.value} -> {status.value} is not allowed")
        return replace(self, status=status, updated_at=check_timestamp(at, "at"))

    @staticmethod
    def check_successor(old: ApprovalRequest, new: ApprovalRequest) -> None:
        """Raise unless ``new`` is a legal next version of ``old`` (used by repositories)."""
        for name in _REQUEST_IDENTITY:
            if getattr(old, name) != getattr(new, name):
                raise DomainValidationError(f"approval {old.id}: field {name!r} cannot change")
        if new.status not in APPROVAL_TRANSITIONS[old.status]:
            raise StatusTransitionError(f"approval {old.id}: {old.status.value} -> {new.status.value} is not allowed")

    def to_dict(self) -> dict[str, Any]:
        return seal(
            self.RECORD_TYPE,
            self.SCHEMA_VERSION,
            {
                "id": self.id,
                "project_id": self.project_id,
                "run_id": self.run_id,
                "node": self.node,
                "agent": self.agent,
                "action": self.action.to_dict(),
                "action_fingerprint": self.action_fingerprint,
                "reason": self.reason,
                "risk": self.risk,
                "requested_at": self.requested_at,
                "status": self.status.value,
                "context": self.context,
                "expires_at": self.expires_at,
                "updated_at": self.updated_at,
            },
        )

    @classmethod
    def from_dict(cls, data: Any) -> ApprovalRequest:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(body, required=(*_REQUEST_IDENTITY, "status", "updated_at"), where=cls.RECORD_TYPE)
        return cls(**{**body, "action": ExactAction.from_dict(body["action"])})


@dataclass(frozen=True)
class ApprovalDecision(RecordMixin):
    """The single, final human decision on one approval request."""

    RECORD_TYPE = "approval_decision"
    SCHEMA_VERSION = 1

    id: str
    project_id: str
    request_id: str
    action_fingerprint: str  # what the decider was shown; must equal the request's
    outcome: ApprovalOutcome
    decided_at: str
    actor: Actor
    comment: str = ""

    def __post_init__(self) -> None:
        check_pattern(self.id, DECISION_ID_RE, "id")
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.request_id, APPROVAL_ID_RE, "request_id")
        if self.id != decision_id(self.project_id, self.request_id):
            raise DomainValidationError("id: does not belong to this request")
        _check_fingerprint(self.action_fingerprint, "action_fingerprint")
        object.__setattr__(self, "outcome", check_enum(ApprovalOutcome, self.outcome, "outcome"))
        object.__setattr__(self, "decided_at", check_timestamp(self.decided_at, "decided_at"))
        if not isinstance(self.actor, Actor):
            raise DomainValidationError("actor: expected an Actor")
        check_text(self.comment, "comment", max_len=1000, allow_empty=True, multiline=True)

    def to_dict(self) -> dict[str, Any]:
        return seal(
            self.RECORD_TYPE,
            self.SCHEMA_VERSION,
            {
                "id": self.id,
                "project_id": self.project_id,
                "request_id": self.request_id,
                "action_fingerprint": self.action_fingerprint,
                "outcome": self.outcome.value,
                "decided_at": self.decided_at,
                "actor": self.actor.to_dict(),
                "comment": self.comment,
            },
        )

    @classmethod
    def from_dict(cls, data: Any) -> ApprovalDecision:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(
            body,
            required=("id", "project_id", "request_id", "action_fingerprint", "outcome", "decided_at", "actor", "comment"),
            where=cls.RECORD_TYPE,
        )
        return cls(**{**body, "actor": Actor.from_dict(body["actor"])})

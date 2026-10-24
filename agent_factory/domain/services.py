"""Domain services: rules that span several records."""

from __future__ import annotations

from dataclasses import dataclass

from ..util import Clock, timestamp, utc_now
from .approvals import ApprovalDecision, ApprovalOutcome, ApprovalRequest, ApprovalStatus
from .errors import (
    ApprovalBindingError,
    ApprovalExpiredError,
    ConflictError,
    RecordNotFoundError,
    StatusTransitionError,
)
from .identity import Actor
from .ids import decision_id
from .repositories import ApprovalRepository


@dataclass(frozen=True)
class DecisionResult:
    """The decision on record, and whether this call is the one that recorded it."""

    decision: ApprovalDecision
    recorded_now: bool


class ApprovalService:
    """Opens approval requests and records decisions, enforcing exact-action binding."""

    def __init__(self, repository: ApprovalRepository, clock: Clock = utc_now) -> None:
        self.repository = repository
        self._clock = clock

    def open_request(self, request: ApprovalRequest) -> ApprovalRequest:
        """Store a pending request. Re-opening the identical request is a no-op."""
        existing = self.repository.get_request(request.project_id, request.id)
        if existing is None:
            if request.status is not ApprovalStatus.PENDING:
                raise StatusTransitionError(f"approval {request.id}: new requests must be pending")
            try:
                self.repository.add_request(request)
                return request
            except ConflictError:  # opened concurrently by another writer: compare with theirs
                existing = self.repository.get_request(request.project_id, request.id)
                if existing is None:
                    raise
        if existing.action_fingerprint != request.action_fingerprint or existing.run_id != request.run_id:
            raise ConflictError(f"approval {request.id} already exists for a different action")
        return existing

    def decide(
        self,
        *,
        project_id: str,
        request_id: str,
        action_fingerprint: str,
        outcome: ApprovalOutcome,
        actor: Actor,
        comment: str = "",
        at: str | None = None,
    ) -> ApprovalDecision:
        """Record the decision for one exact action.

        ``action_fingerprint`` is the fingerprint of the action the decider was
        shown. If it differs from the request's, the decision is refused:
        approving A can never authorize B. An identical repeat of an existing
        decision (same outcome, same actor) returns the original; anything else
        is a conflict.
        """
        return self.record(
            project_id=project_id,
            request_id=request_id,
            action_fingerprint=action_fingerprint,
            outcome=outcome,
            actor=actor,
            comment=comment,
            at=at,
        ).decision

    def record(
        self,
        *,
        project_id: str,
        request_id: str,
        action_fingerprint: str,
        outcome: ApprovalOutcome,
        actor: Actor,
        comment: str = "",
        at: str | None = None,
    ) -> DecisionResult:
        """Like :meth:`decide`, but also says whether this call recorded the decision.

        ``recorded_now`` is False for an identical repeat, including one that
        lost a race to an identical concurrent decision. The HTTP API answers
        201 for the first and 200 for a repeat.
        """
        request = self.repository.get_request(project_id, request_id)
        if request is None:
            raise RecordNotFoundError(f"approval request {request_id} does not exist")
        if action_fingerprint != request.action_fingerprint:
            raise ApprovalBindingError(f"approval {request_id}: the decision is for a different action")
        outcome = ApprovalOutcome(outcome)
        existing = self.repository.get_decision(project_id, request_id)
        if existing is not None:
            return DecisionResult(self._same_decision_or_conflict(existing, outcome, actor), recorded_now=False)
        if request.status is not ApprovalStatus.PENDING:
            raise StatusTransitionError(f"approval {request_id} is {request.status.value} and can no longer be decided")
        now = at or timestamp(self._clock)
        if request.is_expired(now):
            self.repository.update_request(request.transition(ApprovalStatus.EXPIRED, at=now))
            raise ApprovalExpiredError(f"approval {request_id} expired at {request.expires_at}")
        decision = ApprovalDecision(
            id=decision_id(project_id, request_id),
            project_id=project_id,
            request_id=request_id,
            action_fingerprint=action_fingerprint,
            outcome=outcome,
            decided_at=now,
            actor=actor,
            comment=comment,
        )
        try:
            self.repository.save_decision(decision, request.transition(outcome.status, at=now))
        except ConflictError as exc:
            # Another writer decided first (two devices, a double tap). Treat it
            # exactly as if their decision had been there when we looked.
            raced = self.repository.get_decision(project_id, request_id)
            if raced is None or isinstance(exc, ApprovalBindingError):
                raise
            return DecisionResult(self._same_decision_or_conflict(raced, outcome, actor), recorded_now=False)
        return DecisionResult(decision, recorded_now=True)

    @staticmethod
    def _same_decision_or_conflict(existing: ApprovalDecision, outcome: ApprovalOutcome, actor: Actor) -> ApprovalDecision:
        if existing.outcome is outcome and existing.actor.id == actor.id:
            return existing
        raise ConflictError(f"approval {existing.request_id} was already decided ({existing.outcome.value})")

    def expire(self, *, project_id: str, request_id: str, at: str | None = None) -> ApprovalRequest:
        return self._close(project_id, request_id, ApprovalStatus.EXPIRED, at)

    def cancel(self, *, project_id: str, request_id: str, at: str | None = None) -> ApprovalRequest:
        return self._close(project_id, request_id, ApprovalStatus.CANCELLED, at)

    def _close(self, project_id: str, request_id: str, status: ApprovalStatus, at: str | None) -> ApprovalRequest:
        request = self.repository.get_request(project_id, request_id)
        if request is None:
            raise RecordNotFoundError(f"approval request {request_id} does not exist")
        updated = request.transition(status, at=at or timestamp(self._clock))
        self.repository.update_request(updated)
        return updated

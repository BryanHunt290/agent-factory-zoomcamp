"""Approvals: listPendingApprovals, getApproval, decideApproval, reconcileAction, listAttention.

Recording a person's decision is the API's whole job here: an approval
decision, or a reconciliation answer ("did the uncertain action happen?").
Each commits together with the run's wake-up (``RunQueue.wake``, a
transactional outbox), so a worker claims the run within its poll interval.
The API never executes the approved action and never resumes the run: a worker
that holds the run's claim does that, through ``Executor.advance``, after
re-checking that the recorded decision still matches the exact action the
engine is paused on (docs/backend.md#approval-decisions). A stored "approved"
authorizes nothing by itself, and a stored answer changes no execution until
the worker applies it through the execution ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.approvals import ApprovalOutcome, ApprovalStatus
from ...domain.errors import (
    ApprovalBindingError,
    ApprovalExpiredError,
    ConflictError,
    RecordNotFoundError,
    StatusTransitionError,
)
from ...domain.executions import ReconciliationChoice, execution_id
from ...domain.identity import Actor
from ...domain.project import Project
from ...domain.reconciliation import ReconciliationNotRequired, ReconciliationService, StaleReconciliation
from ...domain.repositories import ProductStore
from ...domain.services import ApprovalService
from .. import schemas as api
from ..errors import ApiError, ErrorCode, not_found
from . import views
from .context import POSITION_ERRORS, Service, bad_position

EXPIRED_DETAIL = "This approval request expired before a decision was made."
CANCELLED_DETAIL = "This approval request no longer applies; it was cancelled."
MISMATCH_DETAIL = (
    "The decision is for a different action than this approval request. Reload the approval and decide again."
)
RECONCILE_MISMATCH_DETAIL = "The answer is for a different action than this approval. Reload the approval and answer again."
STALE_DETAIL = "This answer is about an attempt that is no longer the one waiting. Reload the approval and answer again."
NOT_REQUIRED_DETAIL = "Nothing about this action is waiting for an answer: its outcome is not uncertain."


@dataclass(frozen=True)
class DecisionResult:
    approval: api.ApprovalRequest
    recorded_now: bool  # False: an identical decision was already recorded (a safe retry)


class ApprovalApiService(Service):
    def list_pending(self, *, project_id: str | None, limit: int, cursor: str | None) -> api.ApprovalRequestPage:
        """Decidable requests (pending, not expired) across all projects (or one), oldest first."""
        filters = {"status": ApprovalStatus.PENDING.value, "project_id": project_id}
        position = self._position(cursor, "listPendingApprovals", filters)
        now = self.ctx.now()
        with self.ctx.stores.session() as store, store.snapshot():
            if project_id is not None and store.projects.get(project_id) is None:
                raise not_found(ErrorCode.PROJECT_NOT_FOUND)
            try:
                found = store.approvals.page_pending(
                    limit=limit + 1, project_id=project_id, after=tuple(position) if position else None, at=now
                )
            except POSITION_ERRORS:
                raise bad_position() from None
            page = found[:limit]
            projects = store.projects.get_many({r.project_id for r in page})
            notified = {r.id: store.notifications.list_for_approval(r.project_id, r.id) for r in page}
            unknown = store.notifications.with_unknown_outcome([n.id for rows in notified.values() for n in rows])
        last = page[-1] if page else None
        return api.ApprovalRequestPage(
            items=[
                views.approval(r, None, self._name(projects.get(r.project_id)), self.ctx.sanitize, now=now,
                               notification_rows=notified[r.id], unknown_notifications=unknown)  # fmt: skip
                for r in page
            ],
            next_cursor=self._next_cursor(
                len(found) > limit,
                "listPendingApprovals",
                filters,
                [last.requested_at, last.project_id, last.id] if last else [],
            ),
        )

    def get_approval(self, project_id: str, approval_id: str) -> api.ApprovalRequest:
        """The approval, its decision, and what happened to its action (execution and timeline), in one snapshot."""
        with self.ctx.stores.session() as store, store.snapshot():
            project = self._project(store, project_id)
            current = self._current(store, project, approval_id)
        if current is None:
            raise not_found(ErrorCode.APPROVAL_NOT_FOUND)
        return current

    def list_attention(self, *, project_id: str | None, limit: int, cursor: str | None) -> api.AttentionPage:
        """What needs a person now, across all projects (or one), oldest first: (since, project_id, approval_id)."""
        filters = {"project_id": project_id}
        position = self._position(cursor, "listAttention", filters)
        now = self.ctx.now()
        with self.ctx.stores.session() as store, store.snapshot():
            if project_id is not None and store.projects.get(project_id) is None:
                raise not_found(ErrorCode.PROJECT_NOT_FOUND)
            try:
                found = store.approvals.page_attention(
                    limit=limit + 1, at=now, project_id=project_id, after=tuple(position) if position else None
                )
            except POSITION_ERRORS:
                raise bad_position() from None
            page = found[:limit]
            projects = store.projects.get_many({i.project_id for i in page})
        last = page[-1] if page else None
        return api.AttentionPage(
            items=[views.attention_item(i, self._name(projects.get(i.project_id)), self.ctx.sanitize) for i in page],
            next_cursor=self._next_cursor(len(found) > limit, "listAttention", filters, list(last.position) if last else []),
        )

    def decide(
        self, project_id: str, approval_id: str, body: api.ApprovalDecisionRequest, actor: Actor
    ) -> DecisionResult:
        """Record one decision for one exact action, or explain why not. Executes nothing.

        The server, never the request, supplies the decision time (its clock)
        and the actor: the authenticated principal, verified by
        ``require_permission`` before this runs. Authentication proves who
        decided; the fingerprint proves what they decided. The domain's
        ApprovalService enforces the rest: the fingerprint must equal the
        stored request's, a request has at most one decision, an identical
        repeat must come from the same principal, and concurrent decisions
        settle as if sequential.
        """
        with self.ctx.stores.session() as store:
            project = self._project(store, project_id)
            approvals = ApprovalService(store.approvals, clock=self.ctx.clock)
            expired = False
            try:
                with store.transaction():
                    request = store.approvals.get_request(project_id, approval_id)
                    if request is not None:
                        self.ctx.sanitize.check_exact_action(request.action, request.action_fingerprint)
                    try:
                        result = approvals.record(
                            project_id=project_id,
                            request_id=approval_id,
                            action_fingerprint=body.action_fingerprint,
                            outcome=ApprovalOutcome(body.outcome),
                            actor=actor,
                            comment=body.comment,
                        )
                    except ApprovalExpiredError:
                        expired = True  # the request's new "expired" status commits with this block
                    else:
                        if result.recorded_now:
                            # The transactional outbox: the decision and the run's wake-up commit together,
                            # so a worker claims the run within its poll interval and applies the decision.
                            store.queue.wake(project_id, request.run_id)  # type: ignore[union-attr]
            except RecordNotFoundError:
                raise not_found(ErrorCode.APPROVAL_NOT_FOUND) from None
            except ApprovalBindingError:
                raise ApiError(ErrorCode.ACTION_FINGERPRINT_MISMATCH, MISMATCH_DETAIL) from None
            except ConflictError:
                raise self._already_decided(store, project, approval_id) from None
            except StatusTransitionError:
                raise self._closed(store, project, approval_id) from None
            if expired:
                raise ApiError(ErrorCode.APPROVAL_EXPIRED, EXPIRED_DETAIL)
            with store.snapshot():
                current = self._current(store, project, approval_id)
        return DecisionResult(current, result.recorded_now)  # type: ignore[arg-type]

    def reconcile(
        self, project_id: str, approval_id: str, body: api.ReconciliationRequest, actor: Actor
    ) -> DecisionResult:
        """Record one person's answer about exactly one uncertain attempt, and wake the run. Executes nothing.

        The server supplies the time and the actor (the authenticated
        principal, after ``actions:reconcile``). The body must name this
        approval's execution, the attempt now waiting, and the action's
        fingerprint; the domain's ReconciliationService enforces the rest (one
        answer per attempt, an identical repeat by the same person is a safe
        retry). A worker applies the answer later, through the execution
        ledger: for ``did_not_happen_retry`` that is a new authorization, valid
        for 15 minutes from this decision.
        """
        with self.ctx.stores.session() as store:
            project = self._project(store, project_id)
            try:
                with store.transaction():
                    request = store.approvals.get_request(project_id, approval_id)
                    if request is None:
                        raise not_found(ErrorCode.APPROVAL_NOT_FOUND)
                    self.ctx.sanitize.check_exact_action(request.action, request.action_fingerprint)
                    if body.action_fingerprint != request.action_fingerprint:
                        raise ApiError(ErrorCode.ACTION_FINGERPRINT_MISMATCH, RECONCILE_MISMATCH_DETAIL)
                    # Each approval has exactly one execution, and its ID is derived from the approval.
                    if body.execution_id != execution_id(project_id, request.run_id, request.id, request.action_fingerprint):
                        raise ApiError(ErrorCode.RECONCILIATION_STALE, STALE_DETAIL)
                    result = ReconciliationService(store, clock=self.ctx.clock).record(
                        project_id=project_id,
                        run_id=request.run_id,
                        execution_id=body.execution_id,
                        attempt=body.attempt,
                        action_fingerprint=body.action_fingerprint,
                        choice=ReconciliationChoice(body.choice),
                        actor=actor,
                        comment=body.comment,
                    )
            except RecordNotFoundError:  # the approval has no execution: nothing was ever attempted
                raise ApiError(ErrorCode.RECONCILIATION_NOT_REQUIRED, NOT_REQUIRED_DETAIL) from None
            except ApprovalBindingError:
                raise ApiError(ErrorCode.ACTION_FINGERPRINT_MISMATCH, RECONCILE_MISMATCH_DETAIL) from None
            except StaleReconciliation:
                raise ApiError(ErrorCode.RECONCILIATION_STALE, STALE_DETAIL) from None
            except ReconciliationNotRequired:
                raise ApiError(ErrorCode.RECONCILIATION_NOT_REQUIRED, NOT_REQUIRED_DETAIL) from None
            except ConflictError:
                raise self._already_answered(store, project_id, body) from None
            with store.snapshot():
                current = self._current(store, project, approval_id)
        return DecisionResult(current, result.recorded_now)  # type: ignore[arg-type]

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _name(project: Project | None) -> str:
        return project.name if project is not None else ""

    @staticmethod
    def _project(store: ProductStore, project_id: str) -> Project:
        project = store.projects.get(project_id)
        if project is None:
            raise not_found(ErrorCode.PROJECT_NOT_FOUND)
        return project

    def _current(self, store: ProductStore, project: Project, approval_id: str) -> api.ApprovalRequest | None:
        """The approval as a client sees it now, built only from structured records (never from logs)."""
        request = store.approvals.get_request(project.id, approval_id)
        if request is None:
            return None
        decision = store.approvals.get_decision(project.id, approval_id)
        exec_id = execution_id(project.id, request.run_id, request.id, request.action_fingerprint)
        execution = store.executions.get(project.id, request.run_id, exec_id)
        notified = store.notifications.list_for_approval(project.id, approval_id)
        told = {"notification_rows": notified, "unknown_notifications": store.notifications.with_unknown_outcome([n.id for n in notified])}
        if execution is None:
            return views.approval(request, decision, project.name, self.ctx.sanitize, now=self.ctx.now(), **told)
        return views.approval(
            request,
            decision,
            project.name,
            self.ctx.sanitize,
            now=self.ctx.now(),
            execution_record=execution,
            attempts=store.executions.attempts(project.id, request.run_id, exec_id),
            decisions=store.reconciliations.list_for_execution(project.id, exec_id),
            reconciliations=store.executions.reconciliations(project.id, request.run_id, exec_id),
            **told,
        )

    @staticmethod
    def _already_answered(store: ProductStore, project_id: str, body: api.ReconciliationRequest) -> ApiError:
        recorded = store.reconciliations.get(project_id, body.execution_id, body.attempt)
        if recorded is None:
            # A transient conflict (a lock wait timed out): nothing was recorded.
            return ApiError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "The action is being updated by another request. Nothing was recorded; try again shortly.",
            )
        return ApiError(
            ErrorCode.RECONCILIATION_ALREADY_DECIDED,
            f"Attempt {recorded.attempt} was already answered ({recorded.choice.value}). Reload the approval.",
        )

    def _already_decided(self, store: ProductStore, project: Project, approval_id: str) -> ApiError:
        current = self._current(store, project, approval_id)
        if current is None or current.decision is None:
            # A transient conflict (a lock wait timed out): nothing was recorded.
            return ApiError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "The approval is being updated by another request. Nothing was recorded; try again shortly.",
            )
        return ApiError(
            ErrorCode.APPROVAL_ALREADY_DECIDED,
            f"This approval was already {current.decision.outcome}.",
            approval=current.model_dump(mode="json"),
        )

    def _closed(self, store: ProductStore, project: Project, approval_id: str) -> ApiError:
        current = self._current(store, project, approval_id)
        if current is not None and current.status == ApprovalStatus.CANCELLED.value:
            return ApiError(ErrorCode.APPROVAL_CANCELLED, CANCELLED_DETAIL)
        if current is not None and current.decision is not None:
            return self._already_decided(store, project, approval_id)
        return ApiError(ErrorCode.APPROVAL_EXPIRED, EXPIRED_DETAIL)

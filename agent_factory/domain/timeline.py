"""The execution timeline of one approval: what happened to the action a person was asked about.

Built only from structured records: the approval request and decision, the
execution and its attempts (the ledger), reconciliation decisions (recorded
intent), and applied reconciliations. Never from the execution log, which stays
the detailed audit trail. Every entry is safe to show on a phone: times,
kinds, attempt numbers, the deciding person, and the worker identity (a
random ``wkr-`` ID and its fencing generation) that dispatched an attempt. A
dispatch by a local process (no worker identity) shows no process details.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .approvals import ApprovalDecision, ApprovalOutcome, ApprovalRequest, ApprovalStatus
from .executions import ActionExecution, ActionReconciliation, AttemptResult, ExecutionAttempt, ExecutionState, ReconciliationChoice
from .identity import Actor
from .reconciliation import ReconciliationDecision
from .serialization import parse_timestamp

_WORKER_OWNER = re.compile(r"^(wkr-[0-9a-f]{20})@([1-9][0-9]{0,18})$")


class TimelineKind(StrEnum):
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"  # no decision before the request expired
    ACTION_DISPATCHING = "action_dispatching"  # the first attempt was recorded and sent
    RETRY_DISPATCHED = "retry_dispatched"  # an attempt under a person-authorized retry
    ACTION_SUCCEEDED = "action_succeeded"
    ACTION_FAILED = "action_failed"
    ACTION_NOT_DISPATCHED = "action_not_dispatched"  # provably never sent
    OUTCOME_UNCERTAIN = "outcome_uncertain"  # the attempt ended without a known outcome
    RECONCILIATION_REQUIRED = "reconciliation_required"  # only a person can say what happened
    RECONCILIATION_DECIDED = "reconciliation_decided"
    RETRY_AUTHORIZED = "retry_authorized"  # applied: one more attempt, for 15 minutes
    RESOLVED = "resolved"  # the execution reached a final state: succeeded, failed, or lapsed


@dataclass(frozen=True)
class TimelineEntry:
    kind: TimelineKind
    at: str
    attempt: int | None = None
    actor: Actor | None = None  # who decided or reconciled
    worker_id: str | None = None  # which worker dispatched the attempt
    fencing_token: int | None = None  # in which ownership generation
    outcome: str | None = None  # approved/rejected; a reconciliation's choice; a resolution's final state
    until: str | None = None  # an expiry, or a retry authorization's end


def worker_of(owner: str) -> tuple[str | None, int | None]:
    """The worker identity in an attempt's owner (``wkr-…@N``), or nothing for a local process."""
    match = _WORKER_OWNER.fullmatch(owner or "")
    return (match.group(1), int(match.group(2))) if match else (None, None)


def approval_timeline(
    request: ApprovalRequest,
    decision: ApprovalDecision | None,
    *,
    execution: ActionExecution | None = None,
    attempts: Iterable[ExecutionAttempt] = (),
    decisions: Iterable[ReconciliationDecision] = (),
    reconciliations: Iterable[ActionReconciliation] = (),
    now: str,
) -> list[TimelineEntry]:
    """Every step of one approval's life, in order.

    Entries are built in the order things happen (the request, the decision,
    then each attempt with its outcome and its reconciliation, then the
    resolution) and sorted stably by time, so steps recorded in the same
    instant keep their causal order.
    """
    entries = [TimelineEntry(TimelineKind.APPROVAL_REQUESTED, request.requested_at, until=request.expires_at)]
    if decision is not None:
        kind = TimelineKind.APPROVED if decision.outcome is ApprovalOutcome.APPROVED else TimelineKind.REJECTED
        entries.append(TimelineEntry(kind, decision.decided_at, actor=decision.actor, outcome=decision.outcome.value))
    elif request.status is ApprovalStatus.EXPIRED or (request.status is ApprovalStatus.PENDING and request.is_expired(now)):
        entries.append(TimelineEntry(TimelineKind.EXPIRED, request.expires_at or request.updated_at or now))  # type: ignore[arg-type]
    if execution is not None:
        entries += _execution_entries(execution, list(attempts), list(decisions), list(reconciliations))
    return sorted(entries, key=lambda e: parse_timestamp(e.at))


def _execution_entries(
    execution: ActionExecution,
    attempts: list[ExecutionAttempt],
    decisions: list[ReconciliationDecision],
    reconciliations: list[ActionReconciliation],
) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    decided = {d.attempt: d for d in decisions}
    applied = {r.attempt: r for r in reconciliations}
    for a in attempts:  # each attempt in the order it happened: sent, outcome, reconciliation
        worker_id, token = worker_of(a.owner)
        kind = TimelineKind.RETRY_DISPATCHED if a.authorization_seq > 1 else TimelineKind.ACTION_DISPATCHING
        entries.append(TimelineEntry(kind, a.dispatched_at, attempt=a.attempt, worker_id=worker_id, fencing_token=token))
        if a.ended_at is None or a.result is None:
            continue
        ended = {
            AttemptResult.SUCCEEDED: TimelineKind.ACTION_SUCCEEDED,
            AttemptResult.FAILED: TimelineKind.ACTION_FAILED,
            AttemptResult.NOT_DISPATCHED: TimelineKind.ACTION_NOT_DISPATCHED,
            AttemptResult.INDETERMINATE: TimelineKind.OUTCOME_UNCERTAIN,
        }[a.result]
        entries.append(TimelineEntry(ended, a.ended_at, attempt=a.attempt, worker_id=worker_id, fencing_token=token))
        uncertain_now = execution.state is ExecutionState.UNCERTAIN and a.attempt == execution.attempts
        if a.result is AttemptResult.INDETERMINATE and (a.attempt in decided or a.attempt in applied or uncertain_now):
            entries.append(TimelineEntry(TimelineKind.RECONCILIATION_REQUIRED, a.ended_at, attempt=a.attempt))
        entries += _answer(a.attempt, decided, applied)
    if execution.state is ExecutionState.UNCERTAIN and not any(
        e.kind is TimelineKind.RECONCILIATION_REQUIRED and e.attempt == execution.attempts for e in entries
    ):
        # Became uncertain when a retry window closed after an earlier unknown outcome.
        entries.append(TimelineEntry(TimelineKind.RECONCILIATION_REQUIRED, execution.updated_at, attempt=execution.attempts))
    for attempt in sorted((set(decided) | set(applied)) - {a.attempt for a in attempts}):
        entries += _answer(attempt, decided, applied)  # an answer about an attempt not listed (never expected)
    if execution.state.is_final:
        entries.append(
            TimelineEntry(TimelineKind.RESOLVED, execution.finished_at or execution.updated_at, outcome=execution.state.value)
        )
    return entries


def _answer(
    attempt: int, decided: dict[int, ReconciliationDecision], applied: dict[int, ActionReconciliation]
) -> list[TimelineEntry]:
    """A person's answer about ``attempt`` (recorded, or applied), and the retry it authorized, if any."""
    source = decided.get(attempt) or applied.get(attempt)
    if source is None:
        return []
    entries = [
        TimelineEntry(
            TimelineKind.RECONCILIATION_DECIDED, source.decided_at, attempt=attempt, actor=source.actor,
            outcome=source.choice.value,
        )  # fmt: skip
    ]
    record = applied.get(attempt)
    if record is not None and record.choice is ReconciliationChoice.AUTHORIZE_RETRY:
        entries.append(
            TimelineEntry(
                TimelineKind.RETRY_AUTHORIZED, record.decided_at, attempt=attempt, actor=record.actor,
                until=record.retry_authorized_until,
            )  # fmt: skip
        )
    return entries


def needs_reconciliation(execution: ActionExecution | None, decisions: Iterable[ReconciliationDecision]) -> bool:
    """Is a person's reconciliation required now (uncertain, and no decision recorded for its attempt yet)?"""
    if execution is None or execution.state is not ExecutionState.UNCERTAIN:
        return False
    return all(d.attempt != execution.attempts for d in decisions)


__all__ = ["TimelineEntry", "TimelineKind", "approval_timeline", "needs_reconciliation", "worker_of"]

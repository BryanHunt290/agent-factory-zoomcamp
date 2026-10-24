"""Reconciliation decisions: a person's answer to "did this uncertain action happen?", as recorded intent.

When an approved action's outcome is unknown, its execution is ``uncertain``
and the run waits (docs/design/action-execution.md, section 11). A person
answers through the API (``actions:reconcile``) or the local CLI. That answer
is **intent**, recorded here: it changes nothing about the execution yet.

A worker that holds the run's claim applies it later (``Executor.advance``):
it re-checks the decision against the execution, then records it in the
execution ledger (``ExecutionLedger.reconcile``). That ledger record is what
moves the execution on, and for "try once more" it is the new, 15-minute
authorization, counted from the moment the person decided. Recording intent
and applying it are separate steps, so a decision never bypasses the
ActionExecution protocol, and every write to the run is fenced.

* **Exact binding.** A decision names the execution, the attempt it is about,
  and the action fingerprint the person was shown. Anything else is refused.
* **One per attempt.** An identical repeat (same choice, same person) is a
  safe retry; any other second decision for the attempt is a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..util import Clock, timestamp, utc_now
from .approvals import FINGERPRINT_PREFIX
from .errors import ApprovalBindingError, ConflictError, DomainValidationError, RecordNotFoundError, StatusTransitionError
from .executions import EXECUTION_ID_RE, ActionExecution, ExecutionState, ReconciliationChoice
from .identity import Actor, ActorKind
from .ids import PROJECT_ID_RE, RUN_ID_RE
from .serialization import check_enum, check_int, check_optional_text, check_pattern, check_text, check_timestamp


class StaleReconciliation(DomainValidationError):
    """The decision is for an attempt that is not the execution's uncertain attempt (the screen was stale)."""


class ReconciliationNotRequired(StatusTransitionError):
    """The execution is not uncertain: there is nothing for a person to reconcile."""


def _check_fingerprint(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.startswith(FINGERPRINT_PREFIX) or len(value) != len(FINGERPRINT_PREFIX) + 64:
        raise DomainValidationError(f"{name}: expected sha256:<64 hex>, got {value!r}")
    return value


@dataclass(frozen=True)
class ReconciliationDecision:
    """What a person decided about one uncertain attempt, before a worker applies it."""

    project_id: str
    run_id: str
    execution_id: str
    attempt: int
    action_fingerprint: str  # what the person was shown; must be the execution's
    choice: ReconciliationChoice
    actor: Actor
    decided_at: str  # server time
    comment: str = ""

    def __post_init__(self) -> None:
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.run_id, RUN_ID_RE, "run_id")
        check_pattern(self.execution_id, EXECUTION_ID_RE, "execution_id")
        check_int(self.attempt, "attempt", minimum=1)
        _check_fingerprint(self.action_fingerprint, "action_fingerprint")
        object.__setattr__(self, "choice", check_enum(ReconciliationChoice, self.choice, "choice"))
        if not isinstance(self.actor, Actor):
            raise DomainValidationError("actor: expected an Actor")
        if self.actor.kind is ActorKind.SYSTEM:
            raise DomainValidationError("actor: only a person can say what happened in the outside world")
        object.__setattr__(self, "decided_at", check_timestamp(self.decided_at, "decided_at"))
        check_text(self.comment, "comment", max_len=1000, allow_empty=True, multiline=True)

    @property
    def key(self) -> tuple[str, int]:
        return (self.execution_id, self.attempt)

    def same_as(self, choice: ReconciliationChoice, actor: Actor) -> bool:
        """Is a new submission an identical repeat of this decision (a safe retry)?"""
        return self.choice is ReconciliationChoice(choice) and self.actor.id == actor.id


@dataclass(frozen=True)
class RecordedReconciliation:
    decision: ReconciliationDecision
    recorded_now: bool  # False: an identical decision was already recorded


def check_against(decision: ReconciliationDecision, execution: ActionExecution) -> None:
    """Raise unless ``decision`` is exactly about ``execution``'s current uncertain attempt."""
    if (decision.project_id, decision.run_id, decision.execution_id) != (execution.project_id, execution.run_id, execution.id):
        raise ApprovalBindingError(f"reconciliation: is not about execution {execution.id} of run {execution.run_id}")
    if decision.action_fingerprint != execution.action_fingerprint:
        raise ApprovalBindingError(f"reconciliation: is for a different action than execution {execution.id}")
    if execution.state is not ExecutionState.UNCERTAIN:
        raise ReconciliationNotRequired(f"execution {execution.id} is {execution.state.value}; there is nothing to reconcile")
    if decision.attempt != execution.attempts:
        raise StaleReconciliation(
            f"reconciliation: is for attempt {decision.attempt}, but the uncertain attempt is {execution.attempts}"
        )


class ReconciliationService:
    """Records reconciliation decisions (intent) and wakes the run, in the caller's transaction."""

    def __init__(self, store: Any, clock: Clock = utc_now) -> None:  # a ProductStore
        self.store = store
        self._clock = clock

    def record(
        self,
        *,
        project_id: str,
        run_id: str,
        execution_id: str,
        attempt: int,
        action_fingerprint: str,
        choice: ReconciliationChoice | str,
        actor: Actor,
        comment: str = "",
        at: str | None = None,
    ) -> RecordedReconciliation:
        """Record one decision for exactly the attempt the person saw. Changes no execution.

        An identical repeat returns the decision on record (``recorded_now``
        False); a different decision for the same attempt is a conflict, even
        after a worker has applied the first.
        """
        choice = check_enum(ReconciliationChoice, choice, "choice")
        execution = self.store.executions.get(project_id, run_id, execution_id)
        if execution is None:
            raise RecordNotFoundError(f"execution {execution_id} does not exist in run {project_id}/{run_id}")
        if action_fingerprint != execution.action_fingerprint:
            raise ApprovalBindingError(f"reconciliation: is for a different action than execution {execution_id}")
        existing = self.store.reconciliations.get(project_id, execution_id, attempt)
        if existing is not None:
            if existing.same_as(choice, actor):
                return RecordedReconciliation(existing, recorded_now=False)
            raise ConflictError(f"attempt {attempt} of execution {execution_id} was already reconciled")
        decision = ReconciliationDecision(
            project_id=project_id,
            run_id=run_id,
            execution_id=execution_id,
            attempt=attempt,
            action_fingerprint=action_fingerprint,
            choice=choice,
            actor=actor,
            decided_at=at or timestamp(self._clock),
            comment=check_optional_text(comment, "comment", max_len=1000, allow_empty=True, multiline=True) or "",
        )
        check_against(decision, execution)
        try:
            self.store.reconciliations.add(decision)
        except ConflictError:
            raced = self.store.reconciliations.get(project_id, execution_id, attempt)
            if raced is None or not raced.same_as(choice, actor):
                raise
            return RecordedReconciliation(raced, recorded_now=False)
        # The transactional outbox: the run wants a worker from now on, in the same transaction.
        self.store.queue.wake(project_id, run_id)
        return RecordedReconciliation(decision, recorded_now=True)


__all__ = [
    "RecordedReconciliation",
    "ReconciliationDecision",
    "ReconciliationNotRequired",
    "ReconciliationService",
    "StaleReconciliation",
    "check_against",
]

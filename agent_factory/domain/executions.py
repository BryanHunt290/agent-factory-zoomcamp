"""Action executions: what Agent Factory knows about attempting an approved action.

**Approval is not execution.** An ``ApprovalDecision`` proves that a person
authorized one exact action. An :class:`ActionExecution` records what
happened when Agent Factory used that authorization: whether a dispatch was
permitted, whether it may have been sent, and what is known about its
outcome. Execution success is never inferred from approval.

The record follows the approved design (docs/design/action-execution.md):

* six states, :class:`ExecutionState`, with only the transitions in
  :data:`EXECUTION_TRANSITIONS`. An impossible change raises; nothing is
  silently repaired;
* every dispatch is an :class:`ExecutionAttempt`, recorded *before* the side
  effect (write-ahead) and closed with its result afterwards;
* an uncertain attempt is settled only by an :class:`ActionReconciliation`, a
  person's determination. "It did not happen, try once more" is a **new**
  authorization valid for 15 minutes, bound to the same execution, action,
  fingerprint, and the attempt it follows. The original approval is never
  reactivated;
* the execution ID is derived from project, run, approval request, and
  fingerprint (:func:`execution_id`), so the same approved action always maps
  to the same record, whichever process looks it up and however often it
  restarts. Filesystem paths play no part.

:class:`ExecutionLedger` is the one protocol. It runs over an
``ExecutionRepository``, of which there are two kinds of implementation:
the product store (in memory or PostgreSQL) and a per-run file next to the
engine checkpoint. Both enforce the same compare-and-set rule, so the same
domain semantics hold in both.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ..safety.execution import EffectClass, ExecutionSemantics, Idempotency
from ..util import Clock, timestamp, utc_now
from .approvals import ExactAction, _check_fingerprint
from .errors import ConflictError, DomainValidationError, RecordNotFoundError, StatusTransitionError
from .identity import Actor, ActorKind
from .ids import APPROVAL_ID_RE, PROJECT_ID_RE, RUN_ID_RE
from .principals import Permission
from .serialization import (
    RecordMixin,
    canonical,
    check_bool,
    check_enum,
    check_fields,
    check_int,
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

if TYPE_CHECKING:
    from .repositories import ExecutionRepository

EXECUTION_ID_RE = re.compile(r"^axe-[0-9a-f]{20}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^af1-axe-[0-9a-f]{20}-k[1-9][0-9]{0,5}$")

#: How long a person's "it did not happen, try once more" authorizes a new attempt.
RETRY_AUTHORIZATION_VALIDITY = timedelta(minutes=15)
#: The permission a future API will require to reconcile an uncertain action. Deliberately not
#: approvals:decide: saying what happened in the outside world is a different act from approving.
RECONCILE_PERMISSION = Permission.ACTIONS_RECONCILE.value
#: Replayable results are capped; anything larger is replayed as ok/summary only.
MAX_RESULT_BYTES = 4 * 1024
MAX_SUMMARY_CHARS = 500
MAX_DETAIL_CHARS = 500


class ExecutionState(StrEnum):
    AUTHORIZED = "authorized"  # approved for this exact action; no dispatch of the current attempt permitted yet
    DISPATCHING = "dispatching"  # a dispatch was permitted and recorded; the effect may or may not have happened
    SUCCEEDED = "succeeded"  # the tool reported success (or a person confirmed it); final
    FAILED = "failed"  # definitively did not take effect; final
    UNCERTAIN = "uncertain"  # the outcome cannot be determined and no automatic retry is allowed
    LAPSED = "lapsed"  # the authorization ended without any dispatch that could have had an effect; final

    @property
    def is_final(self) -> bool:
        return self in (ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.LAPSED)


#: Legal changes. ``authorized -> uncertain`` covers one case only: the window for a retry closed
#: after an earlier dispatch of the same authorization ended with an unknown outcome.
EXECUTION_TRANSITIONS: Mapping[ExecutionState, frozenset[ExecutionState]] = MappingProxyType(
    {
        ExecutionState.AUTHORIZED: frozenset(
            {ExecutionState.DISPATCHING, ExecutionState.LAPSED, ExecutionState.UNCERTAIN}
        ),
        ExecutionState.DISPATCHING: frozenset(
            {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.AUTHORIZED, ExecutionState.UNCERTAIN}
        ),
        ExecutionState.UNCERTAIN: frozenset({ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.AUTHORIZED}),
        ExecutionState.SUCCEEDED: frozenset(),
        ExecutionState.FAILED: frozenset(),
        ExecutionState.LAPSED: frozenset(),
    }
)


class AttemptResult(StrEnum):
    SUCCEEDED = "succeeded"  # the tool returned
    FAILED = "failed"  # the tool reported a definitive failure: no effect
    NOT_DISPATCHED = "not_dispatched"  # provably never sent
    INDETERMINATE = "indeterminate"  # sent, outcome unknown (or the process stopped)


class ReconciliationChoice(StrEnum):
    IT_HAPPENED = "it_happened"
    DID_NOT_HAPPEN = "did_not_happen"  # and do not retry
    AUTHORIZE_RETRY = "did_not_happen_retry"  # a new, short authorization for one more attempt


class LedgerKind(StrEnum):
    CHECKPOINT = "checkpoint"  # a per-run file next to the engine checkpoint (no database)
    PRODUCT_STORE = "product_store"  # the product store's repositories (PostgreSQL in production)


def _digest(material: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(dict(material)).encode("utf-8")).hexdigest()


def execution_id(project_id: str, run_id: str, request_id: str, action_fingerprint: str) -> str:
    """The stable ID of the execution of one approved action. No path, no clock, no randomness."""
    return "axe-" + _digest(
        {"v": 1, "project": project_id, "run": run_id, "request": request_id, "fingerprint": action_fingerprint}
    )[:20]


def idempotency_key(exec_id: str, key_generation: int = 1) -> str:
    """The key sent to a keyed idempotent target: the same on every retry of one execution."""
    check_pattern(exec_id, EXECUTION_ID_RE, "execution_id")
    check_int(key_generation, "key_generation", minimum=1)
    return f"af1-{exec_id}-k{key_generation}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _check_owner(value: Any, name: str) -> str:
    return check_text(value, name, max_len=200)


def _check_result(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    data = check_json_object(value, "result", max_bytes=MAX_RESULT_BYTES + 1024)
    check_fields(data, required=("ok", "summary", "data"), optional=("data_omitted", "confirmed_by"), where="result")
    check_bool(data["ok"], "result.ok")
    check_text(data["summary"], "result.summary", max_len=MAX_SUMMARY_CHARS, allow_empty=True, multiline=True)
    return data


def _check_failure(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    data = check_json_object(value, "failure")
    check_fields(data, required=("type", "message"), where="failure")
    check_text(data["type"], "failure.type", max_len=100)
    check_text(data["message"], "failure.message", max_len=MAX_DETAIL_CHARS, allow_empty=True, multiline=True)
    return data


def bounded_result(ok: bool, summary: str, data: Any, *, replayable: bool) -> dict[str, Any]:
    """A result small and safe enough to record and replay; oversized or opaque data is omitted."""
    entry: dict[str, Any] = {"ok": bool(ok), "summary": _clip(summary or "", MAX_SUMMARY_CHARS), "data": None}
    if replayable and data is not None:
        try:
            check_json_object({"data": data}, "result.data", max_bytes=MAX_RESULT_BYTES)
            entry["data"] = data
        except DomainValidationError:
            entry["data_omitted"] = True
    elif data is not None:
        entry["data_omitted"] = True
    return entry


def failure_record(exc_type: str, message: str) -> dict[str, Any]:
    return {"type": _clip(exc_type, 100), "message": _clip(" ".join(str(message).split()), MAX_DETAIL_CHARS)}


# ------------------------------------------------------------------ records


@dataclass(frozen=True)
class ExecutionSpec:
    """What the engine knows when an approved action is about to be used for the first time."""

    project_id: str
    run_id: str
    request_id: str
    node: str
    visit: int
    agent: str
    action: ExactAction
    semantics: ExecutionSemantics
    authorized_at: str
    authorized_until: str | None = None

    @property
    def execution_id(self) -> str:
        return execution_id(self.project_id, self.run_id, self.request_id, self.action.fingerprint)


_IDENTITY = (
    "id", "project_id", "run_id", "request_id", "node", "visit", "agent", "action", "action_fingerprint",
    "effect", "idempotency", "max_attempts", "key_retention_s", "created_at",
)  # fmt: skip
_EXECUTION_FIELDS = (
    *_IDENTITY, "state", "version", "authorization_seq", "authorized_at", "authorized_until", "attempts",
    "dispatches", "key_generation", "open_attempt", "owner", "updated_at", "finished_at", "result", "failure",
    "lapse_reason",
)  # fmt: skip


@dataclass(frozen=True)
class ActionExecution(RecordMixin):
    """The execution of one approved, exact action. Immutable: every change is a new version."""

    RECORD_TYPE = "action_execution"
    SCHEMA_VERSION = 1

    id: str
    project_id: str
    run_id: str
    request_id: str | None  # the approval request it executes; always set for approval-gated actions
    node: str
    visit: int
    agent: str
    action: ExactAction  # tool, category, and the parameters exactly as approved
    action_fingerprint: str
    effect: EffectClass
    idempotency: Idempotency | None
    max_attempts: int  # automatic dispatches per authorization
    key_retention_s: int | None
    state: ExecutionState
    version: int  # 1 when created; +1 on every change (compare-and-set)
    authorization_seq: int  # 1 = the approval; +1 for each person-authorized retry
    authorized_at: str
    authorized_until: str | None  # a dispatch must BEGIN before this; None = no expiry
    attempts: int  # attempt records so far, across authorizations
    dispatches: int  # dispatches under the current authorization that may have had an effect
    key_generation: int
    open_attempt: int | None  # the attempt in flight (state dispatching)
    owner: str | None  # who holds the open attempt
    created_at: str
    updated_at: str
    finished_at: str | None = None
    result: dict[str, Any] | None = None  # replayable outcome (succeeded)
    failure: dict[str, Any] | None = None  # why it failed (failed)
    lapse_reason: str | None = None

    def __post_init__(self) -> None:
        check_pattern(self.id, EXECUTION_ID_RE, "id")
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.run_id, RUN_ID_RE, "run_id")
        if self.request_id is not None:
            check_pattern(self.request_id, APPROVAL_ID_RE, "request_id")
        check_text(self.node, "node", max_len=64)
        check_int(self.visit, "visit", minimum=1)
        check_text(self.agent, "agent", max_len=64)
        if not isinstance(self.action, ExactAction):
            raise DomainValidationError("action: expected an ExactAction")
        _check_fingerprint(self.action_fingerprint, "action_fingerprint")
        if self.action_fingerprint != self.action.fingerprint:
            raise DomainValidationError("action_fingerprint: does not match the action")
        if self.request_id is not None and self.id != execution_id(
            self.project_id, self.run_id, self.request_id, self.action_fingerprint
        ):
            raise DomainValidationError("id: does not belong to this project, run, approval, and action")
        object.__setattr__(self, "effect", check_enum(EffectClass, self.effect, "effect"))
        if self.idempotency is not None:
            object.__setattr__(self, "idempotency", check_enum(Idempotency, self.idempotency, "idempotency"))
        if (self.effect is EffectClass.IDEMPOTENT) != (self.idempotency is not None):
            raise DomainValidationError("idempotency: required for, and only for, idempotent actions")
        check_int(self.max_attempts, "max_attempts", minimum=1)
        if self.effect is EffectClass.NON_IDEMPOTENT and self.max_attempts != 1:
            raise DomainValidationError("max_attempts: a non-idempotent action gets exactly one automatic attempt")
        if (self.idempotency is Idempotency.KEYED) != (self.key_retention_s is not None):
            raise DomainValidationError("key_retention_s: required for, and only for, keyed idempotency")
        if self.key_retention_s is not None:
            check_int(self.key_retention_s, "key_retention_s", minimum=1)
        object.__setattr__(self, "state", check_enum(ExecutionState, self.state, "state"))
        check_int(self.version, "version", minimum=1)
        check_int(self.authorization_seq, "authorization_seq", minimum=1)
        object.__setattr__(self, "authorized_at", check_timestamp(self.authorized_at, "authorized_at"))
        object.__setattr__(self, "authorized_until", check_optional_timestamp(self.authorized_until, "authorized_until"))
        if self.authorized_until and parse_timestamp(self.authorized_until) <= parse_timestamp(self.authorized_at):
            raise DomainValidationError("authorized_until: must be later than authorized_at")
        check_int(self.attempts, "attempts", minimum=0)
        check_int(self.dispatches, "dispatches", minimum=0)
        if self.dispatches > self.max_attempts or self.dispatches > self.attempts:
            raise DomainValidationError("dispatches: more than max_attempts or than the attempts recorded")
        check_int(self.key_generation, "key_generation", minimum=1)
        dispatching = self.state is ExecutionState.DISPATCHING
        if dispatching != (self.open_attempt is not None) or dispatching != (self.owner is not None):
            raise DomainValidationError("open_attempt and owner: set exactly while dispatching")
        if self.open_attempt is not None and self.open_attempt != self.attempts:
            raise DomainValidationError("open_attempt: must be the latest attempt")
        if self.owner is not None:
            _check_owner(self.owner, "owner")
        object.__setattr__(self, "created_at", check_timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", check_timestamp(self.updated_at, "updated_at"))
        if parse_timestamp(self.updated_at) < parse_timestamp(self.created_at):
            raise DomainValidationError("updated_at: earlier than created_at")
        object.__setattr__(self, "finished_at", check_optional_timestamp(self.finished_at, "finished_at"))
        if self.state.is_final != (self.finished_at is not None):
            raise DomainValidationError("finished_at: set exactly when the execution is final")
        object.__setattr__(self, "result", _check_result(self.result))
        if (self.state is ExecutionState.SUCCEEDED) != (self.result is not None):
            raise DomainValidationError("result: recorded exactly for a succeeded execution")
        object.__setattr__(self, "failure", _check_failure(self.failure))
        if (self.state is ExecutionState.FAILED) != (self.failure is not None):
            raise DomainValidationError("failure: recorded exactly for a failed execution")
        if self.lapse_reason is not None:
            check_text(self.lapse_reason, "lapse_reason", max_len=200)
        if (self.state is ExecutionState.LAPSED) != (self.lapse_reason is not None):
            raise DomainValidationError("lapse_reason: recorded exactly for a lapsed execution")

    # ------------------------------------------------------------ creation

    @classmethod
    def authorized(cls, spec: ExecutionSpec, *, at: str) -> ActionExecution:
        sem = spec.semantics
        return cls(
            id=spec.execution_id,
            project_id=spec.project_id,
            run_id=spec.run_id,
            request_id=spec.request_id,
            node=spec.node,
            visit=spec.visit,
            agent=spec.agent,
            action=spec.action,
            action_fingerprint=spec.action.fingerprint,
            effect=sem.effect,
            idempotency=sem.idempotency,
            max_attempts=sem.max_attempts,
            key_retention_s=sem.key_retention_s,
            state=ExecutionState.AUTHORIZED,
            version=1,
            authorization_seq=1,
            authorized_at=spec.authorized_at,
            authorized_until=spec.authorized_until,
            attempts=0,
            dispatches=0,
            key_generation=1,
            open_attempt=None,
            owner=None,
            created_at=at,
            updated_at=at,
        )

    # ------------------------------------------------------------ queries

    @property
    def idempotency_key(self) -> str | None:
        """The key a keyed target receives: stable across retries, never minted per attempt."""
        return idempotency_key(self.id, self.key_generation) if self.idempotency is Idempotency.KEYED else None

    def window_open(self, at: str) -> bool:
        return self.authorized_until is None or parse_timestamp(at) < parse_timestamp(self.authorized_until)

    @property
    def may_have_effect(self) -> bool:
        """Did a dispatch under the current authorization possibly take effect?"""
        return self.dispatches > 0 and self.effect is not EffectClass.READ_ONLY

    # --------------------------------------------------------- transitions

    def _next(self, at: str, **changes: Any) -> ActionExecution:
        return replace(self, version=self.version + 1, updated_at=at, **changes)

    def start_attempt(self, *, owner: str, at: str) -> tuple[ActionExecution, ExecutionAttempt]:
        """Permit one dispatch: the write-ahead record that must be durable before the side effect."""
        if self.state is not ExecutionState.AUTHORIZED:
            raise StatusTransitionError(f"execution {self.id} is {self.state.value}; only an authorized one can dispatch")
        if self.dispatches >= self.max_attempts:
            raise StatusTransitionError(f"execution {self.id} has no automatic attempts left")
        if not self.window_open(at):
            raise StatusTransitionError(f"execution {self.id}: the authorization expired at {self.authorized_until}")
        number = self.attempts + 1
        attempt = ExecutionAttempt(
            execution_id=self.id,
            attempt=number,
            authorization_seq=self.authorization_seq,
            owner=owner,
            idempotency_key=self.idempotency_key,
            dispatched_at=at,
        )
        new = self._next(
            at,
            state=ExecutionState.DISPATCHING,
            attempts=number,
            dispatches=self.dispatches + 1,
            open_attempt=number,
            owner=owner,
        )
        return new, attempt

    def state_after(self, result: AttemptResult, attempt: ExecutionAttempt, at: str) -> ExecutionState:
        """The class policy: what an attempt's result means for the execution."""
        if result is AttemptResult.SUCCEEDED:
            return ExecutionState.SUCCEEDED
        if result is AttemptResult.FAILED:
            return ExecutionState.FAILED
        if result is AttemptResult.NOT_DISPATCHED:
            return ExecutionState.AUTHORIZED  # nothing left the process: the dispatch slot is given back
        # INDETERMINATE: the effect may or may not exist.
        can_retry = self.dispatches < self.max_attempts and self.window_open(at)
        if self.effect is EffectClass.READ_ONLY:
            return ExecutionState.AUTHORIZED if can_retry else ExecutionState.FAILED
        if self.effect is EffectClass.IDEMPOTENT:
            if self.idempotency is Idempotency.KEYED and self.key_retention_s is not None:
                age = parse_timestamp(at) - parse_timestamp(attempt.dispatched_at)
                if age >= timedelta(seconds=self.key_retention_s):
                    return ExecutionState.UNCERTAIN  # the target may have forgotten the key: a retry could duplicate
            return ExecutionState.AUTHORIZED if can_retry else ExecutionState.UNCERTAIN
        return ExecutionState.UNCERTAIN  # NON_IDEMPOTENT: never resent automatically

    def finish_attempt(
        self,
        attempt: ExecutionAttempt,
        *,
        result: AttemptResult,
        at: str,
        payload: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        detail: str = "",
    ) -> tuple[ActionExecution, ExecutionAttempt]:
        """Close the open attempt with its result and move the execution on by the class policy."""
        if self.state is not ExecutionState.DISPATCHING or attempt.attempt != self.open_attempt:
            raise StatusTransitionError(f"execution {self.id}: attempt {attempt.attempt} is not the open attempt")
        if attempt.execution_id != self.id or attempt.ended_at is not None:
            raise StatusTransitionError(f"execution {self.id}: attempt {attempt.attempt} does not belong here or is closed")
        result = check_enum(AttemptResult, result, "result")
        closed = attempt.close(result=result, at=at, detail=detail)
        state = self.state_after(result, attempt, at)
        changes: dict[str, Any] = {"state": state, "open_attempt": None, "owner": None}
        if result is AttemptResult.NOT_DISPATCHED:
            changes["dispatches"] = self.dispatches - 1
        if state is ExecutionState.SUCCEEDED:
            if payload is None:
                raise DomainValidationError("a succeeded attempt needs its result")
            changes.update(result=payload, finished_at=at)
        elif state is ExecutionState.FAILED:
            if failure is None:
                failure = failure_record("RetriesExhausted", detail or "every automatic attempt ended without an outcome")
            changes.update(failure=failure, finished_at=at)
        return self._next(at, **changes), closed

    def lapse(self, *, at: str, reason: str) -> ActionExecution:
        """End an authorization that was never used for a dispatch that could have had an effect."""
        if self.state is not ExecutionState.AUTHORIZED:
            raise StatusTransitionError(f"execution {self.id} is {self.state.value}; only an authorized one can lapse")
        if self.may_have_effect:
            raise StatusTransitionError(
                f"execution {self.id}: an earlier dispatch may have taken effect, so it cannot lapse"
            )
        return self._next(at, state=ExecutionState.LAPSED, lapse_reason=reason, finished_at=at)

    def close_authorization(self, *, at: str, reason: str) -> ActionExecution:
        """The authorization can no longer be used: it expired, its node visit ended, or the run ended.

        If no dispatch under it could have taken effect it lapses (known: never
        happened). If an earlier dispatch ended with an unknown outcome and the
        retry it allowed can no longer happen, only a person can say what
        happened: the execution becomes uncertain.
        """
        if self.state is not ExecutionState.AUTHORIZED:
            raise StatusTransitionError(f"execution {self.id} is {self.state.value}; only an authorized one can close")
        if self.may_have_effect:
            return self._next(at, state=ExecutionState.UNCERTAIN)
        return self.lapse(at=at, reason=reason)

    def resolve(self, reconciliation: ActionReconciliation, *, at: str) -> ActionExecution:
        """Apply a person's determination of the uncertain attempt."""
        if self.state is not ExecutionState.UNCERTAIN:
            raise StatusTransitionError(f"execution {self.id} is {self.state.value}; only an uncertain one is reconciled")
        if reconciliation.execution_id != self.id or reconciliation.action_fingerprint != self.action_fingerprint:
            raise DomainValidationError(f"reconciliation: does not belong to execution {self.id} and its exact action")
        if reconciliation.attempt != self.attempts:
            raise DomainValidationError(
                f"reconciliation: is for attempt {reconciliation.attempt}, but the uncertain attempt is {self.attempts}"
            )
        choice = reconciliation.choice
        if choice is ReconciliationChoice.IT_HAPPENED:
            confirmed = reconciliation.actor.display_name
            payload = {
                "ok": True,
                "summary": _clip(f"Confirmed by {confirmed}: the action happened.", MAX_SUMMARY_CHARS),
                "data": None,
                "confirmed_by": confirmed,
            }
            return self._next(at, state=ExecutionState.SUCCEEDED, result=payload, finished_at=at)
        if choice is ReconciliationChoice.DID_NOT_HAPPEN:
            failure = failure_record(
                "ConfirmedNotExecuted", "A person confirmed the action did not happen and chose not to retry it."
            )
            return self._next(at, state=ExecutionState.FAILED, failure=failure, finished_at=at)
        return self._next(
            at,
            state=ExecutionState.AUTHORIZED,
            authorization_seq=self.authorization_seq + 1,
            authorized_at=reconciliation.decided_at,
            authorized_until=reconciliation.retry_authorized_until,
            dispatches=0,
        )

    @staticmethod
    def check_successor(old: ActionExecution, new: ActionExecution) -> None:
        """Raise unless ``new`` is a legal next version of ``old``. Every store applies this."""
        for name in _IDENTITY:
            if getattr(old, name) != getattr(new, name):
                raise DomainValidationError(f"execution {old.id}: field {name!r} cannot change")
        if new.version != old.version + 1:
            raise ConflictError(f"execution {old.id}: version {new.version} does not follow {old.version}")
        if new.state not in EXECUTION_TRANSITIONS[old.state]:
            raise StatusTransitionError(f"execution {old.id}: {old.state.value} -> {new.state.value} is not allowed")
        if new.attempts not in (old.attempts, old.attempts + 1):
            raise DomainValidationError(f"execution {old.id}: attempts can only grow by one at a time")
        if new.authorization_seq not in (old.authorization_seq, old.authorization_seq + 1):
            raise DomainValidationError(f"execution {old.id}: authorization_seq can only grow by one")
        if new.authorization_seq != old.authorization_seq and not (
            old.state is ExecutionState.UNCERTAIN and new.state is ExecutionState.AUTHORIZED
        ):
            raise DomainValidationError(f"execution {old.id}: only a reconciliation retry is a new authorization")
        if new.key_generation < old.key_generation:
            raise DomainValidationError(f"execution {old.id}: key_generation cannot go back")

    # ------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, Any]:
        return seal(
            self.RECORD_TYPE,
            self.SCHEMA_VERSION,
            {
                **{name: getattr(self, name) for name in _EXECUTION_FIELDS},
                "action": self.action.to_dict(),
                "effect": self.effect.value,
                "idempotency": self.idempotency.value if self.idempotency else None,
                "state": self.state.value,
            },
        )

    @classmethod
    def from_dict(cls, data: Any) -> ActionExecution:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(body, required=_EXECUTION_FIELDS, where=cls.RECORD_TYPE)
        return cls(**{**body, "action": ExactAction.from_dict(body["action"])})


_ATTEMPT_FIELDS = (
    "execution_id", "attempt", "authorization_seq", "owner", "idempotency_key", "dispatched_at", "ended_at",
    "result", "detail",
)  # fmt: skip


@dataclass(frozen=True)
class ExecutionAttempt(RecordMixin):
    """One durable dispatch: written before the side effect, closed with its result afterwards."""

    RECORD_TYPE = "execution_attempt"
    SCHEMA_VERSION = 1

    execution_id: str
    attempt: int
    authorization_seq: int
    owner: str
    idempotency_key: str | None
    dispatched_at: str
    ended_at: str | None = None
    result: AttemptResult | None = None
    detail: str = ""  # curated: an exception type or a fixed explanation, never raw output

    def __post_init__(self) -> None:
        check_pattern(self.execution_id, EXECUTION_ID_RE, "execution_id")
        check_int(self.attempt, "attempt", minimum=1)
        check_int(self.authorization_seq, "authorization_seq", minimum=1)
        _check_owner(self.owner, "owner")
        if self.idempotency_key is not None:
            check_pattern(self.idempotency_key, IDEMPOTENCY_KEY_RE, "idempotency_key")
        object.__setattr__(self, "dispatched_at", check_timestamp(self.dispatched_at, "dispatched_at"))
        object.__setattr__(self, "ended_at", check_optional_timestamp(self.ended_at, "ended_at"))
        if self.result is not None:
            object.__setattr__(self, "result", check_enum(AttemptResult, self.result, "result"))
        if (self.ended_at is None) != (self.result is None):
            raise DomainValidationError("attempt: ended_at and result are set together")
        if self.ended_at and parse_timestamp(self.ended_at) < parse_timestamp(self.dispatched_at):
            raise DomainValidationError("attempt: ended before it was dispatched")
        check_text(self.detail, "detail", max_len=MAX_DETAIL_CHARS, allow_empty=True)

    def close(self, *, result: AttemptResult, at: str, detail: str = "") -> ExecutionAttempt:
        if self.ended_at is not None:
            raise StatusTransitionError(f"attempt {self.attempt} of {self.execution_id} is already closed")
        return replace(self, ended_at=at, result=check_enum(AttemptResult, result, "result"), detail=_clip(detail, MAX_DETAIL_CHARS))

    def to_dict(self) -> dict[str, Any]:
        body = {name: getattr(self, name) for name in _ATTEMPT_FIELDS}
        body["result"] = self.result.value if self.result else None
        return seal(self.RECORD_TYPE, self.SCHEMA_VERSION, body)

    @classmethod
    def from_dict(cls, data: Any) -> ExecutionAttempt:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(body, required=_ATTEMPT_FIELDS, where=cls.RECORD_TYPE)
        return cls(**body)


_RECONCILIATION_FIELDS = (
    "execution_id", "attempt", "action_fingerprint", "choice", "decided_at", "actor", "comment",
    "retry_authorized_until",
)  # fmt: skip


@dataclass(frozen=True)
class ActionReconciliation(RecordMixin):
    """A person's determination of one uncertain attempt. One per attempt; never edited.

    For ``AUTHORIZE_RETRY`` it is also the new authorization: valid until
    ``retry_authorized_until`` (15 minutes), bound to this execution (project,
    run, exact action), its fingerprint, and the attempt it follows.
    """

    RECORD_TYPE = "action_reconciliation"
    SCHEMA_VERSION = 1

    execution_id: str
    attempt: int
    action_fingerprint: str  # what the person was shown; must be the execution's
    choice: ReconciliationChoice
    decided_at: str  # server time
    actor: Actor  # placeholder until authentication exists; never "system"
    comment: str = ""
    retry_authorized_until: str | None = None

    def __post_init__(self) -> None:
        check_pattern(self.execution_id, EXECUTION_ID_RE, "execution_id")
        check_int(self.attempt, "attempt", minimum=1)
        _check_fingerprint(self.action_fingerprint, "action_fingerprint")
        object.__setattr__(self, "choice", check_enum(ReconciliationChoice, self.choice, "choice"))
        object.__setattr__(self, "decided_at", check_timestamp(self.decided_at, "decided_at"))
        if not isinstance(self.actor, Actor):
            raise DomainValidationError("actor: expected an Actor")
        if self.actor.kind is ActorKind.SYSTEM:
            raise DomainValidationError("actor: only a person can say what happened in the outside world")
        check_text(self.comment, "comment", max_len=1000, allow_empty=True, multiline=True)
        object.__setattr__(
            self, "retry_authorized_until", check_optional_timestamp(self.retry_authorized_until, "retry_authorized_until")
        )
        retry = self.choice is ReconciliationChoice.AUTHORIZE_RETRY
        if retry != (self.retry_authorized_until is not None):
            raise DomainValidationError("retry_authorized_until: set exactly for a retry authorization")
        if retry and parse_timestamp(self.retry_authorized_until) <= parse_timestamp(self.decided_at):  # type: ignore[arg-type]
            raise DomainValidationError("retry_authorized_until: must be later than decided_at")

    def to_dict(self) -> dict[str, Any]:
        body = {name: getattr(self, name) for name in _RECONCILIATION_FIELDS}
        body.update(choice=self.choice.value, actor=self.actor.to_dict())
        return seal(self.RECORD_TYPE, self.SCHEMA_VERSION, body)

    @classmethod
    def from_dict(cls, data: Any) -> ActionReconciliation:
        body = open_envelope(data, record_type=cls.RECORD_TYPE, version=cls.SCHEMA_VERSION)
        check_fields(body, required=_RECONCILIATION_FIELDS, where=cls.RECORD_TYPE)
        return cls(**{**body, "actor": Actor.from_dict(body["actor"])})


def check_replace(
    old: ActionExecution,
    new: ActionExecution,
    *,
    attempt: ExecutionAttempt | None,
    reconciliation: ActionReconciliation | None,
) -> None:
    """The rules every store applies to one compare-and-set, before writing anything."""
    ActionExecution.check_successor(old, new)
    opening = new.state is ExecutionState.DISPATCHING
    closing = old.state is ExecutionState.DISPATCHING
    if opening:
        if attempt is None or attempt.ended_at is not None:
            raise DomainValidationError(f"execution {old.id}: a dispatch needs a new, open attempt record")
        if (attempt.execution_id, attempt.attempt) != (new.id, new.open_attempt) or attempt.owner != new.owner:
            raise DomainValidationError(f"execution {old.id}: the attempt record does not match the dispatch")
        if attempt.authorization_seq != new.authorization_seq or attempt.idempotency_key != new.idempotency_key:
            raise DomainValidationError(f"execution {old.id}: the attempt record does not match the authorization")
    elif closing:
        if attempt is None or attempt.ended_at is None:
            raise DomainValidationError(f"execution {old.id}: leaving dispatching needs the closed attempt record")
        if (attempt.execution_id, attempt.attempt) != (old.id, old.open_attempt):
            raise DomainValidationError(f"execution {old.id}: the closed attempt is not the open one")
    elif attempt is not None:
        raise DomainValidationError(f"execution {old.id}: no attempt record belongs to this change")
    if reconciliation is not None:
        if old.state is not ExecutionState.UNCERTAIN:
            raise DomainValidationError(f"execution {old.id}: only an uncertain execution is reconciled")
        if reconciliation.execution_id != old.id or reconciliation.attempt != old.attempts:
            raise DomainValidationError(f"execution {old.id}: the reconciliation is for another attempt")
        if reconciliation.action_fingerprint != old.action_fingerprint:
            raise DomainValidationError(f"execution {old.id}: the reconciliation is for a different action")
    elif old.state is ExecutionState.UNCERTAIN:
        raise DomainValidationError(f"execution {old.id}: an uncertain execution changes only by reconciliation")


# ------------------------------------------------------------------ ledger


@dataclass(frozen=True)
class Dispatch:
    """Permission to dispatch: the attempt is durably recorded and the side effect may now begin."""

    execution: ActionExecution
    attempt: ExecutionAttempt


class ExecutionLedger:
    """The execution protocol, over one ``ExecutionRepository``.

    Every change is a compare-and-set on the record's version, so of two
    writers acting on the same version exactly one succeeds. ``begin`` returns
    a :class:`Dispatch` only after the attempt record is durable; it returns
    ``None`` if another writer changed the execution first, and then nothing
    may be dispatched.
    """

    def __init__(self, repository: ExecutionRepository, *, kind: LedgerKind, clock: Clock = utc_now) -> None:
        self.repository = repository
        self.kind = LedgerKind(kind)
        self._clock = clock

    def now(self) -> str:
        return timestamp(self._clock)

    # ------------------------------------------------------------- reads

    def get(self, project_id: str, run_id: str, exec_id: str) -> ActionExecution | None:
        return self.repository.get(project_id, run_id, exec_id)

    def list_for_run(self, project_id: str, run_id: str) -> list[ActionExecution]:
        return self.repository.list_for_run(project_id, run_id)

    def attempts(self, execution: ActionExecution) -> list[ExecutionAttempt]:
        return self.repository.attempts(execution.project_id, execution.run_id, execution.id)

    def reconciliations(self, execution: ActionExecution) -> list[ActionReconciliation]:
        return self.repository.reconciliations(execution.project_id, execution.run_id, execution.id)

    def open_attempt(self, execution: ActionExecution) -> ExecutionAttempt:
        if execution.open_attempt is None:
            raise StatusTransitionError(f"execution {execution.id} has no open attempt")
        for attempt in self.attempts(execution):
            if attempt.attempt == execution.open_attempt:
                if attempt.ended_at is not None:
                    raise DomainValidationError(f"execution {execution.id}: its open attempt is recorded as closed")
                return attempt
        raise DomainValidationError(f"execution {execution.id}: the open attempt {execution.open_attempt} is missing")

    # ------------------------------------------------------------ writes

    def authorize(self, spec: ExecutionSpec) -> tuple[ActionExecution, bool]:
        """The execution for an approved action, created on first use. Idempotent."""
        return self.repository.create(ActionExecution.authorized(spec, at=self.now()))

    def begin(self, execution: ActionExecution, *, owner: str) -> Dispatch | ActionExecution | None:
        """Write-ahead: permit and durably record one dispatch.

        Returns a :class:`Dispatch` (dispatch now), the closed execution (its
        authorization expired: lapsed, or uncertain after an earlier unknown
        outcome), or ``None`` (another writer got there first: do not dispatch).
        """
        now = self.now()
        if not execution.window_open(now):
            closed = execution.close_authorization(at=now, reason="authorization_expired")
            return closed if self.repository.replace(execution, closed) else None
        new, attempt = execution.start_attempt(owner=owner, at=now)
        if not self.repository.replace(execution, new, attempt=attempt):
            return None
        return Dispatch(new, attempt)

    def finish(
        self,
        execution: ActionExecution,
        attempt: ExecutionAttempt,
        *,
        result: AttemptResult,
        payload: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        detail: str = "",
    ) -> ActionExecution:
        new, closed = execution.finish_attempt(
            attempt, result=result, at=self.now(), payload=payload, failure=failure, detail=detail
        )
        if not self.repository.replace(execution, new, attempt=closed):
            raise ConflictError(f"execution {execution.id} changed while its owner was dispatching it")
        return new

    def close(self, execution: ActionExecution, *, reason: str) -> ActionExecution:
        """End an unused authorization: lapsed, or uncertain if an earlier dispatch may have had an effect."""
        new = execution.close_authorization(at=self.now(), reason=reason)
        if not self.repository.replace(execution, new):
            raise ConflictError(f"execution {execution.id} changed while its authorization was being closed")
        return new

    def reconcile(
        self,
        project_id: str,
        run_id: str,
        exec_id: str,
        *,
        attempt: int,
        action_fingerprint: str,
        choice: ReconciliationChoice,
        actor: Actor,
        comment: str = "",
        decided_at: str | None = None,
    ) -> tuple[ActionExecution, ActionReconciliation, bool]:
        """Record a person's determination of an uncertain attempt; ``True`` if this call recorded it.

        An identical repeat (same attempt, choice, and person) returns the
        recorded one; a different second determination is a conflict.
        ``decided_at`` is when the person decided (server time), if earlier
        than now: a worker applying a recorded decision passes it, so that a
        retry authorization's 15 minutes count from the person's decision, never
        from whenever a worker got to it.
        """
        execution = self.get(project_id, run_id, exec_id)
        if execution is None:
            raise RecordNotFoundError(f"execution {exec_id} does not exist in run {project_id}/{run_id}")
        choice = check_enum(ReconciliationChoice, choice, "choice")
        if action_fingerprint != execution.action_fingerprint:
            raise DomainValidationError(f"execution {exec_id}: the reconciliation is for a different action")
        if execution.state is not ExecutionState.UNCERTAIN:
            for recorded in self.reconciliations(execution):
                if recorded.attempt == attempt:
                    if recorded.choice is choice and recorded.actor.id == actor.id:
                        return execution, recorded, False
                    raise ConflictError(f"attempt {attempt} of execution {exec_id} was already reconciled")
            raise StatusTransitionError(f"execution {exec_id} is {execution.state.value}; there is nothing to reconcile")
        now = self.now()
        decided = check_timestamp(decided_at, "decided_at") if decided_at is not None else now
        if parse_timestamp(decided) > parse_timestamp(now):
            decided = now  # never later than the moment it is applied
        retry_until = (
            format_timestamp(parse_timestamp(decided) + RETRY_AUTHORIZATION_VALIDITY)
            if choice is ReconciliationChoice.AUTHORIZE_RETRY
            else None
        )
        reconciliation = ActionReconciliation(
            execution_id=exec_id,
            attempt=attempt,
            action_fingerprint=action_fingerprint,
            choice=choice,
            decided_at=decided,
            actor=actor,
            comment=comment,
            retry_authorized_until=retry_until,
        )
        new = execution.resolve(reconciliation, at=now)
        if not self.repository.replace(execution, new, reconciliation=reconciliation):
            raise ConflictError(f"execution {exec_id} changed while it was being reconciled")
        return new, reconciliation, True

    def close_abandoned(
        self,
        execution: ActionExecution,
        *,
        result: AttemptResult,
        payload: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        detail: str = "",
    ) -> ActionExecution:
        """Close the open attempt of an owner that is gone, with the best evidence available."""
        attempt = self.open_attempt(execution)
        return self.finish(execution, attempt, result=result, payload=payload, failure=failure, detail=detail)


__all__ = [
    "EXECUTION_ID_RE",
    "EXECUTION_TRANSITIONS",
    "MAX_RESULT_BYTES",
    "RECONCILE_PERMISSION",
    "RETRY_AUTHORIZATION_VALIDITY",
    "ActionExecution",
    "ActionReconciliation",
    "AttemptResult",
    "Dispatch",
    "ExecutionAttempt",
    "ExecutionLedger",
    "ExecutionSpec",
    "ExecutionState",
    "LedgerKind",
    "ReconciliationChoice",
    "bounded_result",
    "check_replace",
    "execution_id",
    "failure_record",
    "idempotency_key",
]

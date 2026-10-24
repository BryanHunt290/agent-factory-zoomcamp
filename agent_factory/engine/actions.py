"""The execution boundary: write-ahead dispatch of approval-gated tool calls.

``ToolGateway`` hands every approved action to an :class:`ActionGuard`. The
guard is the only code that runs an approved tool, and it follows the
approved protocol (docs/design/action-execution.md, "dispatch protocol"):

1. find or create the action's execution record (stable ID from project,
   run, approval request, and fingerprint);
2. a recorded success is **replayed**, never run again; a recorded failure
   or lapse is reported again; an uncertain outcome raises
   ``ReconciliationRequired``;
3. otherwise **write-ahead**: the ledger records the attempt and commits it
   (``ExecutionLedger.begin``), then ``action_dispatching`` is appended,
   fsync'd, to the hash-chained execution log. Only then does the tool run;
4. the outcome goes first to the execution log, then to the ledger. A crash
   between the two leaves the outcome on disk for recovery to find.

If the outcome is unknown, the tool's class decides: class A and B retry
(B with the same idempotency key, inside the provider's window); class C
becomes ``uncertain`` and is never resent automatically.

For a worker holding the run's claim (docs/workers.md), the write-ahead
record is committed only if the worker's fence is current (the store fences
it), and ``before_dispatch`` proves ownership once more after it, the last
step before the tool runs. If ownership was lost in between, the action is not
sent: ``action_not_dispatched`` goes to the execution log, the ledger keeps
the open attempt, and the next owner's recovery reads that evidence and
returns the authorization. Once the tool has been invoked, losing ownership
cannot recall it: the outcome is recorded if this process still can, and
otherwise recovery applies the uncertainty rules.

Agents never implement any of this: they call tools, and the gateway and
guard decide whether a call runs, replays, or stops the run.
"""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.approvals import ExactAction
from ..domain.claims import OwnershipLost
from ..domain.errors import DomainError, StorageError
from ..domain.executions import (
    ActionExecution,
    ActionReconciliation,
    AttemptResult,
    Dispatch,
    ExecutionAttempt,
    ExecutionLedger,
    ExecutionSpec,
    ExecutionState,
    LedgerKind,
    ReconciliationChoice,
    bounded_result,
    execution_id,
    failure_record,
)
from ..domain.identity import Actor
from ..domain.serialization import parse_timestamp
from ..safety.approval import PermissionDenied
from ..safety.execution import ExecutionLedgerUnavailable, ExecutionSemantics, ReconciliationRequired, semantics_of
from ..tools.base import NotDispatched, OutcomeUnknown, Tool, ToolError, ToolResult
from .execution_store import LedgerIntegrityError, check_binding, ensure_binding, ledger_path
from .log import ExecutionLog

#: Execution-log events that record an attempt's outcome, and what each means.
OUTCOME_EVENTS: Mapping[str, AttemptResult] = {
    "action_succeeded": AttemptResult.SUCCEEDED,
    "action_failed": AttemptResult.FAILED,
    "action_not_dispatched": AttemptResult.NOT_DISPATCHED,
    "action_indeterminate": AttemptResult.INDETERMINATE,
}
LOST_OUTCOME = "the process stopped after the dispatch was recorded and before its outcome was recorded"
REPORTED_UNKNOWN = "the tool reported that the outcome is unknown"


def new_owner() -> str:
    """Who holds a run while it is being changed: host, process, and a random suffix. Not a secret."""
    return f"{socket.gethostname()[:100]}:{os.getpid()}:{secrets.token_hex(4)}"


@dataclass(frozen=True)
class ExecutionContext:
    """What a dispatch carries to the tool (via ``ToolContext``)."""

    execution_id: str
    attempt: int
    idempotency_key: str | None


class ActionGuard:
    """Write-ahead execution, replay, and recovery for one run's approval-gated actions.

    Every method assumes the caller holds the run lock: no other process is
    changing this run. That is what makes "an attempt is open, so its owner
    stopped" a sound conclusion during recovery.
    """

    MAX_STEPS = 32  # bound on the decision loop of one call; never reached by a correct ledger

    def __init__(
        self,
        *,
        ledger: ExecutionLedger,
        log: ExecutionLog,
        run_dir: Path,
        project_id: str | Callable[[], str],
        run_id: str,
        owner: str,
        semantics_for: Callable[[str], ExecutionSemantics],
        approval_window: Callable[[str], str | None] = lambda request_id: None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> None:
        self.ledger = ledger
        self.log = log
        self.run_dir = Path(run_dir)
        self._project_id = project_id
        self.run_id = run_id
        self.owner = owner
        self._semantics_for = semantics_for
        self._approval_window = approval_window
        #: Proves the worker still owns the run; raises OwnershipLost (docs/workers.md).
        self._before_dispatch = before_dispatch
        self._bound = False
        self._store(lambda: check_binding(self.run_dir, run_id, ledger.kind))

    # ----------------------------------------------------------- plumbing

    @property
    def project_id(self) -> str:
        """Resolved on first use: a run that never uses an approved action never needs one."""
        if callable(self._project_id):
            self._project_id = self._project_id()
        return self._project_id

    def _store(self, operation: Callable[[], Any]) -> Any:
        """Run a ledger operation; infrastructure failures and integrity failures block the run."""
        try:
            return operation()
        except (StorageError, LedgerIntegrityError) as exc:
            raise ExecutionLedgerUnavailable(f"{type(exc).__name__}: {exc}") from None

    def _bind(self) -> None:
        if self._bound:
            return
        if self.ledger.kind is LedgerKind.PRODUCT_STORE:
            # Durably mark the run as database-backed before its first execution exists, so it
            # can never be resumed against an empty workspace ledger and dispatch a second time.
            self._store(lambda: ensure_binding(self.run_dir, self.run_id, self.project_id, self.ledger.kind))
        self._bound = True

    def _list(self) -> list[ActionExecution]:
        if self.ledger.kind is LedgerKind.CHECKPOINT and not ledger_path(self.run_dir).is_file():
            return []  # nothing recorded for this run yet
        return self._store(lambda: self.ledger.list_for_run(self.project_id, self.run_id))

    def _get(self, exec_id: str) -> ActionExecution | None:
        return self._store(lambda: self.ledger.get(self.project_id, self.run_id, exec_id))

    def _event(self, event: str, execution: ActionExecution, **fields: Any) -> None:
        self.log.append(
            event,
            run_id=self.run_id,
            execution_id=execution.id,
            request_id=execution.request_id,
            tool=execution.action.tool,
            category=execution.action.category,
            action_fingerprint=execution.action_fingerprint,
            **fields,
        )

    # ------------------------------------------------------------ queries

    def uncertain(self) -> list[ActionExecution]:
        return [e for e in self._list() if e.state is ExecutionState.UNCERTAIN]

    def find(self, exec_id: str) -> ActionExecution | None:
        return self._get(exec_id)

    def attempts(self, execution: ActionExecution) -> list[ExecutionAttempt]:
        return self._store(lambda: self.ledger.attempts(execution))

    def reconcile(
        self,
        exec_id: str,
        *,
        attempt: int,
        action_fingerprint: str,
        choice: ReconciliationChoice,
        actor: Actor,
        comment: str = "",
        decided_at: str | None = None,
    ) -> tuple[ActionExecution, ActionReconciliation, bool]:
        """Record a person's determination of an uncertain attempt (see ExecutionLedger.reconcile)."""
        return self._store(
            lambda: self.ledger.reconcile(
                self.project_id, self.run_id, exec_id, attempt=attempt, action_fingerprint=action_fingerprint,
                choice=choice, actor=actor, comment=comment, decided_at=decided_at,
            )  # fmt: skip
        )

    # ----------------------------------------------------- the gateway call

    def execute(
        self,
        *,
        request_id: str,
        approval: Mapping[str, Any],
        tool: Tool,
        action: ExactAction,
        node: str,
        visit: int,
        agent: str,
        invoke: Callable[[ExecutionContext], ToolResult],
    ) -> ToolResult:
        """Run, replay, or refuse one approved action. The only path by which an approved tool runs."""
        semantics = semantics_of(tool)
        execution = self._current(request_id, approval, action, semantics, node=node, visit=visit, agent=agent)
        for _ in range(self.MAX_STEPS):
            state = execution.state
            if state is ExecutionState.SUCCEEDED:
                return self._replay_success(execution)
            if state is ExecutionState.FAILED:
                self._replay_failure(execution)
            if state is ExecutionState.LAPSED:
                self._event("action_replayed", execution, outcome="lapsed")
                raise PermissionDenied(
                    f"the approval for {execution.action.tool} lapsed ({execution.lapse_reason}); a new approval is needed"
                )
            if state is ExecutionState.UNCERTAIN:
                raise ReconciliationRequired(execution.id, execution.attempts, self._uncertain_reason(execution))
            if state is ExecutionState.DISPATCHING:
                execution = self.recover(execution)  # an earlier owner stopped mid-dispatch
                continue
            begun = self._store(lambda e=execution: self.ledger.begin(e, owner=self.owner))
            if begun is None:  # another writer changed it first: look again, never dispatch blind
                execution = self._get(execution.id) or execution
                continue
            if isinstance(begun, ActionExecution):  # the authorization expired before this attempt could begin
                execution = begun
                self._closed(execution, reason="authorization_expired")
                if execution.state is ExecutionState.LAPSED:
                    raise PermissionDenied(
                        f"the approval for {execution.action.tool} expired before the action could start; "
                        "a new approval is needed"
                    )
                raise ReconciliationRequired(execution.id, execution.attempts, self._uncertain_reason(execution))
            execution = self._dispatch(begun, semantics, invoke)
            if execution.state is ExecutionState.SUCCEEDED:
                return self._last_result  # the tool's own result, not a reconstruction
            if execution.state is ExecutionState.FAILED and self._last_error is not None:
                raise self._last_error
            if execution.state is ExecutionState.AUTHORIZED and isinstance(self._last_error, NotDispatched):
                raise self._last_error  # provably not sent; the agent decides, the authorization stays usable
        raise ExecutionLedgerUnavailable(f"execution {execution.id} did not settle; the ledger is inconsistent")

    def _current(
        self,
        request_id: str,
        approval: Mapping[str, Any],
        action: ExactAction,
        semantics: ExecutionSemantics,
        *,
        node: str,
        visit: int,
        agent: str,
    ) -> ActionExecution:
        exec_id = execution_id(self.project_id, self.run_id, request_id, action.fingerprint)
        existing = self._get(exec_id)
        if existing is not None:
            return existing
        self._bind()
        spec = ExecutionSpec(
            project_id=self.project_id,
            run_id=self.run_id,
            request_id=request_id,
            node=node,
            visit=visit,
            agent=agent,
            action=action,
            semantics=semantics,
            authorized_at=approval.get("decided_at") or self.ledger.now(),
            authorized_until=self._window(request_id, approval),
        )
        execution, created = self._store(lambda: self.ledger.authorize(spec))
        if created:
            self._event(
                "action_authorized",
                execution,
                effect=execution.effect.value,
                max_attempts=execution.max_attempts,
                authorized_until=execution.authorized_until,
            )
        return execution

    def _window(self, request_id: str, approval: Mapping[str, Any]) -> str | None:
        """Until when the approval authorizes a new dispatch: the earlier of its own expiry and the store's."""
        windows = [w for w in (approval.get("expires_at"), self._approval_window(request_id)) if w]
        return min(windows, key=parse_timestamp) if windows else None

    _last_result: ToolResult
    _last_error: BaseException | None = None

    def _dispatch(
        self, dispatch: Dispatch, semantics: ExecutionSemantics, invoke: Callable[[ExecutionContext], ToolResult]
    ) -> ActionExecution:
        execution, attempt = dispatch.execution, dispatch.attempt
        # Local write-ahead: durable (fsync'd) audit evidence that the action may now be sent.
        self._event(
            "action_dispatching", execution, attempt=attempt.attempt, idempotency_key=attempt.idempotency_key,
            owner=self.owner,
        )  # fmt: skip
        self._last_error = None
        if self._before_dispatch is not None:
            self._confirm_owner(execution, attempt)
        context = ExecutionContext(execution.id, attempt.attempt, attempt.idempotency_key)
        try:
            result = invoke(context)
        except NotDispatched as exc:
            self._last_error = exc
            outcome, payload, failure, detail = AttemptResult.NOT_DISPATCHED, None, None, type(exc).__name__
            self._event("action_not_dispatched", execution, attempt=attempt.attempt, error=failure_record(type(exc).__name__, str(exc)))
        except ToolError as exc:
            self._last_error = exc
            failure = failure_record(type(exc).__name__, str(exc))
            outcome, payload, detail = AttemptResult.FAILED, None, type(exc).__name__
            self._event("action_failed", execution, attempt=attempt.attempt, error=failure)
        except OutcomeUnknown as exc:
            outcome, payload, failure, detail = AttemptResult.INDETERMINATE, None, None, REPORTED_UNKNOWN
            self._event("action_indeterminate", execution, attempt=attempt.attempt, reason=REPORTED_UNKNOWN,
                        error_type=type(exc).__name__)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - after the write-ahead record, any surprise is an unknown outcome
            reason = f"the tool failed unexpectedly after the dispatch began ({type(exc).__name__})"
            outcome, payload, failure, detail = AttemptResult.INDETERMINATE, None, None, reason
            self._event("action_indeterminate", execution, attempt=attempt.attempt, reason=reason)
        else:
            self._last_result = result
            payload = bounded_result(result.ok, result.summary, result.data, replayable=semantics.replayable_result)
            outcome, failure, detail = AttemptResult.SUCCEEDED, None, ""
            self._event("action_succeeded", execution, attempt=attempt.attempt, ok=payload["ok"],
                        summary=payload["summary"], result=payload)  # fmt: skip
        new = self._store(
            lambda: self.ledger.finish(execution, attempt, result=outcome, payload=payload, failure=failure, detail=detail)
        )
        if outcome is AttemptResult.INDETERMINATE:
            self._after_indeterminate(new, detail)
        return new

    def _confirm_owner(self, execution: ActionExecution, attempt: ExecutionAttempt) -> None:
        """The last step before the side effect: does this process still own the run?

        The write-ahead record was committed under a fence check, but ownership
        can change at any moment after that commit. If it has, nothing is sent.
        This process may no longer write the ledger, so the evidence goes to
        the execution log, where the next owner's recovery finds it.
        """
        try:
            self._before_dispatch()  # type: ignore[misc]
        except OwnershipLost:
            self._event(
                "action_not_dispatched", execution, attempt=attempt.attempt,
                error=failure_record("OwnershipLost", "this worker no longer owns the run; the action was not sent"),
            )  # fmt: skip
            raise

    def _after_indeterminate(self, execution: ActionExecution, reason: str) -> None:
        if execution.state is ExecutionState.UNCERTAIN:
            self._event("action_uncertain", execution, attempt=execution.attempts, reason=reason)
        elif execution.state is ExecutionState.FAILED:
            self._last_error = ToolError(f"{execution.action.tool}: every automatic attempt ended without an outcome")
            self._event("action_failed", execution, attempt=execution.attempts, error=execution.failure)
        # AUTHORIZED: the class allows another attempt; the loop dispatches it (same idempotency key).

    def _closed(self, execution: ActionExecution, *, reason: str) -> None:
        if execution.state is ExecutionState.LAPSED:
            self._event("action_lapsed", execution, reason=execution.lapse_reason)
        elif execution.state is ExecutionState.UNCERTAIN:
            self._event(
                "action_uncertain", execution, attempt=execution.attempts,
                reason=f"an earlier attempt's outcome is unknown and its retry can no longer happen ({reason})",
            )  # fmt: skip

    # --------------------------------------------------------------- replay

    def _replay_success(self, execution: ActionExecution) -> ToolResult:
        recorded = execution.result or {}
        self._event("action_replayed", execution, outcome="succeeded", confirmed=bool(recorded.get("confirmed_by")))
        return ToolResult(
            data=recorded.get("data"),
            summary=str(recorded.get("summary", "")),
            ok=bool(recorded.get("ok", True)),
            details={"replayed": True, "execution_id": execution.id},
        )

    def _replay_failure(self, execution: ActionExecution) -> None:
        failure = execution.failure or {}
        self._event("action_replayed", execution, outcome="failed")
        if failure.get("type") == "ConfirmedNotExecuted":
            raise PermissionDenied(f"{execution.action.tool}: {failure.get('message', '')}")
        raise ToolError(f"{execution.action.tool} already failed: {failure.get('message', '')}")

    @staticmethod
    def _uncertain_reason(execution: ActionExecution) -> str:
        return f"attempt {execution.attempts} of {execution.action.tool} may or may not have taken effect"

    # ------------------------------------------------------------- recovery

    def _evidence(self, exec_id: str, attempt: int) -> Mapping[str, Any] | None:
        found = None
        for entry in self.log.entries():
            if entry.get("event") in OUTCOME_EVENTS and entry.get("execution_id") == exec_id and entry.get("attempt") == attempt:
                found = entry
        return found

    def recover(self, execution: ActionExecution) -> ActionExecution:
        """Close the open attempt of an owner that stopped, with the best evidence there is.

        Sound only under the run lock: holding it proves no live process on
        this host is still dispatching. An outcome found in the execution log
        is known; without one the outcome is unknown and the class decides.
        """
        attempt = self._store(lambda: self.ledger.open_attempt(execution))
        evidence = self._evidence(execution.id, attempt.attempt)
        if evidence is not None:
            result = OUTCOME_EVENTS[evidence["event"]]
            payload = evidence.get("result") if result is AttemptResult.SUCCEEDED else None
            failure = evidence.get("error") if result is AttemptResult.FAILED else None
            detail = "outcome recovered from the execution log"
        else:
            result, payload, failure, detail = AttemptResult.INDETERMINATE, None, None, LOST_OUTCOME
        new = self._store(
            lambda: self.ledger.close_abandoned(execution, result=result, payload=payload, failure=failure, detail=detail)
        )
        self._event(
            "action_recovered", new, attempt=attempt.attempt, previous_owner=attempt.owner, result=result.value,
            source="execution_log" if evidence is not None else "class_policy", state=new.state.value,
        )  # fmt: skip
        if result is AttemptResult.INDETERMINATE:
            self._after_indeterminate(new, detail)
            self._last_error = None
        return new

    def recover_open(self) -> None:
        """At the start of a resume: settle every attempt a previous owner left open."""
        for execution in self._list():
            if execution.state is ExecutionState.DISPATCHING:
                self.recover(execution)

    # ------------------------------------------------- unused authorizations

    def _close(self, execution: ActionExecution, reason: str) -> None:
        closed = self._store(lambda: self.ledger.close(execution, reason=reason))
        self._closed(closed, reason=reason)

    def close_visit(self, node: str, visit: int, approvals: Mapping[str, Mapping[str, Any]]) -> None:
        """A node visit completed: authorizations it never used can never be used (request IDs include the visit)."""
        self._materialize_unused(approvals, node=node, visit=visit)
        for execution in self._list():
            if execution.state is ExecutionState.AUTHORIZED and (execution.node, execution.visit) == (node, visit):
                self._close(execution, "node_visit_ended")

    def close_run(self, approvals: Mapping[str, Mapping[str, Any]], *, reason: str) -> None:
        """The run reached a final status: no authorization of it can be used any more."""
        self._materialize_unused(approvals)
        for execution in self._list():
            if execution.state is ExecutionState.AUTHORIZED:
                self._close(execution, reason)

    def _materialize_unused(
        self, approvals: Mapping[str, Mapping[str, Any]], *, node: str | None = None, visit: int | None = None
    ) -> None:
        """Record approved actions the agent never asked for again, so their lapse is visible."""
        for request_id, record in sorted(approvals.items()):
            if not record.get("approved") or "visit" not in record or "agent" not in record:
                continue  # rejected, or recorded before this version (no node visit to bind to)
            if node is not None and (record.get("node"), record.get("visit")) != (node, visit):
                continue
            try:
                action = ExactAction(tool=record["tool"], category=record["category"], params=record["params"])
            except (KeyError, DomainError):
                continue
            if self._get(execution_id(self.project_id, self.run_id, request_id, action.fingerprint)) is None:
                self._current(
                    request_id, record, action, self._semantics_for(action.tool),
                    node=record["node"], visit=record["visit"], agent=record["agent"],
                )  # fmt: skip


def workspace_guard(workspace: Any, run_id: str, *, registry: Any, clock: Any = None) -> ActionGuard:
    """A guard on the workspace's checkpoint ledger, for a ``ToolGateway`` used outside an executor.

    The caller must hold the run exclusively (as an executor holds its run lock).
    """
    from ..util import utc_now
    from .execution_store import CheckpointExecutionRepository

    clock = clock or utc_now
    runs_dir = Path(workspace.runs_dir)
    ledger = ExecutionLedger(CheckpointExecutionRepository(runs_dir), kind=LedgerKind.CHECKPOINT, clock=clock)
    return ActionGuard(
        ledger=ledger,
        log=ExecutionLog(runs_dir / run_id / ExecutionLog.FILENAME, clock),
        run_dir=runs_dir / run_id,
        project_id=workspace.ensure_project_id,
        run_id=run_id,
        owner=new_owner(),
        semantics_for=lambda name: semantics_of(registry.get(name)),
    )


__all__ = ["ActionGuard", "ExecutionContext", "LOST_OUTCOME", "OUTCOME_EVENTS", "new_owner", "workspace_guard"]

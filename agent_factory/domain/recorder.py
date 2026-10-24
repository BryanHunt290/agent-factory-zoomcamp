"""Bridge from the engine to the product domain.

The executor calls :meth:`ProgressRecorder.sync` after **every committed
checkpoint**. The recorder derives product records from that committed state:

* the run record's status;
* milestones, via the project's milestone policy (node + outcome -> milestone);
* approval requests;
* milestones for approved actions (``action_execution``), from the execution
  records the run's ledger keeps in the same store;
* the final run milestone;
* notification intents for attention events (an approval request waiting
  for a decision, an uncertain action waiting for a person), in the same
  transaction as the records that make them durable (docs/notifications.md).

Deriving from committed state, rather than emitting mid-step, gives three
properties:

* **Idempotent.** Milestone IDs are deterministic (project, run, key), so
  syncing the same state twice records nothing new.
* **No phantom milestones.** If the process dies after a node ran but before
  its checkpoint was saved, the node re-runs on resume. Only the outcome that
  was actually committed ever becomes a milestone.
* **No gaps.** Anything committed but not yet recorded is picked up by the
  next sync.

Human decisions are the exception: they are stored *before* the engine
applies them. :meth:`record_decision` (the local CLI) and the API record a
decision, and wake the run in the same transaction; a worker that holds the
run's claim applies it later (``Executor.advance``). Before anything is
applied, :meth:`consumable_decision` and :meth:`reconciliation_decision`
re-check the stored records against the engine's exact pending action, the
approval's expiry, and the person who decided. A contradiction raises
``DecisionRefused`` and nothing is applied.

Each :meth:`sync` is one transaction: the records derived from a checkpoint
are stored together or not at all.

The execution log is not touched. It remains the low-level audit trail.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..state import ProjectState, RunStatus
from ..util import Clock, timestamp, utc_now
from .attention import AttentionItem, AttentionKind
from .approvals import ApprovalDecision, ApprovalOutcome, ApprovalRequest, ApprovalStatus, ExactAction, approval_expiry, risk_for
from .errors import ApprovalBindingError, ConflictError, DecisionRefused, DomainValidationError, StatusTransitionError
from .executions import ActionExecution, ExecutionState, ReconciliationChoice
from .identity import Actor, ActorKind
from .ids import milestone_id
from .model_usage import RunModelUsage
from .notifications import NotificationIntent
from .milestones import (
    ACTION_EXECUTION,
    HUMAN_DECISION,
    RUN_FINISHED,
    RUN_RESUMED,
    Milestone,
    MilestonePolicy,
    MilestoneSource,
    MilestoneStatus,
)
from .principals import Permission
from .project import Project, Run
from .reconciliation import ReconciliationDecision, check_against
from .repositories import MilestoneQuery, ProductStore
from .serialization import parse_timestamp
from .services import ApprovalService

if TYPE_CHECKING:
    from ..workspace import Workspace

_TEXT_LIMIT = 1000


def _clip(text: str, limit: int = _TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ProgressRecorder:
    """Records one project's runs, milestones, and approvals from engine checkpoints."""

    def __init__(
        self,
        store: ProductStore,
        project: Project,
        *,
        node_agents: Mapping[str, str] | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.project = project
        self.policy: MilestonePolicy = project.milestone_policy
        self._node_agents = dict(node_agents or {})
        self._clock = clock
        self.approvals = ApprovalService(store.approvals, clock)

    @classmethod
    def for_workspace(
        cls,
        store: ProductStore,
        workspace: Workspace,
        *,
        name: str,
        policy: MilestonePolicy,
        template: str | None = None,
        description: str = "",
        node_agents: Mapping[str, str] | None = None,
        clock: Clock = utc_now,
    ) -> ProgressRecorder:
        """Bind to the workspace's project, registering the project on first use.

        An already-registered project keeps its stored milestone policy, so
        the meaning of its earlier milestones never changes underneath it.
        """
        project_id = workspace.ensure_project_id()
        project = store.projects.get(project_id)
        if project is None:
            project = Project(
                id=project_id,
                name=name,
                created_at=timestamp(clock),
                milestone_policy=policy,
                template=template,
                description=description,
            )
            try:
                store.projects.add(project)
            except ConflictError:  # registered concurrently by another process: use theirs
                project = store.projects.get(project_id) or project
        return cls(store, project, node_agents=node_agents, clock=clock)

    # ------------------------------------------------------------------ sync

    def sync(self, state: ProjectState) -> None:
        """Derive and store every record for one committed checkpoint, atomically."""
        now = timestamp(self._clock)
        with self.store.transaction():
            self._sync_run(state, now)
            self._sync_history(state, now)
            self._sync_blocked(state, now)
            self._sync_approvals(state, now)
            self._sync_executions(state, now)
            self._sync_resumptions(state, now)
            self._sync_finished(state, now)
            self._sync_model_usage(state, now)

    def consumable_decision(self, state: ProjectState, *, now: str) -> dict[str, Any] | None:
        """The decision a worker may apply to the run's pending approval, after re-checking it; or its expiry.

        Returned in the engine's own terms (``Executor._apply_decision``), or
        None while the request still waits (no decision, not expired). Checked,
        in order:

        * the stored request is the engine's exact pending action: same run,
          node, agent, and fingerprint (else ``DecisionRefused``);
        * the stored decision carries that fingerprint, and the request's
          status agrees with it (else ``DecisionRefused``);
        * expiry (``now`` is server time; the earlier of the engine's and the
          store's expiry counts): no decision before it, a decision made at or
          after it, or an approval applied at or after it, is applied as
          ``refused="expired"``, never as approved;
        * an approval stands only while its decider may decide: an enrolled,
          active principal that still holds ``approvals:decide``, or the local
          operator. Otherwise it is applied as refused, never as approved. A
          rejection is always applied.
        """
        pending = state.pending_approval
        if state.status is not RunStatus.AWAITING_APPROVAL or not pending:
            return None
        action = self._action(pending)
        request_id = pending["request_id"]
        request = self.store.approvals.get_request(self.project.id, request_id)
        deadlines = [pending.get("expires_at") or approval_expiry(pending["requested_at"])]
        if request is not None:
            stored = (request.run_id, request.node, request.agent, request.action_fingerprint)
            if stored != (state.run_id, pending["node"], pending["agent"], action.fingerprint):
                raise DecisionRefused(f"approval {request_id}: the stored request is not the action the engine is waiting on")
            if request.expires_at is not None:
                deadlines.append(request.expires_at)
        expires_at = min(deadlines, key=parse_timestamp)
        expired = parse_timestamp(now) >= parse_timestamp(expires_at)
        decision = self.store.approvals.get_decision(self.project.id, request_id) if request is not None else None
        if decision is None:
            if not expired:
                return None
            return {"approved": False, "approver": "", "note": "", "decided_at": now, "refused": "expired"}
        if decision.action_fingerprint != action.fingerprint:
            raise DecisionRefused(f"approval {request_id}: the stored decision is for a different action than the engine's")
        if request.status is not decision.outcome.status:  # type: ignore[union-attr]
            raise DecisionRefused(
                f"approval {request_id}: the request is {request.status.value}, but its decision is {decision.outcome.value}"  # type: ignore[union-attr]
            )
        decided: dict[str, Any] = {
            "approver": decision.actor.display_name,
            "note": decision.comment,
            "decided_at": decision.decided_at,
            "actor_id": decision.actor.id,
        }
        if decision.outcome is ApprovalOutcome.REJECTED:
            return {**decided, "approved": False}
        if expired or parse_timestamp(decision.decided_at) >= parse_timestamp(expires_at):
            return {**decided, "approved": False, "refused": "expired"}
        distrust = self._distrust(decision.actor, Permission.APPROVALS_DECIDE)
        if distrust is not None:
            return {**decided, "approved": False, "refused": distrust}
        return {**decided, "approved": True}

    def reconciliation_decision(self, execution: ActionExecution) -> ReconciliationDecision | None:
        """The recorded decision about ``execution``'s uncertain attempt, re-checked; None while none is recorded.

        The decision must be exactly about this execution's uncertain attempt
        and action, by someone who may still decide; otherwise
        ``DecisionRefused``, and the execution stays uncertain.
        """
        decision = self.store.reconciliations.get(self.project.id, execution.id, execution.attempts)
        if decision is None:
            return None
        try:
            check_against(decision, execution)
        except (DomainValidationError, ConflictError, StatusTransitionError) as exc:
            raise DecisionRefused(f"reconciliation of {execution.id}: {exc}") from None
        distrust = self._distrust(decision.actor, Permission.ACTIONS_RECONCILE)
        if distrust is not None:
            raise DecisionRefused(f"reconciliation of {execution.id}: not applied ({distrust})")
        return decision

    def _distrust(self, actor: Actor, permission: Permission) -> str | None:
        """Why ``actor`` may not make this decision now, or None.

        A user must still be an enrolled, active principal holding ``permission``
        when a worker applies the decision, not only when it was recorded. The
        local operator (the unauthenticated CLI, with the workspace's own
        filesystem access) may; a system actor never may.
        """
        if actor.kind is ActorKind.SYSTEM:
            return "system_actor"
        if actor.kind is ActorKind.USER:
            principal = self.store.principals.get(actor.id)
            if principal is None:
                return "principal_unknown"
            if not principal.active:
                return "principal_disabled"
            if permission not in self.store.principals.permissions(actor.id):
                return "permission_revoked"
        return None

    def record_decision(
        self,
        state: ProjectState,
        request_id: str,
        *,
        approved: bool,
        approver: str,
        note: str,
        decided_at: str,
    ) -> ApprovalDecision:
        """Validate and record a decision before the engine acts on it.

        The fingerprint is computed from the action the engine is waiting on.
        If the domain already holds a different decision for it, or the action
        does not match, this raises and the run stays paused.
        """
        pending = state.pending_approval or {}
        with self.store.transaction():
            if self.store.approvals.get_request(self.project.id, request_id) is None:
                self.approvals.open_request(self._request_from_pending(state, pending))
            decision = self.approvals.decide(
                project_id=self.project.id,
                request_id=request_id,
                action_fingerprint=self._action(pending).fingerprint,
                outcome=ApprovalOutcome.APPROVED if approved else ApprovalOutcome.REJECTED,
                actor=Actor.unauthenticated(approver),
                comment=note,
                at=decided_at,
            )
            # The transactional outbox: the decision and the run's wake-up commit together.
            self.store.queue.wake(self.project.id, state.run_id)
        return decision

    # ------------------------------------------------------------------- run

    def _sync_run(self, state: ProjectState, now: str) -> None:
        pid = self.project.id
        reason = _clip(self._status_reason(state))
        run = self.store.runs.get(pid, state.run_id)
        if run is None:
            created = state.created_at or now
            updated = now if parse_timestamp(now) >= parse_timestamp(created) else created
            self.store.runs.add(
                Run(
                    project_id=pid,
                    run_id=state.run_id,
                    number=len(self.store.runs.list_for_project(pid)) + 1,
                    graph_name=state.graph_name,
                    status=state.status,
                    created_at=created,
                    updated_at=updated,
                    status_reason=reason,
                )
            )
        elif run.status is not state.status:
            self.store.runs.update(run.transition(state.status, at=now, reason=reason))
        elif run.status_reason != reason:
            self.store.runs.update(Run.from_dict({**run.to_dict(), "status_reason": reason, "updated_at": now}))

    @staticmethod
    def _status_reason(state: ProjectState) -> str:
        if state.status is RunStatus.BLOCKED and state.blocked:
            return f"{state.blocked['type']}: {state.blocked['message']}"
        if state.status in (RunStatus.FAILED, RunStatus.EXHAUSTED) and state.error:
            return f"{state.error['type']}: {state.error['message']}"
        if state.status is RunStatus.AWAITING_APPROVAL and state.pending_approval:
            return f"waiting for a decision on approval {state.pending_approval['request_id']}"
        if state.status is RunStatus.AWAITING_RECONCILIATION and state.pending_reconciliation:
            pending = state.pending_reconciliation
            return f"waiting for a person to say whether action {pending['execution_id']} ({pending['tool']}) happened"
        return ""

    # ------------------------------------------------------------ milestones

    def _sync_history(self, state: ProjectState, now: str) -> None:
        previous: dict[str, Any] | None = None
        for entry in state.history:
            rule = self.policy.rule_for(entry["node"])
            if rule is not None and entry["outcome"] in rule.outcomes:
                metadata: dict[str, Any] = {"outcome": entry["outcome"], "visit": entry["visit"]}
                if previous is not None:
                    metadata["after"] = {"node": previous["node"], "outcome": previous["outcome"]}
                self._record(
                    state,
                    now,
                    key=f"step:{entry['step']}",
                    milestone_type=rule.milestone_type,
                    status=rule.outcomes[entry["outcome"]],
                    summary=entry.get("summary", ""),
                    source=self._source(entry["node"], entry["step"]),
                    metadata=metadata,
                )
            previous = entry

    def _sync_blocked(self, state: ProjectState, now: str) -> None:
        if state.status is not RunStatus.BLOCKED or not state.blocked:
            return
        rule = self.policy.rule_for(state.blocked["node"])
        if rule is None or not rule.record_blocked:
            return  # the run record's status still says blocked
        step = state.blocked["step"]
        self._record(
            state,
            now,
            key=f"step:{step}",  # the same key the node's real outcome will use once unblocked
            milestone_type=rule.milestone_type,
            status=MilestoneStatus.BLOCKED,
            summary=f"Blocked: {state.blocked['message']}",
            source=self._source(state.blocked["node"], step),
            metadata={"blocked": state.blocked["type"]},
        )

    def _sync_approvals(self, state: ProjectState, now: str) -> None:
        pending = state.pending_approval
        if pending is not None and state.status is RunStatus.AWAITING_APPROVAL:
            request = self.approvals.open_request(self._request_from_pending(state, pending))
            if request.status is ApprovalStatus.PENDING and not request.is_expired(now):
                self._notify(
                    AttentionItem(
                        kind=AttentionKind.DECISION_REQUIRED, project_id=request.project_id, run_id=request.run_id,
                        approval_id=request.id, since=request.requested_at, tool=request.action.tool,
                        category=request.action.category, expires_at=request.expires_at,
                    ),
                    now,
                )  # fmt: skip
            self._record(
                state,
                now,
                key=f"approval:{request.id}",
                milestone_type=HUMAN_DECISION,
                status=MilestoneStatus.BLOCKED,
                summary=f"Approval needed: {request.agent} wants to run {request.action.tool} {request.action.params}",
                source=MilestoneSource(node=request.node, agent=request.agent, step=pending.get("step")),
                metadata={"approval_id": request.id, "category": request.action.category, "tool": request.action.tool},
            )
        for request_id, decided in sorted(state.approvals.items()):
            refused = decided.get("refused")
            outcome = "approved" if decided.get("approved") else "rejected"
            status, summary = MilestoneStatus.COMPLETED, f"{outcome.capitalize()} by {decided.get('approver', 'unknown')}"
            if refused == "expired":
                outcome, status = "expired", MilestoneStatus.FAILED
                summary = f"Expired: the approval for {decided.get('tool')} could no longer be used; a new request is needed"
            elif refused:
                outcome, status = "refused", MilestoneStatus.FAILED
                summary = f"Not honored ({refused}): the approval for {decided.get('tool')} was not used"
            self._reconcile_decision(request_id, decided, now)
            self._record(
                state,
                now,
                key=f"approval:{request_id}",
                milestone_type=HUMAN_DECISION,
                status=status,
                summary=summary,
                source=MilestoneSource(),
                metadata={
                    "approval_id": request_id,
                    "category": decided.get("category"),
                    "tool": decided.get("tool"),
                    "decision": outcome,
                },
            )

    def _reconcile_decision(self, request_id: str, decided: Mapping[str, Any], now: str) -> None:
        """Make the stored decision agree with one the engine has already applied.

        Normally the store already holds it (``record_decision`` ran first). It
        can be missing if the decision was made while no product store was
        attached; it is then recorded now, bound to the action the engine
        recorded. A stored decision that contradicts the engine is never
        overwritten: that raises, and needs a human to investigate.
        """
        request = self.store.approvals.get_request(self.project.id, request_id)
        if request is None:
            return  # never opened in this store; the human_decision milestone still records it
        if decided.get("refused"):
            # The engine applied an approval that may not be used. Never record a decision for it:
            # an undecided request is marked expired; a stored decision stays as the person made it.
            if decided["refused"] == "expired" and request.status is ApprovalStatus.PENDING:
                self.approvals.expire(project_id=self.project.id, request_id=request_id, at=now)
            return
        outcome = ApprovalOutcome.APPROVED if decided.get("approved") else ApprovalOutcome.REJECTED
        stored = self.store.approvals.get_decision(self.project.id, request_id)
        if stored is not None:
            if stored.outcome is not outcome:
                raise ConflictError(
                    f"approval {request_id}: the product store records {stored.outcome.value}, "
                    f"but the engine applied {outcome.value}"
                )
            return
        engine_action = ExactAction(
            tool=decided.get("tool"), category=decided.get("category"), params=decided.get("params", {})
        )
        self.approvals.decide(
            project_id=self.project.id,
            request_id=request_id,
            action_fingerprint=engine_action.fingerprint,  # must equal the stored request's, or this raises
            outcome=outcome,
            actor=Actor.unauthenticated(decided.get("approver", "")),
            comment=decided.get("note", ""),
            at=decided.get("decided_at") or now,
        )

    def _sync_executions(self, state: ProjectState, now: str) -> None:
        """One milestone per approved action that was attempted: progress a person cares about.

        Only meaningful changes become milestone statuses: running, done,
        failed, outcome unknown (a person must check), and a person-authorized
        retry. An authorization that was never used gets no milestone; the
        approval's own milestone already shows the decision.
        """
        for execution in self.store.executions.list_for_run(self.project.id, state.run_id):
            if (
                execution.state is ExecutionState.UNCERTAIN
                and execution.request_id is not None
                and self.store.reconciliations.get(self.project.id, execution.id, execution.attempts) is None
            ):
                self._notify(
                    AttentionItem(
                        kind=AttentionKind.RECONCILIATION_REQUIRED, project_id=execution.project_id, run_id=execution.run_id,
                        approval_id=execution.request_id, since=execution.updated_at, tool=execution.action.tool,
                        category=execution.action.category, execution_id=execution.id, attempt=execution.attempts,
                    ),
                    now,
                )  # fmt: skip
            shown = self._execution_milestone(execution)
            mid = milestone_id(self.project.id, state.run_id, f"action:{execution.id}")
            if shown is None and self.store.milestones.get(mid) is None:
                continue
            status, summary = shown or (MilestoneStatus.FAILED, f"An approved action was not carried out: {execution.action.tool}.")
            self._record(
                state,
                now,
                key=f"action:{execution.id}",
                milestone_type=ACTION_EXECUTION,
                status=status,
                summary=summary,
                source=MilestoneSource(node=execution.node, agent=execution.agent),
                metadata={
                    "execution_id": execution.id,
                    "approval_id": execution.request_id,
                    "tool": execution.action.tool,
                    "category": execution.action.category,
                    "action_fingerprint": execution.action_fingerprint,
                    "state": execution.state.value,
                    "attempt": execution.attempts,
                    "effect": execution.effect.value,
                },
            )

    def _sync_resumptions(self, state: ProjectState, now: str) -> None:
        """One milestone each time a person's decision let the run continue. Never for crash recovery."""
        for request_id, decided in sorted(state.approvals.items()):
            if "applied_at" not in decided:
                continue  # applied before this version recorded it
            tool, who = decided.get("tool"), decided.get("approver") or "a person"
            if decided.get("refused") == "expired":
                summary = f"Resumed: the approval for {tool} expired, so the run continued without it."
            elif decided.get("refused"):
                summary = f"Resumed: the approval for {tool} was not honored, so the run continued without it."
            else:
                verb = "approved" if decided.get("approved") else "rejected"
                summary = f"Resumed after {who} {verb} {tool}."
            self._record(
                state, now, key=f"resumed:approval:{request_id}", milestone_type=RUN_RESUMED,
                status=MilestoneStatus.COMPLETED, summary=summary, source=MilestoneSource(node=decided.get("node")),
                metadata={"approval_id": request_id, "after": "approval"},
            )  # fmt: skip
        if state.status is RunStatus.AWAITING_RECONCILIATION:
            return  # a reconciliation was applied, but the run still waits for another
        for execution in self.store.executions.list_for_run(self.project.id, state.run_id):
            for reconciliation in self.store.executions.reconciliations(self.project.id, state.run_id, execution.id):
                tool, who = execution.action.tool, reconciliation.actor.display_name
                summary = {
                    ReconciliationChoice.IT_HAPPENED: f"Resumed after {who} confirmed {tool} happened.",
                    ReconciliationChoice.DID_NOT_HAPPEN: f"Resumed after {who} confirmed {tool} did not happen.",
                    ReconciliationChoice.AUTHORIZE_RETRY: f"Resumed after {who} authorized one more attempt of {tool}.",
                }[reconciliation.choice]
                self._record(
                    state, now, key=f"resumed:action:{execution.id}:{reconciliation.attempt}", milestone_type=RUN_RESUMED,
                    status=MilestoneStatus.COMPLETED, summary=summary,
                    source=MilestoneSource(node=execution.node, agent=execution.agent),
                    metadata={"execution_id": execution.id, "attempt": reconciliation.attempt, "after": "reconciliation"},
                )  # fmt: skip

    @staticmethod
    def _execution_milestone(execution: ActionExecution) -> tuple[MilestoneStatus, str] | None:
        tool = execution.action.tool
        state = execution.state
        if state is ExecutionState.DISPATCHING:
            if execution.authorization_seq > 1:
                return MilestoneStatus.IN_PROGRESS, f"Retrying an approved action, as a person authorized: {tool}."
            return MilestoneStatus.IN_PROGRESS, f"Running an approved action: {tool}."
        if state is ExecutionState.SUCCEEDED:
            confirmed = (execution.result or {}).get("confirmed_by")
            suffix = " A person confirmed it happened." if confirmed else ""
            return MilestoneStatus.COMPLETED, f"An approved action completed: {tool}.{suffix}"
        if state is ExecutionState.FAILED:
            if (execution.failure or {}).get("type") == "ConfirmedNotExecuted":
                return MilestoneStatus.FAILED, f"An approved action did not take effect: {tool}. A person confirmed it did not happen."
            return MilestoneStatus.FAILED, f"An approved action did not take effect: {tool}."
        if state is ExecutionState.UNCERTAIN:
            return MilestoneStatus.BLOCKED, (
                f"Action outcome unknown: {tool} was started (attempt {execution.attempts}) and Agent Factory "
                "cannot tell whether it happened. A person must check before the run continues."
            )
        if state is ExecutionState.AUTHORIZED and execution.authorization_seq > 1:
            return MilestoneStatus.IN_PROGRESS, f"A person authorized one more attempt of {tool}."
        return None  # authorized and unused, or lapsed without ever being attempted

    def _sync_finished(self, state: ProjectState, now: str) -> None:
        if not state.status.is_terminal:
            return
        completed = state.status is RunStatus.COMPLETED
        if completed:
            summary = f"Run completed after {state.step} steps"
        else:
            message = (state.error or {}).get("message", "")
            summary = f"Run {state.status.value}: {message}"
        self._record(
            state,
            now,
            key="run:finished",
            milestone_type=RUN_FINISHED,
            status=MilestoneStatus.COMPLETED if completed else MilestoneStatus.FAILED,
            summary=summary,
            source=MilestoneSource(node=state.current_node, step=state.step),
            metadata={"run_status": state.status.value, "steps": state.step},
        )

    def _sync_model_usage(self, state: ProjectState, now: str) -> None:
        """The checkpoint's summary of the run's model usage, stored when it changed (counts only)."""
        summary = state.metadata.get("model_usage")
        if not isinstance(summary, dict):
            return
        usage = RunModelUsage.from_summary(self.project.id, state.run_id, summary, at=now)
        if not usage.same_counts(self.store.model_usage.get(self.project.id, state.run_id)):
            self.store.model_usage.put(usage)

    # --------------------------------------------------------------- helpers

    def _notify(self, item: AttentionItem, now: str) -> None:
        """The transactional outbox: a notification intent per eligible destination, in this sync's transaction.

        Idempotent: syncing the same attention event again creates nothing
        (one notification per event and destination). Nothing is sent here; a
        dispatcher sends it later (docs/notifications.md).
        """
        self.store.notifications.enqueue(NotificationIntent.for_item(item, self.project.name, at=now))

    def _record(
        self,
        state: ProjectState,
        now: str,
        *,
        key: str,
        milestone_type: str,
        status: MilestoneStatus,
        summary: str,
        source: MilestoneSource,
        metadata: dict[str, Any],
    ) -> None:
        """Create the milestone, move it to a new status, or do nothing if already recorded."""
        pid = self.project.id
        definition = self.policy.definition(milestone_type)
        mid = milestone_id(pid, state.run_id, key)
        summary = _clip(summary)
        existing = self.store.milestones.get(mid)
        if existing is None:
            self.store.milestones.add(
                Milestone(
                    id=mid,
                    project_id=pid,
                    run_id=state.run_id,
                    sequence=self.store.milestones.next_sequence(pid, state.run_id),
                    key=key,
                    type=milestone_type,
                    category=definition.category,
                    title=definition.title,
                    summary=summary,
                    status=status,
                    created_at=now,
                    occurred_at=now,
                    source=source,
                    metadata=metadata,
                    resolves_id=self._unresolved_failure(state.run_id, milestone_type)
                    if status is MilestoneStatus.COMPLETED
                    else None,
                )
            )
            return
        if existing.status is status:
            if existing.summary == summary and existing.metadata == metadata:
                return  # already recorded: syncing is idempotent
            if existing.is_terminal:
                raise ConflictError(f"milestone {mid} is already {status.value} with different content")
            self.store.milestones.update(existing.revise(at=now, summary=summary, metadata=metadata))
            return
        resolves = existing.resolves_id
        if resolves is None and status is MilestoneStatus.COMPLETED:
            resolves = self._unresolved_failure(state.run_id, milestone_type)
        self.store.milestones.update(
            existing.transition(status, at=now, summary=summary, metadata=metadata, resolves_id=resolves)
        )

    def _unresolved_failure(self, run_id: str, milestone_type: str) -> str | None:
        """The latest failed milestone of this type in the run that nothing has resolved yet."""
        same_type = self.store.milestones.query(
            MilestoneQuery(project_id=self.project.id, run_id=run_id, types=frozenset({milestone_type}))
        )
        resolved = {m.resolves_id for m in same_type if m.resolves_id}
        failed = [m for m in same_type if m.status is MilestoneStatus.FAILED and m.id not in resolved]
        return failed[-1].id if failed else None

    def _source(self, node: str, step: int) -> MilestoneSource:
        return MilestoneSource(node=node, agent=self._node_agents.get(node), step=step)

    @staticmethod
    def _action(pending: Mapping[str, Any]) -> ExactAction:
        return ExactAction(tool=pending["tool"], category=pending["category"], params=pending["params"])

    def _request_from_pending(self, state: ProjectState, pending: Mapping[str, Any]) -> ApprovalRequest:
        action = self._action(pending)
        return ApprovalRequest(
            id=pending["request_id"],
            project_id=self.project.id,
            run_id=state.run_id,
            node=pending["node"],
            agent=pending["agent"],
            action=action,
            action_fingerprint=action.fingerprint,
            reason=pending["reason"],
            risk=risk_for(action.category),
            requested_at=pending["requested_at"],
            expires_at=pending.get("expires_at") or approval_expiry(pending["requested_at"]),
            context={"step": pending.get("step")},
        )

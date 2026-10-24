"""Graph execution.

The executor walks a :class:`GraphDefinition` one node at a time:

1. log ``node_entered``;
2. run the node's agent against a snapshot of the state, with tool calls
   routed through a :class:`ToolGateway` (each logged as an ``action``);
3. log ``node_result``, check that the outcome has an edge (else fail with
   :class:`InvalidTransitionError`) and that the edge/target budgets allow it
   (else the run is ``exhausted``);
4. apply the agent's updates, log ``transition``, and checkpoint.

Because the checkpoint is written only after a node completes, an
interruption at any point resumes by re-running the interrupted node.

Re-running a node must never repeat an approved side effect. Approved actions
therefore run only through the run's :class:`ActionGuard`, which records
every dispatch durably before it happens and replays recorded outcomes
(docs/design/action-execution.md). Every public method holds the run's lock,
so one process at a time changes a run.

A worker that holds the run's claim passes a :class:`RunOwnership`
(docs/workers.md). The executor then proves ownership before every checkpoint
write and immediately before every dispatch, and its database writes are
fenced by the store; a lost ownership raises ``OwnershipLost``, which stops
the executor at once without recording anything as the run's outcome. It also
hands the run back at a node boundary when the owner asks (``should_yield``).
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ..agents.base import Agent, AgentContext, AgentOutputError, AgentResult
from ..domain.approvals import approval_expiry
from ..domain.claims import OwnershipLost
from ..domain.errors import DecisionRefused, DomainError
from ..domain.executions import (
    ActionExecution,
    ExecutionLedger,
    LedgerKind,
    ReconciliationChoice,
)
from ..domain.identity import Actor
from ..domain.serialization import parse_timestamp
from ..graph.definition import GraphDefinition, GraphValidationError, InvalidTransitionError
from ..providers.base import ModelProvider, ProviderConfigError, provider_produces_trusted_code
from ..providers.budget import ModelBudget, ModelBudgetExhausted, UsageLedger
from ..providers.capabilities import manifest_for
from ..providers.gateway import ModelGateway, ModelUnavailable
from ..providers.gateway import RetryPolicy as ModelRetryPolicy
from ..providers.guard import RequestGuard
from ..safety.approval import ApprovalRequired
from ..safety.execution import ExecutionLedgerUnavailable, ExecutionSemantics, ReconciliationRequired, semantics_of
from ..safety.permissions import PermissionPolicy
from ..sandbox.types import SandboxBlocked
from ..state import AGENT_OUTPUT_FIELDS, ProjectState, RunStatus, TaskSpec
from ..tools import ToolGateway, ToolRegistry, default_registry
from ..util import Clock, timestamp, utc_now
from ..workspace import Workspace
from .actions import ActionGuard, new_owner
from .checkpoint import CheckpointStore, validate_run_id
from .execution_store import CheckpointExecutionRepository
from .locks import FileLock
from .log import ExecutionLog
from .ownership import RunOwnership

if TYPE_CHECKING:
    from ..capabilities.registry import CapabilityRegistry
    from ..domain.recorder import ProgressRecorder

RUN_LOCK = "run.lock"
#: A workspace's runs share its project files: one run at a time may run agents or tools in it (per host).
WORKSPACE_LOCK = "workspace.lock"


class ExecutorError(RuntimeError):
    pass


class RunNotResumable(ExecutorError):
    pass


class ApprovalPending(ExecutorError):
    pass


class ApprovalExpired(ExecutorError):
    """The pending approval expired: it can no longer be decided. Resuming applies the expiry."""


class ReconciliationPending(ExecutorError):
    """The run waits for a person to say whether an uncertain action happened."""


class RunLocked(ExecutorError):
    """Another process (or executor) is changing this run right now."""


class RetryBudgetExhausted(ExecutorError):
    """Recorded as the error type when a run stops on a retry budget."""


class Executor:
    def __init__(
        self,
        *,
        graph: GraphDefinition,
        agents: Mapping[str, Agent],
        provider: ModelProvider,
        workspace: Workspace,
        tools: ToolRegistry | None = None,
        policy: PermissionPolicy | None = None,
        clock: Clock = utc_now,
        recorder: ProgressRecorder | None = None,
        ledger: ExecutionLedger | None = None,
        ownership: RunOwnership | None = None,
        model_budget: ModelBudget | None = None,
        request_guard: RequestGuard | None = None,
        model_retry: ModelRetryPolicy | None = None,
        model_sleep: Callable[[float], None] = time.sleep,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        missing = graph.agent_names - set(agents)
        if missing:
            raise GraphValidationError(f"no agent registered for graph nodes: {sorted(missing)}")
        self.graph = graph
        self.agents = dict(agents)
        self.provider = provider
        self.workspace = workspace
        self.tools = tools or default_registry()
        self.policy = policy or PermissionPolicy()
        #: The Capability Registry (docs/capabilities.md). When set, the Tool Gateway also requires each tool
        #: to belong to a registered, available capability, whose rules can only tighten the policy.
        self.capabilities = capabilities
        self.clock = clock
        self.store = CheckpointStore(workspace.runs_dir)
        #: Optional product-domain recorder (projects, runs, milestones,
        #: approvals). None keeps the v0.2 behaviour exactly.
        self.recorder = recorder
        #: Where approved actions are recorded: the product store when there is one, otherwise a
        #: ledger file next to each run's checkpoint. Approval-gated tools work either way.
        self.ledger = ledger or self._default_ledger()
        #: Set when a worker holds the run's claim: every protected mutation must then prove that
        #: this process still owns the run (docs/workers.md). None keeps the single-host behaviour.
        self.ownership = ownership
        #: Model calls (docs/model-providers.md): every one goes through a ModelGateway, under these budgets,
        #: past this guard (which refuses requests carrying a secret the orchestrator holds).
        self.model_budget = model_budget or ModelBudget()
        self.request_guard = request_guard or RequestGuard.from_environment()
        self.model_retry = model_retry or ModelRetryPolicy()
        self._model_sleep = model_sleep

    def usage_ledger(self, run_id: str) -> UsageLedger:
        """The run's model-usage ledger (``model_usage.jsonl`` next to its checkpoint)."""
        return UsageLedger(self.store.run_dir(run_id), pricing=self.model_budget.pricing)

    def _default_ledger(self) -> ExecutionLedger:
        if self.recorder is not None:
            return ExecutionLedger(self.recorder.store.executions, kind=LedgerKind.PRODUCT_STORE, clock=self.clock)
        return ExecutionLedger(CheckpointExecutionRepository(self.store.runs_dir), kind=LedgerKind.CHECKPOINT, clock=self.clock)

    # ------------------------------------------------------------ public API

    def log_for(self, run_id: str) -> ExecutionLog:
        return ExecutionLog(self.store.run_dir(run_id) / ExecutionLog.FILENAME, self.clock)

    @contextmanager
    def _locked(self, run_id: str, *, files: bool = False) -> Iterator[str]:
        """Hold the run's lock for one operation; yields the owner token of this holder.

        ``files``: the operation may run agents or tools, which change the
        workspace's project files. Every run of a workspace shares those files,
        so the workspace's lock is held too: two runs of one project never run
        at the same time on a host (a worker offers the other run again
        shortly). With an ownership, the run's claim is also proved once the
        locks are held.
        """
        if self.ownership is not None and self.ownership.run_id != run_id:
            raise OwnershipLost(f"this executor owns run {self.ownership.run_id!r}, not {run_id!r}", reason="wrong_run")
        held: list[FileLock] = []
        try:
            if files:
                workspace = FileLock(self.store.runs_dir.parent / WORKSPACE_LOCK)
                if not workspace.try_acquire():
                    raise RunLocked(
                        f"run {run_id!r} cannot continue now: another run of this workspace is changing its files; "
                        "try again when it has finished"
                    )
                held.append(workspace)
            lock = FileLock(self.store.run_dir(run_id) / RUN_LOCK)
            if not lock.try_acquire():
                raise RunLocked(f"run {run_id!r} is being changed by another process; try again when it has finished")
            held.append(lock)
            if self.ownership is None:
                yield new_owner()
            else:
                self.ownership.verify()
                yield self.ownership.owner
        finally:
            for lock in reversed(held):
                lock.release()

    def start(
        self,
        task: TaskSpec,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProjectState:
        run_id = validate_run_id(run_id or self._new_run_id())
        with self._locked(self._new_run(run_id), files=True) as owner:
            state, log = self._create(task, run_id, metadata)
            return self._run(state, log, self._guard(state, log, owner))

    def create(
        self,
        task: TaskSpec,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProjectState:
        """Create a run without running it: its first checkpoint, for a worker to resume (docs/workers.md)."""
        run_id = validate_run_id(run_id or self._new_run_id())
        with self._locked(self._new_run(run_id)):
            state, _ = self._create(task, run_id, metadata)
            return state

    def _new_run(self, run_id: str) -> str:
        if self.ownership is not None:
            raise ExecutorError("a worker continues runs that already exist; it never creates one")
        return run_id

    def _create(
        self, task: TaskSpec, run_id: str, metadata: Mapping[str, Any] | None
    ) -> tuple[ProjectState, ExecutionLog]:
        if self.store.exists(run_id):
            raise ExecutorError(f"run {run_id!r} already exists; use resume()")
        now = timestamp(self.clock)
        state = ProjectState(
            run_id=run_id,
            graph_name=self.graph.name,
            task=task,
            current_node=self.graph.start,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        log = self.log_for(run_id)
        log.append(
            "run_started",
            run_id=run_id,
            graph=self.graph.name,
            node=self.graph.start,
            task=asdict(task),
            provider=self.provider.name,
            max_steps=self.graph.max_steps,
        )
        self._apply_provider_trust(state, log)
        self._save(state)
        return state, log

    def resume(self, run_id: str) -> ProjectState:
        with self._locked(run_id, files=True) as owner:
            state = self.store.load(run_id)
            if self._approval_expired(state):
                state = self._apply_decision(
                    state, approved=False, approver="", note="", decided_at=timestamp(self.clock), refused="expired"
                )
            return self._resume(state, owner)

    def advance(self, run_id: str, *, unblock: bool = False) -> ProjectState:
        """Consume a stored human decision if there is one, then continue the run if it is running.

        For workers and claimed operator commands (docs/workers.md). Under the
        run lock (and, with an ownership, after proving the claim):

        * a run waiting for an approval gets the stored decision applied,
          after it is re-checked against the exact pending action, its expiry,
          and the person who made it; or its expiry, once due;
        * a run waiting for a reconciliation gets each stored reconciliation
          decision applied to the execution ledger, after re-checking it;
        * a running run continues. A run still waiting, a blocked run (unless
          ``unblock``), or a finished run is returned unchanged.

        A decision is consumed once: the checkpoint (for approvals) and the
        ledger's reconciliation record are the proof that it was applied.
        """
        with self._locked(run_id, files=True) as owner:
            state = self.store.load(run_id)
            if state.status is RunStatus.AWAITING_APPROVAL:
                self._consume_decision(state)
            elif state.status is RunStatus.AWAITING_RECONCILIATION:
                state = self._consume_reconciliations(state, owner)
            if state.status is RunStatus.RUNNING or (unblock and state.status is RunStatus.BLOCKED):
                return self._resume(state, owner)
            if self.recorder is not None and state.status in (RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_RECONCILIATION):
                # A run still waiting for a person: make sure its records (the approval request, the
                # attention it needs, and that attention's notification intents) exist. A process that died
                # after the checkpoint but before its sync committed left them unrecorded; syncing is idempotent.
                self.recorder.sync(state)
            return state

    def _resume(self, state: ProjectState, owner: str) -> ProjectState:
        run_id = state.run_id
        if state.graph_name != self.graph.name:
            raise ExecutorError(f"run {run_id!r} uses graph {state.graph_name!r}, not {self.graph.name!r}")
        if state.status.is_terminal:
            raise RunNotResumable(f"run {run_id!r} has already finished with status {state.status.value!r}")
        if state.status is RunStatus.AWAITING_APPROVAL:
            request_id = (state.pending_approval or {}).get("request_id")
            raise ApprovalPending(f"run {run_id!r} is waiting for a decision on approval request {request_id}")
        log = self.log_for(run_id)
        guard = self._guard(state, log, owner)
        if state.status is RunStatus.AWAITING_RECONCILIATION:
            if guard.uncertain():
                pending = (state.pending_reconciliation or {}).get("execution_id")
                raise ReconciliationPending(
                    f"run {run_id!r} is waiting for a person to say whether action {pending} happened"
                )
            # Reconciled elsewhere (e.g. recorded in the product store): the run may continue.
            log.append("reconciliation_applied", run_id=run_id, execution_id=(state.pending_reconciliation or {}).get("execution_id"))
            state.pending_reconciliation = None
            state.status = RunStatus.RUNNING
        previous = state.status
        if state.status is RunStatus.BLOCKED:
            state.status = RunStatus.RUNNING
            state.blocked = None
        owned = {"owner": self.ownership.owner} if self.ownership is not None else {}
        log.append(
            "run_resumed",
            run_id=run_id,
            node=state.current_node,
            step=state.step,
            previous_status=previous.value,
            **owned,
        )
        self._apply_provider_trust(state, log)
        if _has_approved_actions(state):
            if self.recorder is not None:
                # Record any decision the engine applied without the store (e.g. `approve`
                # without --database) before an execution is bound to it.
                self.recorder.sync(state)
            try:
                guard.recover_open()  # attempts a previous owner left open
                uncertain = guard.uncertain()
            except ExecutionLedgerUnavailable as exc:
                return self._block(state, log, state.current_node, exc, state.step)
            if uncertain:
                return self._pause_for_reconciliation(state, log, guard, uncertain[0])
        return self._run(state, log, guard)

    def decide_approval(
        self,
        run_id: str,
        request_id: str,
        *,
        approved: bool,
        approver: str = "human",
        note: str = "",
    ) -> ProjectState:
        """Record a human decision on the pending approval request."""
        with self._locked(run_id):
            state = self.store.load(run_id)
            self.check_pending_approval(state, request_id)
            decided_at = timestamp(self.clock)
            if self.recorder is not None:
                # Record (and validate) the decision before the engine acts on it: a
                # conflicting or misbound decision leaves the run paused, unchanged.
                self.recorder.record_decision(
                    state, request_id, approved=bool(approved), approver=approver, note=note, decided_at=decided_at
                )
            return self._apply_decision(
                state, approved=bool(approved), approver=approver, note=note, decided_at=decided_at
            )

    def reconcile(self, run_id: str) -> ProjectState:
        """Bring the product store and this run's checkpoint back into agreement.

        The checkpoint is the source of truth for execution; product records
        are derived from it. Decisions are the exception: they are stored
        before the engine applies them. Reconciling therefore

        1. applies a stored decision the engine has not applied yet (the
           process stopped between the two writes). Its exact-action binding
           is checked against the engine's pending action first;
        2. re-derives every product record from the checkpoint.

        Both steps are idempotent, so reconciling is always safe to repeat.
        It never runs an agent and never dispatches an action.
        """
        if self.recorder is None:
            raise ExecutorError("reconcile needs a product store")
        with self._locked(run_id):
            state = self.store.load(run_id)
            if self._consume_decision(state):
                return state
            self.recorder.sync(state)
            return state

    def check_pending_approval(self, state: ProjectState, request_id: str) -> None:
        """Raise unless the run waits for ``request_id`` and that approval can still be decided."""
        pending = state.pending_approval
        if state.status is not RunStatus.AWAITING_APPROVAL or not pending:
            raise ExecutorError(f"run {state.run_id!r} has no pending approval request")
        if pending["request_id"] != request_id:
            raise ExecutorError(f"pending request is {pending['request_id']!r}, not {request_id!r}")
        if self._approval_expired(state):
            raise ApprovalExpired(
                f"approval request {request_id} expired at {pending.get('expires_at')}; resuming the run applies the expiry"
            )

    def _approval_expired(self, state: ProjectState) -> bool:
        pending = state.pending_approval
        if state.status is not RunStatus.AWAITING_APPROVAL or not pending:
            return False
        expires_at = pending.get("expires_at") or approval_expiry(pending["requested_at"])
        return parse_timestamp(timestamp(self.clock)) >= parse_timestamp(expires_at)

    def _consume_decision(self, state: ProjectState) -> bool:
        """Apply the stored decision on the pending approval (or its due expiry). True if one was applied.

        The recorder re-checks everything first (``consumable_decision``): the
        stored request and decision against the engine's exact pending action,
        the expiry, and the person who decided. A contradiction raises
        ``DecisionRefused`` and nothing is applied. Without a product store the
        only decision is a due expiry.
        """
        if self.recorder is None:
            if not self._approval_expired(state):
                return False
            found: dict[str, Any] | None = dict(approved=False, approver="", note="", decided_at=timestamp(self.clock),
                                                refused="expired")  # fmt: skip
        else:
            try:
                found = self.recorder.consumable_decision(state, now=timestamp(self.clock))
            except DecisionRefused as exc:
                self._refused(state, exc)
                raise
            if found is None:
                return False
        self._apply_decision(state, **found, source="product_store" if self.recorder is not None else None)
        return True

    def _refused(self, state: ProjectState, exc: DecisionRefused) -> None:
        """Record in the run's audit log that a stored decision was refused, and why. Nothing else changes."""
        self._still_owner()
        self.log_for(state.run_id).append(
            "decision_refused", run_id=state.run_id, node=state.current_node, reason=str(exc),
            **({"owner": self.ownership.owner} if self.ownership is not None else {}),
        )  # fmt: skip

    def _consume_reconciliations(self, state: ProjectState, owner: str) -> ProjectState:
        """Apply stored reconciliation decisions to the execution ledger, then let the run continue if none is left.

        Each decision is re-checked by the recorder (``reconciliation_decision``)
        and applied through the ActionExecution protocol (``ActionGuard.reconcile``):
        a "try once more" becomes a new authorization there, valid for 15
        minutes from the person's decision. Applying the same decision twice
        records it once.
        """
        if self.recorder is None:
            return state
        log = self.log_for(state.run_id)
        guard = self._guard(state, log, owner)
        try:
            applied = 0
            for execution in guard.uncertain():
                try:
                    decision = self.recorder.reconciliation_decision(execution)
                except DecisionRefused as exc:
                    self._refused(state, exc)
                    raise
                if decision is None:
                    continue  # still waiting for a person
                execution, reconciliation, recorded = guard.reconcile(
                    execution.id, attempt=decision.attempt, action_fingerprint=decision.action_fingerprint,
                    choice=decision.choice, actor=decision.actor, comment=decision.comment,
                    decided_at=decision.decided_at,
                )  # fmt: skip
                applied += 1
                log.append(
                    "action_reconciled", run_id=state.run_id, execution_id=execution.id, attempt=reconciliation.attempt,
                    choice=reconciliation.choice.value, actor=decision.actor.display_name, actor_id=decision.actor.id,
                    recorded_now=recorded, state=execution.state.value,
                    retry_authorized_until=reconciliation.retry_authorized_until, source="product_store", owner=owner,
                )  # fmt: skip
            remaining = guard.uncertain()
        except ExecutionLedgerUnavailable as exc:
            return self._block(state, log, state.current_node, exc, state.step)
        if remaining:
            if applied:  # one answered, another still waiting: the run waits for that one
                state.pending_reconciliation = self._reconciliation_details(guard, remaining[0])
                self._save(state)
            return state
        # Nothing is uncertain any more: answered now, or answered and applied by an owner that
        # stopped before its checkpoint. Either way the run continues.
        log.append(
            "reconciliation_applied", run_id=state.run_id,
            execution_id=(state.pending_reconciliation or {}).get("execution_id"), source="product_store", owner=owner,
        )  # fmt: skip
        state.pending_reconciliation = None
        state.status = RunStatus.RUNNING
        self._save(state)
        return state

    def reconcile_action(
        self,
        run_id: str,
        execution_id: str,
        *,
        attempt: int,
        action_fingerprint: str,
        choice: ReconciliationChoice | str,
        approver: str = "human",
        comment: str = "",
    ) -> ProjectState:
        """Record a person's determination of an uncertain action, and let the run continue.

        ``choice`` is it_happened, did_not_happen (do not retry), or
        did_not_happen_retry: a NEW authorization for one more attempt, valid
        for 15 minutes. The original approval is never reactivated. The
        actor is an unauthenticated placeholder until authentication exists.
        """
        with self._locked(run_id) as owner:
            state = self.store.load(run_id)
            log = self.log_for(run_id)
            guard = self._guard(state, log, owner)
            actor = Actor.unauthenticated(approver)
            execution, reconciliation, recorded = guard.reconcile(
                execution_id,
                attempt=attempt,
                action_fingerprint=action_fingerprint,
                choice=ReconciliationChoice(choice),
                actor=actor,
                comment=comment,
            )
            log.append(
                "action_reconciled",
                run_id=run_id,
                execution_id=execution.id,
                attempt=reconciliation.attempt,
                choice=reconciliation.choice.value,
                actor=actor.display_name,
                recorded_now=recorded,
                state=execution.state.value,
                retry_authorized_until=reconciliation.retry_authorized_until,
            )
            remaining = guard.uncertain()
            if remaining:
                state.pending_reconciliation = self._reconciliation_details(guard, remaining[0])
            elif state.status is RunStatus.AWAITING_RECONCILIATION:
                state.pending_reconciliation = None
                state.status = RunStatus.RUNNING
            self._save(state)
            return state

    def _apply_decision(
        self,
        state: ProjectState,
        *,
        approved: bool,
        approver: str,
        note: str,
        decided_at: str,
        source: str | None = None,
        actor_id: str | None = None,
        refused: str | None = None,
    ) -> ProjectState:
        """Apply a decision to the engine. ``refused`` (e.g. ``expired``) applies an approval that may not be used."""
        self._still_owner()
        pending = state.pending_approval
        if pending is None:
            raise ExecutorError(f"run {state.run_id!r} has no pending approval request")
        request_id = pending["request_id"]
        run_id = state.run_id
        approved = bool(approved) and refused is None
        state.approvals[request_id] = {
            "approved": approved,
            "approver": approver,
            "note": note,
            "decided_at": decided_at,
            "tool": pending["tool"],
            "category": pending["category"],
            "params": pending["params"],
            # The node visit the approval belongs to: request IDs include it, so the
            # authorization can only be used, and lapses, within that visit.
            "node": pending["node"],
            "visit": state.node_visits.get(pending["node"], 0) + 1,
            "agent": pending["agent"],
            # The approval can be used (a dispatch can begin) only before it expires.
            "expires_at": pending.get("expires_at") or approval_expiry(pending["requested_at"]),
            "applied_at": timestamp(self.clock),
        }
        if actor_id is not None:
            state.approvals[request_id]["actor_id"] = actor_id
        if refused is not None:
            state.approvals[request_id]["refused"] = refused  # why an approval may not be used
        state.pending_approval = None
        state.status = RunStatus.RUNNING
        entry: dict[str, Any] = {
            "run_id": run_id,
            "node": pending["node"],
            "request_id": request_id,
            "approved": approved,
            "approver": approver,
            "note": note,
        }
        if source is not None:
            entry["source"] = source  # e.g. applied during reconciliation from the product store
        if actor_id is not None:
            entry["actor_id"] = actor_id
        if refused is not None:
            entry["refused"] = refused
        if self.ownership is not None:
            entry["owner"] = self.ownership.owner  # which worker, in which generation, applied it
        self.log_for(run_id).append("approval_decision", **entry)
        self._save(state)
        return state

    # ------------------------------------------------------------- main loop

    def _run(self, state: ProjectState, log: ExecutionLog, guard: ActionGuard) -> ProjectState:
        while True:
            node = self.graph.node(state.current_node)
            if node.terminal:
                self._still_owner()
                state.status = RunStatus.COMPLETED
                log.append("run_completed", run_id=state.run_id, node=node.name, step=state.step)
                self._close_run(state, log, guard, "run_completed")
                self._save(state)
                return state

            if state.step >= self.graph.max_steps:
                return self._exhaust(state, log, node.name, f"max_steps ({self.graph.max_steps}) reached", guard)

            visit = state.node_visits.get(node.name, 0) + 1
            if node.max_visits is not None and visit > node.max_visits:
                return self._exhaust(state, log, node.name, f"node {node.name!r} visit limit ({node.max_visits}) reached", guard)

            if self.ownership is not None and self.ownership.should_yield():
                # Hand the run back between nodes: everything so far is checkpointed, and the run is
                # still running for the next owner (a shutdown, a used-up budget, or a doubtful lease).
                log.append("run_yielded", run_id=state.run_id, step=state.step, node=node.name, owner=self.ownership.owner)
                return state

            agent = self.agents[node.agent]  # type: ignore[index]
            step = state.step
            log.append("node_entered", run_id=state.run_id, step=step, node=node.name, agent=agent.name, visit=visit)

            def record(entry: dict[str, Any], _node: str = node.name, _step: int = step) -> None:
                log.append("action", run_id=state.run_id, step=_step, node=_node, **{"kind": "tool", **entry})

            ctx = AgentContext(
                run_id=state.run_id,
                node=node.name,
                visit=visit,
                state=state.snapshot(),
                tools=ToolGateway(
                    registry=self.tools,
                    policy=self.policy,
                    workspace=self.workspace,
                    run_id=state.run_id,
                    node=node.name,
                    agent=agent.name,
                    visit=visit,
                    approvals=state.approvals,
                    on_action=record,
                    executions=guard,
                    capabilities=self.capabilities,
                ),
                provider=self.provider,
                record_action=record,
                models=ModelGateway(
                    provider=self.provider,
                    ledger=self.usage_ledger(state.run_id),
                    budget=self.model_budget,
                    guard=self.request_guard,
                    manifest=manifest_for(agent.name, self.policy, self.tools, self.capabilities),
                    run_id=state.run_id,
                    node=node.name,
                    agent=agent.name,
                    visit=visit,
                    record_action=record,
                    retry=self.model_retry,
                    sleep=self._model_sleep,
                ),
                host_paths=(str(self.workspace.root), str(self.workspace.project_dir)),
            )

            try:
                result = agent.run(ctx)
                self._check_result(agent, result)
            except ApprovalRequired as exc:
                return self._pause_for_approval(state, log, exc, step)
            except ReconciliationRequired as exc:
                return self._pause_for_reconciliation(state, log, guard, exc.execution_id)
            except (SandboxBlocked, ExecutionLedgerUnavailable, ModelBudgetExhausted, ModelUnavailable, ProviderConfigError) as exc:
                # Infrastructure/policy prevented safe execution, a model budget is used up, or the model
                # provider is unreachable or misconfigured: block the run without committing the node, so
                # no repair retry is consumed and nothing keeps spending. A person resumes it.
                return self._block(state, log, node.name, exc, step)
            except Exception as exc:  # agent, provider, tool, or permission failure
                return self._fail(state, log, node.name, exc, guard)

            if _has_approved_actions(state):
                try:
                    # A swallowed ReconciliationRequired must still stop the run, and authorizations
                    # this node visit never used can never be used again.
                    uncertain = guard.uncertain()
                    if not uncertain:
                        guard.close_visit(node.name, visit, state.approvals)
                        uncertain = guard.uncertain()
                except ExecutionLedgerUnavailable as exc:
                    return self._block(state, log, node.name, exc, step)
                if uncertain:
                    return self._pause_for_reconciliation(state, log, guard, uncertain[0])

            log.append(
                "node_result",
                run_id=state.run_id,
                step=step,
                node=node.name,
                outcome=result.outcome,
                summary=result.summary,
                updated_fields=sorted(result.updates),
            )

            try:
                edge = self.graph.transition(node.name, result.outcome)
            except InvalidTransitionError as exc:
                log.append(
                    "transition",
                    run_id=state.run_id,
                    step=step,
                    source=node.name,
                    outcome=result.outcome,
                    target=None,
                    accepted=False,
                    reason=str(exc),
                )
                return self._fail(state, log, node.name, exc, guard)

            # The node finished: commit its outputs before checking budgets so
            # the final test report / review is kept even on exhaustion.
            for key, value in result.updates.items():
                setattr(state, key, value)
            state.node_visits[node.name] = visit
            state.history.append(
                {"step": step, "node": node.name, "visit": visit, "outcome": result.outcome, "summary": result.summary}
            )
            state.step += 1

            used = state.edge_traversals.get(edge.key, 0)
            target = self.graph.node(edge.target)
            target_visits = state.node_visits.get(target.name, 0)
            reason = None
            if edge.max_traversals is not None and used >= edge.max_traversals:
                reason = f"retry budget exhausted: edge {edge.key} already taken {used}/{edge.max_traversals} times"
            elif target.max_visits is not None and target_visits >= target.max_visits:
                reason = f"node {target.name!r} visit limit ({target.max_visits}) reached"
            if reason:
                log.append(
                    "transition",
                    run_id=state.run_id,
                    step=step,
                    source=node.name,
                    outcome=result.outcome,
                    target=edge.target,
                    edge=edge.key,
                    accepted=False,
                    reason=reason,
                )
                return self._exhaust(state, log, node.name, reason, guard)

            state.edge_traversals[edge.key] = used + 1
            state.current_node = target.name
            log.append(
                "transition",
                run_id=state.run_id,
                step=step,
                source=node.name,
                outcome=result.outcome,
                target=target.name,
                edge=edge.key,
                traversal=used + 1,
                max_traversals=edge.max_traversals,
                accepted=True,
            )
            self._save(state)

    # --------------------------------------------------------------- helpers

    def _project_id(self) -> str:
        if self.recorder is not None:
            return self.recorder.project.id
        return self.workspace.project_id or self.workspace.ensure_project_id()

    def _semantics_for(self, tool_name: str) -> ExecutionSemantics:
        tool = self.tools.get(tool_name)
        if tool is None:
            raise ExecutorError(f"approved tool {tool_name!r} is not registered")
        return semantics_of(tool)

    def _approval_window(self, request_id: str) -> str | None:
        """When the approval stops authorizing a new dispatch (product store requests may expire)."""
        if self.recorder is None:
            return None
        request = self.recorder.store.approvals.get_request(self.recorder.project.id, request_id)
        return request.expires_at if request is not None else None

    def _guard(self, state: ProjectState, log: ExecutionLog, owner: str) -> ActionGuard:
        return ActionGuard(
            ledger=self.ledger,
            log=log,
            run_dir=self.store.run_dir(state.run_id),
            project_id=self._project_id,  # resolved on first use only
            run_id=state.run_id,
            owner=owner,
            semantics_for=self._semantics_for,
            approval_window=self._approval_window,
            before_dispatch=self.ownership.verify if self.ownership is not None else None,
        )

    @staticmethod
    def _check_result(agent: Agent, result: Any) -> None:
        if not isinstance(result, AgentResult):
            raise AgentOutputError(f"{agent.name} returned {type(result).__name__}, not AgentResult")
        if not isinstance(result.outcome, str) or not result.outcome:
            raise AgentOutputError(f"{agent.name} returned an empty outcome")
        illegal = set(result.updates) - (agent.writes & AGENT_OUTPUT_FIELDS)
        if illegal:
            raise AgentOutputError(f"{agent.name} may not write state fields {sorted(illegal)}")

    def _pause_for_approval(
        self, state: ProjectState, log: ExecutionLog, exc: ApprovalRequired, step: int
    ) -> ProjectState:
        self._still_owner()
        requested_at = timestamp(self.clock)
        request = {
            **exc.request.to_dict(), "step": step, "requested_at": requested_at, "expires_at": approval_expiry(requested_at),
        }  # fmt: skip
        state.pending_approval = request
        state.status = RunStatus.AWAITING_APPROVAL
        log.append("approval_requested", run_id=state.run_id, step=step, node=request["node"], request=request)
        log.append(
            "run_paused",
            run_id=state.run_id,
            step=step,
            node=request["node"],
            reason="awaiting_approval",
            request_id=request["request_id"],
        )
        self._save(state)
        return state

    @staticmethod
    def _reconciliation_details(guard: ActionGuard, execution: ActionExecution) -> dict[str, Any]:
        """Everything a person needs to judge the uncertain action. No secrets: the action is shown
        exactly as it was approved (long values as hashes), plus what is and is not known."""
        attempts = guard.attempts(execution)
        last = attempts[-1] if attempts else None
        return {
            "execution_id": execution.id,
            "attempt": execution.attempts,
            "request_id": execution.request_id,
            "node": execution.node,
            "agent": execution.agent,
            "tool": execution.action.tool,
            "category": execution.action.category,
            "params": execution.action.params,
            "action_fingerprint": execution.action_fingerprint,
            "effect": execution.effect.value,
            "dispatched_at": last.dispatched_at if last else None,
            "reason": (last.detail if last and last.detail else "the outcome of the attempt is unknown"),
        }

    def _pause_for_reconciliation(
        self, state: ProjectState, log: ExecutionLog, guard: ActionGuard, execution: ActionExecution | str
    ) -> ProjectState:
        self._still_owner()
        if isinstance(execution, str):
            found = guard.find(execution)
            if found is None:
                raise ExecutorError(f"uncertain execution {execution} is not recorded")
            execution = found
        details = self._reconciliation_details(guard, execution)
        state.pending_reconciliation = details
        state.status = RunStatus.AWAITING_RECONCILIATION
        log.append(
            "reconciliation_required",
            run_id=state.run_id,
            step=state.step,
            node=details["node"],
            execution_id=details["execution_id"],
            attempt=details["attempt"],
            tool=details["tool"],
            category=details["category"],
            action_fingerprint=details["action_fingerprint"],
            dispatched_at=details["dispatched_at"],
            reason=details["reason"],
        )
        log.append("run_paused", run_id=state.run_id, step=state.step, node=details["node"], reason="awaiting_reconciliation")
        self._save(state)
        return state

    def _apply_provider_trust(self, state: ProjectState, log: ExecutionLog) -> None:
        """Downgrade the workspace to UNTRUSTED if this provider's output is not trusted."""
        if provider_produces_trusted_code(self.provider):
            return
        reason = f"provider {self.provider.name!r} does not produce trusted code"
        if self.workspace.mark_untrusted(reason):
            log.append("workspace_trust", run_id=state.run_id, trust="untrusted", reason=reason)

    def _block(
        self, state: ProjectState, log: ExecutionLog, node: str, exc: Exception, step: int
    ) -> ProjectState:
        self._still_owner()
        state.status = RunStatus.BLOCKED
        state.blocked = {
            "type": type(exc).__name__,
            "message": str(exc),
            "node": node,
            "step": step,
            "at": timestamp(self.clock),
        }
        log.append("run_blocked", run_id=state.run_id, step=step, node=node, blocked=state.blocked)
        self._save(state)
        return state

    def _close_run(self, state: ProjectState, log: ExecutionLog, guard: ActionGuard, reason: str) -> None:
        """Best effort: a finished run's unused authorizations lapse. Failing here is logged, never hidden;
        it cannot authorize anything, because a finished run never dispatches again."""
        if not _has_approved_actions(state):
            return
        try:
            guard.close_run(state.approvals, reason=reason)
        except (ExecutionLedgerUnavailable, DomainError) as exc:
            log.append("action_close_failed", run_id=state.run_id, reason=reason, error_type=type(exc).__name__)

    def _fail(
        self, state: ProjectState, log: ExecutionLog, node: str, exc: BaseException, guard: ActionGuard
    ) -> ProjectState:
        self._still_owner()
        state.status = RunStatus.FAILED
        state.error = {"type": type(exc).__name__, "message": str(exc), "node": node, "step": state.step}
        log.append("run_failed", run_id=state.run_id, step=state.step, node=node, error=state.error)
        self._close_run(state, log, guard, "run_failed")
        self._save(state)
        return state

    def _exhaust(
        self, state: ProjectState, log: ExecutionLog, node: str, reason: str, guard: ActionGuard
    ) -> ProjectState:
        self._still_owner()
        state.status = RunStatus.EXHAUSTED
        state.error = {"type": RetryBudgetExhausted.__name__, "message": reason, "node": node, "step": state.step}
        log.append("run_exhausted", run_id=state.run_id, step=state.step, node=node, reason=reason)
        self._close_run(state, log, guard, "run_exhausted")
        self._save(state)
        return state

    def _still_owner(self) -> None:
        """With an ownership, prove it before recording a run outcome, so a stale worker records none.

        (``_save`` proves it again before the checkpoint is written.) Node-level
        entries (``node_entered``, ``node_result``, ``transition``) may still
        record a node a stale worker ran before it noticed, exactly as a crash
        leaves them; the checkpoint never does.
        """
        if self.ownership is not None:
            self.ownership.verify()

    def _save(self, state: ProjectState) -> None:
        if self.ownership is not None:
            # A checkpoint file cannot be fenced inside the database's transaction, so ownership is
            # proved immediately before it is written (docs/workers.md, "checkpoints").
            self.ownership.verify()
        state.updated_at = timestamp(self.clock)
        usage = UsageLedger(self.store.run_dir(state.run_id), pricing=self.model_budget.pricing)
        if usage.log.path.is_file():
            # The checkpoint carries a summary of the run's model usage (its ledger is the source of truth).
            state.metadata["model_usage"] = usage.totals().to_dict()
        self.store.save(state)
        if self.recorder is not None:
            # Product records are derived only from committed checkpoints.
            self.recorder.sync(state)

    def _new_run_id(self) -> str:
        return f"run-{self.clock().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


def _has_approved_actions(state: ProjectState) -> bool:
    """Executions exist only for approved actions, so a run without any never consults the ledger.

    The engine records an approval in the checkpoint before any execution of it
    can be created, so "no approved entry" means "no execution": runs that use
    no approval-gated tool behave, and depend on storage, exactly as before.
    """
    return any(record.get("approved") for record in state.approvals.values())

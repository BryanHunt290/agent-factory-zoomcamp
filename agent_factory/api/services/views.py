"""Domain records -> API models. Presentation only: no storage, no rules.

The API model is shaped for what a client needs, not like the domain record
or the database row (docs/api-contract.md#api-model-domain-model-database-row):
some fields are renamed, some hidden (``metadata``, ``context``, keys), some
derived (a project's status from its latest run), and engine text is curated.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...domain.approvals import ApprovalDecision, ApprovalRequest, ApprovalStatus
from ...domain.attention import AttentionItem, AttentionKind
from ...domain.executions import ActionExecution, ActionReconciliation, AttemptResult, ExecutionAttempt, ExecutionState
from ...domain.identity import Actor
from ...domain.ids import APPROVAL_ID_RE
from ...domain.reconciliation import ReconciliationDecision
from ...domain.timeline import TimelineEntry, approval_timeline, needs_reconciliation
from ...domain.milestones import HUMAN_DECISION, Milestone, MilestonePolicy
from ...domain.model_usage import RunModelUsage
from ...domain.notifications import Notification, summarize
from ...domain.project import Project, Run
from ...state import RunStatus
from ...templates import ProjectTemplate
from .. import schemas as api
from ..redaction import TextSanitizer, milestone_summary, run_status_reason


def milestone_types(policy: MilestonePolicy) -> list[api.MilestoneTypeDefinition]:
    return [
        api.MilestoneTypeDefinition(
            type=d.type, title=d.title, category=d.category.value, description=d.description, order=d.order
        )
        for d in policy.catalog.definitions
    ]


def template(t: ProjectTemplate) -> api.Template:
    return api.Template(name=t.name, description=t.description, milestone_types=milestone_types(t.milestone_policy))


def run_summary(run: Run | None) -> api.RunSummary | None:
    if run is None:
        return None
    return api.RunSummary(run_id=run.run_id, number=run.number, status=run.status.value, updated_at=run.updated_at)


def activity(since: str | None, milestones: int) -> api.Activity | None:
    return None if since is None else api.Activity(since=since, milestones=milestones)


def project_status(latest: Run | None) -> str:
    return latest.status.value if latest is not None else "not_started"


def project_summary(
    p: Project, latest: Run | None, pending: int, since: str | None, recent: int
) -> api.ProjectSummary:
    return api.ProjectSummary(
        id=p.id,
        name=p.name,
        template=p.template,
        status=project_status(latest),
        latest_run=run_summary(latest),
        pending_approvals=pending,
        activity=activity(since, recent),
    )


def project(p: Project, latest: Run | None, pending: int, since: str | None, recent: int) -> api.Project:
    return api.Project(
        id=p.id,
        name=p.name,
        description=p.description,
        template=p.template,
        created_at=p.created_at,
        status=project_status(latest),
        latest_run=run_summary(latest),
        pending_approvals=pending,
        activity=activity(since, recent),
        milestone_types=milestone_types(p.milestone_policy),
    )


def model_usage(u: RunModelUsage | None) -> api.ModelUsage:
    """Counts only. A run that never called a model shows zeros; no cost without configured prices."""
    if u is None:
        return api.ModelUsage(calls=0, failed_calls=0, input_tokens=0, output_tokens=0, cached_tokens=0,
                              estimated_cost_usd=None, by_model=[])  # fmt: skip
    return api.ModelUsage(
        calls=u.calls, failed_calls=u.failed_calls, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
        cached_tokens=u.cached_tokens, estimated_cost_usd=u.estimated_cost_usd,
        by_model=[api.ModelUsageByModel(**m.to_dict()) for m in u.by_model],
    )  # fmt: skip


def run(r: Run, pending_approval_id: str | None, usage: RunModelUsage | None = None) -> api.Run:
    return api.Run(
        project_id=r.project_id,
        run_id=r.run_id,
        number=r.number,
        workflow=r.graph_name,
        status=r.status.value,
        status_reason=run_status_reason(r),
        pending_approval_id=pending_approval_id if r.status is RunStatus.AWAITING_APPROVAL else None,
        created_at=r.created_at,
        updated_at=r.updated_at,
        model_usage=model_usage(usage),
    )


def _step(value: object, sanitize: TextSanitizer) -> api.StepRef | None:
    if not isinstance(value, dict):
        return None
    node, outcome = value.get("node"), value.get("outcome")
    if isinstance(node, str) and isinstance(outcome, str) and 0 < len(node) <= 64 and 0 < len(outcome) <= 64:
        return api.StepRef(node=sanitize(node, limit=64), outcome=sanitize(outcome, limit=64))
    return None


def milestone(m: Milestone, sanitize: TextSanitizer) -> api.Milestone:
    approval_id = m.metadata.get("approval_id")
    if m.type != HUMAN_DECISION or not isinstance(approval_id, str) or not APPROVAL_ID_RE.fullmatch(approval_id):
        approval_id = None
    return api.Milestone(
        id=m.id,
        project_id=m.project_id,
        run_id=m.run_id,
        sequence=m.sequence,
        type=m.type,
        category=m.category.value,
        title=sanitize(m.title, limit=120),
        summary=milestone_summary(m, sanitize),
        status=m.status.value,
        occurred_at=m.occurred_at,
        source=api.MilestoneSource(
            node=sanitize(m.source.node, limit=64) if m.source.node is not None else None,
            agent=sanitize(m.source.agent, limit=64) if m.source.agent is not None else None,
        ),
        follows=_step(m.metadata.get("after"), sanitize),
        resolves_milestone_id=m.resolves_id,
        approval_id=approval_id,
    )


def actor(a: Actor) -> api.Actor:
    return api.Actor(kind=a.kind.value, id=a.id, display_name=a.display_name, verified=a.verified)


def decision(d: ApprovalDecision) -> api.ApprovalDecision:
    return api.ApprovalDecision(
        id=d.id,
        outcome=d.outcome.value,
        action_fingerprint=d.action_fingerprint,
        decided_at=d.decided_at,
        decided_by=actor(d.actor),
        comment=d.comment,
    )


def action_summary(tool: str, category: str) -> str:
    """The tool and its category, never the parameters: safe for any list."""
    return f"{tool} ({category.replace('_', ' ')})"


#: The contract's limit on a timeline. Every retry needs a person, so a real timeline stays far below it.
MAX_TIMELINE = 200


def timeline_entry(e: TimelineEntry) -> api.TimelineEntry:
    worker = None
    if e.worker_id is not None and e.fencing_token is not None:
        worker = api.WorkerRef(worker_id=e.worker_id, fencing_token=e.fencing_token)
    return api.TimelineEntry(
        kind=e.kind.value,
        at=e.at,
        attempt=e.attempt,
        actor=actor(e.actor) if e.actor is not None else None,
        worker=worker,
        outcome=e.outcome,
        until=e.until,
    )


def _latest_uncertain(execution: ActionExecution, attempts: Sequence[ExecutionAttempt]) -> int | None:
    uncertain = {a.attempt for a in attempts if a.result is AttemptResult.INDETERMINATE}
    if execution.state is ExecutionState.UNCERTAIN:
        uncertain.add(execution.attempts)
    return max(uncertain, default=None)


def execution(
    e: ActionExecution,
    *,
    attempts: Sequence[ExecutionAttempt],
    decisions: Sequence[ReconciliationDecision],
    reconciliations: Sequence[ActionReconciliation],
    now: str,
) -> api.ActionExecution:
    """What happened to the approved action: its state, latest attempt, an open retry window, the latest answer."""
    latest = _latest_uncertain(e, attempts)
    decided = {d.attempt: d for d in decisions}
    applied = {r.attempt: r for r in reconciliations}
    answer = (decided.get(latest) or applied.get(latest)) if latest is not None else None
    retry_open = e.state is ExecutionState.AUTHORIZED and e.authorization_seq > 1 and e.authorized_until is not None
    return api.ActionExecution(
        id=e.id,
        state=e.state.value,
        attempt=e.attempts,
        retry_authorized_until=e.authorized_until if retry_open and e.window_open(now) else None,
        reconciliation=None
        if answer is None
        else api.Reconciliation(
            attempt=answer.attempt,
            choice=answer.choice.value,
            decided_at=answer.decided_at,
            decided_by=actor(answer.actor),
            comment=answer.comment,
            applied=answer.attempt in applied,
        ),
    )


def notifications(rows: Sequence[Notification], unknown: set[str]) -> list[api.NotificationStatus]:
    """Whether people were told, per attention event: counts and states only, never who or where."""
    return [
        api.NotificationStatus(
            about=s.attention_kind.value, attempt=s.attention_attempt, state=s.state.value, deliveries=s.deliveries,
            sent=s.sent, failed=s.failed, created_at=s.created_at, last_attempt_at=s.last_attempt_at, sent_at=s.sent_at,
            outcome_unknown=s.outcome_unknown, failure=s.failure_category.value if s.failure_category else None,
        )  # fmt: skip
        for s in summarize(list(rows), unknown)
    ][-50:]


def approval(
    r: ApprovalRequest,
    d: ApprovalDecision | None,
    project_name: str,
    sanitize: TextSanitizer,
    *,
    now: str,
    execution_record: ActionExecution | None = None,
    attempts: Sequence[ExecutionAttempt] = (),
    decisions: Sequence[ReconciliationDecision] = (),
    reconciliations: Sequence[ActionReconciliation] = (),
    notification_rows: Sequence[Notification] = (),
    unknown_notifications: set[str] = frozenset(),  # type: ignore[assignment]
) -> api.ApprovalRequest:
    """The approval, its decision, and what happened to its action, as of ``now`` (server time).

    A pending request whose expiry has passed is shown as ``expired`` even
    before anything records that: expiry never waits for a writer.
    """
    sanitize.check_exact_action(r.action, r.action_fingerprint)
    expired = r.status is ApprovalStatus.PENDING and r.is_expired(now)
    if r.status is ApprovalStatus.PENDING and not expired:
        attention: str | None = AttentionKind.DECISION_REQUIRED.value
    elif needs_reconciliation(execution_record, decisions):
        attention = AttentionKind.RECONCILIATION_REQUIRED.value
    else:
        attention = None
    steps = approval_timeline(
        r, d, execution=execution_record, attempts=attempts, decisions=decisions, reconciliations=reconciliations, now=now
    )
    if len(steps) > MAX_TIMELINE:  # keep the request and the most recent steps
        steps = steps[:1] + steps[-(MAX_TIMELINE - 1):]
    return api.ApprovalRequest(
        id=r.id,
        project_id=r.project_id,
        project_name=project_name,
        run_id=r.run_id,
        node=sanitize(r.node, limit=64),
        agent=sanitize(r.agent, limit=64),
        action=api.ExactAction(tool=r.action.tool, category=r.action.category, params=r.action.params),
        action_fingerprint=r.action_fingerprint,
        reason=sanitize(r.reason, limit=500),
        risk=sanitize(r.risk, limit=500),
        status=ApprovalStatus.EXPIRED.value if expired else r.status.value,
        requested_at=r.requested_at,
        expires_at=r.expires_at,
        decision=decision(d) if d is not None else None,
        action_summary=action_summary(r.action.tool, r.action.category),
        attention=attention,
        execution=None
        if execution_record is None
        else execution(execution_record, attempts=attempts, decisions=decisions, reconciliations=reconciliations, now=now),
        notifications=notifications(notification_rows, unknown_notifications),
        timeline=[timeline_entry(e) for e in steps],
    )


def attention_item(i: AttentionItem, project_name: str, sanitize: TextSanitizer) -> api.AttentionItem:
    what = action_summary(i.tool, i.category)
    if i.kind is AttentionKind.DECISION_REQUIRED:
        summary = f"Approval needed: {what}."
    else:
        summary = f"Outcome unknown: {what} was started; say whether it happened."
    return api.AttentionItem(
        kind=i.kind.value,
        project_id=i.project_id,
        project_name=project_name,
        run_id=i.run_id,
        approval_id=i.approval_id,
        execution_id=i.execution_id,
        attempt=i.attempt,
        tool=i.tool,
        category=i.category,
        summary=sanitize(summary, limit=300),
        since=i.since,
        expires_at=i.expires_at,
    )

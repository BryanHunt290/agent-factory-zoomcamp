"""Workers consume human decisions (E5), on the in-memory store: approve, reject, expire, reconcile, retry.

Each test drives the action fixture's run (one approval-gated class C notice)
through real ``Worker`` loops. Decisions are recorded as the API records them
(decision and wake-up in one transaction); a worker that holds the run's claim
re-checks the decision against the engine's exact pending action and applies
it, and every side effect goes through the ActionExecution ledger. The oracle
file counts real effects independently of everything Agent Factory records.
The same flows run against PostgreSQL, with real races and real process
deaths, in tests/test_postgres_decisions.py.
"""

from datetime import timedelta

import pytest
from decision_support import DECIDE, RECONCILE, Flow, claimed, person, released

from agent_factory.domain.approvals import ApprovalStatus
from agent_factory.domain.attention import AttentionKind
from agent_factory.domain.errors import ApprovalExpiredError, StatusTransitionError
from agent_factory.domain.executions import ExecutionState, ReconciliationChoice
from agent_factory.domain.identity import Actor, ActorKind
from agent_factory.domain.milestones import HUMAN_DECISION, RUN_RESUMED, MilestoneStatus
from agent_factory.domain.principals import Permission, PrincipalStatus
from agent_factory.domain.repositories import MilestoneQuery
from agent_factory.domain.timeline import TimelineKind, approval_timeline
from agent_factory.engine.checkpoint import CheckpointStore
from agent_factory.state import RunStatus


@pytest.fixture
def flow(tmp_path):
    return Flow.memory(tmp_path)


@pytest.fixture
def flaky(tmp_path):
    """The gateway times out after accepting the first notice."""
    return Flow.memory(tmp_path, kind="unknown-notice")


def milestones(flow, milestone_type=None):
    found = flow.store.milestones.query(MilestoneQuery(project_id=flow.project_id))
    return [m for m in found if milestone_type is None or m.type == milestone_type]


# --------------------------------------------------------------- approvals


def test_a_paused_run_sleeps_until_a_decision_wakes_it_and_the_action_runs_exactly_once(flow):
    pause = flow.paused()
    assert claimed(pause)["fencing_token"] == 1
    assert claimed(flow.work()) is None  # nothing wakes it yet: no decision, not expired
    flow.decide("approved")
    events = flow.work()
    assert claimed(events)["fencing_token"] == 2  # the decision's wake-up made it claimable at once
    assert flow.checkpoint().status is RunStatus.COMPLETED and flow.notices() == 1
    assert released(events)["reason"] == "finished"
    assert claimed(flow.work()) is None and flow.notices() == 1  # consumed once: nothing left to do


def test_every_consumed_decision_is_attributable_to_the_person_and_the_worker(flow):
    flow.paused()
    flow.decide("approved")
    events = flow.work()
    worker, token = events[0]["worker_id"], claimed(events)["fencing_token"]
    [applied] = flow.events("approval_decision")
    assert applied["actor_id"] == flow.dana.id and applied["approver"] == "dana"  # the person, from the store
    assert applied["source"] == "product_store" and applied["owner"] == f"{worker}@{token}"  # the worker
    decision = flow.store.approvals.get_decision(flow.project_id, flow.request().id)
    assert decision.actor.kind is ActorKind.USER and decision.actor.id == flow.dana.id
    [attempt] = flow.store.executions.attempts(flow.project_id, "run-1", flow.execution().id)
    assert attempt.owner == f"{worker}@{token}"  # which worker, in which generation, sent it
    timeline = approval_timeline(
        flow.request(), decision, execution=flow.execution(), now=flow.now(),
        attempts=flow.store.executions.attempts(flow.project_id, "run-1", flow.execution().id),
    )  # fmt: skip
    kinds = [(e.kind, e.worker_id, e.fencing_token) for e in timeline]
    assert kinds == [
        (TimelineKind.APPROVAL_REQUESTED, None, None), (TimelineKind.APPROVED, None, None),
        (TimelineKind.ACTION_DISPATCHING, worker, token), (TimelineKind.ACTION_SUCCEEDED, worker, token),
        (TimelineKind.RESOLVED, None, None),
    ]  # fmt: skip
    for record in events:
        assert "token" not in {k.lower() for k in record} and "authorization" not in str(record).lower()


def test_a_rejection_reaches_the_engine_which_follows_its_graph_and_never_asks_again(flow):
    flow.paused()
    flow.decide("rejected")
    flow.work()
    state = flow.checkpoint()
    assert state.status is RunStatus.COMPLETED and flow.notices() == 0
    assert len(state.implementation["refused"]) == 1 and "rejected" in state.implementation["refused"][0]
    assert len(flow._requests()) == 1 and flow.request().status is ApprovalStatus.REJECTED  # asked once, not again
    [decided] = milestones(flow, HUMAN_DECISION)
    assert (decided.status, decided.summary) == (MilestoneStatus.COMPLETED, "Rejected by dana")
    assert any(m.summary == "Resumed after dana rejected send_notice." for m in milestones(flow, RUN_RESUMED))


def test_an_undecided_approval_expires_on_time_and_its_action_never_runs(flow):
    pause = flow.paused()
    request = flow.request()
    assert released(pause)["wake_at"] == request.expires_at  # its only wake-up without a decision
    assert request.expires_at == (flow.clock.now + timedelta(hours=12)).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")  # fmt: skip
    flow.clock.at(request.expires_at)
    flow.clock.advance(seconds=-1)
    assert claimed(flow.work()) is None  # one second before: still waiting, nothing polls it
    flow.clock.advance(seconds=1)  # the expiry instant itself is expired
    events = flow.work()
    assert claimed(events) is not None
    state = flow.checkpoint()
    assert state.status is RunStatus.COMPLETED and flow.notices() == 0
    assert state.approvals[request.id]["refused"] == "expired"
    assert flow.request().status is ApprovalStatus.EXPIRED
    [decided] = milestones(flow, HUMAN_DECISION)
    assert decided.status is MilestoneStatus.FAILED and decided.summary.startswith("Expired:")
    with pytest.raises((ApprovalExpiredError, StatusTransitionError)):
        flow.decide("approved")  # too late: a new request needs a new visit of the step


def test_a_decision_made_in_time_but_reaching_a_worker_after_expiry_is_never_used(flow):
    """Decision versus expiry: the approval must also be *used* before it expires."""
    flow.paused()
    request = flow.request()
    flow.clock.at(request.expires_at)
    flow.clock.advance(minutes=-1)
    flow.decide("approved")
    flow.clock.advance(minutes=2)  # the worker gets there after the expiry
    flow.work()
    state = flow.checkpoint()
    assert state.approvals[request.id]["refused"] == "expired" and flow.notices() == 0
    assert flow.request().status is ApprovalStatus.APPROVED  # the person's decision stays as they made it
    assert [e["refused"] for e in flow.events("approval_decision")] == ["expired"]


def test_an_approval_applied_in_time_cannot_begin_after_it_expires(flow):
    flow.paused()
    request = flow.request()
    flow.decide("approved")

    def late(ex):  # the node starts after the expiry, e.g. a slow model call before the tool
        inner = ex.agents["operator"]
        from worker_support import BeforeAgent

        ex.agents["operator"] = BeforeAgent(inner, lambda: flow.clock.at(request.expires_at))

    flow.adjusters.append(late)
    flow.work()
    assert flow.notices() == 0
    execution = flow.execution()
    assert execution.state is ExecutionState.LAPSED and execution.lapse_reason == "authorization_expired"


@pytest.mark.parametrize("tamper", ["params", "node", "agent"])
def test_a_decision_that_no_longer_matches_the_exact_pending_action_is_refused_and_the_run_parked(flow, tamper):
    flow.paused()
    flow.decide("approved")
    store = CheckpointStore(flow.ws.runs_dir)
    state = store.load("run-1")
    changed = {"params": {**state.pending_approval["params"], "to": "attacker@example.test"},
               "node": "elsewhere", "agent": "intruder"}[tamper]  # fmt: skip
    state.pending_approval[tamper] = changed
    store.save(state)
    events = flow.work()
    assert (released(events)["reason"], released(events)["requeued"]) == ("decision_refused", False)
    assert flow.notices() == 0 and flow.checkpoint().status is RunStatus.AWAITING_APPROVAL
    assert flow.checkpoint().approvals == {}  # nothing applied
    [refusal] = flow.events("decision_refused")
    assert "not the action the engine is waiting on" in refusal["reason"] and refusal["owner"].startswith("wkr-")
    assert claimed(flow.work()) is None  # parked: no worker picks it up again by itself


def test_a_decision_whose_request_status_disagrees_is_refused(flow):
    flow.paused()
    flow.decide("approved")
    request = flow.request()
    flow.store.unit_of_work.requests[(flow.project_id, request.id)] = request.__class__(
        **{**request.__dict__, "status": ApprovalStatus.REJECTED}
    )  # a corrupted row: approved decision, rejected request
    events = flow.work()
    assert released(events)["reason"] == "decision_refused" and flow.notices() == 0


@pytest.mark.parametrize("change,reason", [("disable", "principal_disabled"), ("revoke", "permission_revoked")])
def test_an_approval_by_someone_who_may_no_longer_decide_is_never_used(flow, change, reason):
    flow.paused()
    flow.decide("approved")
    if change == "disable":
        flow.store.principals.update(flow.dana.with_status(PrincipalStatus.DISABLED, flow.now()))
    else:
        flow.store.principals.revoke(flow.dana.id, {Permission.APPROVALS_DECIDE})
    flow.work()
    assert flow.notices() == 0
    [applied] = flow.events("approval_decision")
    assert (applied["approved"], applied["refused"]) == (False, reason)
    [decided] = milestones(flow, HUMAN_DECISION)
    assert decided.status is MilestoneStatus.FAILED and f"Not honored ({reason})" in decided.summary


def test_a_rejection_is_applied_even_if_its_author_was_disabled_since(flow):
    flow.paused()
    flow.decide("rejected")
    flow.store.principals.update(flow.dana.with_status(PrincipalStatus.DISABLED, flow.now()))
    flow.work()
    [applied] = flow.events("approval_decision")
    assert (applied["approved"], applied.get("refused")) == (False, None) and flow.notices() == 0


def test_the_local_operator_may_decide(flow):
    flow.paused()
    flow.decide("approved", Actor.unauthenticated("local operator"))
    flow.work()
    assert flow.notices() == 1  # the CLI's operator, with the workspace's own access


def test_a_system_actor_never_approves(flow):
    """Only a person approves a dangerous action; an automated "approval" is applied as refused."""
    flow.paused()
    flow.decide("approved", Actor(kind=ActorKind.SYSTEM, id="policy:auto", display_name="auto policy"))
    flow.work()
    [applied] = flow.events("approval_decision")
    assert (applied["approved"], applied["refused"]) == (False, "system_actor") and flow.notices() == 0


# ------------------------------------------------------------ reconciliation


def test_a_waiting_reconciliation_is_listed_and_wakes_nothing_until_answered(flaky):
    flaky.uncertain()
    [item] = flaky.store.approvals.page_attention(limit=10, at=flaky.now())
    assert item.kind is AttentionKind.RECONCILIATION_REQUIRED and item.attempt == 1
    assert claimed(flaky.work()) is None  # no deadline: only an answer wakes it


def test_it_happened_is_applied_by_a_worker_and_nothing_is_sent_again(flaky):
    flaky.uncertain()
    flaky.reconcile("it_happened")
    events = flaky.work()
    assert flaky.checkpoint().status is RunStatus.COMPLETED and flaky.notices() == 1
    execution = flaky.execution()
    assert execution.state is ExecutionState.SUCCEEDED
    [applied] = flaky.store.executions.reconciliations(flaky.project_id, "run-1", execution.id)
    assert applied.actor.id == flaky.dana.id and applied.choice is ReconciliationChoice.IT_HAPPENED
    [reconciled] = flaky.events("action_reconciled")
    assert reconciled["owner"] == f"{events[0]['worker_id']}@{claimed(events)['fencing_token']}"
    assert reconciled["actor_id"] == flaky.dana.id and reconciled["source"] == "product_store"
    assert any("confirmed send_notice happened" in m.summary for m in milestones(flaky, RUN_RESUMED))


def test_did_not_happen_is_applied_and_never_retried(flaky):
    flaky.uncertain()
    flaky.reconcile("did_not_happen")
    flaky.work()
    state = flaky.checkpoint()
    assert state.status is RunStatus.COMPLETED and flaky.notices() == 1
    assert flaky.execution().state is ExecutionState.FAILED and len(state.implementation["refused"]) == 1


def test_a_retry_is_a_new_15_minute_authorization_from_the_decision_for_exactly_one_more_attempt(flaky):
    flaky.uncertain()
    decided_at = flaky.now()
    flaky.reconcile("did_not_happen_retry")
    original = (flaky.request(), flaky.store.approvals.get_decision(flaky.project_id, flaky.request().id))
    flaky.clock.advance(minutes=14)  # a worker gets there 14 minutes later: still in time
    flaky.work()
    assert flaky.checkpoint().status is RunStatus.COMPLETED and flaky.notices() == 2  # the one more attempt
    execution = flaky.execution()
    assert (execution.state, execution.authorization_seq, execution.attempts) == (ExecutionState.SUCCEEDED, 2, 2)
    [applied] = flaky.store.executions.reconciliations(flaky.project_id, "run-1", execution.id)
    assert applied.decided_at == decided_at  # the window counts from the person's decision
    assert applied.retry_authorized_until == (flaky.clock.now - timedelta(minutes=14) + timedelta(minutes=15)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")  # fmt: skip
    assert (flaky.request(), flaky.store.approvals.get_decision(flaky.project_id, flaky.request().id)) == original


def test_a_retry_that_reaches_a_worker_after_its_window_never_dispatches(flaky):
    flaky.uncertain()
    flaky.reconcile("did_not_happen_retry")
    flaky.clock.advance(minutes=15)  # the boundary instant is expired
    flaky.work()
    assert flaky.notices() == 1
    execution = flaky.execution()
    assert execution.state is ExecutionState.LAPSED and execution.lapse_reason == "authorization_expired"


def test_a_reconciliation_by_someone_who_may_no_longer_reconcile_is_never_applied(flaky):
    flaky.uncertain()
    flaky.reconcile("it_happened")
    flaky.store.principals.revoke(flaky.dana.id, {Permission.ACTIONS_RECONCILE})
    events = flaky.work()
    assert (released(events)["reason"], released(events)["requeued"]) == ("decision_refused", False)
    assert flaky.execution().state is ExecutionState.UNCERTAIN and flaky.notices() == 1
    assert flaky.checkpoint().status is RunStatus.AWAITING_RECONCILIATION


def test_approvals_decide_alone_cannot_produce_an_applied_reconciliation(flaky):
    """Even recorded directly (bypassing the API), an answer by a decide-only person is refused by the worker."""
    flaky.uncertain()
    decider = person(flaky.store, "decider", DECIDE)
    flaky.reconcile("it_happened", decider.actor())
    assert released(flaky.work())["reason"] == "decision_refused"
    assert flaky.execution().state is ExecutionState.UNCERTAIN


def test_a_reconciler_without_approvals_decide_can_still_answer(flaky):
    flaky.uncertain()
    reconciler = person(flaky.store, "reconciler", RECONCILE)
    flaky.reconcile("it_happened", reconciler.actor())
    flaky.work()
    assert flaky.execution().state is ExecutionState.SUCCEEDED


# ---------------------------------------------------------- the overnight report


def test_the_overnight_questions_are_answered_by_structured_records_alone(flaky):
    """No narrative yet (E5 brief): every question an overnight report asks has a structured, attributed answer."""
    from agent_factory.domain.milestones import ACTION_EXECUTION, RUN_FINISHED
    from agent_factory.domain.timeline import worker_of

    night = flaky.now()
    flaky.paused()
    flaky.clock.advance(hours=2)
    flaky.decide("approved")
    flaky.clock.advance(minutes=1)
    flaky.work()  # sent; the gateway timed out: outcome unknown
    flaky.clock.advance(hours=3)
    flaky.reconcile("did_not_happen_retry", flaky.dana.actor())
    flaky.clock.advance(minutes=2)
    flaky.work()  # the retry
    morning = flaky.now()
    flaky.clock.advance(hours=1)
    report_at = flaky.now()  # the report is read later; milestone ranges are half-open: since <= t < until
    store, project = flaky.store, flaky.project_id

    request = flaky.request()
    assert (request.agent, request.node) == ("operator", "operate")  # who asked
    assert (request.action.tool, request.action.category, request.action.params) == (
        "send_notice", "external_communication", {"to": "billing@example.test", "text": "invoice ready"},
    )  # fmt: skip
    assert request.action_fingerprint == request.action.fingerprint  # exactly what
    decision = store.approvals.get_decision(project, request.id)
    assert (decision.actor.id, decision.actor.verified, decision.outcome.value) == (flaky.dana.id, True, "approved")
    execution = flaky.execution()
    attempts = store.executions.attempts(project, "run-1", execution.id)
    assert [a.result.value for a in attempts] == ["indeterminate", "succeeded"]  # what happened, attempt by attempt
    workers = [worker_of(a.owner) for a in attempts]
    assert all(w[0] and w[0].startswith("wkr-") and w[1] >= 1 for w in workers)  # which worker, which generation
    [answer] = store.reconciliations.list_for_execution(project, execution.id)
    [applied] = store.executions.reconciliations(project, "run-1", execution.id)
    assert (answer.actor.id, answer.choice.value) == (flaky.dana.id, "did_not_happen_retry")  # who reconciled
    assert applied.retry_authorized_until is not None and execution.authorization_seq == 2  # a retry was authorized
    assert execution.state is ExecutionState.SUCCEEDED and store.runs.get(project, "run-1").status is RunStatus.COMPLETED
    assert all(night <= t <= morning for t in (decision.decided_at, answer.decided_at, attempts[-1].ended_at))  # when
    overnight = store.milestones.query(MilestoneQuery(project_id=project, since=night, until=report_at))
    types = {m.type for m in overnight}
    assert {HUMAN_DECISION, ACTION_EXECUTION, RUN_RESUMED, RUN_FINISHED} <= types
    resumed = [m.summary for m in overnight if m.type == RUN_RESUMED]
    assert resumed == ["Resumed after dana approved send_notice.", "Resumed after dana authorized one more attempt of send_notice."]
    assert store.approvals.page_attention(limit=10, at=report_at) == []  # nothing still needs anyone

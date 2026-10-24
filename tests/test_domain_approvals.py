"""Approval requests and decisions: exact-action binding, idempotency, state machine."""

import pytest

from agent_factory.domain import (
    Actor,
    ActorKind,
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    ConflictError,
    DomainValidationError,
    ExactAction,
    RecordNotFoundError,
    StatusTransitionError,
    decision_id,
    risk_for,
)
from agent_factory.domain import Project, Run, in_memory_store, standard_milestone_policy
from agent_factory.domain.approvals import approval_expiry
from agent_factory.domain.errors import RecordNotFoundError
from agent_factory.domain.ids import new_principal_id
from agent_factory.domain.principals import Principal, PrincipalStatus
from agent_factory.state import RunStatus
from agent_factory.safety import DANGEROUS_CATEGORIES
from agent_factory.safety.approval import make_request_id

PID = "prj-" + "c3" * 10
T0 = "2026-10-02T01:00:00.000000Z"
T1 = "2026-10-02T01:30:00.000000Z"
T2 = "2026-10-02T03:00:00.000000Z"


def _action(path="legacy_calc.py", tool="delete_file", category="destructive_delete"):
    return ExactAction(tool=tool, category=category, params={"path": path})


def _request(path="legacy_calc.py", **kw):
    action = _action(path)
    base = dict(
        id=make_request_id("run-1", "developer", 1, "delete_file", {"path": path}),
        project_id=PID,
        run_id="run-1",
        node="developer",
        agent="developer",
        action=action,
        action_fingerprint=action.fingerprint,
        reason="destructive_delete requires human approval",
        risk=risk_for("destructive_delete"),
        requested_at=T0,
        expires_at=approval_expiry(T0),
        context={"step": 1},
    )
    base.update(kw)
    return ApprovalRequest(**base)


def store_with_run(store):
    """Approval requests belong to a run, which belongs to a project."""
    store.projects.add(Project(id=PID, name="Calculator", created_at=T0, milestone_policy=standard_milestone_policy()))
    store.runs.add(Run(project_id=PID, run_id="run-1", number=1, graph_name="software-dev-v1",
                       status=RunStatus.AWAITING_APPROVAL, created_at=T0, updated_at=T0))  # fmt: skip
    return store


@pytest.fixture
def svc():
    return ApprovalService(store_with_run(in_memory_store()).approvals)


# ------------------------------------------------------------ exact action


def test_fingerprint_covers_tool_category_and_every_parameter():
    base = _action().fingerprint
    assert base == _action().fingerprint
    assert base.startswith("sha256:")
    assert base != _action(path="other.py").fingerprint
    assert base != _action(tool="write_file").fingerprint
    assert base != _action(category="spend_money").fingerprint
    reordered = ExactAction(tool="t", category="spend_money", params={"a": 1, "b": 2})
    assert reordered.fingerprint == ExactAction(tool="t", category="spend_money", params={"b": 2, "a": 1}).fingerprint


def test_request_fingerprint_must_match_its_action():
    with pytest.raises(DomainValidationError, match="does not match"):
        _request(action_fingerprint=_action("other.py").fingerprint)


def test_engine_request_ids_are_reused_unchanged():
    req = _request()
    assert req.id == make_request_id("run-1", "developer", 1, "delete_file", {"path": "legacy_calc.py"})


def test_every_protected_category_has_a_risk_description():
    for category in DANGEROUS_CATEGORIES:
        assert len(risk_for(category.value)) > 10


def test_request_carries_everything_a_mobile_approval_screen_needs():
    req = _request()
    shown = {
        "project": req.project_id,
        "agent/node": f"{req.agent}/{req.node}",
        "requested action": f"{req.action.tool} {req.action.params}",
        "why": req.reason,
        "risk": req.risk,
        "requested at": req.requested_at,
    }
    assert all(value for value in shown.values())


# ------------------------------------------------------------- decisions


def test_decision_is_recorded_and_bound_to_the_exact_action(svc):
    req = svc.open_request(_request())
    decision = svc.decide(
        project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
        outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), comment="dead code", at=T1,
    )  # fmt: skip
    assert decision.id == decision_id(PID, req.id)
    assert decision.action_fingerprint == req.action_fingerprint
    stored = svc.repository.get_request(PID, req.id)
    assert stored.status is ApprovalStatus.APPROVED and stored.updated_at == T1
    assert svc.repository.get_decision(PID, req.id) == decision


def test_approval_for_action_a_never_authorizes_action_b(svc):
    a = svc.open_request(_request("a.py"))
    b = svc.open_request(_request("b.py"))
    svc.decide(project_id=PID, request_id=a.id, action_fingerprint=a.action_fingerprint,
               outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), at=T1)  # fmt: skip
    # A's approval does not touch B ...
    assert svc.repository.get_request(PID, b.id).status is ApprovalStatus.PENDING
    assert svc.repository.get_decision(PID, b.id) is None
    # ... and a decision made while looking at A cannot be applied to B.
    with pytest.raises(ApprovalBindingError):
        svc.decide(project_id=PID, request_id=b.id, action_fingerprint=a.action_fingerprint,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), at=T1)  # fmt: skip
    assert svc.repository.get_request(PID, b.id).status is ApprovalStatus.PENDING


def test_identical_duplicate_decision_is_idempotent(svc):
    req = svc.open_request(_request())
    kwargs = dict(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
                  outcome=ApprovalOutcome.REJECTED, actor=Actor.unauthenticated("bryan"))  # fmt: skip
    first = svc.decide(**kwargs, at=T1)
    again = svc.decide(**kwargs, at=T2)  # e.g. a double tap on a phone
    assert again == first and again.decided_at == T1


def _racing(repository, competitor):
    """Make ``competitor`` land first: just before our write, as another device's would."""
    real_save = repository.save_decision

    def save_decision(decision, request):
        repository.save_decision = real_save
        competitor()
        return real_save(decision, request)

    repository.save_decision = save_decision


@pytest.mark.parametrize("same_person, expect_conflict", [(True, False), (False, True)])
def test_a_decision_racing_another_is_settled_as_if_they_were_sequential(same_person, expect_conflict):
    store = store_with_run(in_memory_store())
    svc = ApprovalService(store.approvals)
    req = svc.open_request(_request())
    kwargs = dict(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint, outcome=ApprovalOutcome.APPROVED)
    rival = Actor.unauthenticated("bryan" if same_person else "someone-else")
    _racing(store.approvals, lambda: ApprovalService(store.approvals).decide(**kwargs, actor=rival, at=T1))
    if expect_conflict:
        with pytest.raises(ConflictError):
            svc.decide(**kwargs, actor=Actor.unauthenticated("bryan"), at=T2)
    else:  # both taps were the same person's identical decision: the first one stands
        assert svc.decide(**kwargs, actor=Actor.unauthenticated("bryan"), at=T2).decided_at == T1
    assert store.approvals.get_decision(PID, req.id).actor == rival


@pytest.mark.parametrize(
    "outcome, actor",
    [
        (ApprovalOutcome.REJECTED, Actor.unauthenticated("bryan")),  # different outcome
        (ApprovalOutcome.APPROVED, Actor.unauthenticated("someone-else")),  # different person
    ],
)
def test_conflicting_second_decision_is_rejected(svc, outcome, actor):
    req = svc.open_request(_request())
    svc.decide(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
               outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), at=T1)  # fmt: skip
    with pytest.raises(ConflictError):
        svc.decide(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
                   outcome=outcome, actor=actor, at=T2)  # fmt: skip
    assert svc.repository.get_decision(PID, req.id).outcome is ApprovalOutcome.APPROVED


def test_unknown_request_cannot_be_decided(svc):
    with pytest.raises(RecordNotFoundError):
        svc.decide(project_id=PID, request_id="apr-" + "0" * 16, action_fingerprint="sha256:" + "0" * 64,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("x"), at=T1)  # fmt: skip


def test_reopening_a_request_is_idempotent_but_cannot_change_its_action(svc):
    req = svc.open_request(_request())
    assert svc.open_request(_request()) == req
    changed = _request("other.py", id=req.id)
    with pytest.raises(ConflictError):
        svc.open_request(changed)


# --------------------------------------------------------- state machine


@pytest.mark.parametrize(
    "start, target",
    [
        ("approved", "rejected"),
        ("rejected", "approved"),
        ("expired", "approved"),
        ("cancelled", "pending"),
        ("approved", "pending"),
        ("pending", "pending"),
    ],
)
def test_invalid_approval_transitions_are_rejected(start, target):
    req = _request(status=ApprovalStatus(start), updated_at=None if start == "pending" else T1)
    with pytest.raises(StatusTransitionError):
        req.transition(ApprovalStatus(target), at=T2)


@pytest.mark.parametrize("target", ["approved", "rejected", "expired", "cancelled"])
def test_pending_can_reach_each_final_state(target):
    final = _request().transition(ApprovalStatus(target), at=T1)
    assert final.status is ApprovalStatus(target) and final.updated_at == T1


def test_expired_request_cannot_be_decided(svc):
    req = svc.open_request(_request(expires_at=T1))
    with pytest.raises(ApprovalExpiredError):
        svc.decide(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), at=T2)  # fmt: skip
    assert svc.repository.get_request(PID, req.id).status is ApprovalStatus.EXPIRED
    assert svc.repository.get_decision(PID, req.id) is None
    with pytest.raises(StatusTransitionError):
        svc.decide(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), at=T2)  # fmt: skip


def test_cancelled_request_cannot_be_decided(svc):
    req = svc.open_request(_request())
    svc.cancel(project_id=PID, request_id=req.id, at=T1)
    with pytest.raises(StatusTransitionError):
        svc.decide(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), at=T2)  # fmt: skip


# ------------------------------------------------------- identity, schema


def _principal(subject: str = "bryan-sub") -> Principal:
    return Principal(id=new_principal_id(), issuer="https://issuer.example.test", subject=subject,
                     status=PrincipalStatus.ACTIVE, created_at=T0, updated_at=T0, display_name="Bryan")  # fmt: skip


def test_an_authenticated_principal_is_recorded_as_a_verified_user():
    store = store_with_run(in_memory_store())
    principal = _principal()
    store.principals.add(principal)
    svc = ApprovalService(store.approvals)
    req = svc.open_request(_request())
    decision = svc.decide(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
                          outcome=ApprovalOutcome.APPROVED, actor=principal.actor(), at=T1)  # fmt: skip
    assert decision.actor == Actor(kind=ActorKind.USER, id=principal.id, display_name="Bryan", verified=True)
    assert ApprovalDecision.from_json(decision.to_json()).actor == principal.actor()


def test_a_user_decision_needs_an_enrolled_principal():
    store = store_with_run(in_memory_store())
    svc = ApprovalService(store.approvals)
    req = svc.open_request(_request())
    with pytest.raises(RecordNotFoundError, match="not enrolled"):
        svc.decide(project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.authenticated(new_principal_id(), "Ghost"), at=T1)  # fmt: skip
    assert store.approvals.get_decision(PID, req.id) is None


def test_approval_records_round_trip():
    req = _request(expires_at=T2)
    assert ApprovalRequest.from_json(req.to_json()) == req
    decision = ApprovalDecision(
        id=decision_id(PID, req.id), project_id=PID, request_id=req.id, action_fingerprint=req.action_fingerprint,
        outcome=ApprovalOutcome.REJECTED, decided_at=T1, actor=Actor.unauthenticated("bryan"), comment="not yet",
    )  # fmt: skip
    assert ApprovalDecision.from_json(decision.to_json()) == decision


def test_decision_id_must_belong_to_its_request():
    req = _request()
    with pytest.raises(DomainValidationError, match="id"):
        ApprovalDecision(
            id=decision_id(PID, "apr-" + "1" * 16), project_id=PID, request_id=req.id,
            action_fingerprint=req.action_fingerprint, outcome=ApprovalOutcome.APPROVED, decided_at=T1,
            actor=Actor.unauthenticated("x"),
        )  # fmt: skip

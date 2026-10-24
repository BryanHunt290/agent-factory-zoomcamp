"""The ActionExecution model: identity, the six-state machine, class policy, and reconciliation.

Pure domain tests: no storage, no engine (docs/design/action-execution.md).
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from agent_factory.domain.approvals import ExactAction
from agent_factory.domain.errors import DomainValidationError, SchemaVersionError, StatusTransitionError
from agent_factory.domain.executions import (
    EXECUTION_TRANSITIONS,
    RECONCILE_PERMISSION,
    RETRY_AUTHORIZATION_VALIDITY,
    ActionExecution,
    ActionReconciliation,
    AttemptResult,
    ExecutionAttempt,
    ExecutionSpec,
    ExecutionState,
    ReconciliationChoice,
    bounded_result,
    check_replace,
    execution_id,
    idempotency_key,
)
from agent_factory.domain.identity import Actor, ActorKind
from agent_factory.domain.serialization import format_timestamp, parse_timestamp
from agent_factory.safety.execution import EffectClass, ExecutionSemantics, Idempotency

P = "prj-" + "a" * 20
T0 = "2026-10-03T10:00:00.000000Z"
T1 = "2026-10-03T10:01:00.000000Z"
T2 = "2026-10-03T10:02:00.000000Z"
OWNER = "host:1:abcd"
ACTION = ExactAction(tool="send_notice", category="external_communication", params={"to": "a@example.test"})
REQUEST = "apr-" + "1" * 16

SEMANTICS = {
    "read_only": ExecutionSemantics(effect=EffectClass.READ_ONLY, max_attempts=2),
    "natural": ExecutionSemantics(effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.NATURAL, max_attempts=2),
    "keyed": ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.KEYED, key_retention=timedelta(hours=1), max_attempts=2
    ),
    "non_idempotent": ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT),
}


def authorized(kind="non_idempotent", until=None, action=ACTION) -> ActionExecution:
    spec = ExecutionSpec(
        project_id=P, run_id="run-1", request_id=REQUEST, node="operate", visit=1, agent="operator",
        action=action, semantics=SEMANTICS[kind], authorized_at=T0, authorized_until=until,
    )  # fmt: skip
    return ActionExecution.authorized(spec, at=T0)


def dispatched(kind="non_idempotent", at=T1):
    return authorized(kind).start_attempt(owner=OWNER, at=at)


# ---------------------------------------------------------------- identity


def test_execution_ids_are_stable_and_bound_to_project_run_approval_and_action():
    first = execution_id(P, "run-1", REQUEST, ACTION.fingerprint)
    assert first == execution_id(P, "run-1", REQUEST, ACTION.fingerprint)  # same action, any process, any restart
    assert first.startswith("axe-") and len(first) == 24
    other_action = ExactAction(tool="send_notice", category="external_communication", params={"to": "b@example.test"})
    variants = {
        execution_id("prj-" + "b" * 20, "run-1", REQUEST, ACTION.fingerprint),
        execution_id(P, "run-2", REQUEST, ACTION.fingerprint),
        execution_id(P, "run-1", "apr-" + "2" * 16, ACTION.fingerprint),
        execution_id(P, "run-1", REQUEST, other_action.fingerprint),
    }
    assert first not in variants and len(variants) == 4


def test_a_record_whose_id_does_not_match_its_identity_is_refused():
    with pytest.raises(DomainValidationError, match="does not belong"):
        replace(authorized(), id="axe-" + "0" * 20)


def test_idempotency_keys_are_derived_from_the_execution_and_stable_across_attempts():
    execution, attempt = dispatched("keyed")
    assert attempt.idempotency_key == idempotency_key(execution.id) == f"af1-{execution.id}-k1"
    retried, closed = execution.finish_attempt(attempt, result=AttemptResult.INDETERMINATE, at=T1)
    assert retried.state is ExecutionState.AUTHORIZED
    again, second = retried.start_attempt(owner="other:2:ef01", at=T1)
    assert second.attempt == 2 and second.idempotency_key == attempt.idempotency_key  # never a new key per retry
    assert dispatched("non_idempotent")[1].idempotency_key is None  # only keyed tools get one


# ------------------------------------------------------------ state machine


def test_the_six_states_and_their_transitions_are_exactly_the_approved_model():
    S = ExecutionState
    assert [s.value for s in S] == ["authorized", "dispatching", "succeeded", "failed", "uncertain", "lapsed"]
    assert EXECUTION_TRANSITIONS == {
        S.AUTHORIZED: {S.DISPATCHING, S.LAPSED, S.UNCERTAIN},
        S.DISPATCHING: {S.SUCCEEDED, S.FAILED, S.AUTHORIZED, S.UNCERTAIN},
        S.UNCERTAIN: {S.SUCCEEDED, S.FAILED, S.AUTHORIZED},
        S.SUCCEEDED: set(),
        S.FAILED: set(),
        S.LAPSED: set(),
    }
    assert {s for s in S if s.is_final} == {S.SUCCEEDED, S.FAILED, S.LAPSED}


@pytest.mark.parametrize("target", [s for s in ExecutionState if s not in (ExecutionState.DISPATCHING, ExecutionState.LAPSED, ExecutionState.UNCERTAIN)])
def test_an_illegal_transition_is_refused_not_repaired(target):
    old = authorized()
    with pytest.raises((StatusTransitionError, DomainValidationError)):
        new = replace(old, version=2, state=target, **_fields_for(target))
        ActionExecution.check_successor(old, new)


def _fields_for(state):
    if state is ExecutionState.SUCCEEDED:
        return {"result": {"ok": True, "summary": "", "data": None}, "finished_at": T1}
    if state is ExecutionState.FAILED:
        return {"failure": {"type": "X", "message": ""}, "finished_at": T1}
    return {}


def test_final_executions_never_change():
    execution, attempt = dispatched()
    done, _ = execution.finish_attempt(
        attempt, result=AttemptResult.SUCCEEDED, at=T2, payload=bounded_result(True, "sent", None, replayable=True)
    )
    for action in (lambda e: e.start_attempt(owner=OWNER, at=T2), lambda e: e.lapse(at=T2, reason="x")):
        with pytest.raises(StatusTransitionError):
            action(done)


def test_a_dispatch_needs_an_authorized_execution_with_attempts_left_and_an_open_window():
    with pytest.raises(StatusTransitionError, match="only an authorized"):
        dispatched()[0].start_attempt(owner=OWNER, at=T2)
    expired = authorized(until=T1)
    with pytest.raises(StatusTransitionError, match="expired"):
        expired.start_attempt(owner=OWNER, at=T1)  # must BEGIN before the authorization expires


# --------------------------------------------------------------- class policy


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("read_only", ExecutionState.AUTHORIZED),  # safe to retry
        ("natural", ExecutionState.AUTHORIZED),  # a repeat leaves the same end state
        ("keyed", ExecutionState.AUTHORIZED),  # retry with the same key, inside the window
        ("non_idempotent", ExecutionState.UNCERTAIN),  # never resent automatically
    ],
)
def test_an_unknown_outcome_follows_the_tools_class(kind, expected):
    execution, attempt = dispatched(kind)
    new, closed = execution.finish_attempt(attempt, result=AttemptResult.INDETERMINATE, at=T2)
    assert new.state is expected and closed.result is AttemptResult.INDETERMINATE


def test_a_keyed_retry_outside_the_providers_window_becomes_uncertain():
    execution, attempt = dispatched("keyed", at=T0)
    late = format_timestamp(parse_timestamp(T0) + timedelta(hours=1))
    new, _ = execution.finish_attempt(attempt, result=AttemptResult.INDETERMINATE, at=late)
    assert new.state is ExecutionState.UNCERTAIN  # the provider may have forgotten the key


@pytest.mark.parametrize("kind,exhausted", [("read_only", ExecutionState.FAILED), ("natural", ExecutionState.UNCERTAIN)])
def test_retries_are_bounded_per_authorization(kind, exhausted):
    execution, attempt = dispatched(kind)
    for _ in range(execution.max_attempts - 1):
        execution, _ = execution.finish_attempt(attempt, result=AttemptResult.INDETERMINATE, at=T1)
        execution, attempt = execution.start_attempt(owner=OWNER, at=T1)
    final, _ = execution.finish_attempt(attempt, result=AttemptResult.INDETERMINATE, at=T1)
    assert final.state is exhausted


def test_known_outcomes_are_not_retried():
    execution, attempt = dispatched("natural")
    failed, _ = execution.finish_attempt(
        attempt, result=AttemptResult.FAILED, at=T1, failure={"type": "ToolError", "message": "refused"}
    )
    assert failed.state is ExecutionState.FAILED  # a definitive failure is not an automatic retry


def test_provably_unsent_attempts_give_their_dispatch_back():
    execution, attempt = dispatched("non_idempotent")
    new, closed = execution.finish_attempt(attempt, result=AttemptResult.NOT_DISPATCHED, at=T1)
    assert new.state is ExecutionState.AUTHORIZED and new.dispatches == 0 and closed.result is AttemptResult.NOT_DISPATCHED


def test_an_authorization_with_a_possible_effect_cannot_lapse():
    execution, attempt = dispatched("natural")
    retry, _ = execution.finish_attempt(attempt, result=AttemptResult.INDETERMINATE, at=T1)
    with pytest.raises(StatusTransitionError, match="may have taken effect"):
        retry.lapse(at=T2, reason="node_visit_ended")
    closed = retry.close_authorization(at=T2, reason="node_visit_ended")
    assert closed.state is ExecutionState.UNCERTAIN  # only a person can say what happened
    assert authorized().close_authorization(at=T2, reason="node_visit_ended").state is ExecutionState.LAPSED


# ------------------------------------------------------------- reconciliation


def uncertain():
    execution, attempt = dispatched()
    new, _ = execution.finish_attempt(attempt, result=AttemptResult.INDETERMINATE, at=T1)
    return new


def reconciliation(choice, execution=None, **changes):
    execution = execution or uncertain()
    values = dict(
        execution_id=execution.id, attempt=execution.attempts, action_fingerprint=execution.action_fingerprint,
        choice=choice, decided_at=T2, actor=Actor.unauthenticated("dana"),
        retry_authorized_until=(
            format_timestamp(parse_timestamp(T2) + RETRY_AUTHORIZATION_VALIDITY)
            if choice is ReconciliationChoice.AUTHORIZE_RETRY else None
        ),
    )  # fmt: skip
    values.update(changes)
    return ActionReconciliation(**values)


def test_it_happened_records_a_confirmed_success():
    execution = uncertain()
    done = execution.resolve(reconciliation(ReconciliationChoice.IT_HAPPENED, execution), at=T2)
    assert done.state is ExecutionState.SUCCEEDED and done.result["confirmed_by"] == "dana"


def test_did_not_happen_records_a_failure_and_no_retry():
    execution = uncertain()
    done = execution.resolve(reconciliation(ReconciliationChoice.DID_NOT_HAPPEN, execution), at=T2)
    assert done.state is ExecutionState.FAILED and done.failure["type"] == "ConfirmedNotExecuted"


def test_a_retry_is_a_new_fifteen_minute_authorization_bound_to_the_prior_attempt():
    execution = uncertain()
    rec = reconciliation(ReconciliationChoice.AUTHORIZE_RETRY, execution)
    retry = execution.resolve(rec, at=T2)
    assert RETRY_AUTHORIZATION_VALIDITY == timedelta(minutes=15)
    assert retry.state is ExecutionState.AUTHORIZED
    assert retry.authorization_seq == execution.authorization_seq + 1  # new, not the original reactivated
    assert retry.authorized_at == T2 and retry.authorized_until == "2026-10-03T10:17:00.000000Z"
    assert retry.dispatches == 0 and retry.id == execution.id and retry.action_fingerprint == execution.action_fingerprint
    assert rec.attempt == execution.attempts and rec.execution_id == execution.id


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"attempt": 9}, "uncertain attempt"),
        ({"action_fingerprint": "sha256:" + "0" * 64}, "different action|does not belong"),
    ],
)
def test_a_reconciliation_must_name_the_exact_attempt_and_action(changes, match):
    execution = uncertain()
    with pytest.raises(DomainValidationError, match=match):
        execution.resolve(reconciliation(ReconciliationChoice.IT_HAPPENED, execution, **changes), at=T2)


def test_only_a_person_reconciles_and_a_retry_needs_its_window():
    with pytest.raises(DomainValidationError, match="only a person"):
        reconciliation(ReconciliationChoice.IT_HAPPENED, actor=Actor(kind=ActorKind.SYSTEM, id="sys", display_name="sys"))
    with pytest.raises(DomainValidationError, match="retry_authorized_until"):
        reconciliation(ReconciliationChoice.AUTHORIZE_RETRY, retry_authorized_until=None)
    with pytest.raises(DomainValidationError, match="retry_authorized_until"):
        reconciliation(ReconciliationChoice.IT_HAPPENED, retry_authorized_until=T2)
    assert RECONCILE_PERMISSION == "actions:reconcile"  # not approvals:decide


def test_an_uncertain_execution_changes_only_through_a_reconciliation():
    execution = uncertain()
    resolved = execution.resolve(reconciliation(ReconciliationChoice.IT_HAPPENED, execution), at=T2)
    with pytest.raises(DomainValidationError, match="only by reconciliation"):
        check_replace(execution, resolved, attempt=None, reconciliation=None)


# ------------------------------------------------------------- records


def test_records_round_trip_and_reject_unknown_or_newer_data():
    execution, attempt = dispatched("keyed")
    assert ActionExecution.from_dict(execution.to_dict()) == execution
    assert ExecutionAttempt.from_dict(attempt.to_dict()) == attempt
    rec = reconciliation(ReconciliationChoice.AUTHORIZE_RETRY)
    assert ActionReconciliation.from_dict(rec.to_dict()) == rec
    data = execution.to_dict()
    with pytest.raises(DomainValidationError, match="unknown field"):
        ActionExecution.from_dict({**data, "surprise": 1})
    with pytest.raises(SchemaVersionError, match="newer"):
        ActionExecution.from_dict({**data, "schema_version": 2})


@pytest.mark.parametrize(
    "changes",
    [
        {"max_attempts": 2},  # class C gets exactly one automatic attempt
        {"state": ExecutionState.SUCCEEDED},  # no result recorded
        {"owner": OWNER},  # an owner without an open attempt
        {"authorized_until": T0},  # a window that ends when it starts
        {"action_fingerprint": "sha256:" + "0" * 64},  # not the action's fingerprint
        {"dispatches": 1},  # more dispatches than attempts recorded
    ],
)
def test_contradictory_records_are_refused(changes):
    with pytest.raises(DomainValidationError):
        replace(authorized(), **changes)


def test_results_are_bounded_and_opaque_data_is_not_replayed():
    small = bounded_result(True, "ok", {"n": 1}, replayable=True)
    assert small["data"] == {"n": 1}
    large = bounded_result(True, "x" * 10_000, "y" * 10_000, replayable=True)
    assert len(large["summary"]) <= 500 and large["data"] is None and large["data_omitted"] is True
    opaque = bounded_result(True, "read 3 files", ["a", "b"], replayable=False)
    assert opaque["data"] is None and opaque["data_omitted"] is True

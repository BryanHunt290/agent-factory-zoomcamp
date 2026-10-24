"""E5 over HTTP: reconciliation answers, the execution timeline, the attention inbox, expiry, and wake-ups.

Every scenario runs against the in-memory store (default suite) and real
PostgreSQL through the production pool (``-m postgres --run-postgres``), and
every response is validated against the checked-in openapi.yaml
(``ContractClient``). The API records intent only: these tests also prove
that no answer or decision changes an execution, a run, or a milestone.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

import pytest

pytest.importorskip("fastapi", reason="API tests need the API packages: run with .venv (requirements-api.txt)")
pytest.importorskip("jsonschema", reason="API tests validate against openapi.yaml: run with .venv (requirements-dev.txt)")

from api_support import (  # noqa: E402 - after the importorskip guards
    DISPATCHED_AT, ENDED_AT, NOW, POSIX_PATH, SECRET, T0, T1, WINDOWS_PATH, WORKER_ID, ContractClient, add_approval,
    add_project, add_run, add_uncertain_execution, application, decision_body, decision_url, reconcile_body,
    reconcile_url, run_is_due,
)
from auth_support import ISSUER, enroll, ensure_owner  # noqa: E402

from agent_factory.api.stores import InMemoryStoreProvider  # noqa: E402
from agent_factory.domain.executions import ExecutionLedger, ExecutionState, LedgerKind  # noqa: E402
from agent_factory.domain.identity import ActorKind  # noqa: E402
from agent_factory.domain.principals import Permission  # noqa: E402
from agent_factory.domain.repositories import MilestoneQuery  # noqa: E402
from agent_factory.domain.serialization import format_timestamp  # noqa: E402
from agent_factory.safety.execution import EffectClass, ExecutionSemantics, Idempotency  # noqa: E402
from agent_factory.state import RunStatus  # noqa: E402

CANARIES = (SECRET, "operator-canary", "C:\\private", "/home/private")


@dataclass
class Backend:
    new_provider: Callable[[], Any]
    seed: Any
    kind: str

    def client(self, token=None, **auth_limits):
        authenticator = ISSUER.authenticator(**auth_limits) if auth_limits else None
        app = application(self.new_provider(), authenticator=authenticator)
        return ContractClient(app) if token is None else ContractClient(app, token=token)

    def as_(self, subject, permissions, **auth_limits):
        enroll(self.seed, subject, permissions)
        return self.client(ISSUER.token(subject), **auth_limits)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request):
    if request.param == "memory":
        provider = InMemoryStoreProvider()
        yield Backend(lambda: provider, provider.store, "memory")
        return
    from agent_factory.persistence.pool import PostgresStorePool

    config = request.getfixturevalue("pg_schema")
    seed = request.getfixturevalue("pg_store")
    ensure_owner(seed)
    pools = []

    def new_pool():
        pool = PostgresStorePool(config, min_size=1, max_size=8, timeout=5.0)
        pools.append(pool)
        return pool

    yield Backend(new_pool, seed, "postgres")
    for pool in pools:
        pool.close()


@pytest.fixture
def client(backend):
    with backend.client() as current:
        yield current


@pytest.fixture
def uncertain(backend):
    """A run waiting for a person to say whether its approved action happened."""
    project = add_project(backend.seed)
    add_run(backend.seed, project, status=RunStatus.AWAITING_RECONCILIATION)
    request = add_approval(backend.seed, project)
    return project, request, add_uncertain_execution(backend.seed, project, request)


def approval_url(project, request):
    return f"/api/v1/projects/{project.id}/approvals/{request.id}"


def all_pages(client, url, limit):
    items, cursor, pages = [], None, 0
    while True:
        separator = "&" if "?" in url else "?"
        response = client.get(f"{url}{separator}limit={limit}" + (f"&cursor={cursor}" if cursor else ""))
        assert response.status_code == 200, response.text
        items += response.json()["items"]
        pages += 1
        cursor = response.json()["next_cursor"]
        if cursor is None:
            return items, pages


# ------------------------------------------------------------- the phone view


def test_an_uncertain_action_shows_its_execution_timeline_and_needs_reconciliation(client, uncertain):
    project, request, execution = uncertain
    view = client.get(approval_url(project, request)).json()
    assert view["status"] == "approved" and view["attention"] == "reconciliation_required"
    assert view["action_summary"] == "delete_file (destructive delete)"
    assert view["execution"] == {
        "id": execution.id, "state": "uncertain", "attempt": 1, "retry_authorized_until": None, "reconciliation": None,
    }  # fmt: skip
    steps = [(s["kind"], s["at"], s["attempt"]) for s in view["timeline"]]
    assert steps == [
        ("approval_requested", T1, None),
        ("approved", "2026-10-02T11:10:00.000000Z", None),
        ("action_dispatching", DISPATCHED_AT, 1),
        ("outcome_uncertain", ENDED_AT, 1),
        ("reconciliation_required", ENDED_AT, 1),
    ]
    dispatched = view["timeline"][2]
    assert dispatched["worker"] == {"worker_id": WORKER_ID, "fencing_token": 3}  # which worker, which generation
    assert view["timeline"][1]["actor"]["display_name"] == "seed operator"
    assert view["timeline"][0]["until"] == request.expires_at


def test_a_pending_approval_shows_a_decision_is_required_and_when_it_expires(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    request = add_approval(backend.seed, project)
    view = client.get(approval_url(project, request)).json()
    assert (view["status"], view["attention"], view["execution"]) == ("pending", "decision_required", None)
    assert view["expires_at"] == "2026-10-02T23:00:00.000000Z"  # requested 11:00, 12 hours
    assert [s["kind"] for s in view["timeline"]] == ["approval_requested"]


def test_a_pending_approval_past_its_expiry_is_shown_expired_and_listed_nowhere(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    request = add_approval(backend.seed, project, at=T0, expires_at=T1)  # the API's clock reads 12:00
    view = client.get(approval_url(project, request)).json()
    assert (view["status"], view["attention"]) == ("expired", None)
    assert [s["kind"] for s in view["timeline"]] == ["approval_requested", "expired"]
    assert client.get("/api/v1/approvals?status=pending").json()["items"] == []
    assert client.get("/api/v1/attention").json()["items"] == []
    assert client.get(f"/api/v1/projects/{project.id}").json()["pending_approvals"] == 0


def test_the_expiry_instant_itself_is_expired(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    request = add_approval(backend.seed, project, at=T0, expires_at=format_timestamp(NOW))
    assert client.get(approval_url(project, request)).json()["status"] == "expired"
    refused = client.post(decision_url(project, request), json=decision_body(request))
    assert refused.status_code == 409 and refused.json()["code"] == "approval_expired"


def test_the_phone_view_and_inbox_never_show_secret_or_host_path_canaries(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project, status=RunStatus.AWAITING_RECONCILIATION)
    request = add_approval(backend.seed, project, reason=f"delete {POSIX_PATH} with {SECRET}", risk=f"{WINDOWS_PATH}")
    add_uncertain_execution(backend.seed, project, request)
    responses = [client.get(approval_url(project, request)), client.get("/api/v1/attention")]
    for response in responses:
        assert response.status_code == 200, response.text
        for canary in CANARIES:
            assert canary not in response.text, canary
    for word in ("Traceback", "TimeoutError", "postgres", "docker", "checkpoint"):
        assert word not in responses[0].text  # the attempt's detail and internals stay in the ledger


def test_an_action_with_sensitive_parameters_is_refused_rather_than_shown_redacted(client, backend):
    """The approver must see exactly what they approve; a secret in the parameters fails closed (E3)."""
    project = add_project(backend.seed)
    add_run(backend.seed, project, status=RunStatus.AWAITING_RECONCILIATION)
    request = add_approval(backend.seed, project, path=f"/tmp/{SECRET}")
    execution = add_uncertain_execution(backend.seed, project, request)
    shown = client.get(approval_url(project, request))
    assert shown.status_code == 500 and SECRET not in shown.text
    answered = client.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert answered.status_code == 500 and SECRET not in answered.text
    assert backend.seed.reconciliations.get(project.id, execution.id, 1) is None  # nothing recorded
    inbox = client.get("/api/v1/attention")
    assert inbox.status_code == 200 and SECRET not in inbox.text  # listed by tool and category only


# ------------------------------------------------------------- reconciliation


def test_an_answer_is_201_records_the_principal_and_server_time_and_wakes_the_run(client, backend, uncertain):
    project, request, execution = uncertain
    assert not run_is_due(backend.seed, project.id, "run-1")
    response = client.post(reconcile_url(project, request), json=reconcile_body(execution, comment="Checked the log."))
    assert response.status_code == 201
    assert response.headers["Location"] == approval_url(project, request)
    recorded = backend.seed.reconciliations.get(project.id, execution.id, 1)
    assert recorded.actor.kind is ActorKind.USER and recorded.actor.verified
    assert recorded.decided_at == format_timestamp(NOW) and recorded.comment == "Checked the log."
    assert run_is_due(backend.seed, project.id, "run-1")  # committed with the answer
    view = response.json()
    assert view["attention"] is None  # answered: waiting for a worker, not for a person
    assert view["execution"]["state"] == "uncertain"  # recording is not executing
    assert view["execution"]["reconciliation"] == {
        "attempt": 1, "choice": "it_happened", "decided_at": format_timestamp(NOW),
        "decided_by": view["execution"]["reconciliation"]["decided_by"], "comment": "Checked the log.", "applied": False,
    }  # fmt: skip
    assert view["timeline"][-1]["kind"] == "reconciliation_decided" and view["timeline"][-1]["outcome"] == "it_happened"


def test_recording_an_answer_changes_no_execution_run_or_milestone(client, backend, uncertain):
    project, request, execution = uncertain
    everything = MilestoneQuery(project_id=project.id)
    run_before, milestones_before = backend.seed.runs.get(project.id, "run-1"), backend.seed.milestones.query(everything)
    response = client.post(reconcile_url(project, request), json=reconcile_body(execution, "did_not_happen_retry"))
    assert response.status_code == 201
    assert backend.seed.executions.get(project.id, "run-1", execution.id) == execution
    assert backend.seed.executions.reconciliations(project.id, "run-1", execution.id) == []
    assert backend.seed.runs.get(project.id, "run-1") == run_before
    assert backend.seed.milestones.query(everything) == milestones_before
    assert response.json()["execution"]["retry_authorized_until"] is None  # authorized only when a worker applies it


def test_an_identical_repeat_is_200_and_stores_nothing_new(client, backend, uncertain):
    project, request, execution = uncertain
    first = client.post(reconcile_url(project, request), json=reconcile_body(execution))
    again = client.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert (first.status_code, again.status_code) == (201, 200)
    assert "Location" not in again.headers
    assert again.json()["execution"]["reconciliation"] == first.json()["execution"]["reconciliation"]
    assert len(backend.seed.reconciliations.list_for_execution(project.id, execution.id)) == 1


def test_a_different_answer_or_another_person_is_409_and_keeps_the_first(backend, uncertain):
    project, request, execution = uncertain
    with backend.client() as owner:
        assert owner.post(reconcile_url(project, request), json=reconcile_body(execution)).status_code == 201
        changed = owner.post(reconcile_url(project, request), json=reconcile_body(execution, "did_not_happen"))
    assert changed.status_code == 409 and changed.json()["code"] == "reconciliation_already_decided"
    with backend.as_("second-person", {Permission.ACTIONS_RECONCILE}) as other:
        same_choice = other.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert same_choice.status_code == 409 and same_choice.json()["code"] == "reconciliation_already_decided"
    assert backend.seed.reconciliations.get(project.id, execution.id, 1).choice.value == "it_happened"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"attempt": 2}, "reconciliation_stale"),
        ({"attempt": 10**30}, "reconciliation_stale"),
        ({"execution_id": "axe-" + "0" * 20}, "reconciliation_stale"),
        ({"action_fingerprint": "sha256:" + "0" * 64}, "action_fingerprint_mismatch"),
    ],
    ids=["later-attempt", "huge-attempt", "other-execution", "other-action"],
)
def test_an_answer_not_bound_to_the_waiting_attempt_is_409_and_records_nothing(client, backend, uncertain, change, code):
    project, request, execution = uncertain
    response = client.post(reconcile_url(project, request), json={**reconcile_body(execution), **change})
    assert response.status_code == 409 and response.json()["code"] == code
    assert backend.seed.reconciliations.list_for_execution(project.id, execution.id) == []
    assert not run_is_due(backend.seed, project.id, "run-1")


def test_an_answer_for_an_earlier_attempt_after_an_automatic_retry_is_stale(client, backend):
    """A keyed tool retried automatically: only the last attempt, the uncertain one, can be answered."""
    project = add_project(backend.seed)
    add_run(backend.seed, project, status=RunStatus.AWAITING_RECONCILIATION)
    request = add_approval(backend.seed, project)
    keyed = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.KEYED, key_retention=timedelta(hours=1), max_attempts=2
    )
    execution = add_uncertain_execution(backend.seed, project, request, semantics=keyed, attempts=2)
    assert (execution.state, execution.attempts) == (ExecutionState.UNCERTAIN, 2)
    earlier = client.post(reconcile_url(project, request), json={**reconcile_body(execution), "attempt": 1})
    assert earlier.status_code == 409 and earlier.json()["code"] == "reconciliation_stale"
    view = client.get(approval_url(project, request)).json()
    kinds = [(s["kind"], s["attempt"]) for s in view["timeline"]]
    assert kinds[2:] == [
        ("action_dispatching", 1), ("outcome_uncertain", 1), ("action_dispatching", 2), ("outcome_uncertain", 2),
        ("reconciliation_required", 2),
    ]  # fmt: skip
    assert client.post(reconcile_url(project, request), json=reconcile_body(execution)).status_code == 201


def test_nothing_to_reconcile_is_409(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    pending = add_approval(backend.seed, project)
    from agent_factory.domain.executions import execution_id

    body = {
        "execution_id": execution_id(project.id, "run-1", pending.id, pending.action_fingerprint), "attempt": 1,
        "action_fingerprint": pending.action_fingerprint, "choice": "it_happened",
    }  # fmt: skip
    response = client.post(reconcile_url(project, pending), json=body)
    assert response.status_code == 409 and response.json()["code"] == "reconciliation_not_required"


def test_an_execution_that_already_succeeded_needs_no_answer(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project, status=RunStatus.RUNNING)
    request = add_approval(backend.seed, project)
    execution = add_uncertain_execution(backend.seed, project, request)
    ledger = ExecutionLedger(backend.seed.executions, kind=LedgerKind.PRODUCT_STORE)
    from agent_factory.domain.executions import ReconciliationChoice
    from agent_factory.domain.identity import Actor

    ledger.reconcile(
        project.id, "run-1", execution.id, attempt=1, action_fingerprint=execution.action_fingerprint,
        choice=ReconciliationChoice.IT_HAPPENED, actor=Actor.unauthenticated("operator"),
    )  # fmt: skip
    response = client.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert response.status_code == 409 and response.json()["code"] == "reconciliation_not_required"
    view = client.get(approval_url(project, request)).json()
    assert view["execution"]["state"] == "succeeded" and view["execution"]["reconciliation"]["applied"] is True
    assert view["timeline"][-1]["kind"] == "resolved" and view["timeline"][-1]["outcome"] == "succeeded"


def test_unknown_approvals_and_projects_are_404(client, uncertain):
    project, request, execution = uncertain
    other = f"/api/v1/projects/{project.id}/approvals/apr-{'0' * 16}/reconciliation"
    assert client.post(other, json=reconcile_body(execution)).json()["code"] == "approval_not_found"
    elsewhere = f"/api/v1/projects/prj-{'0' * 20}/approvals/{request.id}/reconciliation"
    assert client.post(elsewhere, json=reconcile_body(execution)).json()["code"] == "project_not_found"


@pytest.mark.parametrize(
    "change",
    [
        {"decided_at": "2026-10-02T00:00:00.000000Z"}, {"actor": {"kind": "user"}}, {"decided_by": "someone"},
        {"retry_authorized_until": "2026-10-03T00:00:00.000000Z"}, {"attempt": "1"}, {"attempt": 0}, {"attempt": True},
        {"choice": "retry"}, {"choice": "IT_HAPPENED"}, {"comment": "x" * 1001}, {"comment": "bell\u0007"},
        {"execution_id": "not-an-id"},
    ],
    ids=lambda c: next(iter(c)) + ":" + repr(next(iter(c.values())))[:12],
)
def test_server_controlled_and_malformed_fields_are_refused(client, backend, uncertain, change):
    project, request, execution = uncertain
    response = client.post(reconcile_url(project, request), json={**reconcile_body(execution), **change})
    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert backend.seed.reconciliations.list_for_execution(project.id, execution.id) == []


def test_concurrent_answers_settle_on_exactly_one(client, backend, uncertain):
    project, request, execution = uncertain
    barrier = threading.Barrier(6)
    choices = ("it_happened", "did_not_happen", "did_not_happen_retry")

    def attempt(i):
        barrier.wait()
        return client.post(reconcile_url(project, request), json=reconcile_body(execution, choices[i % 3]))

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(attempt, range(6)))
    assert sum(r.status_code == 201 for r in responses) == 1
    winner = backend.seed.reconciliations.get(project.id, execution.id, 1).choice.value
    for r in responses:
        if r.status_code == 200:
            assert r.json()["execution"]["reconciliation"]["choice"] == winner
        elif r.status_code == 409:
            assert r.json()["code"] == "reconciliation_already_decided"
        elif r.status_code == 503:
            assert r.json()["code"] == "storage_unavailable"
        else:
            assert r.status_code == 201
    assert len(backend.seed.reconciliations.list_for_execution(project.id, execution.id)) == 1


# --------------------------------------------------------------- permissions


def test_approvals_decide_does_not_allow_reconciling(backend, uncertain):
    project, request, execution = uncertain
    with backend.as_("decider", {Permission.APPROVALS_DECIDE, Permission.APPROVALS_READ}) as decider:
        denied = decider.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert denied.status_code == 403 and denied.json()["required_permission"] == "actions:reconcile"
    assert backend.seed.reconciliations.list_for_execution(project.id, execution.id) == []


def test_actions_reconcile_does_not_allow_deciding_or_reading(backend, uncertain):
    project, request, execution = uncertain
    pending = add_approval(backend.seed, project, path="other.py")
    with backend.as_("reconciler", {Permission.ACTIONS_RECONCILE}) as reconciler:
        decided = reconciler.post(decision_url(project, pending), json=decision_body(pending))
        read = reconciler.get(approval_url(project, request))
        inbox = reconciler.get("/api/v1/attention")
        answered = reconciler.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert decided.status_code == 403 and decided.json()["required_permission"] == "approvals:decide"
    assert read.status_code == 403 and inbox.status_code == 403
    assert answered.status_code == 201
    assert backend.seed.approvals.get_decision(project.id, pending.id) is None


def test_decisions_and_answers_share_one_per_principal_limit(backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project, status=RunStatus.AWAITING_RECONCILIATION)
    add_run(backend.seed, project, run_id="run-2", number=2)
    first = add_approval(backend.seed, project)
    execution = add_uncertain_execution(backend.seed, project, first)
    pending = add_approval(backend.seed, project, run_id="run-2")
    everything = {Permission.APPROVALS_DECIDE, Permission.ACTIONS_RECONCILE, Permission.APPROVALS_READ}
    with backend.as_("busy-person", everything, decisions_per_minute=2) as client:
        assert client.post(reconcile_url(project, first), json=reconcile_body(execution)).status_code == 201
        assert client.post(decision_url(project, pending), json=decision_body(pending)).status_code == 201
        limited = client.post(reconcile_url(project, first), json=reconcile_body(execution))
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1


# ------------------------------------------------------------------ wake-ups


def test_a_decision_wakes_its_run_in_the_same_transaction(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    add_run(backend.seed, project, run_id="run-2", number=2)
    request = add_approval(backend.seed, project)
    assert not run_is_due(backend.seed, project.id, "run-1")
    assert client.post(decision_url(project, request), json=decision_body(request, "rejected")).status_code == 201
    assert run_is_due(backend.seed, project.id, "run-1")
    assert not run_is_due(backend.seed, project.id, "run-2")  # only the decided run


def test_a_refused_decision_wakes_nothing(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    request = add_approval(backend.seed, project)
    wrong = client.post(decision_url(project, request), json={**decision_body(request), "action_fingerprint": "sha256:" + "1" * 64})
    assert wrong.status_code == 409
    expired = add_approval(backend.seed, project, path="late.py", at=T0, expires_at=T1)
    assert client.post(decision_url(project, expired), json=decision_body(expired)).status_code == 409
    assert not run_is_due(backend.seed, project.id, "run-1")


# ------------------------------------------------------------- the inbox


def test_the_inbox_lists_decisions_and_reconciliations_oldest_first_across_projects(client, backend):
    first, second = add_project(backend.seed, name="First"), add_project(backend.seed, name="Second")
    add_run(backend.seed, first, status=RunStatus.AWAITING_RECONCILIATION)
    add_run(backend.seed, second)
    add_run(backend.seed, second, run_id="run-2", number=2)
    uncertain_request = add_approval(backend.seed, first, at=T0)
    execution = add_uncertain_execution(backend.seed, first, uncertain_request)  # uncertain since 11:12
    pending = add_approval(backend.seed, second, at=T1)  # since 11:00
    add_approval(backend.seed, second, run_id="run-2", at="2026-10-01T23:00:00.000000Z")  # expired at 11:00
    items, pages = all_pages(client, "/api/v1/attention", limit=1)
    assert pages == 2
    assert [(i["kind"], i["approval_id"], i["project_name"]) for i in items] == [
        ("decision_required", pending.id, "Second"),
        ("reconciliation_required", uncertain_request.id, "First"),
    ]
    decision_item, reconciliation_item = items
    assert decision_item["since"] == T1 and decision_item["expires_at"] == "2026-10-02T23:00:00.000000Z"
    assert (decision_item["execution_id"], decision_item["attempt"]) == (None, None)
    assert decision_item["summary"] == "Approval needed: delete_file (destructive delete)."
    assert reconciliation_item["since"] == ENDED_AT and reconciliation_item["expires_at"] is None
    assert (reconciliation_item["execution_id"], reconciliation_item["attempt"]) == (execution.id, 1)
    assert reconciliation_item["summary"] == (
        "Outcome unknown: delete_file (destructive delete) was started; say whether it happened."
    )
    scoped = client.get(f"/api/v1/attention?project_id={first.id}").json()["items"]
    assert [i["approval_id"] for i in scoped] == [uncertain_request.id]
    missing = client.get(f"/api/v1/attention?project_id=prj-{'0' * 20}")
    assert missing.status_code == 404 and missing.json()["code"] == "project_not_found"


def test_an_answered_reconciliation_leaves_the_inbox(client, backend, uncertain):
    project, request, execution = uncertain
    assert [i["kind"] for i in client.get("/api/v1/attention").json()["items"]] == ["reconciliation_required"]
    client.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert client.get("/api/v1/attention").json()["items"] == []


def test_inbox_cursors_are_bound_to_their_filter(client, backend):
    first, second = add_project(backend.seed), add_project(backend.seed, name="Second")
    add_run(backend.seed, first)
    add_run(backend.seed, first, run_id="run-2", number=2)
    add_approval(backend.seed, first, at=T0)
    add_approval(backend.seed, first, run_id="run-2", at=T1)
    cursor = client.get(f"/api/v1/attention?project_id={first.id}&limit=1").json()["next_cursor"]
    assert cursor is not None
    reused = client.get(f"/api/v1/attention?project_id={second.id}&limit=1&cursor={cursor}")
    assert reused.status_code == 400 and reused.json()["code"] == "invalid_cursor"
    other_operation = client.get(f"/api/v1/approvals?status=pending&project_id={first.id}&limit=1&cursor={cursor}")
    assert other_operation.status_code == 400


def test_the_inbox_requires_approvals_read(backend):
    with backend.as_("deciding-only", {Permission.APPROVALS_DECIDE}) as client:
        denied = client.get("/api/v1/attention")
    assert denied.status_code == 403 and denied.json()["required_permission"] == "approvals:read"


def test_a_retry_authorized_by_a_worker_shows_its_window(client, backend, uncertain):
    """After a worker applies did_not_happen_retry, the view shows the new 15-minute authorization."""
    project, request, execution = uncertain
    from agent_factory.domain.executions import ReconciliationChoice
    from agent_factory.domain.identity import Actor
    from agent_factory.domain.serialization import parse_timestamp

    ledger = ExecutionLedger(backend.seed.executions, kind=LedgerKind.PRODUCT_STORE, clock=lambda: parse_timestamp("2026-10-02T11:55:00.000000Z"))
    retried, _, _ = ledger.reconcile(
        project.id, "run-1", execution.id, attempt=1, action_fingerprint=execution.action_fingerprint,
        choice=ReconciliationChoice.AUTHORIZE_RETRY, actor=Actor.unauthenticated("operator"),
    )  # fmt: skip
    assert retried.state is ExecutionState.AUTHORIZED and retried.authorization_seq == 2
    view = client.get(approval_url(project, request)).json()
    assert view["execution"]["retry_authorized_until"] == "2026-10-02T12:10:00.000000Z"  # 15 minutes
    assert view["execution"]["reconciliation"]["applied"] is True
    kinds = [s["kind"] for s in view["timeline"]]
    assert kinds[-2:] == ["reconciliation_decided", "retry_authorized"]
    assert view["timeline"][-1]["until"] == "2026-10-02T12:10:00.000000Z"
    # The original approval is neither reactivated nor edited: the retry is its own authorization.
    assert (view["status"], view["decision"]["outcome"], view["attention"]) == ("approved", "approved", None)

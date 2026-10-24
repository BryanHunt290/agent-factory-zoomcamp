"""The HTTP API, end to end over ASGI, in both stores.

Every scenario here runs twice: against the in-memory store (the default
suite) and against real PostgreSQL through the production connection pool
(``-m postgres --run-postgres``). Requests go through FastAPI's TestClient,
so routing, validation, dependency injection, authentication, error handling,
and the request-context middleware all run as they do under uvicorn. Every
response is validated against the checked-in openapi.yaml (``ContractClient``).
Requests carry the test owner's signed token (auth_support.py); authentication
and authorization themselves are tested in test_auth_http.py.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import pytest

pytest.importorskip("fastapi", reason="API tests need the API packages: run with .venv (requirements-api.txt)")
pytest.importorskip("jsonschema", reason="API tests validate against openapi.yaml: run with .venv (requirements-dev.txt)")

from api_support import (  # noqa: E402 - after the importorskip guards
    BODY, KEY, POSIX_PATH, SECRET, T0, T1, T2, WINDOWS_PATH, ContractClient,
    add_approval, add_milestone, add_project, add_run, application, decision_body, decision_url,
)

from auth_support import ensure_owner  # noqa: E402

from agent_factory.api.stores import InMemoryStoreProvider  # noqa: E402
from agent_factory.domain.approvals import ApprovalStatus  # noqa: E402
from agent_factory.domain.milestones import MilestoneCategory, MilestoneStatus  # noqa: E402
from agent_factory.domain.repositories import MilestoneQuery  # noqa: E402
from agent_factory.state import RunStatus  # noqa: E402

DSN_CANARY = "postgresql://operator:dsn-password-canary@127.0.0.1:5432/agent_factory_dev"
CANARIES = (SECRET, "operator-canary", "dsn-password-canary", "C:\\private", "/home/private")


@dataclass
class Backend:
    """A store provider factory (each call is a fresh API process's provider) and a store for seeding."""

    new_provider: Callable[[], Any]
    seed: Any
    kind: str

    def client(self, provider=None):
        return ContractClient(application(provider or self.new_provider()))


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request):
    if request.param == "memory":
        provider = InMemoryStoreProvider()
        yield Backend(lambda: provider, provider.store, "memory")
        return
    from agent_factory.persistence.pool import PostgresStorePool

    config = request.getfixturevalue("pg_schema")
    seed = request.getfixturevalue("pg_store")  # autocommit: seeded rows are visible to the pool
    ensure_owner(seed)  # the principal the clients' tokens name
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


def create(client, body=BODY, key=KEY):
    return client.post("/api/v1/projects", json=body, headers={"Idempotency-Key": key})


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


# ------------------------------------------------------------------ projects


def test_project_creation_returns_201_with_a_server_assigned_identity(client):
    response = create(client)
    assert response.status_code == 201
    project = response.json()
    assert project["id"].startswith("prj-")
    assert project["name"] == BODY["name"] and project["template"] == "calculator"
    assert project["status"] == "not_started" and project["latest_run"] is None
    assert response.headers["Location"] == f"/api/v1/projects/{project['id']}"
    assert "Idempotent-Replayed" not in response.headers
    assert client.get(f"/api/v1/projects/{project['id']}").json() == project


def test_identical_retry_replays_the_original_project_without_creating_another(client, backend):
    first = create(client)
    second = create(client)
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert [p.id for p in backend.seed.projects.list()] == [first.json()["id"]]


def test_same_key_with_a_different_body_is_a_409_conflict(client, backend):
    """Key reuse with a different body conflicts with the state bound to the key (owner decision; docs/backend.md)."""
    first = create(client)
    response = create(client, body={**BODY, "name": "Another name"})
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_key_reused"
    assert "Location" not in response.headers and "Idempotent-Replayed" not in response.headers
    # The conflict changed nothing: the original request still replays its original result.
    replay = create(client)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert [p.id for p in backend.seed.projects.list()] == [first.json()["id"]]


def test_concurrent_identical_creations_create_exactly_one_project(client, backend):
    barrier = threading.Barrier(8)

    def attempt(_):
        barrier.wait()
        return create(client)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(attempt, range(8)))
    created = [r for r in responses if r.status_code == 201]
    # A loser that finds the winner still uncommitted may answer 409 idempotency_key_in_use (retryable).
    assert all(r.status_code == 201 or r.json()["code"] == "idempotency_key_in_use" for r in responses)
    assert created
    assert len({r.json()["id"] for r in created}) == 1
    assert len(backend.seed.projects.list()) == 1
    assert sum("Idempotent-Replayed" not in r.headers for r in created) == 1


def test_idempotency_survives_an_api_restart_and_reconnect(backend):
    with backend.client() as first_process:
        original = create(first_process).json()
    # A new provider is a new process's connection pool: nothing is shared but the database.
    with backend.client() as second_process:
        replay = create(second_process)
        reused = create(second_process, body={**BODY, "description": "changed"})
    assert replay.status_code == 201 and replay.json() == original
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert reused.status_code == 409 and reused.json()["code"] == "idempotency_key_reused"
    assert len(backend.seed.projects.list()) == 1


def test_project_creation_requires_a_well_formed_idempotency_key(client, backend):
    missing = client.post("/api/v1/projects", json=BODY)
    malformed = create(client, key="short")
    assert missing.status_code == 400 and missing.json()["code"] == "idempotency_key_required"
    assert malformed.status_code == 422 and malformed.json()["errors"][0]["pointer"] == "Idempotency-Key"
    assert backend.seed.projects.list() == []


@pytest.mark.parametrize("extra", ["id", "created_at", "status", "workspace_path", "decided_by"])
def test_unknown_or_server_controlled_project_fields_are_rejected(client, backend, extra):
    response = create(client, body={**BODY, extra: "x"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert backend.seed.projects.list() == []


def test_unknown_template_is_a_documented_error(client):
    response = create(client, body={**BODY, "template": "no-such-template"})
    assert response.status_code == 422 and response.json()["code"] == "unknown_template"


@pytest.mark.parametrize("name", ["..\\..\\Windows\\System32", "../../etc/passwd", "C:\\temp\\x", "CON", "a/b"])
def test_project_names_are_labels_and_never_become_workspace_paths(client, backend, tmp_path, name):
    from agent_factory.factory import workspace_for_project

    created = create(client, body={**BODY, "name": name})
    assert created.status_code == 201
    project_id = created.json()["id"]
    base = tmp_path / "workspaces"
    base.mkdir()
    _, workspace = workspace_for_project(backend.seed, project_id, base)
    # The API-assigned ID is the workspace's identity and its only path component.
    assert workspace.project_id == project_id
    assert workspace.root == (base / project_id).resolve()
    assert [p.name for p in base.iterdir()] == [project_id]
    assert not (tmp_path / "etc").exists() and not (tmp_path / "Windows").exists()


def test_project_list_paginates_newest_first_without_gaps_or_duplicates(client, backend):
    ids = [add_project(backend.seed, name=f"P{i}", at=f"2026-10-02T0{i}:00:00.000000Z").id for i in range(5)]
    items, pages = all_pages(client, "/api/v1/projects", limit=2)
    assert [p["id"] for p in items] == list(reversed(ids))
    assert pages == 3
    assert "total" not in client.get("/api/v1/projects").json()


def test_project_summaries_report_latest_run_pending_approvals_and_activity(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    add_approval(backend.seed, project)
    add_milestone(backend.seed, project, 1, at=T0)
    add_milestone(backend.seed, project, 2, at=T2)
    summary = client.get(f"/api/v1/projects?activity_since={T1}").json()["items"][0]
    assert summary["status"] == "awaiting_approval"
    assert summary["latest_run"]["run_id"] == "run-1"
    assert summary["pending_approvals"] == 1
    assert summary["activity"] == {"since": T1, "milestones": 1}


def test_templates_are_listed(client):
    names = [t["name"] for t in client.get("/api/v1/templates").json()["items"]]
    assert "calculator" in names


def test_unknown_project_is_a_404_problem(client):
    response = client.get("/api/v1/projects/prj-" + "0" * 20)
    assert response.status_code == 404 and response.json()["code"] == "project_not_found"
    malformed = client.get("/api/v1/projects/not-an-id")
    assert malformed.status_code == 404 and malformed.json()["code"] == "project_not_found"


# ---------------------------------------------------------------------- runs


def test_runs_are_listed_newest_first_and_scoped_to_their_project(client, backend):
    project, other = add_project(backend.seed), add_project(backend.seed, name="Other")
    for n in range(1, 4):
        add_run(backend.seed, project, run_id=f"run-{n}", number=n, status=RunStatus.COMPLETED)
    add_run(backend.seed, other, run_id="run-9", number=1)
    items, pages = all_pages(client, f"/api/v1/projects/{project.id}/runs", limit=2)
    assert [r["run_id"] for r in items] == ["run-3", "run-2", "run-1"] and pages == 2
    assert client.get(f"/api/v1/projects/{project.id}/runs/run-9").json()["code"] == "run_not_found"


def test_a_run_awaiting_reconciliation_is_reported_with_a_curated_reason(client, backend):
    project = add_project(backend.seed)
    raw = f"waiting for a person to say whether action axe-1 happened {WINDOWS_PATH} {SECRET}"
    add_run(backend.seed, project, status=RunStatus.AWAITING_RECONCILIATION, status_reason=raw)
    run = client.get(f"/api/v1/projects/{project.id}/runs/run-1").json()
    assert run["status"] == "awaiting_reconciliation" and run["pending_approval_id"] is None
    assert run["status_reason"].startswith("Paused: an approved action may already have happened")
    assert client.get(f"/api/v1/projects/{project.id}").json()["status"] == "awaiting_reconciliation"
    for canary in CANARIES:
        assert canary not in json.dumps(run)


def test_action_milestones_are_curated_and_never_show_parameters(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project, status=RunStatus.AWAITING_RECONCILIATION)
    add_milestone(
        backend.seed, project, 1, type="action_execution", category=MilestoneCategory.APPROVAL,
        status=MilestoneStatus.BLOCKED, summary=f"send_notice to {SECRET} {POSIX_PATH}",
        metadata={"tool": "send_notice", "execution_id": "axe-" + "1" * 20, "state": "uncertain"},
    )  # fmt: skip
    [milestone] = client.get(f"/api/v1/projects/{project.id}/milestones").json()["items"]
    assert milestone["type"] == "action_execution" and milestone["status"] == "blocked"
    assert milestone["summary"].startswith("Action outcome unknown: send_notice")
    for canary in CANARIES:
        assert canary not in json.dumps(milestone)


def test_a_paused_run_names_its_pending_approval(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    request = add_approval(backend.seed, project)
    run = client.get(f"/api/v1/projects/{project.id}/runs/run-1").json()
    assert run["status"] == "awaiting_approval"
    assert run["pending_approval_id"] == request.id
    assert run["status_reason"] == "Waiting for a decision on a protected action."


# ---------------------------------------------------------------- milestones


def test_milestones_paginate_chronologically(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    for sequence in range(1, 6):
        add_milestone(backend.seed, project, sequence, at=f"2026-10-02T0{sequence}:00:00.000000Z")
    items, pages = all_pages(client, f"/api/v1/projects/{project.id}/milestones", limit=2)
    assert [m["sequence"] for m in items] == [1, 2, 3, 4, 5] and pages == 3
    run_items, _ = all_pages(client, f"/api/v1/projects/{project.id}/runs/run-1/milestones", limit=4)
    assert run_items == items


def test_milestone_time_range_is_half_open(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    for sequence, at in enumerate((T0, T1, T2), start=1):
        add_milestone(backend.seed, project, sequence, at=at)
    response = client.get(f"/api/v1/projects/{project.id}/milestones?since={T1}&until={T2}")
    assert [m["occurred_at"] for m in response.json()["items"]] == [T1]
    after = client.get(f"/api/v1/projects/{project.id}/milestones?since={T1}")
    assert [m["occurred_at"] for m in after.json()["items"]] == [T1, T2]
    inverted = client.get(f"/api/v1/projects/{project.id}/milestones?since={T2}&until={T1}")
    assert inverted.status_code == 422 and inverted.json()["errors"][0]["pointer"] == "until"


def test_milestone_status_filter(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    add_milestone(backend.seed, project, 1)
    add_milestone(backend.seed, project, 2, status=MilestoneStatus.FAILED)
    response = client.get(f"/api/v1/projects/{project.id}/milestones?status=failed")
    assert [m["sequence"] for m in response.json()["items"]] == [2]


def test_tampered_cursors_are_rejected(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    for sequence in range(1, 4):
        add_milestone(backend.seed, project, sequence, at=f"2026-10-02T0{sequence}:00:00.000000Z")
    url = f"/api/v1/projects/{project.id}/milestones?limit=1"
    cursor = client.get(url).json()["next_cursor"]
    middle = len(cursor) // 2
    flipped = cursor[:middle] + ("A" if cursor[middle] != "A" else "B") + cursor[middle + 1 :]
    for bad in (flipped, cursor[:-4], cursor + "AAAA", "not a cursor", "0" * 40):
        response = client.get(url, params={"cursor": bad})
        assert response.status_code == 400, bad
        assert response.json()["code"] == "invalid_cursor"
    assert client.get(url, params={"cursor": cursor}).status_code == 200


def test_cursors_are_bound_to_their_filters_and_operation(client, backend):
    project, other = add_project(backend.seed), add_project(backend.seed, name="Other")
    add_run(backend.seed, project)
    for sequence in range(1, 4):
        add_milestone(backend.seed, project, sequence, at=f"2026-10-02T0{sequence}:00:00.000000Z")
    base = f"/api/v1/projects/{project.id}/milestones"
    cursor = client.get(base + "?limit=1&status=completed").json()["next_cursor"]
    reuses = [
        base + "?limit=1&status=failed",  # changed filter
        base + "?limit=1",  # dropped filter
        base + f"?limit=1&status=completed&since={T0}",  # added filter
        f"/api/v1/projects/{other.id}/milestones?limit=1&status=completed",  # another project
        f"/api/v1/projects/{project.id}/runs/run-1/milestones?limit=1&status=completed",  # another operation
        "/api/v1/projects?limit=1",
    ]
    for url in reuses:
        response = client.get(url + f"&cursor={cursor}")
        assert response.status_code == 400, url
        assert response.json()["code"] == "invalid_cursor"
    # The page size alone may change between pages.
    assert client.get(base + f"?limit=2&status=completed&cursor={cursor}").status_code == 200


@pytest.mark.parametrize("limit", ["0", "101", "-1", "ten"])
def test_limits_outside_1_to_100_are_validation_problems(client, limit):
    response = client.get(f"/api/v1/projects?limit={limit}")
    assert response.status_code == 422
    assert response.json()["errors"][0] == {"location": "query", "pointer": "limit", "message": response.json()["errors"][0]["message"]}


# ----------------------------------------------------------------- approvals


def test_pending_approvals_span_projects_oldest_first(client, backend):
    first, second = add_project(backend.seed, name="First"), add_project(backend.seed, name="Second")
    add_run(backend.seed, first)
    add_run(backend.seed, second)
    a = add_approval(backend.seed, first, at=T1)
    b = add_approval(backend.seed, second, at=T0)
    decided = add_approval(backend.seed, first, path="other.py", at=T0)
    client.post(decision_url(first, decided), json=decision_body(decided))
    items, pages = all_pages(client, "/api/v1/approvals?status=pending", limit=1)
    assert [(i["id"], i["project_name"]) for i in items] == [(b.id, "Second"), (a.id, "First")]
    assert pages == 2


def test_pending_approvals_can_be_scoped_to_one_project(client, backend):
    first, second = add_project(backend.seed, name="First"), add_project(backend.seed, name="Second")
    add_run(backend.seed, first)
    add_run(backend.seed, second)
    a = add_approval(backend.seed, first)
    add_approval(backend.seed, second)
    response = client.get(f"/api/v1/approvals?status=pending&project_id={first.id}")
    assert [i["id"] for i in response.json()["items"]] == [a.id]
    missing = client.get("/api/v1/approvals?status=pending&project_id=prj-" + "0" * 20)
    assert missing.status_code == 404 and missing.json()["code"] == "project_not_found"
    status_required = client.get("/api/v1/approvals")
    assert status_required.status_code == 422


def test_an_approval_is_only_visible_under_its_own_project(client, backend):
    owner, other = add_project(backend.seed), add_project(backend.seed, name="Other")
    add_run(backend.seed, owner)
    request = add_approval(backend.seed, owner)
    found = client.get(f"/api/v1/projects/{owner.id}/approvals/{request.id}")
    assert found.status_code == 200
    assert found.json()["action_fingerprint"] == request.action_fingerprint
    assert found.json()["decision"] is None
    elsewhere = client.get(f"/api/v1/projects/{other.id}/approvals/{request.id}")
    assert elsewhere.status_code == 404 and elsewhere.json()["code"] == "approval_not_found"
    decided_elsewhere = client.post(decision_url(other, request), json=decision_body(request))
    assert decided_elsewhere.status_code == 404
    assert backend.seed.approvals.get_decision(owner.id, request.id) is None


@pytest.fixture
def pending(backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    return project, add_approval(backend.seed, project)


def test_first_decision_is_201_and_records_server_time_and_the_authenticated_principal(client, backend, pending):
    project, request = pending
    response = client.post(decision_url(project, request), json=decision_body(request, comment="Looks right"))
    assert response.status_code == 201
    assert response.headers["Location"] == f"/api/v1/projects/{project.id}/approvals/{request.id}"
    body = response.json()
    assert body["status"] == "approved"
    decision = body["decision"]
    assert decision["outcome"] == "approved" and decision["comment"] == "Looks right"
    assert decision["action_fingerprint"] == request.action_fingerprint
    assert decision["decided_at"] == T2  # the server's clock
    owner = ensure_owner(backend.seed)
    assert decision["decided_by"] == {"kind": "user", "id": owner.id, "display_name": "Test Owner", "verified": True}
    stored = backend.seed.approvals.get_decision(project.id, request.id)
    assert stored.actor == owner.actor() and stored.decided_at == T2


def test_identical_decision_retry_is_200_with_the_stored_decision(client, pending):
    project, request = pending
    first = client.post(decision_url(project, request), json=decision_body(request))
    second = client.post(decision_url(project, request), json=decision_body(request))
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert "Location" not in second.headers


def test_conflicting_decision_is_409_and_keeps_the_first(client, backend, pending):
    project, request = pending
    client.post(decision_url(project, request), json=decision_body(request, "approved"))
    response = client.post(decision_url(project, request), json=decision_body(request, "rejected"))
    assert response.status_code == 409
    assert response.json()["code"] == "approval_already_decided"
    assert response.json()["approval"]["decision"]["outcome"] == "approved"
    assert backend.seed.approvals.get_decision(project.id, request.id).outcome.value == "approved"


def test_fingerprint_mismatch_is_409_and_records_nothing(client, backend, pending):
    project, request = pending
    body = {**decision_body(request), "action_fingerprint": "sha256:" + "0" * 64}
    response = client.post(decision_url(project, request), json=body)
    assert response.status_code == 409
    assert response.json()["code"] == "action_fingerprint_mismatch"
    assert backend.seed.approvals.get_decision(project.id, request.id) is None
    assert backend.seed.approvals.get_request(project.id, request.id).status is ApprovalStatus.PENDING


def test_a_decision_without_a_fingerprint_is_refused(client, backend, pending):
    project, request = pending
    response = client.post(decision_url(project, request), json={"outcome": "approved"})
    assert response.status_code == 422
    assert backend.seed.approvals.get_decision(project.id, request.id) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"decided_by": {"kind": "user", "id": "usr-admin", "display_name": "Admin", "verified": True}},
        {"actor": "usr-admin"},
        {"verified": True},
        {"decided_at": "2020-01-01T00:00:00.000000Z"},
        {"status": "approved"},
        {"id": "dec-forged"},
    ],
    ids=lambda extra: next(iter(extra)),
)
def test_clients_cannot_set_identity_time_or_status(client, backend, pending, extra):
    project, request = pending
    response = client.post(decision_url(project, request), json={**decision_body(request), **extra})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"][0]["pointer"] == "/" + next(iter(extra))
    assert backend.seed.approvals.get_decision(project.id, request.id) is None


def test_expired_approval_is_409(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    request = add_approval(backend.seed, project, at=T0, expires_at=T1)
    response = client.post(decision_url(project, request), json=decision_body(request))
    assert response.status_code == 409 and response.json()["code"] == "approval_expired"
    assert backend.seed.approvals.get_decision(project.id, request.id) is None


def test_cancelled_approval_is_409(client, backend):
    project = add_project(backend.seed)
    add_run(backend.seed, project)
    request = add_approval(backend.seed, project)
    backend.seed.approvals.update_request(request.transition(ApprovalStatus.CANCELLED, at=T1))
    response = client.post(decision_url(project, request), json=decision_body(request))
    assert response.status_code == 409 and response.json()["code"] == "approval_cancelled"


def test_concurrent_conflicting_decisions_settle_on_exactly_one(client, backend, pending):
    project, request = pending
    barrier = threading.Barrier(6)

    def attempt(i):
        barrier.wait()
        return client.post(decision_url(project, request), json=decision_body(request, ("approved", "rejected")[i % 2]))

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(attempt, range(6)))
    assert sum(r.status_code == 201 for r in responses) == 1
    winner = backend.seed.approvals.get_decision(project.id, request.id).outcome.value
    for r in responses:
        if r.status_code == 200:
            assert r.json()["decision"]["outcome"] == winner
        elif r.status_code == 409:
            assert r.json()["code"] == "approval_already_decided"
        elif r.status_code == 503:
            assert r.json()["code"] == "storage_unavailable"  # a lock wait gave up; nothing recorded by it
        else:
            assert r.status_code == 201


def test_recording_a_decision_changes_no_run_and_executes_nothing(client, backend, pending):
    project, request = pending
    everything = MilestoneQuery(project_id=project.id)
    before = backend.seed.runs.get(project.id, "run-1")
    milestones_before = backend.seed.milestones.query(everything)
    assert client.post(decision_url(project, request), json=decision_body(request)).status_code == 201
    # The run stays paused until a worker re-checks the fingerprint against its checkpoint.
    assert backend.seed.runs.get(project.id, "run-1") == before
    assert backend.seed.milestones.query(everything) == milestones_before


# ------------------------------------------------------- disclosure canaries


def test_responses_never_contain_secret_or_host_path_canaries(client, backend):
    project = add_project(backend.seed)
    add_run(
        backend.seed, project, status=RunStatus.FAILED,
        status_reason=f"ToolError: {WINDOWS_PATH} {POSIX_PATH} {SECRET} {DSN_CANARY}",
    )
    add_run(backend.seed, project, run_id="run-2", number=2, status=RunStatus.AWAITING_APPROVAL)
    add_milestone(
        backend.seed, project, 1, summary=f"wrote {WINDOWS_PATH} and {POSIX_PATH}; {SECRET}; {DSN_CANARY}",
        title=f"Plan {SECRET}",
    )
    request = add_approval(backend.seed, project, run_id="run-2", reason=f"delete {POSIX_PATH} using {SECRET}")
    urls = [
        "/api/v1/projects",
        f"/api/v1/projects/{project.id}",
        f"/api/v1/projects/{project.id}/runs",
        f"/api/v1/projects/{project.id}/runs/run-1",
        f"/api/v1/projects/{project.id}/milestones",
        f"/api/v1/projects/{project.id}/runs/run-1/milestones",
        "/api/v1/approvals?status=pending",
        f"/api/v1/projects/{project.id}/approvals/{request.id}",
    ]
    responses = [client.get(url) for url in urls]
    responses.append(client.post(decision_url(project, request), json=decision_body(request)))
    for response in responses:
        assert response.status_code in (200, 201), response.text
        for canary in CANARIES:
            assert canary not in response.text, (response.request.url, canary)
    run = client.get(f"/api/v1/projects/{project.id}/runs/run-1").json()
    assert run["status_reason"] == "Stopped: a tool reported an error."


# ---------------------------------------------------- errors and request IDs


def test_every_response_has_a_fresh_request_id_and_errors_quote_it(client):
    responses = [client.get("/api/v1/projects"), client.get("/api/v1/projects/not-an-id"), client.get("/readyz")]
    ids = [r.headers["X-Request-Id"] for r in responses]
    assert len(set(ids)) == 3
    assert responses[1].json()["request_id"] == ids[1]


def test_readiness_is_ready_when_the_store_is(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}

"""Authentication and authorization over HTTP, in both stores.

Who is calling (401), what they may do (403), how fast (429), and what an
approval decision records about who made it. Every token is really signed
(auth_support.py) and every response is validated against openapi.yaml.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

import pytest

pytest.importorskip("fastapi", reason="API tests need the API packages: run with .venv (requirements-api.txt)")
pytest.importorskip("jsonschema", reason="API tests validate against openapi.yaml: run with .venv (requirements-dev.txt)")

import yaml  # noqa: E402
from api_support import (  # noqa: E402 - after the importorskip guards
    BODY, KEY, SPEC, ContractClient, add_approval, add_milestone, add_project, add_run, add_uncertain_execution,
    api_routes, application, decision_body, decision_url, reconcile_body, reconcile_url,
)
from auth_support import ISSUER, OWNER_SUBJECT, TEST_ISSUER, TestIssuer, bearer, enroll, ensure_owner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_factory.api.auth import PermissionCheck  # noqa: E402
from agent_factory.api.observability import Redactor  # noqa: E402
from agent_factory.api.stores import InMemoryStoreProvider  # noqa: E402
from agent_factory.domain.principals import ALL_PERMISSIONS, Permission, PrincipalStatus  # noqa: E402
from agent_factory.state import RunStatus  # noqa: E402

REFUSED_DETAIL = "The access token was not accepted. Sign in again to get a new one."


@dataclass
class Api:
    new_provider: Callable[[], Any]
    seed: Any
    kind: str

    def app(self, **auth_limits):
        return application(self.new_provider(), authenticator=ISSUER.authenticator(**auth_limits) if auth_limits else None)

    def client(self, token=None, app=None):
        """``token=None``: no Authorization header at all."""
        return ContractClient(app or self.app(), token=token)

    def principal(self, subject, permissions, **kwargs):
        return enroll(self.seed, subject, permissions, **kwargs)

    def as_(self, subject, permissions, app=None, **token_claims):
        self.principal(subject, permissions)
        return self.client(ISSUER.token(subject, **token_claims), app=app)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def api(request):
    if request.param == "memory":
        provider = InMemoryStoreProvider()
        yield Api(lambda: provider, provider.store, "memory")
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

    yield Api(new_pool, seed, "postgres")
    for pool in pools:
        pool.close()


@pytest.fixture
def world(api):
    """A project with a paused run, a milestone, and a pending approval."""
    project = add_project(api.seed)
    add_run(api.seed, project)
    add_milestone(api.seed, project, 1)
    return project, add_approval(api.seed, project)


# ------------------------------------------------------------------ 401


def test_no_credential_is_401_authentication_required(api):
    with api.client() as anonymous:
        response = anonymous.get("/api/v1/projects")
    assert response.status_code == 401 and response.json()["code"] == "authentication_required"
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="agent-factory"'


def _refused_tokens(api):
    other = TestIssuer(issuer="https://other-issuer.example.test")
    api.principal("disabled-user", ALL_PERMISSIONS, status=PrincipalStatus.DISABLED)
    import time

    now = int(time.time())
    return {
        "malformed": "not-a-jwt",
        "signed by another key": ISSUER.token(key=TestIssuer().private["ec-1"][0]),
        "expired": ISSUER.token(iat=now - 7200, exp=now - 3600),
        "wrong issuer": ISSUER.token(iss="https://evil.example.test"),
        "wrong audience": ISSUER.token(aud="someone-else"),
        "another issuer's keys": other.token(),
        "not enrolled": ISSUER.token("nobody-enrolled-me"),
        "disabled": ISSUER.token("disabled-user"),
    }


def test_every_refused_credential_is_the_same_401_invalid_token(api):
    for reason, token in _refused_tokens(api).items():
        with api.client(token) as client:
            response = client.get("/api/v1/projects")
        assert response.status_code == 401, reason
        body = response.json()
        assert (body["code"], body["detail"]) == ("invalid_token", REFUSED_DETAIL), reason  # nothing about the account
        assert response.headers["WWW-Authenticate"] == 'Bearer realm="agent-factory", error="invalid_token"', reason


@pytest.mark.parametrize("header", ["Bearer", "Basic b3duZXI6cGFzcw==", "Bearer two tokens", "bearer", ""])
def test_a_malformed_authorization_header_is_401(api, header):
    with TestClient(api.app(), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/projects", headers={"Authorization": header})
    assert response.status_code == 401


def test_a_scheme_is_case_insensitive_but_two_headers_are_refused(api):
    token = ISSUER.token()
    with TestClient(api.app(), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/projects", headers={"Authorization": f"bEaReR {token}"}).status_code == 200
        twice = client.get("/api/v1/projects", headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")])
    assert twice.status_code == 401


def test_a_valid_credential_authenticates_its_enrolled_principal(api, caplog):
    owner = ensure_owner(api.seed)
    caplog.set_level(logging.INFO, logger="agent_factory.api")
    with api.client(ISSUER.token()) as client:
        assert client.get("/api/v1/projects").status_code == 200
    access = [r for r in caplog.records if r.name == "agent_factory.api.access"]
    assert (access[-1].principal_id, access[-1].permission, access[-1].auth_result) == (
        owner.id, "projects:read", "allowed",
    )  # fmt: skip


def test_a_disabled_principal_is_refused_until_enabled_again(api):
    principal = api.principal("temp-user", {Permission.PROJECTS_READ})
    token = ISSUER.token("temp-user")
    api.seed.principals.update(principal.with_status(PrincipalStatus.DISABLED, "2026-10-02T00:00:00.000000Z"))
    with api.client(token) as client:
        assert client.get("/api/v1/projects").status_code == 401  # whatever it was granted
    enabled = api.seed.principals.get(principal.id).with_status(PrincipalStatus.ACTIVE, "2026-10-02T00:01:00.000000Z")
    api.seed.principals.update(enabled)
    with api.client(token) as client:
        assert client.get("/api/v1/projects").status_code == 200


def test_identity_is_issuer_and_subject_never_email_or_name(api, world):
    project, request = world
    principal = api.principal("stable-sub", ALL_PERMISSIONS, display_name="Old Name", email="old@example.test")
    with api.client(ISSUER.token("stable-sub", name="New Name", email="new@example.test")) as client:
        decided = client.post(decision_url(project, request), json=decision_body(request))
    assert decided.status_code == 201 and decided.json()["decision"]["decided_by"]["id"] == principal.id
    refreshed = api.seed.principals.get(principal.id)
    assert (refreshed.display_name, refreshed.email) == ("New Name", "new@example.test")  # display only
    assert refreshed.external_identity == (TEST_ISSUER, "stable-sub")
    impostor = ISSUER.token("another-sub", email="new@example.test", name="New Name")  # same email, other subject
    with api.client(impostor) as client:
        assert client.get("/api/v1/projects").status_code == 401


# ------------------------------------------------------------------ 403


def _operations(project, request, uncertain=None, execution=None):
    return [
        ("listProjects", "GET", "/api/v1/projects", None),
        ("createProject", "POST", "/api/v1/projects", BODY),
        ("getProject", "GET", f"/api/v1/projects/{project.id}", None),
        ("listTemplates", "GET", "/api/v1/templates", None),
        ("listRuns", "GET", f"/api/v1/projects/{project.id}/runs", None),
        ("getRun", "GET", f"/api/v1/projects/{project.id}/runs/run-1", None),
        ("listProjectMilestones", "GET", f"/api/v1/projects/{project.id}/milestones", None),
        ("listRunMilestones", "GET", f"/api/v1/projects/{project.id}/runs/run-1/milestones", None),
        ("listPendingApprovals", "GET", "/api/v1/approvals?status=pending", None),
        ("getApproval", "GET", f"/api/v1/projects/{project.id}/approvals/{request.id}", None),
        ("decideApproval", "POST", decision_url(project, request), decision_body(request)),
        ("listAttention", "GET", "/api/v1/attention", None),
        ("reconcileAction", "POST", reconcile_url(project, uncertain), reconcile_body(execution)),
    ]


CONTRACT_PERMISSIONS = {
    op["operationId"]: op["x-permission"]
    for item in SPEC["paths"].values() for op in item.values() if isinstance(op, dict) and "operationId" in op
}  # fmt: skip


def _call(client, method, path, body):
    headers = {"Idempotency-Key": KEY} if method == "POST" and path.endswith("/projects") else {}
    return client.request(method, path, json=body, headers=headers)


def test_every_operation_requires_exactly_its_documented_permission(api, world):
    """With every permission except the required one: 403. With only the required one: allowed."""
    project, request = world
    add_run(api.seed, project, run_id="run-2", number=2, status=RunStatus.AWAITING_RECONCILIATION)
    uncertain = add_approval(api.seed, project, run_id="run-2")
    operations = _operations(project, request, uncertain, add_uncertain_execution(api.seed, project, uncertain))
    assert {name for name, *_ in operations} == set(CONTRACT_PERMISSIONS)
    for name, method, path, body in operations:
        required = Permission(CONTRACT_PERMISSIONS[name])
        with api.as_(f"lacks-{name}", ALL_PERMISSIONS - {required}) as client:
            denied = _call(client, method, path, body)
        assert denied.status_code == 403, name
        assert (denied.json()["code"], denied.json()["required_permission"]) == ("forbidden", required.value), name
        with api.as_(f"only-{name}", {required}) as client:
            allowed = _call(client, method, path, body)
        assert allowed.status_code < 400, (name, allowed.text)


def test_each_route_declares_one_permission_check_and_it_matches_the_contract(api):
    app = api.app()

    def checks(dependant):
        found = [d.call for d in dependant.dependencies if isinstance(d.call, PermissionCheck)]
        return found + [c for d in dependant.dependencies for c in checks(d)]

    product_routes = [r for r in api_routes(app) if r.path.startswith("/api/v1")]
    assert {r.operation_id for r in product_routes} == set(CONTRACT_PERMISSIONS)
    for route in product_routes:
        found = {check.permission.value for check in checks(route.dependant)}
        assert found == {CONTRACT_PERMISSIONS[route.operation_id]}, route.operation_id


def test_approvals_read_does_not_allow_deciding(api, world):
    project, request = world
    with api.as_("reader", {Permission.APPROVALS_READ}) as reader:
        assert reader.get(f"/api/v1/projects/{project.id}/approvals/{request.id}").status_code == 200
        denied = reader.post(decision_url(project, request), json=decision_body(request))
    assert denied.status_code == 403 and denied.json()["required_permission"] == "approvals:decide"
    assert api.seed.approvals.get_decision(project.id, request.id) is None


def test_deciding_does_not_allow_reconciling(api):
    """approvals:decide never implies actions:reconcile, on the real route, whatever else is held."""
    project = add_project(api.seed)
    add_run(api.seed, project, status=RunStatus.AWAITING_RECONCILIATION)
    request = add_approval(api.seed, project)
    execution = add_uncertain_execution(api.seed, project, request)
    with api.as_("decider", ALL_PERMISSIONS - {Permission.ACTIONS_RECONCILE}) as decider:
        denied = decider.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert denied.status_code == 403 and denied.json()["required_permission"] == "actions:reconcile"
    assert api.seed.reconciliations.list_for_execution(project.id, execution.id) == []
    with api.as_("reconciler", {Permission.ACTIONS_RECONCILE}) as reconciler:
        allowed = reconciler.post(reconcile_url(project, request), json=reconcile_body(execution))
    assert allowed.status_code == 201


def test_creating_projects_does_not_allow_reading_them(api):
    with api.as_("creator", {Permission.PROJECTS_CREATE}) as creator:
        created = creator.post("/api/v1/projects", json=BODY, headers={"Idempotency-Key": KEY})
        assert created.status_code == 201
        assert creator.get("/api/v1/projects").status_code == 403
        assert creator.get(f"/api/v1/projects/{created.json()['id']}").status_code == 403


def test_authorization_ignores_permissions_claimed_by_the_client(api, world):
    """Only grants in Agent Factory's store count: not token claims, not request fields, not headers."""
    project, request = world
    api.principal("claims-a-lot", {Permission.APPROVALS_READ})
    token = ISSUER.token("claims-a-lot", permissions=["approvals:decide"], scope="approvals:decide admin", roles=["owner"])
    with api.client(token) as client:
        response = client.post(
            decision_url(project, request),
            json=decision_body(request),
            headers={"X-Permission": "approvals:decide", "X-Principal": ensure_owner(api.seed).id},
        )
    assert response.status_code == 403
    assert api.seed.approvals.get_decision(project.id, request.id) is None


def test_authorization_happens_before_anything_is_looked_up(api, world):
    project, request = world
    with api.client() as anonymous:
        assert anonymous.get("/api/v1/projects/prj-00000000000000000000").status_code == 401
    with api.as_("no-approvals", {Permission.PROJECTS_READ}) as client:
        missing = client.get("/api/v1/projects/prj-00000000000000000000/approvals/apr-0000000000000000")
        wrong = client.post(decision_url(project, request), json={"outcome": "approved", "action_fingerprint": "sha256:" + "0" * 64})
    assert missing.status_code == 403  # no existence check without the permission
    assert wrong.status_code == 403  # 403 before the fingerprint's 409


def test_the_health_endpoints_need_no_credential(api):
    with TestClient(api.app(), base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


# -------------------------------------------------------- approval identity


def test_a_decision_records_the_authenticated_principal(api, world):
    project, request = world
    principal = api.principal("phone-user", ALL_PERMISSIONS, display_name="Phone User")
    with api.client(ISSUER.token("phone-user")) as client:
        response = client.post(decision_url(project, request), json=decision_body(request))
    assert response.status_code == 201
    assert response.json()["decision"]["decided_by"] == {
        "kind": "user", "id": principal.id, "display_name": "Phone User", "verified": True,
    }  # fmt: skip
    stored = api.seed.approvals.get_decision(project.id, request.id)
    assert stored.actor == principal.actor()
    assert (stored.project_id, stored.request_id, stored.action_fingerprint) == (project.id, request.id, request.action_fingerprint)


@pytest.mark.parametrize(
    "extra",
    [
        {"actor": {"kind": "user", "id": "prn-00000000000000000000", "display_name": "Owner", "verified": True}},
        {"decided_by": {"kind": "user", "id": "prn-00000000000000000000", "display_name": "Owner", "verified": True}},
        {"verified": True},
        {"user_id": "prn-00000000000000000000"},
        {"decided_at": "2020-01-01T00:00:00.000000Z"},
    ],
)
def test_a_client_cannot_supply_who_decided_or_when(api, world, extra):
    project, request = world
    with api.as_("honest-client", ALL_PERMISSIONS) as client:
        response = client.post(decision_url(project, request), json={**decision_body(request), **extra})
    assert response.status_code == 422
    assert api.seed.approvals.get_decision(project.id, request.id) is None


def test_the_exact_fingerprint_is_still_required(api, world):
    project, request = world
    with api.as_("careful-owner", ALL_PERMISSIONS) as client:
        assert client.post(decision_url(project, request), json={"outcome": "approved"}).status_code == 422
        wrong = client.post(decision_url(project, request), json=decision_body(request, action_fingerprint="sha256:" + "0" * 64))
    assert wrong.status_code == 409 and wrong.json()["code"] == "action_fingerprint_mismatch"
    assert api.seed.approvals.get_decision(project.id, request.id) is None


def test_an_identical_retry_is_200_only_for_the_same_principal(api, world):
    project, request = world
    with api.as_("first-decider", ALL_PERMISSIONS) as first:
        assert first.post(decision_url(project, request), json=decision_body(request)).status_code == 201
        assert first.post(decision_url(project, request), json=decision_body(request)).status_code == 200
    with api.as_("second-decider", ALL_PERMISSIONS) as second:
        other = second.post(decision_url(project, request), json=decision_body(request))
    assert other.status_code == 409 and other.json()["code"] == "approval_already_decided"


@pytest.mark.postgres
def test_the_documented_audit_query_answers_who_approved_what_when_and_where(pg_schema, pg_store):
    """The SQL in docs/authentication.md, run as written after a real authenticated decision."""
    import re
    from pathlib import Path

    from agent_factory.persistence.pool import PostgresStorePool

    project = add_project(pg_store)
    add_run(pg_store, project)
    request = add_approval(pg_store, project)
    auditor = enroll(pg_store, "auditor-sub", ALL_PERMISSIONS, display_name="Auditor")
    pool = PostgresStorePool(pg_schema, min_size=1, max_size=2, timeout=5.0)
    try:
        with ContractClient(application(pool), token=ISSUER.token("auditor-sub")) as client:
            assert client.post(decision_url(project, request), json=decision_body(request)).status_code == 201
    finally:
        pool.close()
    doc = (Path(__file__).resolve().parent.parent / "docs" / "authentication.md").read_text(encoding="utf-8")
    query = re.search(r"```sql\s(.*?)```", doc, re.DOTALL).group(1)
    [row] = pg_store.unit_of_work.query(query)
    assert (row["principal"], row["issuer"], row["subject"]) == (auditor.id, TEST_ISSUER, "auditor-sub")  # who
    assert (row["action_tool"], row["action_fingerprint"], row["outcome"]) == (  # what
        "delete_file", request.action_fingerprint, "approved",
    )  # fmt: skip
    assert (row["project_id"], row["run_id"]) == (project.id, "run-1") and row["decided_at"]  # where, when


# ------------------------------------------------------------------ 429


def test_repeated_refused_credentials_are_rate_limited_per_client(api):
    with api.client("not-a-jwt", app=api.app(failures_per_minute=3)) as client:  # one app process, one client
        for _ in range(3):
            assert client.get("/api/v1/projects").status_code == 401
        # Even a valid token waits now: what is limited is verification work for this client address.
        limited = client.get("/api/v1/projects", headers=bearer(ISSUER.token()))
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["code"] == "too_many_requests"


def test_decisions_are_rate_limited_per_principal(api):
    project = add_project(api.seed)
    add_run(api.seed, project)
    requests = [add_approval(api.seed, project, path=f"old_{i}.py") for i in range(3)]
    api.principal("calm-decider", ALL_PERMISSIONS)
    with api.as_("busy-decider", ALL_PERMISSIONS, app=api.app(decisions_per_minute=2)) as client:
        statuses = [client.post(decision_url(project, r), json=decision_body(r)).status_code for r in requests]
        calm = client.post(
            decision_url(project, requests[2]), json=decision_body(requests[2]), headers=bearer(ISSUER.token("calm-decider"))
        )
    assert statuses == [201, 201, 429]
    assert calm.status_code == 201  # the limit is per principal


# -------------------------------------------------------- secrets and logs


def test_tokens_and_authorization_headers_never_reach_logs_or_responses(api, caplog):
    caplog.set_level(logging.DEBUG)
    valid, refused = ISSUER.token(), ISSUER.token(aud="someone-else")
    raw_canary = "Bearer raw-credential-canary-5d1e"
    with api.client(valid) as client:
        client.get("/api/v1/projects")
    with api.client(refused) as client:
        rejected = client.get("/api/v1/projects")
    with TestClient(api.app(), base_url="http://127.0.0.1") as client:
        raw = client.get("/api/v1/projects", headers={"Authorization": raw_canary})
    logged = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    for secret in (valid, refused, "raw-credential-canary-5d1e"):
        assert secret not in logged
        assert secret not in rejected.text and secret not in raw.text


def test_the_log_redactor_removes_tokens_however_they_appear():
    token = ISSUER.token()
    line = f"failed with header Authorization: Bearer {token} and again {token}"
    cleaned = Redactor()(line)
    assert token not in cleaned and "Bearer ***" in cleaned


def test_the_unauthenticated_placeholder_actor_is_gone():
    import agent_factory.api.services as services

    assert not hasattr(services, "UNAUTHENTICATED_API_ACTOR")


def test_the_contract_documents_401_403_and_429_on_every_protected_operation():
    spec = yaml.safe_load(open("openapi.yaml", encoding="utf-8"))
    for item in spec["paths"].values():
        for op in item.values():
            if isinstance(op, dict) and "operationId" in op:
                assert {"401", "403", "429"} <= set(op["responses"]), op["operationId"]


def test_owner_subject_constant_is_the_default_token_subject():
    assert ISSUER.token  # the owner every default client authenticates as
    assert OWNER_SUBJECT == "owner-7f3c"

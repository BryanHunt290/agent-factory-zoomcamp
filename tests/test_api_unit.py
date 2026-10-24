"""Focused HTTP boundary, lifecycle, disclosure, and cursor security tests."""

import base64
import json
import logging
from dataclasses import replace
from datetime import timedelta

import pytest

pytest.importorskip("fastapi", reason="API tests need the API packages: run with .venv (requirements-api.txt)")
pytest.importorskip("jsonschema", reason="API tests validate against openapi.yaml: run with .venv (requirements-dev.txt)")

from fastapi.testclient import TestClient  # noqa: E402 - after the importorskip guards

from agent_factory.api.config import ApiSettings, UnsafeBindError  # noqa: E402
from agent_factory.api.observability import JsonFormatter, Redactor
from agent_factory.api.pagination import CursorCodec, InvalidCursor
from agent_factory.api.stores import InMemoryStoreProvider
from agent_factory.domain.approvals import ExactAction
from agent_factory.domain.errors import StorageUnavailableError
from agent_factory.persistence.health import Readiness
from api_support import (
    BODY, KEY, NOW, POSIX_PATH, SECRET, WINDOWS_PATH, ContractClient,
    add_approval, add_project, add_run, application, decision_body, decision_url,
)


@pytest.fixture
def provider():
    return InMemoryStoreProvider()


@pytest.fixture
def client(provider):
    with ContractClient(application(provider)) as current:
        yield current


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.test", ""])
def test_non_loopback_configuration_is_refused(host):
    with pytest.raises(UnsafeBindError, match="loopback only"):
        ApiSettings(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_configuration_is_accepted(host):
    settings = ApiSettings(host=host)
    assert settings.bind_warnings() == []
    assert host in settings.allowed_hosts


@pytest.mark.parametrize("host", ["evil.test", "[::1]evil.test", "127.0.0.1:bad", "::1"])
def test_host_header_must_be_well_formed_loopback(client, host):
    result = client.get("/api/v1/projects", headers={"Host": host})
    assert result.status_code == 400
    assert result.json()["code"] == "malformed_request"


def test_duplicate_host_headers_are_refused(client):
    result = client.get("/api/v1/projects", headers=[("Host", "127.0.0.1"), ("Host", "evil.test")])
    assert result.status_code == 400


@pytest.mark.parametrize(
    "body,content_type",
    [
        ('{"name":"one","name":"two","template":"calculator"}', "application/json"),
        ('{"name":NaN,"template":"calculator"}', "application/json"),
        ('{"name":"one","template":"calculator"}', "text/plain"),
        ('{"name":', "application/json"),
        (b"\xff", "application/json"),
        (" " * (64 * 1024 + 1), "application/json"),
    ],
    ids=["duplicate-key", "nan", "simple-cross-origin-body", "truncated", "non-utf8", "oversized"],
)
def test_ambiguous_or_non_json_bodies_cannot_create_projects(client, provider, body, content_type):
    result = client.post(
        "/api/v1/projects", content=body, headers={"Content-Type": content_type, "Idempotency-Key": KEY}
    )
    assert result.status_code == 400
    assert provider.store.projects.list() == []


def test_validation_errors_never_echo_unknown_keys_or_values(client):
    canary_field = WINDOWS_PATH + SECRET
    result = client.post("/api/v1/projects", json={**BODY, canary_field: SECRET}, headers={"Idempotency-Key": KEY})
    assert result.status_code == 422
    assert SECRET not in result.text and "operator-canary" not in result.text
    assert result.json()["errors"][0]["pointer"] == ""


def test_unknown_paths_are_not_reflected_into_errors_or_api_logs(client, caplog):
    caplog.set_level(logging.INFO, logger="agent_factory.api")
    result = client.get("/not-a-route/" + SECRET)
    assert result.status_code == 404
    assert SECRET not in result.text
    access = [record for record in caplog.records if record.name == "agent_factory.api.access"]
    assert len(access) == 1
    assert access[0].route == "unmatched"


def test_unhandled_exceptions_never_disclose_unknown_secrets_in_response_or_api_logs(provider, caplog):
    class BrokenProvider(InMemoryStoreProvider):
        def session(self):
            raise RuntimeError(SECRET + " " + WINDOWS_PATH + " " + POSIX_PATH)

    caplog.set_level(logging.INFO, logger="agent_factory.api")
    with ContractClient(application(BrokenProvider())) as client:
        result = client.get("/api/v1/projects")
    assert result.status_code == 500
    for canary in [SECRET, "operator-canary"]:
        assert canary not in result.text
        assert canary not in caplog.text
    records = [record for record in caplog.records if record.name == "agent_factory.api.access"]
    assert len(records) == 1
    record = records[0]
    assert record.request_id == result.headers["X-Request-Id"]
    assert record.method == "GET" and record.route == "/api/v1/projects" and record.status == 500
    assert record.duration_ms >= 0
    rendered = json.loads(JsonFormatter().format(record))
    assert {"ts", "request_id", "method", "route", "status", "duration_ms"} <= rendered.keys()


def test_request_ids_are_generated_by_the_server(client):
    first = client.get("/api/v1/projects", headers={"X-Request-Id": "req_ClientSupplied123"})
    second = client.get("/api/v1/projects")
    assert first.headers["X-Request-Id"] != "req_ClientSupplied123"
    assert first.headers["X-Request-Id"] != second.headers["X-Request-Id"]
    assert first.headers["Cache-Control"] == "no-store"
    assert first.headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in first.headers


@pytest.mark.parametrize("problem", [Readiness(False, SECRET), StorageUnavailableError(SECRET)])
def test_readiness_never_reflects_infrastructure_details(problem):
    class UnreadyProvider(InMemoryStoreProvider):
        def readiness(self):
            if isinstance(problem, Exception):
                raise problem
            return problem

    with TestClient(application(UnreadyProvider()), base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database_unavailable"}
    assert response.headers["Retry-After"] == "5"
    assert response.headers["X-Request-Id"].startswith("req_")


def test_lifespan_closes_resources_even_when_open_fails():
    events = []

    class FailingStartup(InMemoryStoreProvider):
        def open(self):
            events.append("open")
            raise StorageUnavailableError("startup failed")

        def close(self):
            events.append("close")

    with pytest.raises(StorageUnavailableError):
        with TestClient(application(FailingStartup()), base_url="http://127.0.0.1"):
            pytest.fail("failed startup must not serve requests")
    assert events == ["open", "close"]


def test_lifespan_opens_and_closes_exactly_once():
    events = []

    class TrackedProvider(InMemoryStoreProvider):
        def open(self):
            events.append("open")

        def close(self):
            events.append("close")

    app = application(TrackedProvider())
    assert events == []
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert events == ["open"]
        assert client.get("/healthz").status_code == 200
    assert events == ["open", "close"]


@pytest.mark.parametrize(
    "params",
    [
        {"path": WINDOWS_PATH},
        {"path": POSIX_PATH},
        {"nested": [{"API_KEY": "unknown-credential-canary"}]},
        {"token": "unknown-credential-canary"},
        {"text": SECRET},
        {"url": "postgresql://someone:unknown-password@localhost/database"},
    ],
)
def test_unsafe_exact_actions_are_never_disclosed_or_decided(provider, params):
    project = add_project(provider.store)
    add_run(provider.store, project)
    original = add_approval(provider.store, project)
    action = ExactAction(tool="delete_file", category="destructive_delete", params=params)
    request = replace(original, id="apr-" + "f" * 16, action=action, action_fingerprint=action.fingerprint)
    provider.store.approvals.add_request(request)
    url = decision_url(project, request)
    with ContractClient(application(provider)) as client:
        responses = [
            client.get(url.removesuffix("/decision")),
            client.get("/api/v1/approvals?status=pending"),
            client.post(url, json=decision_body(request)),
        ]
    for response in responses:
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert SECRET not in response.text
        assert "operator-canary" not in response.text
        assert "unknown-credential-canary" not in response.text
        assert "unknown-password" not in response.text
    assert provider.store.approvals.get_decision(project.id, request.id) is None
    assert provider.store.approvals.get_request(project.id, request.id).status.value == "pending"


def test_safe_exact_actions_and_credential_references_are_returned_unchanged(provider):
    project = add_project(provider.store)
    add_run(provider.store, project)
    original = add_approval(provider.store, project)
    action = ExactAction(
        tool="deploy", category="production_deploy",
        params={"path": "build/output.py", "credential_ref": "local-deployment-key", "options": [1, True, None]},
    )
    request = replace(original, id="apr-" + "e" * 16, action=action, action_fingerprint=action.fingerprint)
    provider.store.approvals.add_request(request)
    with ContractClient(application(provider)) as client:
        response = client.get(decision_url(project, request).removesuffix("/decision"))
    assert response.status_code == 200
    assert response.json()["action"] == action.to_dict()
    assert response.json()["action_fingerprint"] == action.fingerprint


def test_cursor_expires_and_is_bound_to_operation_filters_and_signing_key():
    clock = [NOW]
    codec = CursorCodec(b"x" * 32, ttl=timedelta(minutes=1), clock=lambda: clock[0])
    cursor = codec.encode("listProjects", {"activity_since": None}, ["2026-10-02T00:00:00.000000Z", "prj-" + "a" * 20])
    assert codec.decode(cursor, "listProjects", {"activity_since": None})[1].startswith("prj-")
    with pytest.raises(InvalidCursor):
        codec.decode(cursor, "listRuns", {"activity_since": None})
    with pytest.raises(InvalidCursor):
        codec.decode(cursor, "listProjects", {"activity_since": "2026-10-02T01:00:00.000000Z"})
    with pytest.raises(InvalidCursor):
        CursorCodec(b"y" * 32, clock=lambda: NOW).decode(cursor, "listProjects", {"activity_since": None})
    clock[0] += timedelta(minutes=1)
    with pytest.raises(InvalidCursor, match="expired"):
        codec.decode(cursor, "listProjects", {"activity_since": None})


def test_noncanonical_base64_cursor_tampering_is_rejected():
    codec = CursorCodec(b"x" * 32, clock=lambda: NOW)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    cursor = next(
        candidate for length in range(1, 4)
        if len(candidate := codec.encode("op", {}, ["x" * length])) % 4 in (2, 3)
    )
    changed = cursor[:-1] + alphabet[alphabet.index(cursor[-1]) + 1]
    assert base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)) == base64.urlsafe_b64decode(
        changed + "=" * (-len(changed) % 4)
    )
    with pytest.raises(InvalidCursor):
        codec.decode(changed, "op", {})


def test_log_redaction_also_removes_short_configured_secrets():
    assert Redactor(["abc"])("password=abc") == "password=***"

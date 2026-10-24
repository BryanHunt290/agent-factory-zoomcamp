"""HTTP test helpers shared by offline and real PostgreSQL API tests.

Every client is authenticated by default: ``ContractClient`` sends a real
token for the test owner (every permission), signed by the TEST ONLY issuer in
auth_support.py and verified by the production code path. ``token=None``
gives an unauthenticated client.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from auth_support import ISSUER, bearer, ensure_owner

from agent_factory.api.app import create_app
from agent_factory.api.pagination import CursorCodec
from agent_factory.api.redaction import TextSanitizer
from agent_factory.domain.approvals import ApprovalOutcome, ApprovalRequest, ExactAction, approval_expiry, risk_for
from agent_factory.domain.executions import AttemptResult, ExecutionLedger, ExecutionSpec, LedgerKind
from agent_factory.domain.identity import Actor
from agent_factory.domain.ids import milestone_id, new_project_id
from agent_factory.domain.milestones import Milestone, MilestoneCategory, MilestoneSource, MilestoneStatus
from agent_factory.domain.project import Project, Run
from agent_factory.domain.serialization import format_timestamp, parse_timestamp
from agent_factory.domain.services import ApprovalService
from agent_factory.safety.approval import make_request_id
from agent_factory.safety.execution import EffectClass, ExecutionSemantics
from agent_factory.state import RunStatus
from agent_factory.templates import CALCULATOR_TEMPLATE

SPEC = yaml.safe_load((Path(__file__).resolve().parent.parent / "openapi.yaml").read_text(encoding="utf-8"))
BASE = "urn:agent-factory:api-http-test"
REGISTRY = Registry().with_resource(BASE, Resource.from_contents(SPEC, default_specification=DRAFT202012))
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
T0 = "2026-10-02T10:00:00.000000Z"
T1 = "2026-10-02T11:00:00.000000Z"
T2 = "2026-10-02T12:00:00.000000Z"
KEY = "request-key-0123456789"
BODY = {"name": "HTTP calculator", "template": "calculator", "description": "Created over HTTP"}
SECRET = "secret-canary-api-681bc3"
WINDOWS_PATH = r"C:\private\operator-canary.txt"
POSIX_PATH = "/home/private/operator-canary.txt"


def deref(node):
    while "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/")
        node = SPEC
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _absolute_refs(value):
    if isinstance(value, dict):
        return {k: BASE + v if k == "$ref" and v.startswith("#") else _absolute_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_absolute_refs(v) for v in value]
    return value


def validate(schema, value):
    jsonschema.Draft202012Validator(
        _absolute_refs(schema), registry=REGISTRY, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    ).validate(value)


def api_routes(app):
    """Every APIRoute of the application, through FastAPI's included-router wrappers."""

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from walk(route.original_router.routes)

    return list(walk(app.router.routes))


def operation_for(method, path):
    for template, item in SPEC["paths"].items():
        pattern = re.sub(r"\{[^}]+\}", "[^/]+", template)
        if re.fullmatch(pattern, path):
            return item.get(method.lower())
    return None


OWNER = object()  # ContractClient's default: the test owner's token


class ContractClient(TestClient):
    """Every documented HTTP response is validated against checked-in YAML, not FastAPI's schema."""

    def __init__(self, app, *, token=OWNER):
        token = ISSUER.token() if token is OWNER else token
        headers = bearer(token) if token is not None else None
        super().__init__(app, base_url="http://127.0.0.1", raise_server_exceptions=False, headers=headers)

    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        assert response.headers.get("X-Request-ID", "").startswith("req_")
        operation = operation_for(method, response.request.url.path)
        if operation is not None:
            assert str(response.status_code) in operation["responses"], response.text
            expected = deref(operation["responses"][str(response.status_code)])
            media_type = response.headers["content-type"].split(";")[0]
            assert media_type in expected["content"], response.text
            validate(expected["content"][media_type]["schema"], response.json())
            for name, header in expected.get("headers", {}).items():
                header = deref(header)
                if header.get("required"):
                    assert name in response.headers
                if name in response.headers:
                    value = response.headers[name]
                    if header["schema"].get("type") == "integer":
                        value = int(value)
                    validate(header["schema"], value)
        if response.status_code >= 400:
            assert response.headers["content-type"] == "application/problem+json"
            validate({"$ref": "#/components/schemas/Problem"}, response.json())
            assert response.json()["request_id"] == response.headers["X-Request-ID"]
            assert response.json()["status"] == response.status_code
        return response


def application(stores, *, authenticator=None, clock=None, **kwargs):
    """The API over ``stores``, trusting the TEST ONLY issuer. An in-memory store gets the test owner enrolled;
    a PostgreSQL-backed test enrolls it through its seed store (``ensure_owner``). The API's clock reads
    ``NOW`` unless ``clock`` is given (an end-to-end test shares its workers' clock)."""
    if hasattr(stores, "store"):
        ensure_owner(stores.store)
    return create_app(
        stores=stores,
        authenticator=authenticator or ISSUER.authenticator(),
        cursors=CursorCodec(b"test-only-cursor-signing-material-32-bytes", clock=lambda: NOW),
        sanitizer=TextSanitizer([SECRET]),
        clock=clock or (lambda: NOW),
        **kwargs,
    )


def add_project(store, name="Seed project", at=T0):
    project = Project(
        id=new_project_id(), name=name, created_at=at, template="calculator",
        milestone_policy=CALCULATOR_TEMPLATE.milestone_policy,
    )
    store.projects.add(project)
    return project


def add_run(store, project, run_id="run-1", number=1, status=RunStatus.AWAITING_APPROVAL, **kwargs):
    run = Run(
        project_id=project.id, run_id=run_id, number=number, graph_name="calculator-v1",
        status=status, created_at=T0, updated_at=T1, **kwargs,
    )
    store.runs.add(run)
    return run


def add_milestone(store, project, sequence, at=T0, run_id="run-1", status=MilestoneStatus.COMPLETED, **kwargs):
    values = dict(
        id=milestone_id(project.id, run_id, f"step:{sequence}"), project_id=project.id,
        run_id=run_id, sequence=sequence, key=f"step:{sequence}", type="plan_ready",
        category=MilestoneCategory.WORKFLOW, title="Plan ready", summary="Plan recorded",
        status=status, created_at=at, occurred_at=at,
        source=MilestoneSource(node="planner", agent="planner", step=sequence),
    )
    milestone = Milestone(**{**values, **kwargs})
    store.milestones.add(milestone)
    return milestone


def add_approval(store, project, run_id="run-1", path="legacy_calc.py", at=T1, **kwargs):
    action = ExactAction(tool="delete_file", category="destructive_delete", params={"path": path})
    values = dict(
        id=make_request_id(run_id, "developer", 1, action.tool, action.params), project_id=project.id,
        run_id=run_id, node="developer", agent="developer", action=action,
        action_fingerprint=action.fingerprint, reason="Deletion needs approval", risk=risk_for(action.category),
        requested_at=at, expires_at=approval_expiry(at),
    )
    request = ApprovalRequest(**{**values, **kwargs})
    store.approvals.add_request(request)
    return request


def decision_url(project, request):
    return f"/api/v1/projects/{project.id}/approvals/{request.id}/decision"


def decision_body(request, outcome="approved", **kwargs):
    return {"outcome": outcome, "action_fingerprint": request.action_fingerprint, **kwargs}


WORKER_ID = "wkr-" + "5e" * 10
DECIDED_AT = "2026-10-02T11:10:00.000000Z"
DISPATCHED_AT = "2026-10-02T11:11:00.000000Z"
ENDED_AT = "2026-10-02T11:12:00.000000Z"


class _At:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return parse_timestamp(self.at)


def add_uncertain_execution(store, project, request, *, fencing_token=3, semantics=None, attempts=1):
    """The request approved, then sent by a worker, and ended with an unknown outcome (a timeout after sending).

    Written through the real ledger, as a worker would, so every database constraint applies. ``attempts``
    automatic attempts (a keyed tool's ``semantics``) each end without an outcome, two minutes apart.
    """
    clock = _At(DECIDED_AT)
    ApprovalService(store.approvals, clock=clock).record(
        project_id=project.id, request_id=request.id, action_fingerprint=request.action_fingerprint,
        outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("seed operator"),
    )  # fmt: skip
    ledger = ExecutionLedger(store.executions, kind=LedgerKind.PRODUCT_STORE, clock=clock)
    spec = ExecutionSpec(
        project_id=project.id, run_id=request.run_id, request_id=request.id, node=request.node, visit=1,
        agent=request.agent, action=request.action,
        semantics=semantics or ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT),
        authorized_at=DECIDED_AT, authorized_until=request.expires_at,
    )  # fmt: skip
    execution, _ = ledger.authorize(spec)
    start = parse_timestamp(DISPATCHED_AT)
    for n in range(attempts):
        clock.at = format_timestamp(start + timedelta(minutes=2 * n))
        dispatch = ledger.begin(execution, owner=f"{WORKER_ID}@{fencing_token}")
        clock.at = format_timestamp(start + timedelta(minutes=2 * n + 1))
        execution = ledger.finish(dispatch.execution, dispatch.attempt, result=AttemptResult.INDETERMINATE, detail="TimeoutError")
    return execution


def reconcile_url(project, request):
    return f"/api/v1/projects/{project.id}/approvals/{request.id}/reconciliation"


def reconcile_body(execution, choice="it_happened", **kwargs):
    return {
        "execution_id": execution.id, "attempt": execution.attempts, "action_fingerprint": execution.action_fingerprint,
        "choice": choice, **kwargs,
    }  # fmt: skip


def run_is_due(store, project_id, run_id):
    """Was the run woken: is its claim row available now? (In memory and PostgreSQL.)"""
    db = store.queue.db
    if hasattr(db, "claims"):
        claim = db.claims.get((project_id, run_id))
        return (
            claim is not None and claim.available_at is not None
            and parse_timestamp(claim.available_at) <= datetime.now(timezone.utc)
        )  # fmt: skip
    rows = db.query(
        "SELECT available_at IS NOT NULL AND available_at <= now() AS due FROM run_claims WHERE project_id = %s AND run_id = %s",
        (project_id, run_id),
    )
    return bool(rows and rows[0]["due"])

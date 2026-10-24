"""The API contract (openapi.yaml): valid, complete, and safe by construction.

No HTTP server exists yet; these tests check the contract itself. They need the
dev tools in requirements-dev.txt (PyYAML, openapi-spec-validator, jsonschema),
which live in the project venv:

    .venv\\Scripts\\python -m pytest tests/test_openapi_contract.py
"""

import re
from pathlib import Path
from urllib.parse import quote

import pytest

yaml = pytest.importorskip("yaml", reason="contract tests need PyYAML: run with .venv (requirements-dev.txt)")
spec_validator = pytest.importorskip("openapi_spec_validator", reason="contract tests need openapi-spec-validator (.venv)")
jsonschema = pytest.importorskip("jsonschema", reason="contract tests need jsonschema (.venv)")

from referencing import Registry, Resource  # noqa: E402 - after the importorskip guards
from referencing.jsonschema import DRAFT202012  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "openapi.yaml"
BASE = "urn:agent-factory:openapi"
METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

#: The complete v1 surface. Adding an endpoint is a deliberate contract change: update this too.
OPERATIONS = {
    ("get", "/api/v1/projects"): "listProjects",
    ("post", "/api/v1/projects"): "createProject",
    ("get", "/api/v1/projects/{project_id}"): "getProject",
    ("get", "/api/v1/templates"): "listTemplates",
    ("get", "/api/v1/projects/{project_id}/runs"): "listRuns",
    ("get", "/api/v1/projects/{project_id}/runs/{run_id}"): "getRun",
    ("get", "/api/v1/projects/{project_id}/milestones"): "listProjectMilestones",
    ("get", "/api/v1/projects/{project_id}/runs/{run_id}/milestones"): "listRunMilestones",
    ("get", "/api/v1/approvals"): "listPendingApprovals",
    ("get", "/api/v1/projects/{project_id}/approvals/{approval_id}"): "getApproval",
    ("post", "/api/v1/projects/{project_id}/approvals/{approval_id}/decision"): "decideApproval",
    ("get", "/api/v1/attention"): "listAttention",
    ("post", "/api/v1/projects/{project_id}/approvals/{approval_id}/reconciliation"): "reconcileAction",
}
#: The only writes: creating a project, and recording a person's decision (an approval, or a reconciliation).
WRITE_OPERATIONS = {"createProject", "decideApproval", "reconcileAction"}
#: Every field any request body may carry.
REQUEST_FIELDS = {
    "name", "template", "description", "outcome", "action_fingerprint", "comment", "execution_id", "attempt", "choice",
}  # fmt: skip


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registry(spec):
    return Registry().with_resource(BASE, Resource.from_contents(spec, default_specification=DRAFT202012))


# ------------------------------------------------------------------ helpers


def operations(spec):
    for path, item in spec["paths"].items():
        for method in METHODS:
            if method in item:
                yield path, method, item[method]


def resolve(spec, ref):
    assert ref.startswith("#/"), f"only local references are used: {ref}"
    node = spec
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def deref(spec, node):
    while isinstance(node, dict) and "$ref" in node:
        node = resolve(spec, node["$ref"])
    return node


def walk(node, pointer=""):
    yield pointer, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f"{pointer}/{i}")


def parameters(spec, path, op):
    params = [deref(spec, p) for p in spec["paths"][path].get("parameters", []) + op.get("parameters", [])]
    return {(p["in"], p["name"]): p for p in params}


def raw_parameter_refs(spec, path, op):
    return [p.get("$ref") for p in spec["paths"][path].get("parameters", []) + op.get("parameters", [])]


def validator_for(registry, pointer):
    schema = {"$ref": f"{BASE}#{quote(pointer, safe='/~')}"}
    return jsonschema.Draft202012Validator(
        schema, registry=registry, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )


def schema_validator(registry, name):
    return validator_for(registry, f"/components/schemas/{name}")


def media_examples(spec):
    """(pointer of the media type, example name, example value) for every documented example."""
    for pointer, node in walk(spec):
        if isinstance(node, dict) and "schema" in node and "examples" in node and "/content/" in pointer:
            for name, example in node["examples"].items():
                yield pointer, name, deref(spec, example)["value"]


def all_schemas(spec):
    """Every schema object in the document, with its pointer."""
    for pointer, node in walk(spec):
        if isinstance(node, dict) and (pointer.endswith("/schema") or "/schemas/" in pointer) and (
            "type" in node or "properties" in node
        ):
            yield pointer, node


def property_names(spec):
    names = set()
    for _, node in all_schemas(spec):
        names.update(node.get("properties", {}))
    return names


def reachable_schemas(spec, node, seen=None):
    """Names of the component schemas reachable from ``node``."""
    seen = set() if seen is None else seen
    for _, sub in walk(node):
        if isinstance(sub, dict) and isinstance(sub.get("$ref"), str) and sub["$ref"].startswith("#/components/schemas/"):
            name = sub["$ref"].rsplit("/", 1)[1]
            if name not in seen:
                seen.add(name)
                reachable_schemas(spec, spec["components"]["schemas"][name], seen)
    return seen


# ----------------------------------------------------------- 1-5: structure


def test_the_contract_is_a_valid_openapi_3_1_document(spec):
    assert spec["openapi"].startswith("3.1.")
    spec_validator.validate(spec)  # the official OpenAPI 3.1 schema; raises if invalid


def test_the_contract_is_the_complete_v1_surface(spec):
    found = {(method, path): op["operationId"] for path, method, op in operations(spec)}
    assert found == OPERATIONS


def test_every_path_is_versioned(spec):
    assert all(path.startswith("/api/v1/") for path in spec["paths"])
    assert spec["info"]["version"].startswith("1.")


def test_every_reference_resolves(spec):
    refs = [(pointer, node["$ref"]) for pointer, node in walk(spec) if isinstance(node, dict) and "$ref" in node]
    assert refs
    for pointer, ref in refs:
        try:
            resolve(spec, ref)
        except (KeyError, IndexError, AssertionError) as exc:
            pytest.fail(f"dangling $ref {ref!r} at {pointer}: {exc!r}")


def test_no_component_is_unused(spec):
    used = {node["$ref"] for _, node in walk(spec) if isinstance(node, dict) and "$ref" in node}
    for kind in ("schemas", "parameters", "responses", "headers", "examples"):
        for name in spec["components"][kind]:
            assert f"#/components/{kind}/{name}" in used, f"unused component {kind}/{name}"


def test_operation_ids_are_present_and_unique(spec):
    ids = [op.get("operationId") for _, _, op in operations(spec)]
    assert all(ids) and len(ids) == len(set(ids))


def test_path_parameters_are_declared_with_domain_identifiers(spec):
    expected = {"project_id": "ProjectId", "run_id": "RunId", "approval_id": "ApprovalId"}
    for path, method, op in operations(spec):
        params = parameters(spec, path, op)
        for name in re.findall(r"{(\w+)}", path):
            param = params.get(("path", name))
            assert param and param["required"] is True, f"{method} {path}: {name} undeclared"
            assert param["schema"] == {"$ref": f"#/components/schemas/{expected[name]}"}


def test_the_documentation_covers_every_operation(spec):
    doc = (ROOT / "docs" / "api-contract.md").read_text(encoding="utf-8")
    for _, _, op in operations(spec):
        assert op["operationId"] in doc, f"docs/api-contract.md does not mention {op['operationId']}"


# ---------------------------------------------- 6-8: decision safety rules


def test_a_decision_must_name_the_exact_action_it_approves(spec, registry):
    schema = spec["components"]["schemas"]["ApprovalDecisionRequest"]
    assert set(schema["required"]) == {"outcome", "action_fingerprint"}
    assert schema["properties"]["action_fingerprint"] == {"$ref": "#/components/schemas/ActionFingerprint"}
    validate = schema_validator(registry, "ApprovalDecisionRequest")
    assert validate.is_valid({"outcome": "approved", "action_fingerprint": "sha256:" + "a" * 64})
    assert not validate.is_valid({"outcome": "approved"})  # no fingerprint: refused
    assert not validate.is_valid({"outcome": "approved", "action_fingerprint": "a" * 64})  # not a fingerprint
    assert not validate.is_valid({"outcome": "maybe", "action_fingerprint": "sha256:" + "a" * 64})


@pytest.mark.parametrize(
    "extra",
    [
        {"decided_at": "2026-10-02T06:16:00Z"},
        {"actor": {"kind": "user", "id": "me", "display_name": "Me", "verified": True}},
        {"decided_by": {"kind": "user", "id": "me", "display_name": "Me", "verified": True}},
        {"verified": True},
        {"id": "apd-" + "0" * 20},
        {"status": "approved"},
        {"action": {"tool": "delete_file", "category": "destructive_delete", "params": {"path": "x"}}},
    ],
)
def test_a_decision_cannot_carry_server_controlled_or_identity_fields(registry, extra):
    body = {"outcome": "approved", "action_fingerprint": "sha256:" + "a" * 64, **extra}
    assert not schema_validator(registry, "ApprovalDecisionRequest").is_valid(body)


def test_no_request_can_set_time_identity_or_verification(spec):
    request_schemas = set()
    for _, _, op in operations(spec):
        if "requestBody" in op:
            request_schemas |= reachable_schemas(spec, op["requestBody"])
    assert request_schemas == {
        "CreateProjectRequest", "ApprovalDecisionRequest", "TemplateName", "DecisionOutcome", "ActionFingerprint",
        "ReconciliationRequest", "ExecutionId", "ReconciliationChoice",
    }  # fmt: skip
    for name in request_schemas:
        schema = spec["components"]["schemas"][name]
        assert set(schema.get("properties", {})) <= REQUEST_FIELDS, name
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False, f"{name} must refuse unknown fields"
    actor = spec["components"]["schemas"]["Actor"]
    assert actor["properties"]["verified"]["readOnly"] is True  # set by the server only


def test_decisions_are_recorded_once_and_retries_are_safe(spec):
    op = spec["paths"]["/api/v1/projects/{project_id}/approvals/{approval_id}/decision"]["post"]
    approval = {"$ref": "#/components/schemas/ApprovalRequest"}
    assert op["responses"]["201"]["content"]["application/json"]["schema"] == approval  # recorded now
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == approval  # already recorded
    assert op["responses"]["409"] == {"$ref": "#/components/responses/ApprovalConflict"}
    # The approval itself is the idempotency key: no header needed, and none can override a conflict.
    assert ("header", "Idempotency-Key") not in parameters(spec, "/api/v1/projects/{project_id}/approvals/{approval_id}/decision", op)
    conflict = spec["components"]["responses"]["ApprovalConflict"]["description"]
    for code in ("action_fingerprint_mismatch", "approval_already_decided", "approval_expired", "approval_cancelled"):
        assert code in conflict
    assert "never authorizes" in " ".join(op["description"].split())


def test_creating_a_project_requires_an_idempotency_key(spec):
    op = spec["paths"]["/api/v1/projects"]["post"]
    key = parameters(spec, "/api/v1/projects", op)[("header", "Idempotency-Key")]
    assert key["required"] is True and key["schema"]["minLength"] >= 16
    assert "Idempotent-Replayed" in op["responses"]["201"]["headers"]
    assert op["responses"]["409"] == {"$ref": "#/components/responses/IdempotencyConflict"}
    # Both ways a key can conflict are 409s, told apart by code; reuse is not a validation failure.
    conflict = spec["components"]["responses"]["IdempotencyConflict"]["description"]
    assert "`idempotency_key_reused`" in conflict and "`idempotency_key_in_use`" in conflict
    assert "idempotency_key_reused" not in spec["components"]["responses"]["ValidationFailed"]["description"]
    assert "409" in op["description"] and "422" not in op["description"]


def test_operations_with_query_parameters_document_validation_failures(spec):
    for path, method, op in operations(spec):
        if any(location == "query" for location, _ in parameters(spec, path, op)):
            assert op["responses"].get("422") == {"$ref": "#/components/responses/ValidationFailed"}, op["operationId"]


def test_routing_errors_have_stable_codes(spec):
    codes = spec["components"]["schemas"]["ErrorCode"]
    assert {"route_not_found", "method_not_allowed"} <= set(codes["enum"])
    assert "`route_not_found` (404)" in codes["description"] and "`method_not_allowed` (405)" in codes["description"]


# --------------------------------------- 9-11: capabilities that must not exist


def test_the_only_writes_are_project_creation_and_approval_decisions(spec):
    writes = {op["operationId"] for _, method, op in operations(spec) if method != "get"}
    assert writes == WRITE_OPERATIONS


def test_milestones_and_runs_are_read_only(spec):
    for path, method, op in operations(spec):
        if "/milestones" in path or "/runs" in path:
            assert method == "get", f"{method.upper()} {path} would let clients change execution records"


def test_no_graph_state_or_command_execution_surface(spec):
    forbidden_segments = {
        "state", "checkpoint", "checkpoints", "graph", "nodes", "resume", "pause", "cancel", "execute", "exec",
        "command", "commands", "shell", "sandbox", "sandboxes", "fingerprint", "fingerprints", "verify",
    }  # fmt: skip
    for path in spec["paths"]:
        segments = {s for s in path.split("/") if s and not s.startswith("{")}
        assert not segments & forbidden_segments, path
    forbidden_fields = {"command", "cmd", "shell", "script", "argv", "args", "exec", "env", "environment", "cwd", "workdir"}
    assert not property_names(spec) & forbidden_fields


# -------------------------------------------------------- 12: the error model


def test_every_error_response_uses_the_problem_model(spec):
    for path, method, op in operations(spec):
        codes = set(op["responses"])
        assert {"401", "403", "500", "503"} <= codes, f"{method} {path} lacks standard error responses"
        for status, response in op["responses"].items():
            if int(status) >= 400:
                content = deref(spec, response)["content"]
                assert set(content) == {"application/problem+json"}, f"{method} {path} {status}"
                assert content["application/problem+json"]["schema"] == {"$ref": "#/components/schemas/Problem"}


def test_problem_examples_are_consistent(spec):
    for name, response in spec["components"]["responses"].items():
        for example in response["content"]["application/problem+json"].get("examples", {}).values():
            problem = example["value"]
            assert problem["type"] == f"urn:agent-factory:problem:{problem['code']}", name
            assert problem["code"] in spec["components"]["schemas"]["ErrorCode"]["enum"]


def test_problems_cannot_leak_internals(spec):
    problem = spec["components"]["schemas"]["Problem"]
    assert problem["additionalProperties"] is False
    assert set(problem["properties"]) == {
        "type", "title", "status", "detail", "instance", "code", "request_id", "errors", "approval", "required_permission",
    }  # fmt: skip


# ---------------------------------------------------------- 13: timestamps


def test_timestamps_are_utc_iso_8601(spec):
    from agent_factory.domain.serialization import TIMESTAMP_RE

    timestamp = spec["components"]["schemas"]["Timestamp"]
    assert timestamp["format"] == "date-time" and timestamp["pattern"] == TIMESTAMP_RE.pattern
    assert timestamp["pattern"].endswith("Z$")
    ref = {"$ref": "#/components/schemas/Timestamp"}
    for pointer, node in all_schemas(spec):
        for name, prop in node.get("properties", {}).items():
            if name.endswith("_at") or name in {"since", "until"}:
                options = prop.get("oneOf", [prop])
                assert any(o.get("$ref") == ref["$ref"] for o in options), f"{pointer}/properties/{name}"
    for name in ("Since", "Until", "ActivitySince"):
        assert spec["components"]["parameters"][name]["schema"] == ref


# ---------------------------------------------------------- 14: pagination


def test_pagination_is_consistent(spec):
    schemas = spec["components"]["schemas"]
    pages = {name for name in schemas if name.endswith("Page")}
    assert pages == {"ProjectSummaryPage", "RunPage", "MilestonePage", "ApprovalRequestPage", "AttentionPage"}
    for name in pages:
        page = schemas[name]
        assert set(page["properties"]) == {"items", "next_cursor"} and set(page["required"]) == {"items", "next_cursor"}
        assert page["properties"]["items"]["type"] == "array"
        assert page["properties"]["next_cursor"] == {"$ref": "#/components/schemas/NextCursor"}
    limit = spec["components"]["parameters"]["Limit"]["schema"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 50)
    cursor = spec["components"]["parameters"]["Cursor"]["schema"]
    assert cursor["pattern"] == schemas["NextCursor"]["oneOf"][0]["pattern"]
    for path, method, op in operations(spec):
        ok = op["responses"].get("200", {}).get("content", {}).get("application/json", {}).get("schema", {})
        returns_page = ok.get("$ref", "").rsplit("/", 1)[-1] in pages
        refs = raw_parameter_refs(spec, path, op)
        paginated = "#/components/parameters/Limit" in refs and "#/components/parameters/Cursor" in refs
        assert returns_page == paginated, f"{method} {path}: page response and limit/cursor must go together"


# --------------------------------------------- 15: identifiers and vocabularies


def test_identifiers_match_the_domain(spec):
    from agent_factory.domain import ids
    from agent_factory.domain.approvals import FINGERPRINT_PREFIX, ExactAction

    schemas = spec["components"]["schemas"]
    assert schemas["ProjectId"]["pattern"] == ids.PROJECT_ID_RE.pattern
    assert schemas["RunId"]["pattern"] == ids.RUN_ID_RE.pattern
    assert schemas["MilestoneId"]["pattern"] == ids.MILESTONE_ID_RE.pattern
    assert schemas["ApprovalId"]["pattern"] == ids.APPROVAL_ID_RE.pattern
    assert schemas["DecisionId"]["pattern"] == ids.DECISION_ID_RE.pattern
    assert schemas["MilestoneType"]["pattern"] == ids.TYPE_RE.pattern
    assert schemas["TemplateName"]["pattern"] == ids.NAME_RE.pattern
    fingerprint = ExactAction(tool="t", category="spend_money", params={}).fingerprint
    assert schemas["ActionFingerprint"]["pattern"].startswith("^" + FINGERPRINT_PREFIX)
    assert re.match(schemas["ActionFingerprint"]["pattern"], fingerprint)
    assert re.match(schemas["ProjectId"]["pattern"], ids.new_project_id())


def test_vocabularies_match_the_domain(spec):
    from agent_factory.domain import ApprovalOutcome, ApprovalStatus, MilestoneCategory, MilestoneStatus
    from agent_factory.domain.attention import AttentionKind
    from agent_factory.domain.claims import WORKER_ID_RE
    from agent_factory.domain.executions import EXECUTION_ID_RE, ExecutionState, ReconciliationChoice
    from agent_factory.domain.identity import ActorKind
    from agent_factory.domain.timeline import TimelineKind
    from agent_factory.safety.permissions import ActionCategory
    from agent_factory.state import RunStatus

    schemas = spec["components"]["schemas"]
    expected = {
        "RunStatus": RunStatus, "MilestoneStatus": MilestoneStatus, "MilestoneCategory": MilestoneCategory,
        "ApprovalStatus": ApprovalStatus, "DecisionOutcome": ApprovalOutcome, "ActorKind": ActorKind,
        "ActionCategory": ActionCategory, "ExecutionState": ExecutionState, "ReconciliationChoice": ReconciliationChoice,
        "AttentionKind": AttentionKind, "TimelineKind": TimelineKind,
    }  # fmt: skip
    for name, enum in expected.items():
        assert schemas[name]["enum"] == [e.value for e in enum], name
    assert schemas["ProjectStatus"]["enum"] == ["not_started"] + [s.value for s in RunStatus]
    assert schemas["ExecutionId"]["pattern"] == EXECUTION_ID_RE.pattern
    assert schemas["WorkerRef"]["properties"]["worker_id"]["pattern"] == WORKER_ID_RE.pattern


def test_text_limits_match_the_domain(spec):
    schemas = spec["components"]["schemas"]
    assert schemas["CreateProjectRequest"]["properties"]["name"]["maxLength"] == 200  # Project.name
    assert schemas["CreateProjectRequest"]["properties"]["description"]["maxLength"] == 2000  # Project.description
    assert schemas["ApprovalDecisionRequest"]["properties"]["comment"]["maxLength"] == 1000  # ApprovalDecision.comment
    assert schemas["ReconciliationRequest"]["properties"]["comment"]["maxLength"] == 1000  # ReconciliationDecision.comment


# ------------------------------------------------- 16: no secrets or internals


def test_schemas_expose_no_secret_or_internal_fields(spec):
    forbidden = {
        "password", "secret", "token", "api_key", "apikey", "credential", "credentials", "dsn", "database_url",
        "connection", "connection_string", "env", "environment", "workspace", "workspace_path", "path", "root",
        "checkpoint", "log_path", "container", "container_id", "docker", "image", "image_id", "sandbox", "stack",
        "stacktrace", "traceback", "exception", "metadata", "key", "visit", "context",
    }  # fmt: skip
    leaked = property_names(spec) & forbidden
    assert not leaked, f"internal or secret fields in the contract: {sorted(leaked)}"


def test_examples_contain_no_host_paths_or_credentials(spec):
    suspicious = re.compile(r"^[A-Za-z]:\\|^\\\\|^/(home|Users|root|etc|var|tmp|opt)/|://[^/\s]*:[^/\s]*@|password|BEGIN .*KEY")
    for pointer, node in walk(spec):
        if isinstance(node, str) and ("/examples/" in pointer or pointer.endswith("/example")):
            assert not suspicious.search(node), f"{pointer}: {node!r}"


# ------------------------------------------- 17: authentication placeholders


def test_authentication_is_implemented_and_every_operation_names_one_permission(spec):
    from agent_factory.domain.principals import Permission

    scheme = spec["components"]["securitySchemes"]["bearerAuth"]
    assert (scheme["type"], scheme["scheme"], scheme["bearerFormat"]) == ("http", "bearer", "JWT")
    assert scheme["x-agent-factory-status"] == "implemented"
    assert "Not yet:" in scheme["description"]  # no production identity provider is claimed
    assert "PLANNED" not in SPEC_PATH.read_text(encoding="utf-8")
    assert spec["security"] == [{"bearerAuth": []}]
    permissions = {p.value for p in Permission}
    assert set(spec["components"]["schemas"]["Permission"]["enum"]) == permissions
    for _, _, op in operations(spec):
        assert op["x-permission"] in permissions, op["operationId"]
        assert "x-planned-permission" not in op
        assert "security" not in op or op["security"] == [{"bearerAuth": []}]  # nothing is exempt
        assert {"401", "403", "429"} <= set(op["responses"]), op["operationId"]
    unauthorized = spec["components"]["responses"]["Unauthorized"]
    assert unauthorized["headers"]["WWW-Authenticate"] == {"$ref": "#/components/headers/WWWAuthenticate"}
    assert spec["components"]["headers"]["WWWAuthenticate"]["required"] is True
    assert spec["components"]["responses"]["TooManyRequests"]["headers"]["Retry-After"]


# --------------------------------------------- 18: examples match their schemas


def test_every_example_validates_against_its_schema(spec, registry):
    checked = 0
    for pointer, name, value in media_examples(spec):
        errors = list(validator_for(registry, f"{pointer}/schema").iter_errors(value))
        assert not errors, f"example {name!r} at {pointer}: {[e.message for e in errors]}"
        checked += 1
    assert checked >= 20


def test_examples_agree_with_the_domain(spec):
    """Fingerprints, decision IDs and risk texts in the examples are what Agent Factory would compute."""
    from agent_factory.domain import ExactAction, decision_id, risk_for

    found = 0
    for pointer, node in walk(spec):
        if "/examples/" not in pointer:
            continue  # only example values; schemas have these keys as property names
        if isinstance(node, dict) and {"action", "action_fingerprint", "risk", "project_id", "id"} <= set(node):
            action = ExactAction(**node["action"])
            assert node["action_fingerprint"] == action.fingerprint
            assert node["risk"] == risk_for(action.category)
            if node.get("decision"):
                assert node["decision"]["id"] == decision_id(node["project_id"], node["id"])
                assert node["decision"]["action_fingerprint"] == action.fingerprint
            found += 1
    assert found >= 4

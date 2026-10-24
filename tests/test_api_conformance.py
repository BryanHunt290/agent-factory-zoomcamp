"""The running backend implements the checked-in openapi.yaml, and nothing more.

test_openapi_contract.py proves the *contract* is valid and safe. These tests
prove the *implementation* matches it, by comparing what FastAPI actually
serves (routes, parameters, status codes, models) with openapi.yaml, never
with FastAPI's own generated schema. Every HTTP response in test_api_http.py
is additionally validated against openapi.yaml by ``ContractClient``.

Also here: the local-only rules, the sandbox credential boundary for the API's
own secrets, and the connection pool's failure behaviour.
"""

import re
import socket
import threading

import pytest

pytest.importorskip("fastapi", reason="API tests need the API packages: run with .venv (requirements-api.txt)")
pytest.importorskip("jsonschema", reason="API tests validate against openapi.yaml: run with .venv (requirements-dev.txt)")

from api_support import BODY, KEY, SPEC, ContractClient, api_routes, application, deref  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_factory import cli  # noqa: E402
from agent_factory.api import schemas  # noqa: E402
from agent_factory.api.app import CONTRACT_VERSION  # noqa: E402
from agent_factory.api.errors import SPEC as ERROR_STATUS  # noqa: E402
from agent_factory.api.errors import ErrorCode  # noqa: E402
from agent_factory.api.config import CURSOR_SECRET_FILE_ENV, DEFAULT_HOST, ApiSettings  # noqa: E402
from agent_factory.api.routes.health import INFRASTRUCTURE_PATHS  # noqa: E402
from agent_factory.api.stores import InMemoryStoreProvider  # noqa: E402
from agent_factory.sandbox import DEFAULT_ENV_ALLOWLIST  # noqa: E402
from agent_factory.sandbox.local import scrubbed_env  # noqa: E402
from agent_factory.sandbox.validation import validate_env  # noqa: E402

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def app():
    return application(InMemoryStoreProvider())


@pytest.fixture(scope="module")
def routes(app):
    return {(method.lower(), r.path): r for r in api_routes(app) for method in r.methods if method != "HEAD"}


def spec_operations():
    for path, item in SPEC["paths"].items():
        for method in HTTP_METHODS:
            if method in item:
                yield method, path, item[method], [deref(p) for p in item.get("parameters", []) + item[method].get("parameters", [])]


def flat(dependant, kind):
    found = {p.alias for p in getattr(dependant, kind)}
    for sub in dependant.dependencies:
        found |= flat(sub, kind)
    return found


def schema_name(node):
    return node["$ref"].rsplit("/", 1)[1]


# ----------------------------------------------------------------- the surface


def test_contract_version_matches(app):
    assert SPEC["info"]["version"] == CONTRACT_VERSION == app.version


def test_exactly_the_documented_operations_and_infrastructure_endpoints_exist(routes):
    documented = {(method, path) for method, path, _, _ in spec_operations()}
    assert len(documented) == 13
    product = {key for key in routes if key[1].startswith("/api/")}
    assert product == documented
    assert set(routes) - product == {("get", path) for path in INFRASTRUCTURE_PATHS}


@pytest.mark.parametrize("method,path,operation,parameters", list(spec_operations()), ids=lambda v: v if isinstance(v, str) and v.startswith("/") else None)
def test_each_operation_implements_its_documented_interface(routes, method, path, operation, parameters):
    route = routes[(method, path)]
    assert route.operation_id == operation["operationId"]
    by_location = {loc: {p["name"] for p in parameters if p["in"] == loc} for loc in ("path", "query", "header")}
    assert flat(route.dependant, "path_params") == by_location["path"]
    assert flat(route.dependant, "query_params") == by_location["query"]
    assert flat(route.dependant, "header_params") == by_location["header"]
    success = {code for code in operation["responses"] if code.startswith("2")}
    primary = "201" if "201" in success else "200"  # decideApproval also answers 200 for a repeat
    assert str(route.status_code or 200) == primary
    content = deref(operation["responses"][primary])["content"]["application/json"]["schema"]
    assert route.response_model is getattr(schemas, schema_name(content))
    for parameter in parameters:  # documented as required -> enforced as required
        if parameter["in"] == "query" and parameter.get("required"):
            assert any(p.alias == parameter["name"] and p.field_info.is_required() for p in route.dependant.query_params)


@pytest.mark.parametrize("name", sorted(schemas.MODELS), ids=str)
def test_every_api_model_has_exactly_its_contract_schemas_fields(name):
    model, schema = schemas.MODELS[name], SPEC["components"]["schemas"][name]
    assert set(model.model_fields) == set(schema["properties"])
    required = {field for field, info in model.model_fields.items() if info.is_required()}
    assert required == set(schema.get("required", []))
    assert schema.get("additionalProperties") is False
    assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("operation_id", ["createProject", "decideApproval", "reconcileAction"])
def test_request_bodies_are_the_contracts_closed_schemas(routes, operation_id):
    method, path, operation, _ = next(o for o in spec_operations() if o[2]["operationId"] == operation_id)
    body = operation["requestBody"]
    assert body["required"] is True and list(body["content"]) == ["application/json"]
    assert schema_name(body["content"]["application/json"]["schema"]) in schemas.MODELS


def test_the_models_vocabularies_are_the_contracts():
    """Every enumerated value the models accept or send is exactly the contract's (the domain side: test_openapi_contract.py)."""
    from typing import get_args

    from agent_factory.api.schemas import common

    names = (
        "RunStatus", "ProjectStatus", "MilestoneStatus", "MilestoneCategory", "ApprovalStatus", "DecisionOutcome",
        "ActorKind", "ActionCategory", "ExecutionState", "ReconciliationChoice", "AttentionKind", "TimelineKind",
    )  # fmt: skip
    for name in names:
        assert list(get_args(getattr(common, name))) == SPEC["components"]["schemas"][name]["enum"], name
    assert list(get_args(common.PermissionName)) == SPEC["components"]["schemas"]["Permission"]["enum"]
    assert common.EXECUTION_ID_PATTERN == SPEC["components"]["schemas"]["ExecutionId"]["pattern"]
    assert common.WORKER_ID_PATTERN == SPEC["components"]["schemas"]["WorkerRef"]["properties"]["worker_id"]["pattern"]


def test_the_api_records_intent_and_never_reaches_execution_machinery():
    """The API records decisions and wakes runs; workers own execution (AGENTS.md, docs/backend.md).

    No API module imports the engine, tools, sandbox, workers, agents, or the
    workspace, or names the ledger's, the checkpoints', or the claims' writers.
    """
    import ast
    from pathlib import Path

    root = Path(cli.__file__).resolve().parent / "api"
    packages = {"engine", "tools", "sandbox", "worker", "agents", "providers", "factory", "workspace", "graph"}
    names = {"ExecutionLedger", "ActionGuard", "CheckpointStore", "ClaimRepository", "Executor", "ToolGateway"}
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                if packages & set((node.module or "").split(".")):
                    offenders.append(f"{path.name}: from {node.module}")
                offenders += [f"{path.name}: {a.name}" for a in node.names if a.name in names]
            elif isinstance(node, ast.Import):
                offenders += [f"{path.name}: {a.name}" for a in node.names if packages & set(a.name.split("."))]
            elif isinstance(node, ast.Name) and node.id in names:
                offenders.append(f"{path.name}: {node.id}")
            elif (
                isinstance(node, ast.Attribute) and node.attr in {"create", "replace", "claim_next", "claim_run", "release"}
                and isinstance(node.value, ast.Attribute) and node.value.attr in {"executions", "claims"}
            ):  # fmt: skip
                offenders.append(f"{path.name}: {node.value.attr}.{node.attr}")
    assert offenders == []


def test_error_codes_and_statuses_match_the_contract():
    """Every code a documented error response names is sent with that response's status, and no other."""
    contract_codes = SPEC["components"]["schemas"]["ErrorCode"]["enum"]
    assert sorted(contract_codes) == sorted(code.value for code in ErrorCode)
    checked = set()
    for _, _, operation, _ in spec_operations():
        for status, response in operation["responses"].items():
            if int(status) < 400:
                continue
            for code in re.findall(r"`([a-z_]+)`", deref(response)["description"]):
                if code in contract_codes:
                    assert ERROR_STATUS[ErrorCode(code)][0] == int(status), (operation["operationId"], status, code)
                    checked.add(code)
    # Every code is documented on some operation, except the routing errors (paths outside the contract).
    assert set(contract_codes) - checked == {"route_not_found", "method_not_allowed"}


def _invalid_query(path, parameters):
    names = [p["name"] for p in parameters if p["in"] == "query"]
    concrete = path.replace("{project_id}", "prj-" + "0" * 20).replace("{run_id}", "run-1")
    concrete = concrete.replace("{approval_id}", "apr-" + "0" * 16)
    if "limit" in names:
        query = "limit=0" + ("&status=pending" if "status" in names and "approvals" in path else "")
        return concrete, query, "limit"
    return concrete, f"{names[0]}=not-a-valid-value", names[0]


@pytest.mark.parametrize(
    "path,parameters",
    [(path, params) for method, path, _, params in spec_operations() if method == "get" and any(p["in"] == "query" for p in params)],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_invalid_query_parameters_are_documented_422_problems(path, parameters):
    concrete, query, name = _invalid_query(path, parameters)
    with ContractClient(application(InMemoryStoreProvider())) as client:  # checks the 422 is documented
        response = client.get(f"{concrete}?{query}")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert {"location": "query", "pointer": name} == {k: response.json()["errors"][0][k] for k in ("location", "pointer")}


def test_framework_generated_schema_and_docs_are_not_served():
    with ContractClient(application(InMemoryStoreProvider())) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/api/v1", "/api/v2/projects"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json()["code"] == "route_not_found"


def test_undocumented_methods_are_refused_without_side_effects():
    provider = InMemoryStoreProvider()
    with ContractClient(application(provider)) as client:
        project = client.post("/api/v1/projects", json=BODY, headers={"Idempotency-Key": KEY}).json()
        for path in SPEC["paths"]:
            concrete = re.sub(r"\{project_id\}", project["id"], path)
            concrete = concrete.replace("{run_id}", "run-1").replace("{approval_id}", "apr-" + "0" * 16)
            for method in HTTP_METHODS:
                if method in SPEC["paths"][path]:
                    continue
                response = client.request(method.upper(), concrete, json={})
                assert response.status_code == 405, (method, path)
                assert response.json()["code"] == "method_not_allowed"
        # Nothing that would start a run, change a run, or edit a milestone exists.
        for path in (f"/api/v1/projects/{project['id']}/runs", f"/api/v1/projects/{project['id']}/milestones"):
            assert client.post(path, json={}).status_code == 405
    assert [p.id for p in provider.store.projects.list()] == [project["id"]]


def test_no_wildcard_cors():
    with ContractClient(application(InMemoryStoreProvider())) as client:
        preflight = client.options(
            "/api/v1/projects",
            headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
        )
        response = client.get("/api/v1/projects", headers={"Origin": "https://evil.test"})
    for r in (preflight, response):
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ------------------------------------------------------------------ local-only


def test_the_server_binds_to_loopback_by_default():
    assert DEFAULT_HOST == "127.0.0.1"
    assert ApiSettings().host == "127.0.0.1"
    args = cli.build_parser().parse_args(["api", "serve"])
    assert args.host == "127.0.0.1"


def test_the_serve_command_refuses_a_public_bind(capsys):
    assert cli.main(["api", "serve", "--host", "0.0.0.0"]) == 2
    assert "loopback only" in capsys.readouterr().err


# --------------------------------------------------- sandbox credential boundary


def test_the_apis_own_secrets_are_refused_by_the_sandbox_environment(monkeypatch, tmp_path):
    secret_file = tmp_path / "cursor.key"
    api_env = {
        CURSOR_SECRET_FILE_ENV: str(secret_file),
        "AGENT_FACTORY_DATABASE_URL_FILE": ".local/database.url",
        "AGENT_FACTORY_DATABASE_URL": "postgresql://app:api-db-password-canary@127.0.0.1:5432/agent_factory_dev",
        "AGENT_FACTORY_API_TOKEN": "future-auth-token-canary",
        "IDEMPOTENCY_SECRET": "idempotency-secret-canary",
    }
    for name, value in api_env.items():
        monkeypatch.setenv(name, value)
    # Trusted local fixtures inherit nothing of it ...
    env = scrubbed_env()
    assert not set(env) & set(api_env)
    assert not any("canary" in value for value in env.values())
    # ... and a sandbox request naming it is refused, even if someone allowlists it.
    for name, value in api_env.items():
        assert validate_env({name: value}, frozenset({name}) | DEFAULT_ENV_ALLOWLIST), name


# ------------------------------------------------------- pool failure behaviour


def _unused_loopback_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_an_unreachable_database_is_a_safe_503_and_not_ready():
    pytest.importorskip("psycopg_pool", reason="pool tests need requirements-postgres/api.txt (.venv)")
    from agent_factory.persistence.config import DatabaseConfig
    from agent_factory.persistence.pool import PostgresStorePool

    password = "unreachable-db-password-canary"
    config = DatabaseConfig(f"postgresql://agent_factory_app:{password}@127.0.0.1:{_unused_loopback_port()}/agent_factory_test")
    pool = PostgresStorePool(config, min_size=0, max_size=1, timeout=0.5)
    app = application(pool)
    with ContractClient(app) as client, TestClient(app, base_url="http://127.0.0.1") as infrastructure:
        assert infrastructure.get("/healthz").status_code == 200  # alive: a restart would not fix the database
        ready = infrastructure.get("/readyz")  # infrastructure: plain JSON, not a contract problem
        responses = [
            client.get("/api/v1/projects"),
            client.post("/api/v1/projects", json=BODY, headers={"Idempotency-Key": KEY}),
        ]
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "database_unavailable"}
    for response in responses:
        assert response.status_code == 503
        assert response.json()["code"] == "storage_unavailable"
        assert response.headers["Retry-After"] == "5"
        for canary in (password, "agent_factory_app", str(config.port), "127.0.0.1:", "psycopg", "Traceback"):
            assert canary not in response.text, canary


@pytest.mark.postgres
def test_an_exhausted_pool_answers_503_instead_of_queueing(pg_schema, pg_store):
    from auth_support import ensure_owner

    from agent_factory.persistence.pool import PostgresStorePool

    ensure_owner(pg_store)  # the principal the client's token names
    pool = PostgresStorePool(pg_schema, min_size=1, max_size=1, timeout=0.5)
    held, release = threading.Event(), threading.Event()

    def hold_the_only_connection():
        with pool.session():
            held.set()
            release.wait(10)

    with ContractClient(application(pool)) as client:
        assert client.get("/api/v1/projects").status_code == 200
        holder = threading.Thread(target=hold_the_only_connection)
        holder.start()
        try:
            assert held.wait(5)
            exhausted = client.get("/api/v1/projects")
        finally:
            release.set()
            holder.join()
        recovered = client.get("/api/v1/projects")
    assert exhausted.status_code == 503 and exhausted.json()["code"] == "storage_unavailable"
    assert recovered.status_code == 200
    assert pool.closed


@pytest.mark.postgres
def test_readiness_reports_an_unmigrated_schema(pg_empty_schema):
    from agent_factory.persistence.pool import PostgresStorePool

    app = application(PostgresStorePool(pg_empty_schema, min_size=0, max_size=1, timeout=2.0))
    with ContractClient(app) as client, TestClient(app, base_url="http://127.0.0.1") as infrastructure:
        ready = infrastructure.get("/readyz")
        served = client.get("/api/v1/projects")
    assert ready.status_code == 503 and ready.json() == {"status": "not_ready", "reason": "schema_not_current"}
    assert served.status_code == 503 and served.json()["code"] == "storage_unavailable"


@pytest.mark.postgres
def test_readiness_with_a_migrated_database(pg_schema):
    from agent_factory.persistence.pool import PostgresStorePool

    app = application(PostgresStorePool(pg_schema, min_size=0, max_size=1))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/readyz").json() == {"status": "ready"}

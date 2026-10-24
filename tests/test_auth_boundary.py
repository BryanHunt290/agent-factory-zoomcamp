"""Authentication secrets never cross into generated code, the sandbox, or product records.

The API process alone handles access tokens and authentication settings. These
tests put canaries where a leak would show: in the host environment of a real
local process, in sandbox requests (under telling names and innocent ones),
and in an authenticated decision's stored records.
"""

import ast
import json
from pathlib import Path

import pytest

from agent_factory.sandbox import DEFAULT_ENV_ALLOWLIST, DEFAULT_SANDBOX_ENV
from agent_factory.sandbox.local import LocalProcessBackend, scrubbed_env
from agent_factory.sandbox.manager import SandboxManager
from agent_factory.sandbox.types import CodeTrust, ExecutionRequest
from agent_factory.sandbox.validation import validate_env
from agent_factory.workspace import Workspace

ROOT = Path(__file__).resolve().parent.parent
#: A JWT-shaped canary (header {"alg":"ES256"}, payload {"sub":"canary"}, a fake signature).
TOKEN_CANARY = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ0b2tlbi1jYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"
AUTH_CANARIES = {
    "AGENT_FACTORY_AUTH_MODE": "development",
    "AGENT_FACTORY_AUTH_ISSUER": "https://issuer-canary.example.test",
    "AGENT_FACTORY_AUTH_AUDIENCE": "audience-canary",
    "AGENT_FACTORY_AUTH_JWKS_URL": "https://issuer-canary.example.test/jwks.json",
    "AGENT_FACTORY_AUTH_DEV_ISSUER_DIR": "C:/private/dev-issuer-canary",
    "OIDC_CLIENT_SECRET": "oidc-secret-canary",
    "BEARER_TOKEN": TOKEN_CANARY,
    "DEPLOY_CONFIG": TOKEN_CANARY,  # an innocent name: refused for its value
}


def test_authentication_settings_never_reach_a_trusted_local_process(tmp_path, monkeypatch):
    for name, value in AUTH_CANARIES.items():
        monkeypatch.setenv(name, value)
    assert not set(scrubbed_env()) & set(AUTH_CANARIES)
    ws = Workspace.create(tmp_path / "ws", trust=CodeTrust.TRUSTED_FIXTURE)
    ws.write_text("env_dump.py", "import os\nprint(sorted(os.environ.items()))\n")
    request = ExecutionRequest(execution_id="af-auth-boundary-01", workspace=ws, command=("python", "env_dump.py"),
                               trust=CodeTrust.TRUSTED_FIXTURE)  # fmt: skip
    result = SandboxManager([LocalProcessBackend()]).run(request)
    assert result.exit_code == 0, result.stderr
    for value in AUTH_CANARIES.values():
        assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("name, value", sorted(AUTH_CANARIES.items()))
def test_a_sandbox_request_never_carries_authentication_settings_or_tokens(name, value):
    """Refused even if someone adds the name to the allowlist."""
    assert validate_env({name: value}, frozenset({name}) | DEFAULT_ENV_ALLOWLIST)


def test_the_default_sandbox_environment_is_unaffected():
    assert validate_env(dict(DEFAULT_SANDBOX_ENV), DEFAULT_ENV_ALLOWLIST) == []


def test_the_engine_and_the_sandbox_never_touch_authentication_code():
    """Tokens live in the API process: no engine, domain, sandbox, tool, or safety module imports them."""
    offenders = []
    for package in ("engine", "domain", "sandbox", "tools", "safety", "agents", "graph", "providers", "templates"):
        for path in (ROOT / "agent_factory" / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [("." * node.level) + (node.module or "")]
                if any(n.startswith(("jwt", "cryptography")) or "api" in n.split(".") for n in names):
                    offenders.append(f"{path.relative_to(ROOT)}: {names}")
    assert offenders == []


def test_an_authenticated_decision_stores_the_principal_never_the_token():
    pytest.importorskip("fastapi", reason="needs the API packages: run with .venv")
    pytest.importorskip("jsonschema", reason="needs the contract tools: run with .venv")
    from api_support import ContractClient, add_approval, add_milestone, add_project, add_run, application
    from api_support import decision_body, decision_url
    from auth_support import ISSUER, ensure_owner

    from agent_factory.api.stores import InMemoryStoreProvider

    provider = InMemoryStoreProvider()
    project = add_project(provider.store)
    add_run(provider.store, project)
    add_milestone(provider.store, project, 1)
    request = add_approval(provider.store, project)
    token = ISSUER.token()
    with ContractClient(application(provider), token=token) as client:
        decided = client.post(decision_url(project, request), json=decision_body(request))
        milestones = client.get(f"/api/v1/projects/{project.id}/milestones")
    assert decided.status_code == 201 and milestones.status_code == 200
    db = provider.store.unit_of_work
    stored = json.dumps({name: [repr(v) for v in getattr(db, name).values()] for name in db._TABLES})
    assert token not in stored and token not in decided.text + milestones.text
    assert ensure_owner(provider.store).id in stored  # the principal is what is recorded

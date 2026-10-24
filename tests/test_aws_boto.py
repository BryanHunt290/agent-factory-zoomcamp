"""Stage E1: cloud_read and the real BotoAwsBackend, OFFLINE only (docs/aws-capabilities.md).

No AWS API is called. Every test runs with a synthetic AWS config in a temporary home directory (the real
~/.aws is never read), botocore's Stubber in place of HTTP, and a socket recorder that fails the test if
anything tries to resolve or connect to a host. Static keys planted in the synthetic environment and
credentials file prove that nothing falls back to them.
"""

from __future__ import annotations

import ast
import json
import socket
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

botocore = pytest.importorskip("botocore")
from botocore import credentials as bc  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

from agent_factory.aws import AwsIdentityInspectTool, AwsPackSettings, AwsToolPack, CredentialSlots  # noqa: E402
from agent_factory.aws.backend import AwsBackendError  # noqa: E402
from agent_factory.aws.boto import BotoAwsBackend, classify_profile, map_error  # noqa: E402
from agent_factory.aws.credentials import SlotBinding  # noqa: E402
from agent_factory.capabilities import (  # noqa: E402
    ApprovalRule,
    AvailabilityStatus,
    Capability,
    CapabilityError,
    CapabilityRegistry,
    CostImpact,
    EvidenceKind,
    NetworkRequirement,
    Risk,
)
from agent_factory.providers.capabilities import manifest_for  # noqa: E402
from agent_factory.safety import ExecutionSemanticsError  # noqa: E402
from agent_factory.safety.execution import EffectClass, ExecutionSemantics, Idempotency  # noqa: E402
from agent_factory.safety.permissions import (  # noqa: E402
    DANGEROUS_CATEGORIES,
    ActionCategory,
    Decision,
    PermissionPolicy,
    PolicyEnvironment,
    PolicyError,
)
from agent_factory.tools import ToolRegistry, ToolResult  # noqa: E402
from agent_factory.tools.base import Tool, ToolError  # noqa: E402
from agent_factory.tools.packs import local_packs  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PROFILE = "agent-factory-dev"
FAKE_ACCOUNT = "123456789012"
FAKE_ARN = f"arn:aws:sts::{FAKE_ACCOUNT}:assumed-role/AWSReservedSSO_AgentFactoryIdentity_0123456789abcdef/alex@example.com"
ENV_STATIC_CANARY = "AKIAENVSTATICCANARY01"
FILE_STATIC_CANARY = "AKIAFILESTATICCANARY1"
CONFIG = f"""
[profile {PROFILE}]
sso_session = af
sso_account_id = 000000000000
sso_role_name = AgentFactoryIdentity
region = us-west-2
[sso-session af]
sso_start_url = https://example.awsapps.com/start
sso_region = us-west-2
sso_registration_scopes = sso:account:access
[profile af-static]
aws_access_key_id = {FILE_STATIC_CANARY}
aws_secret_access_key = not-a-real-secret
[profile af-process]
credential_process = this-must-never-run
[profile af-chain]
role_arn = arn:aws:iam::000000000000:role/x
source_profile = af-static
[profile af-workload]
role_arn = arn:aws:iam::000000000000:role/x
credential_source = Ec2InstanceMetadata
[profile af-web]
role_arn = arn:aws:iam::000000000000:role/x
web_identity_token_file = /tmp/token
[profile af-login]
login_session = arn:aws:iam::000000000000:user/fake
[profile af-unknown]
region = us-west-2
[default]
sso_session = af
sso_account_id = 000000000000
sso_role_name = WouldWorkIfFallenBackTo
"""
ALL_PROFILES = frozenset({PROFILE, "af-static", "af-process", "af-chain", "af-workload", "af-web", "af-login", "af-unknown",
                          "af-missing"})  # fmt: skip


@pytest.fixture
def aws_home(tmp_path, monkeypatch):
    """A synthetic AWS home: the real ~/.aws is never read. Static keys sit in the environment and the
    credentials file (for the bound profile too) to prove nothing falls back to them."""
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "config").write_text(CONFIG, encoding="utf-8")
    (home / ".aws" / "credentials").write_text(
        f"[{PROFILE}]\naws_access_key_id = {FILE_STATIC_CANARY}\naws_secret_access_key = x\n"
        f"[default]\naws_access_key_id = {FILE_STATIC_CANARY}\naws_secret_access_key = x\n", encoding="utf-8")  # fmt: skip
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / ".aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ENV_STATIC_CANARY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret-canary")
    monkeypatch.setenv("AWS_PROFILE", "default")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://attacker.invalid")  # must be ignored
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://attacker.invalid")
    return home


@pytest.fixture
def no_network(monkeypatch):
    """Records and refuses every name resolution and connection attempt, from any library."""
    attempts: list[object] = []

    def refuse(*args, **kwargs):
        attempts.append(args[:2])
        raise OSError("network refused by test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: refuse(address))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: refuse(address))
    return attempts


@pytest.fixture
def no_other_provider(monkeypatch):
    """Any credential source other than the approved one fails the test if it is even consulted."""
    touched: list[str] = []
    for cls in (bc.EnvProvider, bc.SharedCredentialProvider, bc.ConfigProvider, bc.ProcessProvider, bc.ContainerProvider,
                bc.InstanceMetadataProvider, bc.AssumeRoleProvider, bc.AssumeRoleWithWebIdentityProvider,
                bc.OriginalEC2Provider, bc.BotoProvider):  # fmt: skip
        def load(self, _name=cls.__name__):
            touched.append(_name)
            raise AssertionError(f"{_name} must never be consulted")

        monkeypatch.setattr(cls, "load", load)
    for fetcher in (getattr(bc, "InstanceMetadataFetcher", None), getattr(bc, "ContainerMetadataFetcher", None)):
        if fetcher is not None:
            monkeypatch.setattr(fetcher, "__init__", lambda self, *a, **k: touched.append("metadata fetcher") or None)
    return touched


class TempCreds:
    """A resolved credential as a provider would return it (offline): never a real key."""

    def __init__(self, method="sso", expires_in=timedelta(hours=1), token="FakeSessionTokenForTests"):
        self.method = method
        self._expiry_time = None if expires_in is None else datetime.now(timezone.utc) + expires_in
        self._token = token

    def get_frozen_credentials(self):
        return bc.ReadOnlyCredentials("ASIAFAKETEMPORARY001", "fake-temporary-secret", self._token)


def loader_for(creds, calls):
    def load(session, profile, kind):
        calls.append((profile, kind))
        return creds
    return load


def stubbing(*responses):
    """A client hook that answers from Stubber: (operation, response) or (operation, error_code, status)."""
    used: list[Stubber] = []
    endpoints: list[str] = []

    def hook(client):
        stubber = Stubber(client)
        for item in responses:
            if len(item) == 2:
                stubber.add_response(item[0], item[1], {})
            else:
                stubber.add_client_error(item[0], service_error_code=item[1], service_message=item[3] if len(item) > 3 else
                                         "User: arn:aws:iam::123456789012:user/secret-name is not authorized",
                                         http_status_code=item[2])  # fmt: skip
        stubber.activate()
        used.append(stubber)
        endpoints.append(client.meta.endpoint_url)

    return hook, used, endpoints


IDENTITY = {"UserId": "AROAFAKEFAKEFAKEFAKE:alex@example.com", "Account": FAKE_ACCOUNT, "Arn": FAKE_ARN}


def backend(**kwargs):
    values = dict(environment="dev", allowed_profiles=ALL_PROFILES, allowed_regions=frozenset({"us-west-2"}))
    values.update(kwargs)
    return BotoAwsBackend(**values)


def binding(profile=PROFILE, slot="aws.dev.identity"):
    return SlotBinding(slot, profile)


def real_settings(be, *, authorized=True):
    return AwsPackSettings(enabled=True, backend=be, slots=CredentialSlots({"aws.dev.identity": PROFILE}),
                           region="us-west-2", real_backend_authorized=authorized)  # fmt: skip


# ------------------------------------------------------------------ cloud_read: one vocabulary, every layer


def test_cloud_read_is_the_same_word_in_the_contract_api_domain_frontend_and_database():
    import yaml

    from agent_factory.api.schemas.common import ActionCategory as ApiCategory

    spec = yaml.safe_load((REPO / "openapi.yaml").read_text(encoding="utf-8"))
    contract = spec["components"]["schemas"]["ActionCategory"]["enum"]
    assert "cloud_read" in contract and contract == [c.value for c in ActionCategory]
    assert list(typing.get_args(ApiCategory)) == contract
    frontend = (REPO / "frontend" / "src" / "api" / "schema.ts").read_text(encoding="utf-8")
    assert '| "cloud_read"' in frontend
    migration = (REPO / "agent_factory" / "persistence" / "migrations" / "0009_cloud_read_category.sql").read_text(encoding="utf-8")
    assert migration.count("'cloud_read'") == 2


def test_cloud_read_policy_by_environment():
    dev, staging, prod = (PermissionPolicy.for_environment(e) for e in PolicyEnvironment)
    rule = ActionCategory.CLOUD_READ
    assert (dev.category_rules[rule], staging.category_rules[rule], prod.category_rules[rule]) == (
        Decision.ALLOW, Decision.REQUIRE_APPROVAL, Decision.REQUIRE_APPROVAL)
    for choice in Decision:  # staging is configurable
        assert PermissionPolicy.for_environment("staging", staging_cloud_read=choice).category_rules[rule] is choice
    with pytest.raises(PolicyError):  # production never auto-allows a cloud read
        PermissionPolicy(category_rules={rule: Decision.ALLOW}, environment="production")
    with pytest.raises(PolicyError):
        prod.with_rules(cloud_read=Decision.ALLOW)
    assert prod.with_rules(cloud_read=Decision.DENY).category_rules[rule] is Decision.DENY  # stricter is fine
    assert rule not in DANGEROUS_CATEGORIES and PermissionPolicy().category_rules[rule] is Decision.ALLOW
    # Still per agent: no agent has a cloud tool by default, whatever the environment.
    for agent in ("planner", "developer", "tester", "reviewer"):
        assert dev.decide(agent, "aws_identity_inspect", rule) is Decision.DENY


def cloud_cap(**overrides):
    base = dict(capability_id="fixture.cloud.read", version=1, pack="fixture", description="Read a fixture cloud thing.",
                implemented_by="fixture_cloud_read", category=ActionCategory.CLOUD_READ, effect=EffectClass.READ_ONLY,
                approval=ApprovalRule.POLICY, cost=CostImpact.NONE, risk=Risk.LOW, mutates=False,
                network=NetworkRequirement.TRUSTED_ADAPTER, credential="aws.dev.readonly",
                input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                output_schema={"type": "object", "additionalProperties": False,
                               "properties": {"x": {"type": "string", "maxLength": 10}}},
                required_permissions=("tool:fixture_cloud_read", "category:cloud_read"), iam_actions=("s3:GetObject",))  # fmt: skip
    base.update(overrides)
    return Capability(**base)


@pytest.mark.parametrize("overrides,message", [
    ({"mutates": True}, "read-only"),
    ({"effect": EffectClass.IDEMPOTENT}, "read-only"),
    ({"cost": CostImpact.UNKNOWN}, "cost"),
    ({"network": NetworkRequirement.NONE}, "trusted adapter"),
    ({"credential": None}, "credential slot"),
    ({"iam_actions": ()}, "read-only IAM"),
    ({"iam_actions": ("s3:PutObject",)}, "read-only IAM"),
    ({"iam_actions": ("s3:DeleteObject", "s3:GetObject")}, "read-only IAM"),
    ({"iam_actions": ("iam:GetRole", "iam:AttachRolePolicy")}, "denied class"),
    ({"input_schema": {"type": "object", "additionalProperties": False, "properties": {"q": {"type": "string"}}}}, "bounded"),
    ({"input_schema": {"type": "object", "additionalProperties": False,
                       "properties": {"q": {"type": "array", "items": {"type": "string", "maxLength": 5}}}}}, "bounded"),
    ({"output_schema": {"type": "object"}}, "closed object"),
    ({"output_schema": {"type": "object", "additionalProperties": False, "properties": {"x": {"type": "string"}}}}, "bounded"),
])  # fmt: skip
def test_cloud_read_definitions_must_be_typed_bounded_and_read_only(overrides, message):
    cloud_cap()  # the baseline is valid
    with pytest.raises(CapabilityError, match=message):
        cloud_cap(**overrides)


class _CloudTool(Tool):
    name = "fixture_cloud_read"
    category = ActionCategory.CLOUD_READ
    execution = ExecutionSemantics(effect=EffectClass.READ_ONLY, rationale="fixture")

    def run(self, workspace) -> ToolResult:  # pragma: no cover - never run here
        return ToolResult()


def test_a_mutating_tool_cannot_masquerade_as_cloud_read():
    ToolRegistry([_CloudTool()])  # a read-only cloud tool registers
    for semantics in (ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT, rationale="writes"),
                      ExecutionSemantics(effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.NATURAL, rationale="puts"),
                      None):  # fmt: skip
        mutating = _CloudTool()
        mutating.execution = semantics
        with pytest.raises(ExecutionSemanticsError, match="cloud_read"):
            ToolRegistry([mutating])

    class Deploy(_CloudTool):
        name = "fixture_cloud_read"
        category = ActionCategory.PRODUCTION_DEPLOY
        execution = ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT, rationale="deploys")

    from test_capabilities import FixturePack

    with pytest.raises(CapabilityError):  # a capability calling a deployment tool cloud_read is refused
        CapabilityRegistry([FixturePack([cloud_cap()], [Deploy()])])


def test_cloud_mutation_needs_approval_and_identity_changes_are_denied():
    from test_capabilities import cap

    with pytest.raises(CapabilityError):  # a cloud mutation can never follow the policy's allow
        cap(capability_id="fixture.cloud.write", implemented_by="fixture_write", category=ActionCategory.PRODUCTION_DEPLOY,
            effect=EffectClass.NON_IDEMPOTENT, mutates=True, network=NetworkRequirement.TRUSTED_ADAPTER)  # fmt: skip
    with pytest.raises(CapabilityError, match="denied class"):
        cap(capability_id="fixture.iam.change", implemented_by="fixture_iam", category=ActionCategory.PRODUCTION_DEPLOY,
            effect=EffectClass.NON_IDEMPOTENT, mutates=True, approval=ApprovalRule.ALWAYS,
            network=NetworkRequirement.TRUSTED_ADAPTER, iam_actions=("iam:PutRolePolicy",))  # fmt: skip


# ------------------------------------------------------------------ BotoAwsBackend: explicit, no fallback


def test_the_real_backend_is_used_only_with_explicit_authorization(aws_home, no_network):
    calls: list = []
    be = backend(credential_loader=loader_for(TempCreds(), calls))
    reg = CapabilityRegistry((*local_packs(), AwsToolPack(real_settings(be, authorized=False))))
    assert reg.availability("aws.identity.inspect").status is AvailabilityStatus.NOT_CONFIGURED
    with pytest.raises(ToolError, match="AWS_NOT_CONFIGURED"):
        AwsIdentityInspectTool(real_settings(be, authorized=False)).run(None)
    assert calls == [] and no_network == []  # not even the credential was resolved
    assert AwsPackSettings().backend is None  # the default is no backend at all
    for path in (REPO / "agent_factory").rglob("*.py"):  # nothing in the product constructs it
        if path.name != "boto.py":
            assert "BotoAwsBackend(" not in path.read_text(encoding="utf-8"), path


def test_the_backend_needs_an_explicit_allowlist_and_never_default_or_internal_dev():
    for kwargs in ({"allowed_profiles": frozenset()}, {"allowed_profiles": frozenset({"default"})},
                   {"allowed_profiles": frozenset({PROFILE, "internal-dev"})}, {"environment": "test"},
                   {"allowed_regions": frozenset()}):  # fmt: skip
        with pytest.raises(ValueError):
            backend(**kwargs)


@pytest.mark.parametrize("profile,kind", [
    ("af-static", "static"), ("af-process", "process"), ("af-chain", "assume-role-chain"),
    ("af-workload", "workload-assume-role"), ("af-web", "web-identity"), ("af-unknown", "unknown"),
])  # fmt: skip
def test_refused_profile_kinds_stop_before_any_provider_runs(aws_home, no_network, no_other_provider, profile, kind):
    calls: list = []
    with pytest.raises(AwsBackendError) as refused:
        backend(credential_loader=loader_for(TempCreds(), calls)).credential_metadata(binding(profile))
    assert refused.value.category == "AWS_IDENTITY_UNAVAILABLE"
    assert ("long-term" if kind == "static" else "unsupported credential type") in refused.value.detail
    assert calls == [] and no_network == [] and no_other_provider == []
    with pytest.raises(AwsBackendError):  # the real path (no test loader) refuses the same way
        backend().credential_metadata(binding(profile))
    assert no_network == [] and no_other_provider == []


def test_the_shared_credentials_file_is_never_opened(aws_home, no_network, monkeypatch):
    import botocore.configloader as loader

    opened: list[str] = []
    real = loader.raw_config_parse
    monkeypatch.setattr(loader, "raw_config_parse", lambda path, *a, **k: opened.append(str(path)) or real(path, *a, **k))
    hook, _, _ = stubbing(("get_caller_identity", IDENTITY))
    backend(credential_loader=loader_for(TempCreds(), []), client_hook=hook).get_caller_identity(binding(), "us-west-2")
    credentials = str(aws_home / ".aws" / "credentials")
    assert opened and credentials not in opened  # the config was read; the file of long-term keys never was


def test_a_missing_profile_never_falls_back_to_default(aws_home, no_network):
    # [default] in the synthetic config is a working SSO profile: falling back to it would succeed. It must not.
    calls: list = []
    with pytest.raises(AwsBackendError) as missing:
        backend(credential_loader=loader_for(TempCreds(), calls)).credential_metadata(binding("af-missing"))
    assert missing.value.category == "AWS_NOT_CONFIGURED" and calls == []


def test_classification_reads_keys_only():
    assert classify_profile({"sso_session": "x", "sso_account_id": "1", "sso_role_name": "r"}) == "sso"
    assert classify_profile({"sso_start_url": "x", "sso_account_id": "1", "sso_role_name": "r"}) == "sso"
    assert classify_profile({"sso_session": "x", "sso_account_id": "1", "sso_role_name": "r", "aws_access_key_id": "k"}) == "static"
    assert classify_profile({"login_session": "x"}) == "login"
    assert classify_profile({"sso_session": "x"}) == "unknown"  # incomplete is not approved


def test_an_sso_profile_resolves_only_through_the_sso_provider_with_no_fallback(aws_home, no_network, no_other_provider):
    # No SSO token is cached in the synthetic home: the SSO provider fails closed, offline. Static keys in the
    # environment and in the credentials file (for this very profile), the container endpoint and the instance
    # metadata service are all available as fallbacks, and none is consulted.
    with pytest.raises(AwsBackendError) as missing:
        backend().credential_metadata(binding())
    assert missing.value.category in ("AWS_IDENTITY_UNAVAILABLE", "AWS_EXPIRED_CREDENTIAL")
    assert no_network == [] and no_other_provider == []
    assert ENV_STATIC_CANARY not in str(missing.value) + missing.value.detail


def test_aws_login_fails_closed_without_the_unapproved_awscrt_dependency(aws_home, no_network, no_other_provider):
    if bc.EC is not None:
        pytest.skip("awscrt is installed here; the reference machine does not have it")
    with pytest.raises(AwsBackendError) as missing:
        backend().credential_metadata(binding("af-login"))
    assert missing.value.category == "AWS_NOT_CONFIGURED" and no_network == [] and no_other_provider == []


def test_wrong_environment_unexpected_profile_and_unsupported_region_are_refused_first(aws_home, no_network):
    calls: list = []
    be = backend(credential_loader=loader_for(TempCreds(), calls), allowed_profiles=frozenset({PROFILE}))
    for refused_binding in (SlotBinding("aws.staging.identity", PROFILE), binding("af-static")):
        with pytest.raises(AwsBackendError) as refused:
            be.credential_metadata(refused_binding)
        assert refused.value.category == "AWS_NOT_CONFIGURED"
    with pytest.raises(AwsBackendError) as region:
        be.get_caller_identity(binding(), "eu-west-1")
    assert region.value.category == "AWS_UNSUPPORTED_REGION" and calls == [] and no_network == []


@pytest.mark.parametrize("creds,category", [
    (TempCreds(method="shared-credentials-file"), "AWS_IDENTITY_UNAVAILABLE"),  # the provider reports a static method
    (TempCreds(method="iam-role"), "AWS_IDENTITY_UNAVAILABLE"),
    (TempCreds(token=None), "AWS_IDENTITY_UNAVAILABLE"),  # no session token: not temporary
    (TempCreds(expires_in=timedelta(minutes=-5)), "AWS_EXPIRED_CREDENTIAL"),
    (TempCreds(expires_in=timedelta(seconds=20)), "AWS_EXPIRED_CREDENTIAL"),
    (TempCreds(expires_in=None), "AWS_EXPIRED_CREDENTIAL"),  # unknown expiry
    (None, "AWS_IDENTITY_UNAVAILABLE"),
])  # fmt: skip
def test_static_unknown_and_expired_credentials_are_refused(aws_home, no_network, creds, category):
    with pytest.raises(AwsBackendError) as refused:
        backend(credential_loader=loader_for(creds, [])).credential_metadata(binding())
    assert refused.value.category == category and no_network == []


# ------------------------------------------------------------------ exactly STS GetCallerIdentity, safely mapped


def test_identity_inspection_calls_exactly_get_caller_identity_and_returns_a_safe_summary(aws_home, no_network, caplog):
    calls: list = []
    hook, used, endpoints = stubbing(("get_caller_identity", IDENTITY))
    be = backend(credential_loader=loader_for(TempCreds(), calls), client_hook=hook)
    raw = be.get_caller_identity(binding(), "us-west-2")
    assert set(raw) == {"Account", "Arn", "UserId"}  # ResponseMetadata never leaves the backend
    used[0].assert_no_pending_responses()
    hook2, used2, endpoints2 = stubbing(("get_caller_identity", IDENTITY))
    be2 = backend(credential_loader=loader_for(TempCreds(), calls), client_hook=hook2)
    result = AwsIdentityInspectTool(real_settings(be2)).run(None).data
    assert result == {"principal_type": "assumed-role", "principal_name": "AWSReservedSSO_AgentFactoryIdentity_0123456789abcdef",
                      "account_hint": "********9012", "region": "us-west-2", "backend": "boto"}  # fmt: skip
    text = json.dumps(result)
    for leaked in (FAKE_ACCOUNT, "arn:", "alex@example.com", "AROA", "ASIAFAKE", "FakeSessionToken", ENV_STATIC_CANARY):
        assert leaked not in text and leaked not in caplog.text
    assert endpoints == endpoints2 == ["https://sts.us-west-2.amazonaws.com"]  # the configured overrides are ignored
    assert calls == [(PROFILE, "sso"), (PROFILE, "sso")] and no_network == []


def test_the_client_uses_the_explicit_credential_and_region_only(aws_home, no_network):
    seen: dict = {}

    def hook(client):
        creds = client._request_signer._credentials  # the credential the client will sign with
        seen.update(access_key=creds.access_key, token=creds.token, region=client.meta.region_name,
                    endpoint=client.meta.endpoint_url, attempts=client.meta.config.retries.get("total_max_attempts"))  # fmt: skip
        stubber = Stubber(client)
        stubber.add_response("get_caller_identity", IDENTITY, {})
        stubber.activate()

    backend(credential_loader=loader_for(TempCreds(), []), client_hook=hook).get_caller_identity(binding(), "us-west-2")
    assert seen == {"access_key": "ASIAFAKETEMPORARY001", "token": "FakeSessionTokenForTests", "region": "us-west-2",
                    "endpoint": "https://sts.us-west-2.amazonaws.com", "attempts": 2}  # fmt: skip
    assert no_network == []


def test_the_backend_source_has_no_default_session_and_no_other_operation():
    tree = ast.parse((REPO / "agent_factory" / "aws" / "boto.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "") for n in calls}
    assert not names & {"get_credentials", "create_credential_resolver", "get_session", "client", "resource",
                        "_make_api_call", "load_credentials", "assume_role", "get_session_token"}  # fmt: skip
    for n in calls:  # no dynamic dispatch: every getattr names one fixed attribute, never a caller's string
        if getattr(n.func, "id", "") == "getattr":
            assert isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)
            assert not (isinstance(n.args[0], ast.Name) and n.args[0].id == "client")
    # The backend offers exactly the interface's fixed operations: no generic "call", no extra SDK method.
    public = {name for name, value in vars(BotoAwsBackend).items() if callable(value) and not name.startswith("_")}
    assert public == {"credential_metadata", "get_caller_identity", "head_object"}
    client_calls = {n.func.attr for n in calls if isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "client"}  # fmt: skip
    assert client_calls == {"get_caller_identity"}
    created = [n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "create_client"]
    assert len(created) == 1 and created[0].args[0].value == "sts"
    sessions = [n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "Session"]
    assert sessions and all(any(k.arg == "profile" for k in n.keywords) for n in sessions)  # never a default session
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
        {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}  # fmt: skip
    assert not any(str(m).startswith("boto3") for m in imports)


@pytest.mark.parametrize("code,status,category", [
    ("AccessDenied", 403, "AWS_PERMISSION_DENIED"),
    ("ExpiredToken", 403, "AWS_EXPIRED_CREDENTIAL"),
    ("InvalidClientTokenId", 403, "AWS_IDENTITY_UNAVAILABLE"),
    ("SignatureDoesNotMatch", 403, "AWS_IDENTITY_UNAVAILABLE"),
    ("Throttling", 400, "AWS_THROTTLED"),
    ("RegionDisabledException", 403, "AWS_UNSUPPORTED_REGION"),
    ("InternalFailure", 500, "AWS_SERVICE_UNAVAILABLE"),
    ("SomethingNew", 400, "AWS_RESPONSE_INVALID"),
    ("SomethingNew", 503, "AWS_SERVICE_UNAVAILABLE"),
])  # fmt: skip
def test_aws_errors_map_to_fixed_categories_without_their_text(aws_home, no_network, caplog, code, status, category):
    hook, used, _ = stubbing(("get_caller_identity", code, status))
    be = backend(credential_loader=loader_for(TempCreds(), []), client_hook=hook)
    with pytest.raises(ToolError) as failed:
        AwsIdentityInspectTool(real_settings(be)).run(None)
    assert str(failed.value) == f"aws: {category}"
    assert "secret-name" not in caplog.text and "arn:aws" not in caplog.text and "is not authorized" not in caplog.text


@pytest.mark.parametrize("exc,category", [
    (botocore.exceptions.EndpointConnectionError(endpoint_url="https://sts.us-west-2.amazonaws.com"), "AWS_SERVICE_UNAVAILABLE"),
    (botocore.exceptions.ConnectTimeoutError(endpoint_url="x"), "AWS_SERVICE_UNAVAILABLE"),
    (botocore.exceptions.NoCredentialsError(), "AWS_IDENTITY_UNAVAILABLE"),
    (botocore.exceptions.UnauthorizedSSOTokenError(), "AWS_EXPIRED_CREDENTIAL"),
    (botocore.exceptions.ProfileNotFound(profile="x"), "AWS_NOT_CONFIGURED"),
    (botocore.exceptions.NoRegionError(), "AWS_UNSUPPORTED_REGION"),
    (RuntimeError("raw text with arn:aws:iam::123456789012:root"), "AWS_SERVICE_UNAVAILABLE"),
])  # fmt: skip
def test_sdk_exceptions_map_to_fixed_categories(exc, category):
    mapped = map_error(exc)
    assert mapped.category == category and "arn:aws" not in str(mapped) + mapped.detail


def test_an_invalid_response_is_refused(aws_home, no_network):
    hook, _, _ = stubbing(("get_caller_identity", {"UserId": "AROAFAKE:x", "Account": FAKE_ACCOUNT}))  # no Arn
    with pytest.raises(AwsBackendError) as invalid:
        backend(credential_loader=loader_for(TempCreds(), []), client_hook=hook).get_caller_identity(binding(), "us-west-2")
    assert invalid.value.category == "AWS_RESPONSE_INVALID"


# ------------------------------------------------------------------ availability, evidence, model, sandbox


def test_the_probe_with_the_real_backend_follows_the_designed_order(aws_home, no_network):
    hook, used, _ = stubbing(("get_caller_identity", IDENTITY))
    good = backend(credential_loader=loader_for(TempCreds(), []), client_hook=hook)
    reg = CapabilityRegistry((*local_packs(), AwsToolPack(real_settings(good))))
    assert reg.availability("aws.identity.inspect").status is AvailabilityStatus.AVAILABLE
    assert reg.availability("aws.s3.metadata.read").status is AvailabilityStatus.NOT_CONFIGURED  # S3 is Stage F
    kinds = {e.kind for e in reg.evidence("aws.identity.inspect")}
    assert EvidenceKind.AVAILABLE_NOW in kinds and EvidenceKind.LIVE_TESTED not in kinds  # available is not live-tested
    expired = backend(credential_loader=loader_for(TempCreds(expires_in=timedelta(minutes=-1)), []))
    assert CapabilityRegistry((*local_packs(), AwsToolPack(real_settings(expired)))).availability(
        "aws.identity.inspect").status is AvailabilityStatus.NO_IDENTITY  # fmt: skip
    hook3, _, _ = stubbing(("get_caller_identity", "AccessDenied", 403))
    denied = backend(credential_loader=loader_for(TempCreds(), []), client_hook=hook3)
    assert CapabilityRegistry((*local_packs(), AwsToolPack(real_settings(denied)))).availability(
        "aws.identity.inspect").status is AvailabilityStatus.PERMISSION_DENIED  # fmt: skip
    assert no_network == []


def test_no_profile_slot_account_or_credential_reaches_the_model_manifest(aws_home, no_network):
    hook, _, _ = stubbing(("get_caller_identity", IDENTITY))
    be = backend(credential_loader=loader_for(TempCreds(), []), client_hook=hook)
    reg = CapabilityRegistry((*local_packs(), AwsToolPack(real_settings(be))))
    policy = PermissionPolicy().with_agent_tools("developer", {"aws_identity_inspect"})
    text = json.dumps([manifest_for(a, policy, reg.tool_registry(), reg).to_dict() for a in ("planner", "developer")]
                      + [reg.view_for("developer", policy).to_dict()])  # fmt: skip
    assert "aws_identity_inspect" in text  # described, as available
    for secret in (PROFILE, "aws.dev.identity", FAKE_ACCOUNT, "arn:", "ASIAFAKE", "FakeSessionToken", "sts:",
                   ENV_STATIC_CANARY, "af-static"):  # fmt: skip
        assert secret not in text


def test_no_aws_credential_or_config_material_reaches_generated_code(monkeypatch):
    from agent_factory.providers.context import withheld
    from agent_factory.sandbox.validation import validate_env
    from agent_factory.tools.testing import DEFAULT_ENV_ALLOWLIST, DEFAULT_SANDBOX_ENV

    names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_CONFIG_FILE",
             "AWS_SHARED_CREDENTIALS_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
             "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_LOGIN_CACHE_DIRECTORY")  # fmt: skip
    for name in names:
        monkeypatch.setenv(name, "host-canary")
        assert validate_env({name: "x"}, DEFAULT_ENV_ALLOWLIST | {name}), name  # refused even if allowlisted
    assert not [k for k in (*DEFAULT_SANDBOX_ENV, *DEFAULT_ENV_ALLOWLIST) if k.upper().startswith("AWS")]
    assert "host-canary" not in json.dumps(dict(DEFAULT_SANDBOX_ENV))  # the sandbox env is built, never inherited
    for path in (".aws/credentials", ".aws/config", ".aws/sso/cache/abc.json", ".aws/login/cache/x.json"):
        assert withheld(path), path


# ------------------------------------------------------------------ the network guard itself


@pytest.mark.parametrize("address", [("169.254.169.254", 80), ("169.254.170.2", 80), ("sts.us-west-2.amazonaws.com", 443),
                                     ("aws-mcp.us-west-2.api.aws", 443), ("aws.amazon.com", 443)])  # fmt: skip
def test_the_suite_network_guard_refuses_aws_and_metadata_endpoints(address):
    with socket.socket() as s, pytest.raises(Exception, match="non-loopback"):
        s.connect(address)

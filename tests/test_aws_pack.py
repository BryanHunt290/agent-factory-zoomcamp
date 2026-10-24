"""AwsToolPack with the FAKE backend: typed capabilities, credential slots, redaction, fail closed (docs/aws-capabilities.md).

Offline only. No AWS API is called, no AWS profile is read, and nothing here imports an AWS SDK. The
conftest network guard refuses every non-loopback connection during these tests.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_factory.aws import (
    OPERATIONS,
    AwsBackend,
    AwsIdentityInspectTool,
    AwsPackSettings,
    AwsS3MetadataReadTool,
    AwsToolPack,
    CredentialMetadata,
    CredentialRefused,
    CredentialSlots,
    FakeAwsBackend,
    SlotBinding,
    aws_capabilities,
    check_credential,
    fake_object,
)
from agent_factory.aws.backend import FAKE_ACCOUNT, FAKE_SECRET_CANARY, FAKE_SESSION_CANARY
from agent_factory.aws.credentials import LONG_TERM_KINDS, TEMPORARY_KINDS
from agent_factory.aws.redaction import MAX_RESULT_BYTES, AwsResultRejected
from agent_factory.capabilities import (
    ApprovalRule,
    AvailabilityStatus,
    CapabilityRegistry,
    CostImpact,
    EvidenceKind,
    Requirement,
    assess,
)
from agent_factory.engine.actions import workspace_guard
from agent_factory.safety import ApprovalRequired, PermissionDenied
from agent_factory.safety.execution import EffectClass
from agent_factory.safety.permissions import ActionCategory, Decision, PermissionPolicy
from agent_factory.tools import ToolGateway
from agent_factory.tools.base import ToolError
from agent_factory.tools.packs import local_packs

REPO = Path(__file__).resolve().parents[1]
BUCKET, KEY = "af-dev-bucket", "reports/2026/summary.txt"
SLOTS = {"aws.dev.identity": "af-dev-sso", "aws.dev.readonly": "af-dev-sso"}


def settings(backend: FakeAwsBackend | None = None, **overrides) -> AwsPackSettings:
    backend = backend if backend is not None else FakeAwsBackend(objects={(BUCKET, KEY): fake_object()})
    values = dict(enabled=True, backend=backend, slots=CredentialSlots(SLOTS), region="us-west-2",
                  designated={BUCKET: "reports/"})  # fmt: skip
    values.update(overrides)
    return AwsPackSettings(**values)


def registry(pack: AwsToolPack) -> CapabilityRegistry:
    return CapabilityRegistry((*local_packs(), pack))


AWS_TOOLS = {"aws_identity_inspect", "aws_s3_metadata_read"}


def aws_policy(environment: str = "production") -> PermissionPolicy:
    """AWS tools are reachable only from an explicit allowlist. In production, every cloud read needs approval."""
    return PermissionPolicy.for_environment(environment).with_agent_tools("developer", AWS_TOOLS)


def gateway(ws, reg, policy, approvals=None, actions=None):
    tools = reg.tool_registry()
    return ToolGateway(registry=tools, policy=policy, workspace=ws, run_id="run1", node="developer", agent="developer",
                       visit=1, approvals=approvals or {}, on_action=(actions if actions is not None else []).append,
                       executions=workspace_guard(ws, "run1", registry=tools), capabilities=reg)  # fmt: skip


# ------------------------------------------------------------------ the two fake read capabilities


def test_identity_inspection_returns_a_bounded_summary_without_the_arn_or_secrets():
    backend = FakeAwsBackend()
    result = AwsIdentityInspectTool(settings(backend)).run(None).data
    assert result == {"principal_type": "assumed-role", "principal_name": "af-dev-identity", "account_hint": "********0000",
                      "region": "us-west-2", "backend": "fake"}  # fmt: skip
    text = json.dumps(result)
    for leaked in (FAKE_ACCOUNT, "arn:", "AROA", FAKE_SECRET_CANARY, FAKE_SESSION_CANARY, "fake-session"):
        assert leaked not in text
    assert backend.calls == [("GetCallerIdentity", "aws.dev.identity", "us-west-2", {})]


def test_s3_metadata_read_returns_only_known_fields_with_canaries_removed():
    backend = FakeAwsBackend(objects={(BUCKET, KEY): fake_object()})
    result = AwsS3MetadataReadTool(settings(backend)).run(None, bucket=BUCKET, key=KEY).data
    assert set(result) == {"bucket", "key", "size", "content_type", "etag", "last_modified", "encryption", "metadata"}
    assert result["size"] == 1234 and result["metadata"] == {"note": "key <redacted>", "owner": "data-team"}
    text = json.dumps(result)
    for leaked in (FAKE_SECRET_CANARY, FAKE_SESSION_CANARY, FAKE_ACCOUNT, "SSEKMSKeyId", "arn:", "ResponseMetadata",
                   "x-session-token"):  # fmt: skip
        assert leaked not in text
    assert len(text) <= MAX_RESULT_BYTES
    assert backend.calls == [("HeadObject", "aws.dev.readonly", "us-west-2", {"bucket": BUCKET, "key": KEY})]


def test_unexpected_structures_and_oversized_results_are_rejected():
    for bad in (fake_object(ContentLength="1234"), fake_object(ContentLength=True), fake_object(Metadata=["x"]),
                {"ContentType": "text/plain"}):  # fmt: skip
        backend = FakeAwsBackend(objects={(BUCKET, KEY): bad})
        with pytest.raises(AwsResultRejected):
            AwsS3MetadataReadTool(settings(backend)).run(None, bucket=BUCKET, key=KEY)
    huge = fake_object(Metadata={f"field{i:02d}": "v" * 199 for i in range(20)}, ContentType="t" * 199,
                       ETag="e" * 199, LastModified="l" * 199)  # fmt: skip
    backend = FakeAwsBackend(objects={(BUCKET, KEY): huge})
    with pytest.raises(AwsResultRejected, match="too large"):
        AwsS3MetadataReadTool(settings(backend)).run(None, bucket=BUCKET, key=KEY)
    backend = FakeAwsBackend(objects={(BUCKET, KEY): "not a mapping"})  # type: ignore[dict-item]
    with pytest.raises(AwsResultRejected):
        AwsS3MetadataReadTool(settings(backend)).run(None, bucket=BUCKET, key=KEY)


def test_only_designated_buckets_and_prefixes_are_read_and_checked_before_any_call():
    backend = FakeAwsBackend(objects={(BUCKET, KEY): fake_object()})
    tool = AwsS3MetadataReadTool(settings(backend))
    for bucket, key in ((BUCKET, "private/keys.txt"), ("someone-elses-bucket", KEY), ("Bad_Bucket", KEY),
                        (BUCKET, ""), (BUCKET, "reports/\x00x"), (BUCKET, "reports/" + "k" * 1100)):  # fmt: skip
        with pytest.raises(ToolError):
            tool.run(None, bucket=bucket, key=key)
    assert backend.calls == []


@pytest.mark.parametrize("backend,message", [
    (FakeAwsBackend(identity=False), "AWS_IDENTITY_UNAVAILABLE"),
    (FakeAwsBackend(denied=frozenset({"HeadObject"})), "AWS_PERMISSION_DENIED"),
    (FakeAwsBackend(regions=frozenset({"eu-west-1"})), "AWS_UNSUPPORTED_REGION"),
    (FakeAwsBackend(), "AWS_NOT_FOUND"),
])  # fmt: skip
def test_backend_failures_map_to_fixed_categories(backend, message):
    with pytest.raises(ToolError, match=f"^aws: {message}$"):
        AwsS3MetadataReadTool(settings(backend)).run(None, bucket=BUCKET, key=KEY)


# ------------------------------------------------------------------ availability and the registry


@pytest.mark.parametrize("pack,identity,s3", [
    (AwsToolPack(), "not_configured", "not_configured"),  # not enabled
    (AwsToolPack(AwsPackSettings(enabled=False, backend=FakeAwsBackend())), "not_configured", "not_configured"),
    (AwsToolPack(AwsPackSettings(enabled=True, backend=None)), "not_configured", "not_configured"),
    (AwsToolPack(settings()), "available", "available"),
    (AwsToolPack(settings(FakeAwsBackend(identity=False))), "no_identity", "no_identity"),
    (AwsToolPack(settings(FakeAwsBackend(denied=frozenset({"GetCallerIdentity"})))), "permission_denied", "permission_denied"),
    (AwsToolPack(settings(FakeAwsBackend(regions=frozenset({"eu-west-1"})))), "unsupported_region", "unsupported_region"),
    (AwsToolPack(settings(FakeAwsBackend(credential_kind="shared-credentials-file"))), "no_identity", "no_identity"),
    (AwsToolPack(settings(FakeAwsBackend(credential_kind="iam-role"))), "no_identity", "no_identity"),  # never direct
    (AwsToolPack(settings(FakeAwsBackend(expires_in=timedelta(seconds=30)))), "no_identity", "no_identity"),
    (AwsToolPack(settings(FakeAwsBackend(expires_in=None))), "no_identity", "no_identity"),
    (AwsToolPack(settings(slots=CredentialSlots({}))), "not_configured", "not_configured"),
    (AwsToolPack(settings(slots=CredentialSlots({"aws.dev.identity": "af-dev-sso"}))), "available", "not_configured"),
    (AwsToolPack(settings(designated={})), "available", "not_configured"),
])  # fmt: skip
def test_the_registry_represents_every_availability_state(pack, identity, s3):
    reg = registry(pack)
    assert reg.availability("aws.identity.inspect").status.value == identity
    assert reg.availability("aws.s3.metadata.read").status.value == s3
    assert reg.availability("aws.deploy.execute").status.value in ("not_configured", "not_implemented", identity)
    kinds = {e.kind for e in reg.evidence("aws.identity.inspect")}
    assert EvidenceKind.IMPLEMENTED in kinds and EvidenceKind.LIVE_TESTED not in kinds  # never live-tested in Stage D
    assert (EvidenceKind.AVAILABLE_NOW in kinds) == (identity == "available")
    assert (EvidenceKind.UNAVAILABLE in kinds) == (identity != "available")


def test_a_real_backend_is_not_accepted_before_stage_e_and_boto3_being_importable_changes_nothing():
    pytest.importorskip("boto3")  # installed globally on the reference machine; importing it makes no call

    class NotFake(FakeAwsBackend):
        kind = "boto"

    pack = AwsToolPack(settings(NotFake()))
    reg = registry(pack)
    assert reg.availability("aws.identity.inspect").status is AvailabilityStatus.NOT_CONFIGURED
    with pytest.raises(ToolError, match="AWS_NOT_CONFIGURED"):
        AwsIdentityInspectTool(settings(NotFake())).run(None)
    assert registry(AwsToolPack()).availability("aws.identity.inspect").status is AvailabilityStatus.NOT_CONFIGURED


# ------------------------------------------------------------------ credential slots: temporary only, no fallback


def test_long_term_keys_and_unknown_kinds_are_refused_and_nothing_falls_back():
    binding = SlotBinding("aws.dev.readonly", "af-dev-sso")
    hour = datetime.now(timezone.utc) + timedelta(hours=1)
    for kind in sorted(LONG_TERM_KINDS) + ["custom-process", "iam-role", "container-role", "", "SSO", "unknown"]:
        with pytest.raises(CredentialRefused):
            check_credential(binding, CredentialMetadata("aws.dev.readonly", "af-dev-sso", kind, True, hour))
    for kind in sorted(TEMPORARY_KINDS):
        check_credential(binding, CredentialMetadata("aws.dev.readonly", "af-dev-sso", kind, True, hour))
        with pytest.raises(CredentialRefused):  # a temporary kind that is not actually temporary
            check_credential(binding, CredentialMetadata("aws.dev.readonly", "af-dev-sso", kind, False, hour))
        for expiry in (None, datetime.now(timezone.utc) - timedelta(minutes=1), datetime.now(timezone.utc) + timedelta(seconds=30),
                       datetime.now() + timedelta(hours=1)):  # unknown, expired, about to expire, naive time  # fmt: skip
            with pytest.raises(CredentialRefused, match="expire"):
                check_credential(binding, CredentialMetadata("aws.dev.readonly", "af-dev-sso", kind, True, expiry))
    with pytest.raises(CredentialRefused, match="no fallback"):  # a credential from another profile
        check_credential(binding, CredentialMetadata("aws.dev.readonly", "some-other-profile", "sso", True, hour))
    for profile in ("default", "internal-dev"):  # out of scope by the owner's decision
        with pytest.raises(CredentialRefused, match="out of scope"):
            SlotBinding("aws.dev.readonly", profile)
    for slot in ("default", "aws", "gcp.dev.readonly", "aws.test.readonly"):
        with pytest.raises(CredentialRefused):
            SlotBinding(slot, "af-dev-sso")
    with pytest.raises(CredentialRefused, match="not configured"):  # an unbound slot never means "the default"
        CredentialSlots({}).resolve("aws.dev.readonly")
    static = FakeAwsBackend(credential_kind="shared-credentials-file", objects={(BUCKET, KEY): fake_object()})
    with pytest.raises(ToolError, match="long-term access keys are refused"):
        AwsS3MetadataReadTool(settings(static)).run(None, bucket=BUCKET, key=KEY)
    assert static.calls == []  # refused before any operation


def test_no_aws_profile_or_environment_is_ever_read(monkeypatch, tmp_path):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                 "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):  # fmt: skip
        monkeypatch.setenv(name, str(tmp_path / "must-not-be-read"))
    for path in (REPO / "agent_factory" / "aws").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"environ", "getenv", "expanduser", "home", "read_text", "open"}, path.name
            if isinstance(node, ast.Name):
                assert node.id not in {"environ", "getenv", "open"}, path.name
    pack = AwsToolPack(settings())
    reg = registry(pack)  # probes run, tools are built: nothing reaches the environment or a file
    assert reg.availability("aws.identity.inspect").available
    assert not (tmp_path / "must-not-be-read").exists()


# ------------------------------------------------------------------ no arbitrary execution path


def test_only_typed_operations_exist_and_no_tool_accepts_an_operation_name():
    abstract = {n for n, m in inspect.getmembers(AwsBackend) if getattr(m, "__isabstractmethod__", False)}
    assert abstract == {"credential_metadata", "get_caller_identity", "head_object"}
    assert OPERATIONS == {("sts", "GetCallerIdentity"), ("s3", "HeadObject")}
    public = {n for n in dir(FakeAwsBackend) if not n.startswith("_") and callable(getattr(FakeAwsBackend, n))}
    assert public == {"credential_metadata", "get_caller_identity", "head_object"}
    for tool in AwsToolPack(settings()).tools():
        params = list(inspect.signature(tool.run).parameters.values())[1:]
        assert all(p.kind is p.KEYWORD_ONLY for p in params), tool.name  # no **kwargs, no positional tricks
        assert not {p.name for p in params} & {"operation", "method", "service", "command", "script", "args", "code"}


def test_no_module_imports_an_aws_sdk_or_runs_a_process_or_a_shell():
    forbidden_imports = {"boto3", "botocore", "aiobotocore", "subprocess", "pty", "shutil", "mcp", "mcp_proxy_for_aws",
                         "requests", "urllib", "http", "socket"}  # fmt: skip
    sdk_users = {}
    for folder in ("aws", "capabilities"):
        for path in (REPO / "agent_factory" / folder).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = {(node.module or "").split(".")[0]}
                else:
                    names = set()
                if "botocore" in names:
                    sdk_users.setdefault(path.name, 0)
                    sdk_users[path.name] += 1
                    names = names - {"botocore"}  # allowed only in boto.py (checked below), never boto3
                assert not names & forbidden_imports, (path.name, names)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in {"exec", "eval", "compile", "__import__"}, path.name
                if isinstance(node, ast.Attribute):
                    assert node.attr not in {"system", "popen", "Popen", "spawn", "startfile"}, path.name
    for ref in (c.capability_id for c in aws_capabilities()):
        assert not set(ref.split(".")) & {"cli", "shell", "script", "run_script", "exec", "execute_any", "call"}, ref
    assert set(sdk_users) == {"boto.py"}  # the real backend alone touches the SDK, and only botocore


# ------------------------------------------------------------------ cloud_read policy, approval, ActionExecution, placeholder


def test_cloud_read_runs_in_development_and_needs_approval_through_the_action_guard_in_production(harness):
    reg = registry(AwsToolPack(settings()))
    ws = harness.workspace
    with pytest.raises(PermissionDenied):  # no agent has AWS tools on its allowlist by default
        gateway(ws, reg, PermissionPolicy()).call_capability("aws.identity.inspect")
    # Development: allowed for an agent that has the tool, with no approval and no ledger entry needed.
    dev_actions: list[dict] = []
    result = gateway(ws, reg, aws_policy("development"), actions=dev_actions).call_capability("aws.identity.inspect")
    assert result.data["principal_type"] == "assumed-role" and "approval_id" not in dev_actions[-1]
    # Staging: configurable (approval by default), never looser than its setting.
    with pytest.raises(ApprovalRequired):
        gateway(ws, reg, aws_policy("staging")).call_capability("aws.identity.inspect")
    staging_open = PermissionPolicy.for_environment("staging", staging_cloud_read=Decision.ALLOW).with_agent_tools("developer", AWS_TOOLS)
    gateway(ws, reg, staging_open).call_capability("aws.identity.inspect")
    # Production: approval, then only through the ActionGuard.
    with pytest.raises(ApprovalRequired) as asked:
        gateway(ws, reg, aws_policy()).call_capability("aws.s3.metadata.read", bucket=BUCKET, key=KEY)
    request = asked.value.request
    assert (request.tool, request.category, request.params) == ("aws_s3_metadata_read", "cloud_read",
                                                                {"bucket": BUCKET, "key": KEY})  # fmt: skip
    approvals = {request.request_id: {"approved": True}}
    actions: list[dict] = []
    result = gateway(ws, reg, aws_policy(), approvals, actions).call_capability("aws.s3.metadata.read", bucket=BUCKET, key=KEY)
    assert result.data["size"] == 1234 and actions[-1]["approval_id"] == request.request_id
    with pytest.raises(ApprovalRequired):  # approval is bound to the exact parameters
        gateway(ws, reg, aws_policy(), approvals).call_capability("aws.s3.metadata.read", bucket=BUCKET, key="reports/other")
    # No extra parameter can smuggle an operation in: it is a different action (a new approval), and even
    # approved, the typed tool refuses it before anything is recorded or sent.
    smuggled = {"bucket": BUCKET, "key": KEY, "operation": "DeleteObject"}
    with pytest.raises(ApprovalRequired) as asked_again:
        gateway(ws, reg, aws_policy(), approvals).call_capability("aws.s3.metadata.read", **smuggled)
    both = {**approvals, asked_again.value.request.request_id: {"approved": True}}
    calls_before = len(reg._packs["aws"]._settings.backend.calls)  # type: ignore[attr-defined]
    with pytest.raises(ToolError, match="bad parameters"):
        gateway(ws, reg, aws_policy(), both, actions).call_capability("aws.s3.metadata.read", **smuggled)
    assert len(reg._packs["aws"]._settings.backend.calls) == calls_before  # type: ignore[attr-defined]


def test_the_mutation_placeholder_is_classified_and_never_executable(harness):
    reg = registry(AwsToolPack(settings()))
    deploy = reg.get("aws.deploy.execute")
    assert (deploy.mutates, deploy.approval, deploy.cost, deploy.category, deploy.effect) == (
        True, ApprovalRule.ALWAYS, CostImpact.RECURRING, ActionCategory.PRODUCTION_DEPLOY, EffectClass.NON_IDEMPOTENT)
    assert deploy.implemented_by is None and not deploy.executable
    assert reg.availability("aws.deploy.execute").status is AvailabilityStatus.NOT_IMPLEMENTED
    assert EvidenceKind.IMPLEMENTED not in {e.kind for e in reg.evidence("aws.deploy.execute")}
    assert reg.requires_approval(deploy, PermissionPolicy())
    policy = aws_policy().with_agent_tools("developer", {"aws_identity_inspect", "aws_s3_metadata_read", "aws_deploy_execute"})
    with pytest.raises(PermissionDenied, match="no implementation"):
        gateway(harness.workspace, reg, policy).call_capability("aws.deploy.execute", stack="app", change_set="cs1")
    assert "aws_deploy_execute" not in reg.tool_registry().names()
    report = assess(reg, [Requirement("deploy", "aws.deploy.execute")]).to_dict()
    assert report["unavailable"] == [{"requirement": "deploy", "capability": "aws.deploy.execute", "reason": "not_implemented"}]


def test_every_aws_definition_documents_exact_iam_actions_and_an_abstract_slot():
    by_id = {c.capability_id: c for c in aws_capabilities()}
    assert by_id["aws.identity.inspect"].iam_actions == ("sts:GetCallerIdentity",)
    assert by_id["aws.s3.metadata.read"].iam_actions == ("s3:GetObject", "s3:ListBucket")
    for c in by_id.values():
        assert c.credential and c.credential.startswith("aws.dev.") and not c.sandbox_compatible
        assert all("*" not in a for a in c.iam_actions)


# ------------------------------------------------------------------ the model boundary


def test_aws_capabilities_never_become_model_tools_and_no_credential_reaches_a_request(harness, monkeypatch):
    from test_anthropic_compat import Loopback, scripted
    from test_model_providers import CANARY_KEY, FakeMessagesApi

    from agent_factory.factory import build_executor
    from agent_factory.state import RunStatus

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-env-canary-7d1f")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "aws-env-session-canary-7d1f")
    api = FakeMessagesApi()
    try:
        scripted(api, {})
        provider = Loopback(model="claude-sonnet-5-5", api_key=CANARY_KEY, base_url=api.url, timeout_s=5)
        ex = build_executor(harness.template, provider, harness.workspace, extra_packs=(AwsToolPack(settings()),))
        state = ex.start(harness.template.task, run_id="run-1")
        assert state.status is RunStatus.COMPLETED and len(api.received) == 3
        for sent in api.received:
            body = sent["body"]
            assert [t["name"] for t in body["tools"]] == ["submit_result"] and body["tool_choice"] == {"type": "auto"}
            text = json.dumps(body)
            for secret in ("aws-env-canary-7d1f", "aws-env-session-canary-7d1f", FAKE_SECRET_CANARY, FAKE_SESSION_CANARY,
                           FAKE_ACCOUNT, "af-dev-sso", "aws.dev.identity", "aws.dev.readonly", BUCKET):  # fmt: skip
                assert secret not in text
            manifest = json.loads(body["system"].rsplit("\n", 1)[1])
            assert not {"aws_identity_inspect", "aws_s3_metadata_read"} & {c["tool"] for c in manifest["available"]}
    finally:
        api.close()


def test_aws_credentials_never_enter_the_sandbox_environment():
    from agent_factory.sandbox.validation import validate_env
    from agent_factory.tools.testing import DEFAULT_ENV_ALLOWLIST

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION"):
        problems = validate_env({name: "x"}, DEFAULT_ENV_ALLOWLIST | {name})  # even if someone allowlisted it
        assert problems, name

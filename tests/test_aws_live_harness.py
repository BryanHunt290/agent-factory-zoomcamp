"""Stage E2a: the AWS live verification harness, tested OFFLINE (docs/aws-capabilities.md, section 10).

No AWS API is called and no AWS credential is read. Every test uses a synthetic AWS config in a temporary
home (the real ~/.aws is never read), botocore's Stubber in place of HTTP, and network guards whose
underlying resolver and connector are fakes. The harness itself (``tests/aws_live_support.py``) is the code
the opt-in live test (``tests/test_aws_live.py``) runs; here it is driven through the same product path:
Capability Registry -> Tool Gateway -> aws.identity.inspect -> BotoAwsBackend -> evidence writer.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

botocore = pytest.importorskip("botocore")
from botocore import credentials as bc  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

import aws_live_support as harness  # noqa: E402
from agent_factory.aws import AwsIdentityInspectTool  # noqa: E402
from agent_factory.capabilities import CapabilityRegistry, EvidenceKind  # noqa: E402
from agent_factory.capabilities.live_evidence import EVIDENCE_PATH, load_live_evidence  # noqa: E402
from agent_factory.tools.gateway import ToolGateway  # noqa: E402
from aws_live_support import (  # noqa: E402
    APPROVED_PLAN,
    LIVE_HOSTS,
    AwsLiveNetworkGuard,
    LivePlan,
    LiveVerificationFailed,
    NetworkRefused,
)
from conftest import AWS_LIVE_OPTION, OPT_IN_SUITES, ForbiddenOperation  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROFILE = "agent-factory-dev"
ACCOUNT = "123456789012"
ROLE = "AWSReservedSSO_AgentFactoryIdentity_0123456789abcdef"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE}/alex@example.com"
USER_ID = "AROAFAKEFAKEFAKEFAKE:alex@example.com"
IDENTITY = {"UserId": USER_ID, "Account": ACCOUNT, "Arn": ARN}
COMMIT = "f8061e89924845b55fc617f747cebcc653a1c889"
SSO = f"""
[profile {PROFILE}]
sso_session = af
sso_account_id = 000000000000
sso_role_name = AgentFactoryIdentity
region = us-west-2
[sso-session af]
sso_start_url = https://example.awsapps.com/start
sso_region = us-west-2
sso_registration_scopes = sso:account:access
[default]
sso_session = af
sso_account_id = 000000000000
sso_role_name = WouldWorkIfFallenBackTo
[profile internal-dev]
sso_session = af
sso_account_id = 000000000000
sso_role_name = OutOfScope
"""
STATIC = f"\n[profile {PROFILE}]\naws_access_key_id = AKIAFILESTATICCANARY1\naws_secret_access_key = not-a-real-secret\n"
PROCESS = f"\n[profile {PROFILE}]\ncredential_process = this-must-never-run\n"
NO_PROFILE = "\n[default]\nsso_session = af\nsso_account_id = 000000000000\nsso_role_name = R\n" \
             "[sso-session af]\nsso_start_url = https://example.awsapps.com/start\nsso_region = us-west-2\n"  # fmt: skip


def write_config(home: Path, text: str) -> None:
    (home / ".aws" / "config").write_text(text, encoding="utf-8")


@pytest.fixture
def aws_home(tmp_path, monkeypatch):
    """A synthetic AWS home with an IAM Identity Center profile. No static key variable is set here."""
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    write_config(home, SSO)
    (home / ".aws" / "credentials").write_text(
        f"[{PROFILE}]\naws_access_key_id = AKIAFILESTATICCANARY1\naws_secret_access_key = x\n", encoding="utf-8")
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / ".aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))
    for name in harness.STATIC_KEY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_PROFILE", "default")  # must be ignored: the profile is always named explicitly
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://attacker.invalid")  # must be ignored
    return home


class TempCreds:
    """A resolved temporary credential as the SSO provider would return it (offline): never a real key."""

    def __init__(self, method="sso", expires_in=timedelta(hours=1)):
        self.method = method
        self._expiry_time = None if expires_in is None else datetime.now(timezone.utc) + expires_in

    def get_frozen_credentials(self):
        return bc.ReadOnlyCredentials("ASIAFAKETEMPORARY001", "fake-temporary-secret", "FakeSessionTokenForTests")


class Offline:
    """The harness's test hooks: a credential loader and an STS client hook answering from Stubber.

    ``sts_calls`` counts every GetCallerIdentity the backend sent to its (stubbed) client.
    """

    def __init__(self, *, creds=None, answer=IDENTITY, error=None, before_call=None):
        self.creds = TempCreds() if creds is None else creds
        self.answer, self.error, self.before_call = answer, error, before_call
        self.loads: list[tuple[str, str]] = []
        self.sts_calls = 0
        self.endpoints: list[str] = []

    def load(self, session, profile, kind):
        self.loads.append((profile, kind))
        return self.creds

    def hook(self, client):
        self.endpoints.append(client.meta.endpoint_url)

        def count(**_):
            self.sts_calls += 1
            if self.before_call:
                self.before_call()

        client.meta.events.register("before-parameter-build.sts.GetCallerIdentity", count)
        stubber = Stubber(client)
        if self.error:
            stubber.add_client_error("get_caller_identity", service_error_code=self.error[0],
                                     service_message=f"User: {ARN} is not authorized", http_status_code=self.error[1])  # fmt: skip
        else:
            stubber.add_response("get_caller_identity", self.answer, {})
        stubber.activate()

    def hooks(self, *, loader=True):
        return {"client_hook": self.hook, **({"credential_loader": self.load} if loader else {})}


def offline_guard():
    """A guard whose resolver and connector are fakes: proves the allowlist without a single packet."""
    resolved, connected = [], []

    def resolve(host, port, *a, **k):
        resolved.append((host, port))
        # Public-looking addresses that are never contacted: the fake connector only records.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("52.119.0.10" if "sts" in str(host) else "52.119.0.20", port))]

    def connect(sock, address):
        connected.append(address)

    guard = AwsLiveNetworkGuard(resolve=resolve, connect=connect)
    guard.fake_resolved, guard.fake_connected = resolved, connected
    return guard


def authorization():
    return harness.authorization_from_environment({harness.AUTHORIZED_BY_VARIABLE: "owner, test only, Stage E2a"})


def verify(tmp_path, offline, *, plan=APPROVED_PLAN, guard=None, log_text=lambda: "", evidence=None, loader=True):
    return harness.run_identity_verification(
        authorization=authorization(), commit=COMMIT, evidence_path=evidence or tmp_path / "evidence" / "live.jsonl",
        guard=guard or offline_guard(), workspace_root=tmp_path / "ws", log_text=log_text, plan=plan,
        test_hooks=offline.hooks(loader=loader))  # fmt: skip


def preflight(tmp_path, *, environ=None, dirty=False, commit=COMMIT, plan=APPROVED_PLAN, evidence=None):
    harness.preflight(plan, environ=environ or {}, evidence_path=evidence or tmp_path / "evidence" / "live.jsonl",
                      commit=commit, dirty=dirty)  # fmt: skip


# ------------------------------------------------------------------ opt-in: zero AWS calls without the flag


def test_the_live_test_is_opt_in_and_the_only_test_in_its_file():
    assert OPT_IN_SUITES["aws_live"] == AWS_LIVE_OPTION == "--run-aws-live"
    source = (REPO / "tests" / "test_aws_live.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    tests = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert tests == ["test_identity_inspection_live"] and harness.SUITE.endswith("::test_identity_inspection_live")
    marks = [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in n.targets)]
    assert len(marks) == 1 and "pytest.mark.aws_live" in ast.unparse(marks[0].value)
    import test_aws_live

    assert [m.name for m in test_aws_live.pytestmark] == ["aws_live"]


def test_without_the_flag_the_live_test_is_skipped_and_never_reaches_aws(request):
    # This run did not pass --run-aws-live: the live test is collected as skipped, and this process has the
    # default guard (no non-loopback name, no non-loopback connection), never the AWS allowlist guard.
    if request.config.getoption(AWS_LIVE_OPTION):
        pytest.skip("only meaningful without --run-aws-live")
    assert socket.getaddrinfo.__name__ == "_guarded_getaddrinfo"
    assert not hasattr(request.node, "aws_live_guard")


@pytest.mark.parametrize("host", sorted(LIVE_HOSTS) + ["sts.amazonaws.com", "s3.us-west-2.amazonaws.com",
                                                      "aws-mcp.us-west-2.api.aws", "instance-data.ec2.internal"])  # fmt: skip
def test_the_default_suite_guard_refuses_to_resolve_aws_names(host):
    for resolve in (lambda: socket.getaddrinfo(host, 443), lambda: socket.gethostbyname(host),
                    lambda: socket.create_connection((host, 443), timeout=1)):  # fmt: skip
        with pytest.raises(ForbiddenOperation, match="non-loopback name"):
            resolve()


@pytest.mark.parametrize("address", [("169.254.169.254", 80), ("169.254.170.2", 80), ("fd00:ec2::254", 80),
                                     ("52.94.0.1", 443)])  # fmt: skip
def test_the_default_suite_guard_refuses_metadata_and_aws_addresses(address):
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    with socket.socket(family) as s, pytest.raises(ForbiddenOperation, match="non-loopback"):
        s.connect(address)


def test_loopback_names_still_resolve_under_the_default_guard():
    assert socket.getaddrinfo("127.0.0.1", 80) and socket.getaddrinfo("localhost", 80)


# ------------------------------------------------------------------ the live network allowlist


def test_the_live_allowlist_is_exactly_three_us_west_2_hosts_on_443():
    assert LIVE_HOSTS == {"sts.us-west-2.amazonaws.com", "portal.sso.us-west-2.amazonaws.com",
                          "oidc.us-west-2.amazonaws.com"}  # fmt: skip
    assert AwsLiveNetworkGuard(resolve=None, connect=None).allowed_hosts == LIVE_HOSTS
    assert harness.LIVE_PORT == 443


def test_the_live_guard_allows_only_allowlisted_names_and_the_addresses_they_resolved_to():
    guard = offline_guard()
    for host in sorted(LIVE_HOSTS):
        guard.getaddrinfo(host, 443)
    guard.guarded_connect(None, ("52.119.0.10", 443))
    assert guard.fake_connected == [("52.119.0.10", 443)] and guard.refused == []
    refusals = [
        lambda: guard.getaddrinfo("s3.us-west-2.amazonaws.com", 443),  # another AWS service
        lambda: guard.getaddrinfo("sts.amazonaws.com", 443),  # the global STS endpoint
        lambda: guard.getaddrinfo("sts.us-east-1.amazonaws.com", 443),  # another region
        lambda: guard.getaddrinfo("aws-mcp.us-west-2.api.aws", 443),  # the AWS MCP server
        lambda: guard.getaddrinfo("attacker.invalid", 443),
        lambda: guard.getaddrinfo("sts.us-west-2.amazonaws.com", 80),  # an allowed name on another port
        lambda: guard.getaddrinfo(None, 443),
        lambda: guard.gethostbyname("sts.us-west-2.amazonaws.com"),
        lambda: guard.guarded_connect(None, ("169.254.169.254", 80)),  # instance metadata
        lambda: guard.guarded_connect(None, ("169.254.170.2", 80)),  # container credentials
        lambda: guard.guarded_connect(None, ("fd00:ec2::254", 80)),  # instance metadata over IPv6
        lambda: guard.guarded_connect(None, ("52.119.0.10", 80)),  # a resolved address on another port
        lambda: guard.guarded_connect_ex(None, ("203.0.113.9", 443)),  # an address no allowed name resolved to
        lambda: guard.guarded_connect(None, "/var/run/socket"),
    ]  # fmt: skip
    for refuse in refusals:
        with pytest.raises(NetworkRefused):
            refuse()
    assert len(guard.refused) == len(refusals)
    assert len(guard.fake_resolved) == 3 and guard.fake_connected == [("52.119.0.10", 443)]  # nothing refused got through


@pytest.mark.parametrize("address", ["169.254.169.254", "169.254.170.2", "10.0.0.5", "127.0.0.1", "fd00:ec2::254"])
def test_the_live_guard_refuses_an_allowed_name_resolving_to_a_private_or_metadata_address(address):
    guard = AwsLiveNetworkGuard(resolve=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
                                connect=lambda sock, a: pytest.fail("must not connect"))  # fmt: skip
    with pytest.raises(NetworkRefused, match="non-public"):
        guard.getaddrinfo("sts.us-west-2.amazonaws.com", 443)
    with pytest.raises(NetworkRefused):
        guard.guarded_connect(None, (address, 443))
    assert guard.addresses == set() and len(guard.refused) == 2


def test_the_installed_live_guard_covers_every_socket_entry_point(monkeypatch):
    guard = offline_guard()
    guard.install(monkeypatch)
    for attempt in (lambda: socket.create_connection(("169.254.169.254", 80), timeout=1),
                    lambda: socket.create_connection(("s3.amazonaws.com", 443), timeout=1),
                    lambda: socket.getaddrinfo("aws-mcp.us-west-2.api.aws", 443),
                    lambda: socket.gethostbyname("169.254.170.2"),
                    lambda: socket.gethostbyname_ex("sts.us-west-2.amazonaws.com")):  # fmt: skip
        with pytest.raises(NetworkRefused):
            attempt()
    with socket.socket() as s, pytest.raises(NetworkRefused):
        s.connect(("169.254.169.254", 80))
    with socket.socket() as s, pytest.raises(NetworkRefused):
        s.connect_ex(("169.254.170.2", 80))
    assert len(guard.refused) == 7 and guard.fake_resolved == [] and guard.fake_connected == []


def test_the_live_suite_may_start_no_process_but_its_two_git_reads(monkeypatch):
    import subprocess

    import conftest

    started = []
    monkeypatch.setattr(conftest, "_real_popen_init", lambda self, args, *a, **k: started.append(args))
    for argv in (["aws", "sso", "login", "--profile", PROFILE], ["aws", "sts", "get-caller-identity"],
                 ["docker", "ps"], ["git", "-C", str(conftest.REPO_ROOT), "push"], ["git", "status", "--porcelain"],
                 "aws configure sso"):  # fmt: skip
        with pytest.raises(ForbiddenOperation, match="no process"):
            conftest._aws_live_popen_init(subprocess.Popen.__new__(subprocess.Popen), argv)
    for command in harness.GIT_COMMANDS:
        conftest._aws_live_popen_init(None, harness.git_argv(conftest.REPO_ROOT, command))
    assert started == [harness.git_argv(conftest.REPO_ROOT, c) for c in harness.GIT_COMMANDS]


def test_the_requested_live_suite_installs_the_allowlist_guard_and_the_process_guard(monkeypatch):
    # What the conftest does for a requested aws_live test, exercised here without --run-aws-live: the real
    # resolver is wrapped (never called below), and every refusal is recorded.
    import subprocess

    import conftest

    guard = conftest.install_aws_live_guards(monkeypatch)
    assert guard.resolve is conftest._real_getaddrinfo and guard.connect is conftest._real_connect
    assert guard.allowed_hosts == LIVE_HOSTS
    for attempt in (lambda: socket.getaddrinfo("s3.us-west-2.amazonaws.com", 443),
                    lambda: socket.create_connection(("169.254.169.254", 80), timeout=1),
                    lambda: socket.create_connection(("169.254.170.2", 80), timeout=1),
                    lambda: socket.getaddrinfo("aws-mcp.us-west-2.api.aws", 443)):  # fmt: skip
        with pytest.raises(NetworkRefused):
            attempt()
    with pytest.raises(ForbiddenOperation, match="no process"):
        subprocess.Popen(["aws", "sso", "login", "--profile", PROFILE])
    assert len(guard.refused) == 4 and guard.resolved == guard.connected == []


# ------------------------------------------------------------------ the product path, offline


def test_a_verification_runs_registry_gateway_capability_backend_then_writes_one_safe_record(tmp_path, aws_home, monkeypatch):
    path = []  # the order in which the product path ran
    real_call_capability, real_call, real_run = ToolGateway.call_capability, ToolGateway.call, AwsIdentityInspectTool.run

    def call_capability(self, capability_id, **params):
        path.append(("gateway.call_capability", capability_id, params))
        return real_call_capability(self, capability_id, **params)

    def call(self, tool_name, **params):
        path.append(("gateway.call", tool_name))
        return real_call(self, tool_name, **params)

    def run(self, workspace):
        assert ("gateway.call", "aws_identity_inspect") in path, "the tool ran outside the Tool Gateway"
        path.append(("tool.run", type(self._settings.backend).__name__))
        return real_run(self, workspace)

    monkeypatch.setattr(ToolGateway, "call_capability", call_capability)
    monkeypatch.setattr(ToolGateway, "call", call)
    monkeypatch.setattr(AwsIdentityInspectTool, "run", run)
    offline = Offline()
    guard = offline_guard()
    evidence = tmp_path / "evidence" / "live.jsonl"
    report = verify(tmp_path, offline, guard=guard, evidence=evidence)

    assert path == [("gateway.call_capability", "aws.identity.inspect", {}), ("gateway.call", "aws_identity_inspect"),
                    ("tool.run", "BotoAwsBackend")]  # fmt: skip
    assert offline.sts_calls == 2  # the probe, then the capability: nothing else
    assert offline.endpoints == ["https://sts.us-west-2.amazonaws.com"] * 2  # the configured override was ignored
    assert offline.loads == [(PROFILE, "sso")]  # one credential resolution, from exactly the approved profile
    assert report.availability == "available" and report.principal_type == "assumed-role"
    assert report.gateway_records == ({"tool": "aws_identity_inspect", "category": "cloud_read", "decision": "allow",
                                       "params": {}, "result": "ok", "summary": "aws identity: assumed-role (boto backend)"},)  # fmt: skip
    assert guard.resolved == guard.connected == guard.refused == []  # offline: the stub answered, nothing was contacted

    (record,) = load_live_evidence(evidence)
    assert (record.capability_id, record.version, record.backend, record.environment, record.commit) == (
        "aws.identity.inspect", 1, "boto", "dev", COMMIT)
    assert record.verification_id == report.verification_id and record.suite == harness.SUITE
    assert record.authorized_by == "owner, test only, Stage E2a" and record.hash == report.evidence_hash
    text = evidence.read_text(encoding="utf-8")
    assert set(json.loads(text)) == {"authorized_by", "backend", "capability_id", "commit", "environment", "hash",
                                     "prev_hash", "recorded_at", "result_digest", "suite", "verification_id", "version"}  # fmt: skip
    for secret in (ACCOUNT, "arn:", USER_ID, "AROA", "alex@example.com", ROLE, "********", PROFILE, "aws.dev.identity",
                   "ASIAFAKE", "fake-temporary-secret", "FakeSessionToken", "AKIAFILE"):  # fmt: skip
        assert secret not in text, secret
    assert not (evidence.parent / "live.jsonl.staged").exists()
    reg = CapabilityRegistry((), environment="dev", live_evidence_path=evidence)
    assert reg.live_evidence_valid


def test_a_stubbed_run_can_never_write_the_repositorys_evidence(tmp_path, aws_home, monkeypatch):
    # The harness compares against its EVIDENCE_PATH, which is the registry's one trusted file. The test points
    # that name at a temporary file, so even a broken check could never write into the repository.
    assert harness.EVIDENCE_PATH is EVIDENCE_PATH
    stand_in = tmp_path / "repository-evidence" / "live-capabilities.jsonl"
    monkeypatch.setattr(harness, "EVIDENCE_PATH", stand_in)
    offline = Offline()
    with pytest.raises(LiveVerificationFailed, match="never writes the repository"):
        verify(tmp_path, offline, evidence=stand_in)
    assert offline.loads == [] and offline.sts_calls == 0 and not stand_in.parent.exists()
    assert not EVIDENCE_PATH.exists()


def test_the_harness_cannot_be_pointed_at_another_capability_or_operation():
    assert harness.CAPABILITY_ID == "aws.identity.inspect" and harness.CAPABILITY_VERSION == 1
    assert harness.TOOL_NAME == "aws_identity_inspect"
    assert set(inspect.signature(harness.run_identity_verification).parameters) == {
        "authorization", "commit", "evidence_path", "guard", "workspace_root", "log_text", "plan", "clock", "test_hooks"}
    assert [f.name for f in dataclasses.fields(LivePlan)] == ["profile", "slot", "region", "environment"]
    tree = ast.parse(Path(harness.__file__).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    # The only capability call is call_capability(CAPABILITY_ID); the harness never reaches the backend or the tool.
    capability_calls = [n for n in calls if n.func.attr in ("call_capability", "call")]
    assert [ast.unparse(n) for n in capability_calls] == ["gateway.call_capability(CAPABILITY_ID)"]
    assert not {n.func.attr for n in calls} & {"get_caller_identity", "head_object", "credential_metadata",
                                                "_make_api_call", "create_client", "probe"}  # fmt: skip
    assert [ast.unparse(n.func) for n in calls if n.func.attr == "run"] == ["subprocess.run"]  # git only; never tool.run
    constants = " ".join(n.value.lower() for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    for name in ("s3:", "s3.", "glue", "lambda", "athena", "redshift", "cloudformation", "bedrock", "iam:", "deploy"):
        assert name not in constants.replace("aws.s3.metadata.read", ""), name


# ------------------------------------------------------------------ every failure fails, and writes nothing


def expect_failure(tmp_path, step, run):
    evidence = tmp_path / "evidence" / "live.jsonl"
    with pytest.raises(LiveVerificationFailed) as failed:
        run()
    assert failed.value.step == step, failed.value
    assert not evidence.exists() and not (evidence.parent / "live.jsonl.staged").exists()
    assert not EVIDENCE_PATH.exists()
    return failed.value


@pytest.mark.parametrize("value", [None, "", "   ", "ab", "x" * 121, f"owner {ACCOUNT}", "owner arn:aws:iam::x",
                                   "owner\nsecond line", "aws_session_token=abc"])  # fmt: skip
def test_missing_or_unusable_authorization_fails(tmp_path, value):
    environ = {} if value is None else {harness.AUTHORIZED_BY_VARIABLE: value}
    expect_failure(tmp_path, "configuration", lambda: harness.authorization_from_environment(environ))


def test_a_dirty_tree_or_an_unknown_commit_fails(tmp_path, aws_home):
    expect_failure(tmp_path, "repository", lambda: preflight(tmp_path, dirty=True))
    expect_failure(tmp_path, "repository", lambda: preflight(tmp_path, commit=""))


@pytest.mark.parametrize("name", harness.STATIC_KEY_VARIABLES)
def test_static_credential_variables_fail(tmp_path, aws_home, name):
    failure = expect_failure(tmp_path, "credentials", lambda: preflight(tmp_path, environ={name: "value-canary"}))
    assert "long-term keys are refused" in failure.reason and name in failure.reason
    assert "value-canary" not in failure.reason  # the name only, never the value


@pytest.mark.parametrize("config,reason", [
    (NO_PROFILE, "does not exist"),  # a working [default] is present: it is never used instead
    (STATIC, "'static'"),
    (PROCESS, "'process'"),
])  # fmt: skip
def test_a_missing_static_or_unsupported_profile_fails_the_pre_flight(tmp_path, aws_home, config, reason):
    write_config(aws_home, config)
    assert reason in expect_failure(tmp_path, "credentials", lambda: preflight(tmp_path)).reason


@pytest.mark.parametrize("config,detail", [
    (NO_PROFILE, "AWS_NOT_CONFIGURED"),
    (STATIC, "long-term access keys are refused"),
    (PROCESS, "unsupported credential type"),
])  # fmt: skip
def test_the_backend_refuses_them_too_before_any_credential_or_call(tmp_path, aws_home, config, detail):
    # Even without the pre-flight, the product refuses: the probe fails, the gateway is never reached.
    write_config(aws_home, config)
    offline = Offline()
    failure = expect_failure(tmp_path, "probe", lambda: verify(tmp_path, offline))
    assert detail in failure.reason and offline.loads == [] and offline.sts_calls == 0


@pytest.mark.parametrize("plan", [
    LivePlan(profile="default"), LivePlan(profile="internal-dev"), LivePlan(profile="agent-factory-prod"),
    LivePlan(region="us-east-1"), LivePlan(region="eu-west-1"), LivePlan(slot="aws.dev.readonly"),
    LivePlan(slot="aws.prod.identity", environment="prod"), LivePlan(environment="staging"),
])  # fmt: skip
def test_any_plan_but_the_approved_one_fails_before_a_backend_exists(tmp_path, aws_home, plan):
    write_config(aws_home, SSO.replace(f"[profile {PROFILE}]", f"[profile {plan.profile}]\n[profile {PROFILE}]"))
    offline = Offline()
    expect_failure(tmp_path, "plan", lambda: verify(tmp_path, offline, plan=plan))
    expect_failure(tmp_path, "plan", lambda: preflight(tmp_path, plan=plan))
    assert offline.loads == [] and offline.sts_calls == 0


def test_the_approved_plan_passes_the_pre_flight(tmp_path, aws_home):
    preflight(tmp_path)  # no exception: the synthetic SSO profile, no evidence yet, no static key variable


def test_an_existing_record_or_an_untrusted_evidence_file_fails_the_pre_flight(tmp_path, aws_home):
    evidence = tmp_path / "evidence" / "live.jsonl"
    verify(tmp_path, Offline(), evidence=evidence)
    with pytest.raises(LiveVerificationFailed, match="already exists"):
        preflight(tmp_path, evidence=evidence)
    evidence.write_text("not json\n", encoding="utf-8")
    with pytest.raises(LiveVerificationFailed, match="does not verify"):
        preflight(tmp_path, evidence=evidence)


@pytest.mark.parametrize("offline,status", [
    (Offline(creds=TempCreds(expires_in=timedelta(minutes=-5))), "no_identity"),  # SSO session expired
    (Offline(creds=TempCreds(expires_in=timedelta(seconds=20))), "no_identity"),  # about to expire
    (Offline(creds=TempCreds(expires_in=None)), "no_identity"),  # no known expiry
    (Offline(creds=TempCreds(method="shared-credentials-file")), "no_identity"),  # a static key behind the profile
    (Offline(creds=TempCreds(method="iam-role")), "no_identity"),  # an unsupported credential type
    (Offline(error=("AccessDenied", 403)), "permission_denied"),
    (Offline(error=("ExpiredToken", 403)), "no_identity"),
    (Offline(error=("Throttling", 400)), "probe_failed"),
    (Offline(error=("InternalFailure", 500)), "probe_failed"),
    (Offline(answer={"UserId": USER_ID, "Account": ACCOUNT}), "probe_failed"),  # a malformed answer
])  # fmt: skip
def test_an_unavailable_capability_fails_and_the_gateway_is_never_called(tmp_path, aws_home, monkeypatch, offline, status):
    called = []
    monkeypatch.setattr(ToolGateway, "call_capability", lambda self, *a, **k: called.append(a))
    failure = expect_failure(tmp_path, "probe", lambda: verify(tmp_path, offline))
    assert f"unavailable: {status}" in failure.reason and called == []
    assert offline.sts_calls <= 1 and ACCOUNT not in failure.reason and "arn:" not in failure.reason


def test_a_missing_sso_sign_in_fails_through_the_real_provider_without_any_network(tmp_path, aws_home, monkeypatch):
    # No credential loader: the real botocore SSO provider runs against the synthetic home, which has no cached
    # sign-in. It fails closed, offline, under the strict live guard.
    guard = offline_guard()
    guard.install(monkeypatch)
    offline = Offline()
    failure = expect_failure(tmp_path, "probe", lambda: verify(tmp_path, offline, guard=guard, loader=False))
    assert "unavailable: no_identity" in failure.reason
    assert offline.sts_calls == 0 and guard.fake_resolved == guard.fake_connected == guard.refused == []


def test_an_unexpected_network_destination_fails_even_when_a_library_swallows_the_refusal(tmp_path, aws_home, monkeypatch):
    guard = offline_guard()
    guard.install(monkeypatch)

    def reach_metadata():
        try:  # e.g. a credential provider probing the instance metadata service, and ignoring the error
            socket.create_connection(("169.254.169.254", 80), timeout=1)
        except OSError:
            pass

    offline = Offline(before_call=reach_metadata)
    failure = expect_failure(tmp_path, "probe", lambda: verify(tmp_path, offline, guard=guard))
    assert "unexpected network destination" in failure.reason and "169.254.169.254" in failure.reason
    assert guard.fake_connected == []


@pytest.mark.parametrize("answer", [
    {**IDENTITY, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_AdministratorAccess_0123abcd/alex"},
    {**IDENTITY, "Arn": f"arn:aws:iam::{ACCOUNT}:user/alex"},
    {**IDENTITY, "Arn": f"arn:aws:iam::{ACCOUNT}:root"},
    {**IDENTITY, "Account": "12345"},
])  # fmt: skip
def test_an_unexpected_identity_fails_and_writes_nothing(tmp_path, aws_home, answer):
    expect_failure(tmp_path, "result", lambda: verify(tmp_path, Offline(answer=answer)))


def test_identifying_material_in_the_logs_fails_and_writes_nothing(tmp_path, aws_home):
    for leaked in (ARN, f"account {ACCOUNT}", USER_ID, "ASIAFAKETEMPORARY001", "aws_session_token = x"):
        expect_failure(tmp_path, "result", lambda: verify(tmp_path, Offline(), log_text=lambda: leaked))
    assert harness.leaks(json.dumps({"principal_name": ROLE, "account_hint": "********9012"})) == []
    digest = "abcdef" + "123456789012" + "abcdef"  # a digit run inside a hex digest is not an account ID
    assert harness.leaks(digest) == [] and harness.leaks(f"::{ACCOUNT}:") != []


def test_a_capability_version_mismatch_fails(tmp_path, aws_home, monkeypatch):
    import agent_factory.aws.pack as pack

    real = pack.aws_capabilities
    monkeypatch.setattr(pack, "aws_capabilities", lambda: tuple(
        dataclasses.replace(c, version=2) if c.capability_id == "aws.identity.inspect" else c for c in real()))
    failure = expect_failure(tmp_path, "capability", lambda: verify(tmp_path, Offline()))
    assert "new authorization" in failure.reason


def test_an_authorization_for_another_capability_version_fails(tmp_path, aws_home):
    from agent_factory.capabilities.live_evidence import LiveVerificationAuthorization

    for auth in (LiveVerificationAuthorization("aws.identity.inspect", 2, "dev", "owner, test"),
                 LiveVerificationAuthorization("aws.s3.metadata.read", 1, "dev", "owner, test"),
                 LiveVerificationAuthorization("aws.identity.inspect", 1, "prod", "owner, test")):  # fmt: skip
        offline = Offline()
        expect_failure(tmp_path, "configuration", lambda: harness.run_identity_verification(
            authorization=auth, commit=COMMIT, evidence_path=tmp_path / "evidence" / "live.jsonl",
            guard=offline_guard(), workspace_root=tmp_path / "ws", log_text=lambda: "",
            test_hooks=offline.hooks()))  # fmt: skip
        assert offline.sts_calls == 0


def test_a_crash_while_writing_evidence_leaves_no_record_and_no_claim(tmp_path, aws_home, monkeypatch):
    evidence = tmp_path / "evidence" / "live.jsonl"

    def half_written(*, path, **kwargs):
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"capability_id":"aws.identity.inspect","backend":"boto"')  # no end: a torn line
        raise KeyboardInterrupt  # the process dies mid-write

    real_append = harness.append_live_verification
    monkeypatch.setattr(harness, "append_live_verification", half_written)
    with pytest.raises(KeyboardInterrupt):
        verify(tmp_path, Offline(), evidence=evidence)
    assert not evidence.exists() and not (evidence.parent / "live.jsonl.staged").exists()
    monkeypatch.setattr(harness, "append_live_verification", real_append)  # (never undo(): it would drop aws_home)

    def crash(*args):
        raise OSError("disk full")

    monkeypatch.setattr(harness.os, "replace", crash)
    with pytest.raises(OSError):
        verify(tmp_path, Offline(), evidence=evidence)
    assert not evidence.exists() and not (evidence.parent / "live.jsonl.staged").exists()


def test_a_failed_check_after_staging_leaves_the_real_file_untouched(tmp_path, aws_home, monkeypatch):
    evidence = tmp_path / "evidence" / "live.jsonl"
    evidence.parent.mkdir()
    evidence.write_bytes(b"")
    real_leaks = harness.leaks
    calls = []

    def leaks_on_the_record(text):  # the result passes; the staged evidence record "leaks"
        calls.append(text)
        return ["fixture"] if "\"suite\"" in text else real_leaks(text)

    monkeypatch.setattr(harness, "leaks", leaks_on_the_record)
    with pytest.raises(LiveVerificationFailed, match="evidence record"):
        verify(tmp_path, Offline(), evidence=evidence)
    assert evidence.read_bytes() == b"" and not (evidence.parent / "live.jsonl.staged").exists()
    assert load_live_evidence(evidence) == ()


def test_the_repository_still_holds_no_evidence_and_the_product_never_builds_the_real_backend():
    assert not EVIDENCE_PATH.exists()
    def authorizing(path):  # code (not prose) that passes real_backend_authorized=True
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return any(isinstance(k, ast.keyword) and k.arg == "real_backend_authorized" and isinstance(k.value, ast.Constant)
                   and k.value.value is True for k in ast.walk(tree))  # fmt: skip

    for path in (REPO / "agent_factory").rglob("*.py"):
        if path.name != "boto.py":
            assert "BotoAwsBackend(" not in path.read_text(encoding="utf-8"), path
        assert not authorizing(path), path
    users = [p.name for p in (REPO / "tests").glob("*.py") if authorizing(p)]
    assert sorted(users) == ["aws_live_support.py"]  # the harness is the only place that authorizes the real backend


def test_a_passed_verification_is_read_back_as_live_tested_for_that_version_and_environment_only(tmp_path, aws_home):
    evidence = tmp_path / "evidence" / "live.jsonl"
    verify(tmp_path, Offline(), evidence=evidence)
    from agent_factory.aws import AwsToolPack

    for environment, expected in (("dev", True), ("staging", False), ("prod", False)):
        reg = CapabilityRegistry((AwsToolPack(),), environment=environment, live_evidence_path=evidence)
        kinds = {e.kind for e in reg.evidence("aws.identity.inspect")}
        assert (EvidenceKind.LIVE_TESTED in kinds) is expected
        assert EvidenceKind.LIVE_TESTED not in {e.kind for e in reg.evidence("aws.s3.metadata.read")}

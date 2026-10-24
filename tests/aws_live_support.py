"""The Stage E2 live verification harness for ``aws.identity.inspect`` (docs/aws-capabilities.md, section 10).

This is test code, not product code: nothing in ``agent_factory/`` constructs ``BotoAwsBackend``, and only
this harness sets ``real_backend_authorized=True``. It verifies exactly ONE capability, through the real
product path, and writes LIVE_TESTED evidence only after every check has passed:

    CapabilityRegistry (probe: STS call 1)
      -> ToolGateway.call_capability("aws.identity.inspect")   (cloud_read policy: allow, for agent "verifier")
        -> AwsIdentityInspectTool -> BotoAwsBackend -> STS GetCallerIdentity (call 2)
      -> the safe summary checked
      -> append_live_verification (the only evidence writer)

There is deliberately no parameter for the capability, the operation, or the backend: the harness cannot be
pointed at another capability. The plan (profile, slot, region, environment) has exactly one accepted value,
the one the owner approved; anything else is refused before a backend exists.

Offline tests (``tests/test_aws_live_harness.py``) drive the same function with botocore's Stubber, a
synthetic AWS config, and a network guard whose underlying resolver and connector are fakes. A run with
test hooks can never write the repository's evidence file.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_factory.aws import AwsPackSettings, AwsToolPack, CredentialSlots, aws_capabilities
from agent_factory.aws.boto import BotoAwsBackend, classify_profile
from agent_factory.aws.credentials import OUT_OF_SCOPE_PROFILES, CredentialRefused, SlotBinding
from agent_factory.capabilities import AvailabilityStatus, CapabilityRegistry, EvidenceKind
from agent_factory.capabilities.live_evidence import (
    EVIDENCE_PATH,
    LiveEvidenceError,
    LiveVerificationAuthorization,
    append_live_verification,
    load_live_evidence,
)
from agent_factory.safety.permissions import ActionCategory, PermissionPolicy
from agent_factory.tools.gateway import ToolGateway
from agent_factory.tools.packs import local_packs
from agent_factory.workspace import Workspace

#: The one capability, and the one version, this harness may verify. A new version needs a new authorization.
CAPABILITY_ID = "aws.identity.inspect"
CAPABILITY_VERSION = 1
TOOL_NAME = "aws_identity_inspect"
AGENT = "verifier"
SUITE = "tests/test_aws_live.py::test_identity_inspection_live"
AUTHORIZED_BY_VARIABLE = "AGENT_FACTORY_AWS_LIVE_AUTHORIZED_BY"
#: The only hosts a live run may resolve or connect to, on port 443 only (resolved offline from botocore 1.43.57).
LIVE_HOSTS = frozenset({
    "sts.us-west-2.amazonaws.com",  # GetCallerIdentity
    "portal.sso.us-west-2.amazonaws.com",  # SSO GetRoleCredentials (credential resolution, not a capability call)
    "oidc.us-west-2.amazonaws.com",  # only if the cached SSO token needs a refresh
})  # fmt: skip
LIVE_PORT = 443
#: Names that would mean a static key is in play. Only their presence is checked, never a value.
STATIC_KEY_VARIABLES = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
_SUMMARY_KEYS = frozenset({"principal_type", "principal_name", "account_hint", "region", "backend"})
#: What must never appear in the result, the gateway's records, the logs, or the evidence.
LEAK_PATTERNS = (
    re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])"),  # a full account ID (as in an ARN or text)
    re.compile(r"arn:aws", re.IGNORECASE),  # an ARN
    re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|AIPA)[A-Z0-9]{8,}"),  # key and principal IDs
    re.compile(r"(?i)(?:secret|session)[_ -]?(?:access[_ -]?)?(?:key|token)\s*[=:]"),
    re.compile(r"(?i)\b(?:accessToken|refreshToken|clientSecret|aws_session_token)\b"),
)


class LiveVerificationFailed(AssertionError):
    """The verification did not pass. ``step`` names where it stopped; no evidence was written."""

    def __init__(self, step: str, reason: str) -> None:
        self.step = step
        self.reason = reason
        super().__init__(f"{step}: {reason}")


@dataclass(frozen=True)
class LivePlan:
    """What the owner approved for Stage E2. There is exactly one acceptable plan (``APPROVED_PLAN``)."""

    profile: str = "agent-factory-dev"
    slot: str = "aws.dev.identity"
    region: str = "us-west-2"
    environment: str = "dev"


APPROVED_PLAN = LivePlan()


# ------------------------------------------------------------------ the network allowlist


class NetworkRefused(OSError):
    """A destination outside the live allowlist. Recorded by the guard, so a library that swallows it still fails."""


@dataclass
class AwsLiveNetworkGuard:
    """Name resolution and connections only to ``LIVE_HOSTS`` on port 443, and only to public addresses.

    * A name outside the allowlist is never even resolved (no DNS query leaves).
    * A literal IP is never connected to unless an allowlisted name resolved to it in this run, so the
      metadata services (169.254.169.254, 169.254.170.2, fd00:ec2::254) are unreachable however they are named.
    * An allowlisted name that resolves to a non-public address (DNS rebinding) is refused.
    * Every refusal is recorded: ``refused`` non-empty fails the verification, even if a library caught the error.

    ``resolve`` and ``connect`` are the real socket functions in a live run; offline tests pass fakes, so the
    guard is proved without a single packet.
    """

    resolve: Callable[..., Any]
    connect: Callable[[Any, Any], Any]
    allowed_hosts: frozenset[str] = LIVE_HOSTS
    port: int = LIVE_PORT
    addresses: set[str] = field(default_factory=set)
    resolved: list[str] = field(default_factory=list)
    connected: list[tuple[str, int]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def _refuse(self, what: str) -> NetworkRefused:
        self.refused.append(what)
        return NetworkRefused(f"network refused by the AWS live guard: {what}")

    def getaddrinfo(self, host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if not isinstance(name, str):
            raise self._refuse(f"resolve {name!r}")
        name = name.lower().rstrip(".")
        if name not in self.allowed_hosts or str(port) not in (str(self.port), "https"):
            raise self._refuse(f"resolve {name}:{port}")
        infos = self.resolve(host, port, *args, **kwargs)
        for info in infos:
            address = str(info[4][0]).split("%")[0]
            if not ipaddress.ip_address(address).is_global:
                raise self._refuse(f"{name} resolved to a non-public address")
            self.addresses.add(address)
        self.resolved.append(name)
        return infos

    def gethostbyname(self, host: Any, *args: Any) -> Any:
        raise self._refuse(f"gethostbyname {host!r}")

    def check_connect(self, address: Any) -> None:
        if not isinstance(address, tuple) or len(address) < 2:
            raise self._refuse(f"connect {address!r}")
        host, port = str(address[0]).split("%")[0], address[1]
        if host not in self.addresses or port != self.port:
            raise self._refuse(f"connect {host}:{port}")
        self.connected.append((host, port))

    def guarded_connect(self, sock: Any, address: Any) -> Any:
        self.check_connect(address)
        return self.connect(sock, address)

    def guarded_connect_ex(self, sock: Any, address: Any) -> Any:
        self.check_connect(address)
        return self.connect(sock, address)

    def install(self, monkeypatch: Any) -> None:
        guard = self
        monkeypatch.setattr(socket, "getaddrinfo", self.getaddrinfo)
        monkeypatch.setattr(socket, "gethostbyname", self.gethostbyname)
        monkeypatch.setattr(socket, "gethostbyname_ex", self.gethostbyname)
        monkeypatch.setattr(socket.socket, "connect", lambda sock, address: guard.guarded_connect(sock, address))
        monkeypatch.setattr(socket.socket, "connect_ex", lambda sock, address: guard.guarded_connect_ex(sock, address))


# ------------------------------------------------------------------ pre-flight (offline)

#: The only processes a live run may start: these two read-only git commands on this repository.
GIT_COMMANDS = (("rev-parse", "--verify", "HEAD"), ("status", "--porcelain", "--untracked-files=all"))


def git_argv(root: Path, command: tuple[str, ...]) -> list[str]:
    return ["git", "-C", str(root), *command]


def repository_state(root: Path) -> tuple[str, bool]:
    """(HEAD commit, whether the tree has any change). Read-only."""
    import subprocess

    def run(command: tuple[str, ...]) -> str:
        done = subprocess.run(git_argv(root, command), capture_output=True, text=True, timeout=30, check=False)
        if done.returncode != 0:
            raise LiveVerificationFailed("repository", f"git {command[0]} failed")
        return done.stdout

    commit = run(GIT_COMMANDS[0]).strip()
    return commit, bool(run(GIT_COMMANDS[1]).strip())



def authorization_from_environment(environ: Mapping[str, str] = os.environ) -> LiveVerificationAuthorization:
    """Who authorized this run, for the evidence. Without it the run does not start."""
    who = (environ.get(AUTHORIZED_BY_VARIABLE) or "").strip()
    if not who:
        raise LiveVerificationFailed("configuration", f"{AUTHORIZED_BY_VARIABLE} is not set: the owner's authorization "
                                                      "is required for a live AWS verification")  # fmt: skip
    if any(p.search(who) for p in LEAK_PATTERNS) or not re.fullmatch(r"[\w ,.:;()/@+-]{3,120}", who):
        raise LiveVerificationFailed("configuration", f"{AUTHORIZED_BY_VARIABLE} must be a short plain-text note")
    try:
        return LiveVerificationAuthorization(CAPABILITY_ID, CAPABILITY_VERSION, APPROVED_PLAN.environment, who)
    except LiveEvidenceError as exc:
        raise LiveVerificationFailed("configuration", str(exc)) from None


def check_plan(plan: LivePlan) -> None:
    if plan != APPROVED_PLAN:
        raise LiveVerificationFailed("plan", "only the approved plan (agent-factory-dev, aws.dev.identity, us-west-2, "
                                             "dev) may be verified")  # fmt: skip
    if not all(host.split(".")[-3] == plan.region for host in LIVE_HOSTS):
        raise LiveVerificationFailed("plan", "the network allowlist does not match the region")


def preflight(plan: LivePlan, *, environ: Mapping[str, str], evidence_path: Path, commit: str, dirty: bool) -> None:
    """Everything that must hold before any credential is resolved. Reads configuration KEYS only."""
    check_plan(plan)
    if dirty:
        raise LiveVerificationFailed("repository", "uncommitted changes: the evidence records the commit it ran at")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise LiveVerificationFailed("repository", "the commit could not be determined")
    present = [name for name in STATIC_KEY_VARIABLES if environ.get(name)]
    if present:
        raise LiveVerificationFailed("credentials", f"static credential variables are set ({', '.join(present)}): "
                                                    "long-term keys are refused; unset them")  # fmt: skip
    # The out-of-scope profiles can be selected nowhere: not by the backend, not by a slot.
    for profile in OUT_OF_SCOPE_PROFILES:
        try:
            BotoAwsBackend(environment=plan.environment, allowed_profiles=frozenset({profile}),
                           allowed_regions=frozenset({plan.region}))  # fmt: skip
        except ValueError:
            pass
        else:
            raise LiveVerificationFailed("credentials", f"the backend accepted the out-of-scope profile {profile!r}")
        try:
            SlotBinding(plan.slot, profile)
        except CredentialRefused:
            pass
        else:
            raise LiveVerificationFailed("credentials", f"a slot accepted the out-of-scope profile {profile!r}")
    # The profile must exist and its keys must describe IAM Identity Center. botocore reads the config file; no
    # value is inspected here, the shared credentials file is not opened, and nothing is resolved.
    import botocore.session

    session = botocore.session.Session(profile=plan.profile)
    session.set_config_variable("credentials_file", str(Path(__file__).resolve().parent / "__no_credentials_file__"))
    profiles = session.full_config.get("profiles") or {}
    if plan.profile not in profiles:
        raise LiveVerificationFailed("credentials", f"the profile {plan.profile!r} does not exist (no other profile is "
                                                    "ever used instead)")  # fmt: skip
    kind = classify_profile(profiles[plan.profile])
    if kind != "sso":
        raise LiveVerificationFailed("credentials", f"the profile's configuration is {kind!r}, not IAM Identity Center "
                                                    "(sso): refused before any credential is resolved")  # fmt: skip
    # No record for this capability version may exist yet, and the file must verify.
    try:
        existing = load_live_evidence(evidence_path)
    except LiveEvidenceError as exc:
        raise LiveVerificationFailed("evidence", f"the evidence file does not verify: {exc}") from None
    if any((r.capability_id, r.version, r.environment) == (CAPABILITY_ID, CAPABILITY_VERSION, plan.environment)
           for r in existing):  # fmt: skip
        raise LiveVerificationFailed("evidence", "a LIVE_TESTED record for this capability version already exists")
    # Generated code never sees AWS: the sandbox environment carries no AWS_* name.
    from agent_factory.sandbox.validation import DEFAULT_ENV_ALLOWLIST, DEFAULT_SANDBOX_ENV

    if [k for k in (*DEFAULT_SANDBOX_ENV, *DEFAULT_ENV_ALLOWLIST) if k.upper().startswith("AWS")]:
        raise LiveVerificationFailed("sandbox", "the sandbox environment carries an AWS_* name")
    # The AWS pack's only executable capabilities are the two reads; the deployment placeholder has no tool.
    executable = {c.capability_id: c for c in aws_capabilities() if c.executable}
    if set(executable) != {CAPABILITY_ID, "aws.s3.metadata.read"} or any(
            c.category is not ActionCategory.CLOUD_READ or c.mutates for c in executable.values()):  # fmt: skip
        raise LiveVerificationFailed("capabilities", "the AWS pack offers something other than the two cloud reads")


# ------------------------------------------------------------------ the verification


def leaks(text: str) -> list[str]:
    return [p.pattern for p in LEAK_PATTERNS if p.search(text)]


#: Evidence fields that are hexadecimal digests or a commit by construction (checked by the writer's format rules).
_HEX_FIELDS = ("hash", "prev_hash", "result_digest", "commit")


def evidence_leaks(text: str) -> list[str]:
    """``leaks`` over every evidence record, without its hex digests (a digest can hold any digit run)."""
    found: list[str] = []
    for line in text.splitlines():
        if line.strip():
            record = json.loads(line)
            found += leaks(json.dumps({k: v for k, v in record.items() if k not in _HEX_FIELDS}))
    return found


def check_summary(result: Any, plan: LivePlan) -> None:
    if not isinstance(result, dict) or set(result) != _SUMMARY_KEYS:
        raise LiveVerificationFailed("result", "the result is not exactly the five-key safe summary")
    expected = {"principal_type": "assumed-role", "region": plan.region, "backend": "boto"}
    if any(result[k] != v for k, v in expected.items()):
        raise LiveVerificationFailed("result", "principal type, region, or backend is not the expected one")
    if not re.fullmatch(r"AWSReservedSSO_AgentFactoryIdentity_[0-9a-f]{1,32}", str(result["principal_name"])):
        raise LiveVerificationFailed("result", "the principal is not the AgentFactoryIdentity permission set's role")
    if not re.fullmatch(r"\*{8}\d{4}", str(result["account_hint"])):
        raise LiveVerificationFailed("result", "the account hint is not masked to its last four digits")


@dataclass(frozen=True)
class VerificationReport:
    """What a passed verification returns. Nothing here is a credential, an account ID, or an ARN."""

    availability: str
    principal_type: str
    gateway_records: tuple[dict[str, Any], ...]
    verification_id: str
    evidence_hash: str


def run_identity_verification(*, authorization: LiveVerificationAuthorization, commit: str, evidence_path: Path,
                              guard: AwsLiveNetworkGuard, workspace_root: Path, log_text: Callable[[], str],
                              plan: LivePlan = APPROVED_PLAN, clock: Callable[[], datetime] | None = None,
                              test_hooks: Mapping[str, Any] | None = None) -> VerificationReport:  # fmt: skip
    """Verify ``aws.identity.inspect`` through the product path, then (and only then) append one evidence record.

    ``test_hooks`` (``credential_loader``, ``client_hook``) exist for the offline tests; with any hook set the
    repository's evidence file is refused, so a stubbed run can never produce real LIVE_TESTED evidence.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    check_plan(plan)
    hooks = dict(test_hooks or {})
    if set(hooks) - {"credential_loader", "client_hook"}:
        raise LiveVerificationFailed("plan", "unknown test hook")
    if hooks and Path(evidence_path).resolve() == EVIDENCE_PATH.resolve():
        raise LiveVerificationFailed("evidence", "a run with test hooks never writes the repository's evidence")
    if (authorization.capability_id, authorization.version, authorization.environment) != (
            CAPABILITY_ID, CAPABILITY_VERSION, plan.environment):  # fmt: skip
        raise LiveVerificationFailed("configuration", "the authorization is for another capability version")

    def network_clean(step: str) -> None:
        if guard.refused:
            raise LiveVerificationFailed(step, f"unexpected network destination: {guard.refused[0]}")

    # Build: the real backend, explicitly authorized, one profile, one region. Building the registry runs the probe.
    backend = BotoAwsBackend(environment=plan.environment, allowed_profiles=frozenset({plan.profile}),
                             allowed_regions=frozenset({plan.region}), **hooks)  # fmt: skip
    settings = AwsPackSettings(enabled=True, backend=backend, slots=CredentialSlots({plan.slot: plan.profile}),
                               region=plan.region, real_backend_authorized=True)  # fmt: skip
    packs = (*local_packs(), AwsToolPack(settings))
    registry = CapabilityRegistry(packs, environment=plan.environment, live_evidence_path=evidence_path)
    network_clean("probe")
    cap = registry.get(CAPABILITY_ID)
    if cap is None or cap.version != CAPABILITY_VERSION or cap.implemented_by != TOOL_NAME:
        raise LiveVerificationFailed("capability", f"{CAPABILITY_ID} version {CAPABILITY_VERSION} is not the registered "
                                                   "capability: a new version needs a new authorization")  # fmt: skip
    if cap.category is not ActionCategory.CLOUD_READ or cap.mutates:
        raise LiveVerificationFailed("capability", f"{CAPABILITY_ID} is not a cloud_read")
    availability = registry.availability(CAPABILITY_ID)
    if availability.status is not AvailabilityStatus.AVAILABLE:
        raise LiveVerificationFailed("probe", f"{CAPABILITY_ID} is unavailable: {availability.status.value} "
                                              f"({availability.detail})")  # fmt: skip

    # Call: through the Tool Gateway, under the development policy, for an agent allowed exactly this one tool.
    records: list[dict[str, Any]] = []
    policy = PermissionPolicy.for_environment("development").with_agent_tools(AGENT, {TOOL_NAME})
    gateway = ToolGateway(registry=registry.tool_registry(), policy=policy, workspace=Workspace(workspace_root),
                          run_id="aws-live-e2", node="verify", agent=AGENT, visit=1, approvals={},
                          on_action=records.append, capabilities=registry)  # fmt: skip
    try:
        result = gateway.call_capability(CAPABILITY_ID)
    except Exception as exc:  # noqa: BLE001 - reported as a failed verification, with the gateway's fixed text
        network_clean("call")
        raise LiveVerificationFailed("call", f"{type(exc).__name__}: {exc}"[:200]) from None
    network_clean("call")
    expected = {"tool": TOOL_NAME, "category": ActionCategory.CLOUD_READ.value, "decision": "allow", "params": {},
                "result": "ok"}  # fmt: skip
    if len(records) != 1 or any(records[0].get(k) != v for k, v in expected.items()):
        raise LiveVerificationFailed("gateway", "the gateway did not record exactly one allowed cloud_read call")

    # Check: exactly the safe summary, and nothing identifying anywhere it went.
    check_summary(result.data, plan)
    exposed = json.dumps({"result": result.data, "summary": result.summary, "records": records}) + log_text()
    if leaks(exposed):
        raise LiveVerificationFailed("result", "identifying material appeared in the result, records, or logs")

    # Record: the only writer, after everything above passed. It appends to a staged copy of the evidence file;
    # the copy is read back and checked, and only then replaces the real file in one atomic rename. A crash or a
    # failed check before the rename leaves the real file exactly as it was: no partial record, no claim.
    now = clock()
    verification_id = f"e2-identity-{now.strftime('%Y%m%dT%H%M%SZ')}"
    evidence_path = Path(evidence_path)
    staged = evidence_path.with_name(evidence_path.name + ".staged")
    try:
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(evidence_path.read_bytes() if evidence_path.is_file() else b"")
        try:
            record = append_live_verification(
                authorization=authorization, capability_id=CAPABILITY_ID, version=cap.version,
                backend=result.data["backend"], verification_id=verification_id, suite=SUITE,
                environment=plan.environment, commit=commit, recorded_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                safe_result=result.data, path=staged)  # fmt: skip
        except LiveEvidenceError as exc:
            raise LiveVerificationFailed("evidence", str(exc)) from None
        # Re-read: a registry with the AWS pack NOT enabled (no further AWS call) must now see LIVE_TESTED.
        reread = CapabilityRegistry((*local_packs(), AwsToolPack()), environment=plan.environment,
                                    live_evidence_path=staged)  # fmt: skip
        live = [e for e in reread.evidence(CAPABILITY_ID) if e.kind is EvidenceKind.LIVE_TESTED]
        if not reread.live_evidence_valid or [e.source for e in live] != [f"verification:{verification_id}"]:
            raise LiveVerificationFailed("evidence", "the written record is not read back as LIVE_TESTED")
        if evidence_leaks(staged.read_text(encoding="utf-8")):
            raise LiveVerificationFailed("evidence", "identifying material appeared in the evidence record")
        os.replace(staged, evidence_path)
    finally:
        staged.unlink(missing_ok=True)
    return VerificationReport(availability=availability.status.value, principal_type=result.data["principal_type"],
                              gateway_records=tuple(records), verification_id=verification_id,
                              evidence_hash=record.hash)  # fmt: skip


__all__ = [
    "APPROVED_PLAN",
    "AUTHORIZED_BY_VARIABLE",
    "CAPABILITY_ID",
    "CAPABILITY_VERSION",
    "LIVE_HOSTS",
    "AwsLiveNetworkGuard",
    "LivePlan",
    "LiveVerificationFailed",
    "NetworkRefused",
    "VerificationReport",
    "authorization_from_environment",
    "check_plan",
    "leaks",
    "preflight",
    "run_identity_verification",
]

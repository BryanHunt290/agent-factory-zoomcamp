"""AwsToolPack: typed AWS capabilities behind the Tool Gateway (docs/aws-capabilities.md).

Stages D and E1 of docs/design/capability-registry-aws.md. Three definitions:

* ``aws.identity.inspect``: wraps only STS GetCallerIdentity. Returns a bounded summary (principal type and
  name, the last four digits of the account), never the ARN, the user ID, or a credential.
* ``aws.s3.metadata.read``: wraps only S3 HeadObject on a designated bucket and prefix. Returns size, type,
  ETag, date, encryption, and cleaned user metadata.
* ``aws.deploy.execute``: a definition with NO implementation. It shows how a mutation is classified
  (approval always, recurring cost, ActionExecution) and is never available.

There is no generic AWS call, no SDK method name from a caller, no script, no shell, and no CLI.

Both executable tools are ``cloud_read`` (Stage E1, contract-first): read-only, typed, bounded, through
the trusted adapter with an approved credential slot. Development policy allows them for an agent that
has them on its allowlist; production requires approval. They are declared read-only.

The pack is enabled only with explicit ``AwsPackSettings(enabled=True, backend=...)``. The fake backend
is the default. The real ``BotoAwsBackend`` is accepted only with ``real_backend_authorized=True`` as
well; otherwise the pack stays ``not_configured``. Nothing in Agent Factory sets that today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..capabilities.model import (
    ApprovalRule,
    Availability,
    AvailabilityStatus,
    Capability,
    CostImpact,
    NetworkRequirement,
    Risk,
)
from ..capabilities.packs import ToolPack
from ..safety.execution import EffectClass, ExecutionSemantics
from ..safety.permissions import ActionCategory
from ..tools.base import Tool, ToolError, ToolResult
from ..workspace import Workspace
from .backend import AwsBackend, AwsBackendError
from .credentials import CredentialRefused, CredentialSlots, SlotBinding, check_credential
from .redaction import bounded, clean_map, clean_text, project

IDENTITY_SLOT = "aws.dev.identity"
READ_SLOT = "aws.dev.readonly"
DEPLOY_SLOT = "aws.dev.deploy"
#: Backends accepted without further authorization: the fake. The real one needs ``real_backend_authorized``.
ALLOWED_BACKEND_KINDS = frozenset({"fake"})
REAL_BACKEND_KINDS = frozenset({"boto"})
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_NAME_RE = re.compile(r"[A-Za-z0-9+=,.@_-]{1,64}")
_PRINCIPAL_TYPES = {"assumed-role": "assumed-role", "user": "user", "federated-user": "federated-user", "root": "root"}
_READ = ExecutionSemantics(
    effect=EffectClass.READ_ONLY, max_attempts=3,
    rationale="one read-only AWS call (cloud_read): it changes nothing in AWS, so repeating it is harmless",
)  # fmt: skip


@dataclass(frozen=True)
class AwsPackSettings:
    """Explicit configuration. Nothing here is read from the environment or from ~/.aws."""

    enabled: bool = False
    backend: AwsBackend | None = None
    slots: CredentialSlots = field(default_factory=CredentialSlots)
    region: str = "us-west-2"
    #: bucket -> the key prefix Agent Factory may read under (``""`` for the whole designated bucket).
    designated: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    #: The owner's authorization to use a REAL backend. Without it, only the fake backend is accepted.
    real_backend_authorized: bool = False

    def backend_accepted(self) -> bool:
        kind = getattr(self.backend, "kind", None)
        return kind in ALLOWED_BACKEND_KINDS or (kind in REAL_BACKEND_KINDS and self.real_backend_authorized is True)

    def __post_init__(self) -> None:
        if not _REGION_RE.fullmatch(self.region):
            raise ValueError("a region like 'us-west-2' is required")
        object.__setattr__(self, "designated", MappingProxyType(dict(self.designated)))
        for bucket in self.designated:
            if not _BUCKET_RE.fullmatch(bucket):
                raise ValueError(f"invalid designated bucket name {bucket!r}")


class _AwsTool(Tool):
    category = ActionCategory.CLOUD_READ
    execution = _READ
    slot: str = ""

    def __init__(self, settings: AwsPackSettings) -> None:
        self._settings = settings

    def _prepare(self) -> tuple[AwsBackend, SlotBinding]:
        """Everything that must hold before any backend operation: configured, bound, a temporary credential."""
        s = self._settings
        if not s.enabled or s.backend is None or not s.backend_accepted():
            raise ToolError("aws: AWS_NOT_CONFIGURED")
        try:
            binding = s.slots.resolve(self.slot)
            check_credential(binding, s.backend.credential_metadata(binding))
        except CredentialRefused as exc:
            raise ToolError(f"aws: AWS_IDENTITY_UNAVAILABLE: {exc.reason}") from None
        except AwsBackendError as exc:
            raise ToolError(f"aws: {exc.category}") from None
        return s.backend, binding


class AwsIdentityInspectTool(_AwsTool):
    name = "aws_identity_inspect"
    description = "Inspect which AWS identity Agent Factory would act as (STS GetCallerIdentity only)."
    slot = IDENTITY_SLOT

    def run(self, workspace: Workspace) -> ToolResult:
        backend, binding = self._prepare()
        try:
            raw = backend.get_caller_identity(binding, self._settings.region)
        except AwsBackendError as exc:
            raise ToolError(f"aws: {exc.category}") from None
        fields = project(raw, {"Arn": ("arn", str, True), "Account": ("account", str, True)})
        resource = fields["arn"].split(":", 5)[-1] if fields["arn"].count(":") >= 5 else ""
        kind, _, rest = resource.partition("/")
        name_match = _NAME_RE.match(rest.split("/")[0]) if rest else None
        summary = bounded({
            "principal_type": _PRINCIPAL_TYPES.get(kind, "other"),
            "principal_name": name_match.group(0) if name_match else "",
            "account_hint": "********" + fields["account"][-4:] if re.fullmatch(r"\d{12}", fields["account"]) else "",
            "region": self._settings.region,
            "backend": backend.kind,
        })  # fmt: skip
        return ToolResult(data=summary, summary=f"aws identity: {summary['principal_type']} ({backend.kind} backend)")


class AwsS3MetadataReadTool(_AwsTool):
    name = "aws_s3_metadata_read"
    description = "Read one S3 object's metadata (S3 HeadObject only) in a designated bucket and prefix."
    slot = READ_SLOT

    def run(self, workspace: Workspace, *, bucket: str, key: str) -> ToolResult:
        if not isinstance(bucket, str) or not _BUCKET_RE.fullmatch(bucket):
            raise ToolError("aws: invalid bucket name")
        if not isinstance(key, str) or not 1 <= len(key) <= 1024 or re.search(r"[\x00-\x1f\x7f]", key):
            raise ToolError("aws: invalid object key")
        prefix = self._settings.designated.get(bucket)
        if prefix is None or not key.startswith(prefix):
            raise ToolError("aws: not a designated bucket and prefix")  # checked before any AWS call
        backend, binding = self._prepare()
        try:
            raw = backend.head_object(binding, self._settings.region, bucket=bucket, key=key)
        except AwsBackendError as exc:
            raise ToolError(f"aws: {exc.category}") from None
        fields = project(raw, {
            "ContentLength": ("size", int, True), "ContentType": ("content_type", str, False),
            "ETag": ("etag", str, False), "LastModified": ("last_modified", str, False),
            "ServerSideEncryption": ("encryption", str, False), "Metadata": ("metadata", dict, False),
        })  # fmt: skip
        if "metadata" in fields:
            fields["metadata"] = clean_map(fields["metadata"])
        result = bounded({"bucket": bucket, "key": clean_text(key), **fields})
        return ToolResult(data=result, summary=f"s3 metadata: {result['size']} bytes")


_NO_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
_DOC = ("docs/aws-capabilities.md", "docs/design/capability-registry-aws.md")


def aws_capabilities() -> tuple[Capability, ...]:
    """The pack's definitions. Each documents the exact IAM actions it would need; none is a wildcard."""
    return (
        Capability(
            capability_id="aws.identity.inspect", version=1, pack="aws",
            description="Inspect which AWS identity Agent Factory would act as: principal type and name only.",
            implemented_by="aws_identity_inspect", category=ActionCategory.CLOUD_READ, effect=EffectClass.READ_ONLY,
            approval=ApprovalRule.POLICY, cost=CostImpact.NONE, risk=Risk.LOW, mutates=False,
            network=NetworkRequirement.TRUSTED_ADAPTER, credential=IDENTITY_SLOT,
            input_schema=_NO_INPUT,
            output_schema={"type": "object", "additionalProperties": False,
                           "required": ["principal_type", "principal_name", "account_hint", "region", "backend"],
                           "properties": {"principal_type": {"type": "string", "enum": [*_PRINCIPAL_TYPES, "other"]},
                                          "principal_name": {"type": "string", "maxLength": 64},
                                          "account_hint": {"type": "string", "maxLength": 12},
                                          "region": {"type": "string", "maxLength": 20},
                                          "backend": {"type": "string", "maxLength": 10}}},
            required_permissions=("tool:aws_identity_inspect", "category:cloud_read"),
            # GetCallerIdentity needs no permission and cannot be denied; it is listed for completeness.
            iam_actions=("sts:GetCallerIdentity",),
            tests=("tests/test_aws_pack.py::test_identity_inspection_returns_a_bounded_summary_without_the_arn_or_secrets",
                   "tests/test_aws_boto.py::test_identity_inspection_calls_exactly_get_caller_identity_and_returns_a_safe_summary"),
            documentation=_DOC, tags=("aws", "read", "identity"),
        ),
        Capability(
            capability_id="aws.s3.metadata.read", version=1, pack="aws",
            description="Read one object's size, type, date and user metadata in a designated bucket and prefix.",
            implemented_by="aws_s3_metadata_read", category=ActionCategory.CLOUD_READ, effect=EffectClass.READ_ONLY,
            approval=ApprovalRule.POLICY, cost=CostImpact.LOW, risk=Risk.LOW, mutates=False,
            network=NetworkRequirement.TRUSTED_ADAPTER, credential=READ_SLOT,
            input_schema={"type": "object", "additionalProperties": False, "required": ["bucket", "key"],
                          "properties": {"bucket": {"type": "string", "pattern": _BUCKET_RE.pattern},
                                         "key": {"type": "string", "minLength": 1, "maxLength": 1024}}},
            output_schema={"type": "object", "additionalProperties": False, "required": ["bucket", "key", "size"],
                           "properties": {"bucket": {"type": "string", "maxLength": 63},
                                          "key": {"type": "string", "maxLength": 200},
                                          "size": {"type": "integer", "minimum": 0},
                                          "content_type": {"type": "string", "maxLength": 200},
                                          "etag": {"type": "string", "maxLength": 200},
                                          "last_modified": {"type": "string", "maxLength": 200},
                                          "encryption": {"type": "string", "maxLength": 200},
                                          "metadata": {"type": "object", "maxProperties": 20,
                                                       "additionalProperties": {"type": "string", "maxLength": 200}}}},
            required_permissions=("tool:aws_s3_metadata_read", "category:cloud_read"),
            # HeadObject needs s3:GetObject on the object; s3:ListBucket on the bucket makes a missing key a 404
            # instead of a 403. Both are scoped to the designated bucket and prefix in the proposed policy.
            iam_actions=("s3:GetObject", "s3:ListBucket"),
            tests=("tests/test_aws_pack.py::test_s3_metadata_read_returns_only_known_fields_with_canaries_removed",),
            documentation=_DOC, tags=("aws", "read", "s3"),
        ),
        Capability(
            capability_id="aws.deploy.execute", version=1, pack="aws",
            description="Deploy a reviewed change set to a named stack. Not implemented: never available.",
            implemented_by=None, category=ActionCategory.PRODUCTION_DEPLOY, effect=EffectClass.NON_IDEMPOTENT,
            approval=ApprovalRule.ALWAYS, cost=CostImpact.RECURRING, risk=Risk.CRITICAL, mutates=True,
            network=NetworkRequirement.TRUSTED_ADAPTER, credential=DEPLOY_SLOT,
            input_schema={"type": "object", "additionalProperties": False, "required": ["stack", "change_set"],
                          "properties": {"stack": {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9-]{0,127}$"},
                                         "change_set": {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9-]{0,127}$"}}},
            iam_actions=("cloudformation:DescribeChangeSet", "cloudformation:ExecuteChangeSet",
                         "cloudformation:DescribeStacks"),
            documentation=_DOC, tags=("aws", "mutation", "placeholder"),
        ),
    )  # fmt: skip


class AwsToolPack(ToolPack):
    name = "aws"
    #: Only a verification through the real backend can ever count as LIVE_TESTED for this pack.
    live_backends = REAL_BACKEND_KINDS

    def __init__(self, settings: AwsPackSettings | None = None) -> None:
        self._settings = settings or AwsPackSettings()
        self._tools: tuple[Tool, ...] = (AwsIdentityInspectTool(self._settings), AwsS3MetadataReadTool(self._settings))

    @property
    def enabled(self) -> bool:
        """Explicit settings only. A credential in the environment or ~/.aws never enables anything."""
        return self._settings.enabled is True and self._settings.backend is not None

    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    def capabilities(self) -> tuple[Capability, ...]:
        return aws_capabilities()

    def _identity_state(self, slot: str, now: str) -> Availability | None:
        backend = self._settings.backend
        assert backend is not None
        try:
            binding = self._settings.slots.resolve(slot)
        except CredentialRefused:
            return Availability(AvailabilityStatus.NOT_CONFIGURED, checked_at=now, detail=f"slot {slot} not bound")
        try:
            check_credential(binding, backend.credential_metadata(binding))
        except CredentialRefused as exc:
            return Availability(AvailabilityStatus.NO_IDENTITY, checked_at=now,
                                detail=f"AWS_IDENTITY_UNAVAILABLE: {exc.reason}"[:160])  # fmt: skip
        except AwsBackendError as exc:
            return _from_error(exc, now)
        return None

    def probe(self, now: str) -> Availability:
        """configured? -> approved credential type? -> unexpired temporary identity? -> allowed region? ->
        GetCallerIdentity -> available. Any doubt is unavailable; nothing falls back."""
        backend = self._settings.backend
        if backend is None or not self._settings.backend_accepted():
            return Availability(AvailabilityStatus.NOT_CONFIGURED, checked_at=now,
                                detail="AWS_NOT_CONFIGURED: a real backend needs the owner's authorization")  # fmt: skip
        refused = self._identity_state(IDENTITY_SLOT, now)
        if refused is not None:
            return refused
        try:
            backend.get_caller_identity(self._settings.slots.resolve(IDENTITY_SLOT), self._settings.region)
        except AwsBackendError as exc:
            return _from_error(exc, now)
        return Availability(AvailabilityStatus.AVAILABLE, checked_at=now, detail=f"{backend.kind} backend")

    def probe_capability(self, capability: Capability, now: str) -> Availability | None:
        if capability.capability_id != "aws.s3.metadata.read":
            return None
        if getattr(self._settings.backend, "kind", None) in REAL_BACKEND_KINDS:
            return Availability(AvailabilityStatus.NOT_CONFIGURED, checked_at=now,
                                detail="AWS_NOT_CONFIGURED: S3 is not implemented for the real backend before Stage F")  # fmt: skip
        if not self._settings.designated:
            return Availability(AvailabilityStatus.NOT_CONFIGURED, checked_at=now, detail="no designated bucket")
        return self._identity_state(READ_SLOT, now)


_STATUS = {
    "AWS_NOT_CONFIGURED": AvailabilityStatus.NOT_CONFIGURED,
    "AWS_IDENTITY_UNAVAILABLE": AvailabilityStatus.NO_IDENTITY,
    "AWS_EXPIRED_CREDENTIAL": AvailabilityStatus.NO_IDENTITY,
    "AWS_PERMISSION_DENIED": AvailabilityStatus.PERMISSION_DENIED,
    "AWS_UNSUPPORTED_REGION": AvailabilityStatus.UNSUPPORTED_REGION,
}


def _from_error(exc: AwsBackendError, now: str) -> Availability:
    status = _STATUS.get(exc.category, AvailabilityStatus.PROBE_FAILED)
    detail = f"{exc.category}: {exc.detail}" if exc.detail else exc.category
    return Availability(status, checked_at=now, detail=detail[:160])


__all__ = ["AwsIdentityInspectTool", "AwsPackSettings", "AwsS3MetadataReadTool", "AwsToolPack", "aws_capabilities"]

"""The capability model: typed, immutable definitions, evidence, and availability (docs/capabilities.md).

A capability says what this Agent Factory installation can do: ``core.files.read``,
``git.commit``, ``aws.identity.inspect``. It is a description with provenance, not a
permission. Four questions stay separate:

* the **Capability Registry**: what exists, is it available, and what proves it?
* the **Tool Gateway**: may this particular action run now?
* **human approval**: does this exact action need the owner's judgment?
* **IAM** (for cloud packs): what is the cloud identity allowed to do?

Nothing here imports a cloud SDK, a pack, or a tool implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..safety.execution import EffectClass
from ..safety.permissions import ActionCategory, Decision

#: ``namespace.part[.part...]``: lower-case, dotted, 2 to 6 segments. The namespace is the owning pack's.
CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}(\.[a-z][a-z0-9_]{0,30}){1,5}$")
#: ``provider.environment.purpose``, e.g. ``aws.dev.readonly``: names WHICH identity, never holds it.
CREDENTIAL_SLOT_RE = re.compile(r"^[a-z][a-z0-9]{0,15}\.(dev|staging|prod)\.[a-z][a-z0-9_]{0,30}$")
#: A test identifier: ``tests/<file>.py::<test function>``.
TEST_ID_RE = re.compile(r"^tests/[A-Za-z0-9_/]+\.py::test_[A-Za-z0-9_]+$")
MAX_DESCRIPTION = 200


class CapabilityError(ValueError):
    """A capability definition, or a registration, is invalid or contradictory. Registration fails closed."""


class ApprovalRule(StrEnum):
    #: Follow the permission policy for the implementing tool's category.
    POLICY = "policy"
    #: Always require a person's approval, whatever the policy says (tightens, never loosens).
    ALWAYS = "always"
    #: Never runs: a denied class (IAM, credentials, organizations). Not even approval enables it.
    DENIED = "denied"


class CostImpact(StrEnum):
    NONE = "none"
    LOW = "low"
    UNKNOWN = "unknown"
    RECURRING = "recurring"  # creates something that bills while it exists


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NetworkRequirement(StrEnum):
    NONE = "none"
    #: Network used by Agent Factory's own trusted adapter, outside the sandbox. Never the sandbox's.
    TRUSTED_ADAPTER = "trusted_adapter"


class EvidenceKind(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    TESTED = "TESTED"
    LIVE_TESTED = "LIVE_TESTED"
    AVAILABLE_NOW = "AVAILABLE_NOW"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    UNAVAILABLE = "UNAVAILABLE"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"  # the pack is not enabled, or has no approved backend/identity configured
    NO_IDENTITY = "no_identity"  # configured, but no valid identity (missing, expired, refused kind)
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_REGION = "unsupported_region"
    DEPENDENCY_MISSING = "dependency_missing"
    PROBE_FAILED = "probe_failed"
    NOT_IMPLEMENTED = "not_implemented"  # a definition with no implementation (a placeholder)


@dataclass(frozen=True)
class Availability:
    """Whether a capability is usable now. Evidence, never authorization: the Tool Gateway still decides."""

    status: AvailabilityStatus
    checked_at: str | None = None
    #: A short, fixed, secret-free explanation (never an exception text, an ARN, or a key).
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status is AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "available": self.available, "checked_at": self.checked_at,
                "detail": self.detail}  # fmt: skip


@dataclass(frozen=True)
class Evidence:
    """One reason Agent Factory believes something about a capability, with its provenance."""

    kind: EvidenceKind
    #: Where it comes from: ``tool:read_file``, ``tests/test_x.py::test_y``, ``probe:fake``, ``policy:destructive_delete``.
    source: str
    detail: str = ""
    observed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "source": self.source, "detail": self.detail, "observed_at": self.observed_at}



def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """A plain, JSON-ready copy of a frozen schema."""
    if isinstance(value, MappingProxyType | dict):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class Capability:
    """One version of one capability. Immutable: schemas are frozen on construction."""

    capability_id: str
    version: int
    pack: str
    description: str
    #: The tool name in the ToolRegistry, or None for a non-executable definition (never available).
    implemented_by: str | None
    category: ActionCategory
    effect: EffectClass
    approval: ApprovalRule
    cost: CostImpact
    risk: Risk
    #: Whether it changes state outside the run's own records (a file, a resource, a commit).
    mutates: bool
    network: NetworkRequirement = NetworkRequirement.NONE
    #: An abstract credential slot (``aws.dev.readonly``), or None. Never a profile name, never a credential.
    credential: str | None = None
    sandbox_compatible: bool = False
    input_schema: Any = field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    output_schema: Any = field(default_factory=lambda: {"type": "object"})
    #: Agent Factory permissions it needs (checked against the implementing tool at registration).
    required_permissions: tuple[str, ...] = ()
    #: Documented IAM actions for a cloud capability (never used to authorize; never a wildcard).
    iam_actions: tuple[str, ...] = ()
    #: Named offline tests that exercise it: a TESTED claim is checked against the test files.
    tests: tuple[str, ...] = ()
    #: Documentation references. Never counted as evidence of anything.
    documentation: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, enum in (("category", ActionCategory), ("effect", EffectClass), ("approval", ApprovalRule),
                           ("cost", CostImpact), ("risk", Risk), ("network", NetworkRequirement)):  # fmt: skip
            try:
                object.__setattr__(self, name, enum(getattr(self, name)))
            except ValueError:
                raise CapabilityError(f"{self.capability_id!r}: invalid {name} {getattr(self, name)!r}") from None
        for name in ("required_permissions", "iam_actions", "tests", "documentation", "tags"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))
        check_definition(self)

    @property
    def ref(self) -> str:
        return f"{self.capability_id}@{self.version}"

    @property
    def namespace(self) -> str:
        return self.capability_id.split(".", 1)[0]

    @property
    def executable(self) -> bool:
        return self.implemented_by is not None


#: IAM action prefixes that change identity, credentials, or the organization: a DENIED class, never approval.
DENIED_IAM_PREFIXES = ("iam:", "organizations:", "account:", "sso:", "sso-admin:", "identitystore:",
                       "sts:AssumeRole", "sts:GetFederationToken", "sts:GetSessionToken")  # fmt: skip
_IAM_ACTION_RE = re.compile(r"^[a-z0-9-]{2,40}:[A-Za-z0-9]{2,80}$")
_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "propertyNames", "items", "enum",
                          "minLength", "maxLength", "pattern", "minItems", "maxItems", "maxProperties", "minimum",
                          "maximum", "description"})  # fmt: skip
_SCHEMA_TYPES = frozenset({"object", "array", "string", "boolean", "integer", "number"})
#: Strings a model-visible description must never contain.
_UNSAFE_TEXT_RE = re.compile(r"arn:|\b\d{12}\b|AKIA|ASIA|secret|password|token|https?://|[A-Za-z]:\\|/home/|/Users/", re.I)


def check_schema(schema: Any, where: str) -> None:
    """A schema in the strict subset agents/schema.py validates: known keywords and types only."""
    if not isinstance(schema, MappingProxyType | dict):
        raise CapabilityError(f"{where}: a schema must be an object")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise CapabilityError(f"{where}: unsupported schema keywords {sorted(unknown)}")
    kind = schema.get("type")
    if kind is not None and kind not in _SCHEMA_TYPES:
        raise CapabilityError(f"{where}: unsupported type {kind!r}")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error:
            raise CapabilityError(f"{where}: invalid pattern") from None
    for name, sub in (schema.get("properties") or {}).items():
        check_schema(sub, f"{where}.{name}")
    for key in ("items", "propertyNames"):
        if key in schema:
            check_schema(schema[key], f"{where}.{key}")
    extra = schema.get("additionalProperties")
    if extra is not None and not isinstance(extra, bool):
        check_schema(extra, f"{where}.additionalProperties")
    required = schema.get("required") or ()
    if not set(required) <= set(schema.get("properties") or {}):
        raise CapabilityError(f"{where}: 'required' names a property that is not defined")


def check_definition(cap: Capability) -> None:
    """Everything that can be checked about one definition alone. Contradictions are refused, never repaired."""
    cid = cap.capability_id
    if not isinstance(cid, str) or not CAPABILITY_ID_RE.fullmatch(cid):
        raise CapabilityError(f"malformed capability id {cid!r}")
    if isinstance(cap.version, bool) or not isinstance(cap.version, int) or cap.version < 1:
        raise CapabilityError(f"{cid}: version must be a positive integer")
    if cap.namespace != cap.pack:
        raise CapabilityError(f"{cid}: the namespace {cap.namespace!r} does not belong to pack {cap.pack!r}")
    if not cap.description or len(cap.description) > MAX_DESCRIPTION or _UNSAFE_TEXT_RE.search(cap.description):
        raise CapabilityError(f"{cid}: the description must be 1-{MAX_DESCRIPTION} safe characters (no ARNs, IDs, "
                              "URLs, paths, or credential words)")  # fmt: skip
    if cap.implemented_by is not None and not re.fullmatch(r"[a-z][a-z0-9_]{1,60}", cap.implemented_by):
        raise CapabilityError(f"{cid}: implemented_by must be a tool name")
    check_schema(cap.input_schema, f"{cid}.input_schema")
    check_schema(cap.output_schema, f"{cid}.output_schema")
    if cap.input_schema.get("type") != "object" or cap.input_schema.get("additionalProperties") is not False:
        raise CapabilityError(f"{cid}: the input schema must be a closed object (typed parameters only)")
    # Effects, costs, and approvals must agree; the stricter reading is never inferred, it must be declared.
    if cap.effect is EffectClass.READ_ONLY and cap.mutates:
        raise CapabilityError(f"{cid}: a read-only capability cannot mutate")
    if not cap.mutates and cap.cost is CostImpact.RECURRING:
        raise CapabilityError(f"{cid}: only something that creates a resource can have a recurring cost")
    if cap.mutates and cap.cost in (CostImpact.UNKNOWN, CostImpact.RECURRING) and cap.approval is ApprovalRule.POLICY:
        raise CapabilityError(f"{cid}: a mutation with {cap.cost.value} cost must always require approval")
    from ..safety.permissions import DANGEROUS_CATEGORIES

    if cap.category in DANGEROUS_CATEGORIES and cap.approval is ApprovalRule.POLICY:
        raise CapabilityError(f"{cid}: a {cap.category.value} capability must declare approval 'always' or 'denied'")
    for action in cap.iam_actions:
        if "*" in action or not _IAM_ACTION_RE.fullmatch(action):
            raise CapabilityError(f"{cid}: IAM action {action!r} must be one exact action (no wildcards)")
    if any(a.startswith(DENIED_IAM_PREFIXES) for a in cap.iam_actions) and cap.approval is not ApprovalRule.DENIED:
        raise CapabilityError(f"{cid}: identity, credential, and organization changes are a denied class")
    if cap.category is ActionCategory.CREDENTIAL_CHANGE and cap.approval is not ApprovalRule.DENIED:
        raise CapabilityError(f"{cid}: credential changes are a denied class")
    if cap.credential is not None and not CREDENTIAL_SLOT_RE.fullmatch(cap.credential):
        raise CapabilityError(f"{cid}: a credential must be an abstract slot like 'aws.dev.readonly'")
    if cap.sandbox_compatible and (cap.credential is not None or cap.network is not NetworkRequirement.NONE):
        raise CapabilityError(f"{cid}: nothing that needs a credential or the network may run in the sandbox")
    if cap.network is NetworkRequirement.TRUSTED_ADAPTER and cap.category not in (
            ActionCategory.NETWORK_ACCESS, ActionCategory.CLOUD_READ) and cap.category not in DANGEROUS_CATEGORIES:  # fmt: skip
        raise CapabilityError(f"{cid}: a capability that uses the network must be in a network, cloud_read, or dangerous category")
    if cap.category is ActionCategory.CLOUD_READ:
        check_cloud_read(cap)
    elif cap.network is NetworkRequirement.TRUSTED_ADAPTER and cap.mutates and cap.approval is ApprovalRule.POLICY:
        raise CapabilityError(f"{cid}: a cloud mutation must always require approval (or be denied)")
    for test in cap.tests:
        if not TEST_ID_RE.fullmatch(test):
            raise CapabilityError(f"{cid}: test {test!r} must be 'tests/<file>.py::test_<name>'")
    expected = {f"tool:{cap.implemented_by}", f"category:{cap.category.value}"} if cap.executable else set()
    if set(cap.required_permissions) != expected:
        raise CapabilityError(f"{cid}: required_permissions must be exactly {sorted(expected)}")


#: The verbs of read-only IAM actions. A cloud_read capability may document nothing else.
READ_ONLY_IAM_VERBS = ("Get", "List", "Describe", "Head", "Lookup")


def _bounded_strings(schema: Any) -> bool:
    """Every string in the schema has a length limit, a pattern, or an enum; every object is closed or bounded."""
    if not isinstance(schema, MappingProxyType | dict):
        return True
    if schema.get("type") == "string" and not ({"maxLength", "pattern", "enum"} & set(schema)):
        return False
    if schema.get("type") == "array" and "maxItems" not in schema:
        return False
    subs = list((schema.get("properties") or {}).values())
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), MappingProxyType | dict):
            subs.append(schema[key])
    return all(_bounded_strings(s) for s in subs)


def check_cloud_read(cap: Capability) -> None:
    """The cloud_read invariants: typed, bounded, read-only, through a trusted adapter with an approved slot."""
    cid = cap.capability_id
    if cap.effect is not EffectClass.READ_ONLY or cap.mutates:
        raise CapabilityError(f"{cid}: a cloud_read capability is read-only and never mutates")
    if cap.cost not in (CostImpact.NONE, CostImpact.LOW):
        raise CapabilityError(f"{cid}: a cloud_read capability cannot have {cap.cost.value} cost")
    if cap.network is not NetworkRequirement.TRUSTED_ADAPTER or cap.sandbox_compatible:
        raise CapabilityError(f"{cid}: a cloud_read capability runs only in the trusted adapter, never in the sandbox")
    if cap.credential is None:
        raise CapabilityError(f"{cid}: a cloud_read capability needs an approved credential slot")
    if not cap.iam_actions or any(a.split(":", 1)[1].startswith(READ_ONLY_IAM_VERBS) is False for a in cap.iam_actions):
        raise CapabilityError(f"{cid}: a cloud_read capability documents only read-only IAM actions (Get/List/Describe/...)")
    if not _bounded_strings(cap.input_schema):
        raise CapabilityError(f"{cid}: a cloud_read capability's input must be bounded (every string and list limited)")
    if cap.output_schema.get("type") != "object" or cap.output_schema.get("additionalProperties") is not False:
        raise CapabilityError(f"{cid}: a cloud_read capability's output must be a closed object")
    if not _bounded_strings(cap.output_schema):
        raise CapabilityError(f"{cid}: a cloud_read capability's output must be bounded")


def effective_decision(policy_decision: Decision, cap: Capability) -> Decision:
    """The decision after the capability's own rules: it can only tighten the policy's, never loosen it."""
    if policy_decision is Decision.DENY or cap.approval is ApprovalRule.DENIED:
        return Decision.DENY
    if cap.approval is ApprovalRule.ALWAYS:
        return Decision.REQUIRE_APPROVAL
    if cap.mutates and cap.cost in (CostImpact.UNKNOWN, CostImpact.RECURRING):
        return Decision.REQUIRE_APPROVAL
    return policy_decision


__all__ = [
    "CAPABILITY_ID_RE",
    "CREDENTIAL_SLOT_RE",
    "DENIED_IAM_PREFIXES",
    "ApprovalRule",
    "Availability",
    "AvailabilityStatus",
    "Capability",
    "CapabilityError",
    "CostImpact",
    "Evidence",
    "EvidenceKind",
    "NetworkRequirement",
    "Risk",
    "check_definition",
    "check_schema",
    "effective_decision",
    "thaw",
]

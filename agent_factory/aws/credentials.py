"""Credential slots and the refusal of long-term keys (docs/aws-capabilities.md).

A capability names an abstract **slot** (``aws.dev.readonly``), never a profile and never a credential.
Configuration binds a slot to one explicitly named profile. Before any call, the backend reports the
*kind* of credential that profile resolves to (metadata only, never the values), and Agent Factory
refuses everything but approved temporary kinds. There is no fallback: an unbound slot, a refused
profile name, or a long-term key stops the capability; nothing tries the default profile, another
profile, or the environment instead.

Nothing in this module reads, holds, or prints a credential.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from ..capabilities.model import CREDENTIAL_SLOT_RE

#: Credential kinds that are temporary and issued by an approved mechanism (docs/design/capability-registry-aws.md
#: section 9). The names are botocore 1.43.57's provider ``METHOD`` labels (verified in its source); anything else
#: is refused. A workload's own role (``iam-role``, ``container-role``) is never used directly: in the cloud it is
#: only the source of an explicit AssumeRole into a capability-family role. Each backend may accept fewer.
TEMPORARY_KINDS = frozenset({"sso", "login", "assume-role", "assume-role-with-web-identity"})
#: Long-term static keys: in a credentials file, the environment, a config file, or passed explicitly.
LONG_TERM_KINDS = frozenset({"shared-credentials-file", "env", "config-file", "explicit", "static"})
#: Profiles the owner placed out of scope for Agent Factory (2026-10-06): never bound, whatever they hold.
#: ``default`` is also the implicit fallback of every SDK, which Agent Factory never uses.
OUT_OF_SCOPE_PROFILES = frozenset({"default", "internal-dev"})
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CredentialRefused(Exception):  # noqa: N818 - a refusal, reported with a fixed reason
    """A slot, binding, or credential kind is not acceptable. The message never contains a credential."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SlotBinding:
    """Configuration: slot -> one explicitly named profile. A name, never a credential."""

    slot: str
    profile: str

    def __post_init__(self) -> None:
        if not CREDENTIAL_SLOT_RE.fullmatch(self.slot) or not self.slot.startswith("aws."):
            raise CredentialRefused("not an AWS credential slot")
        if not _PROFILE_RE.fullmatch(self.profile):
            raise CredentialRefused("a slot must name one profile explicitly")
        if self.profile in OUT_OF_SCOPE_PROFILES:
            raise CredentialRefused(f"profile {self.profile!r} is out of scope for Agent Factory")


@dataclass(frozen=True)
class CredentialMetadata:
    """What a backend may report about the credential behind a binding: never a key, token, or account ID."""

    slot: str
    profile: str
    kind: str
    temporary: bool
    #: When the temporary credential stops working (timezone-aware). None means unknown, which is refused.
    expires_at: datetime | None = None


#: A temporary credential this close to its expiry is treated as expired: a call must not start on borrowed time.
EXPIRY_MARGIN = timedelta(seconds=60)


def slot_environment(slot: str) -> str:
    """``aws.dev.identity`` -> ``dev``."""
    return slot.split(".")[1]


def check_credential(binding: SlotBinding, metadata: CredentialMetadata, *, now: datetime | None = None) -> None:
    """Refuse anything but an approved, unexpired temporary credential from exactly the bound profile."""
    if metadata.slot != binding.slot or metadata.profile != binding.profile:
        raise CredentialRefused("the credential does not come from the bound profile (no fallback to another)")
    if metadata.kind in LONG_TERM_KINDS:
        raise CredentialRefused("long-term access keys are refused; use a temporary credential (SSO, aws login, a role)")
    if metadata.kind not in TEMPORARY_KINDS or metadata.temporary is not True:
        raise CredentialRefused("unsupported credential type: only approved temporary credentials are accepted")
    now = now or datetime.now(timezone.utc)
    if metadata.expires_at is None or metadata.expires_at.tzinfo is None or metadata.expires_at <= now + EXPIRY_MARGIN:
        raise CredentialRefused("the temporary credential is expired, about to expire, or has no known expiry")


class CredentialSlots:
    """The slot bindings an installation configured. Unbound slots are refused, never defaulted."""

    def __init__(self, bindings: Mapping[str, str] | None = None) -> None:
        resolved = {slot: SlotBinding(slot, profile) for slot, profile in (bindings or {}).items()}
        self._bindings = MappingProxyType(resolved)

    def resolve(self, slot: str) -> SlotBinding:
        binding = self._bindings.get(slot)
        if binding is None:
            raise CredentialRefused(f"credential slot {slot!r} is not configured")
        return binding

    def slots(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))


__all__ = [
    "LONG_TERM_KINDS",
    "OUT_OF_SCOPE_PROFILES",
    "TEMPORARY_KINDS",
    "CredentialMetadata",
    "CredentialRefused",
    "CredentialSlots",
    "SlotBinding",
    "EXPIRY_MARGIN",
    "check_credential",
    "slot_environment",
]

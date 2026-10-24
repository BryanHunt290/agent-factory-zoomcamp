"""Principals and permissions: who may use the product API, and what each may do.

*Authentication* (in the API layer, docs/authentication.md) answers "who is
making this request?": it verifies a signed token from a trusted OpenID
Connect issuer and maps the token's issuer and subject to a
:class:`Principal`, Agent Factory's own stable identity. *Authorization*
answers "what may that identity do?": every API operation requires one
:class:`Permission`, and a principal holds only the permissions granted to it.

* **Identity is (issuer, subject)**, the pair an OpenID Connect provider
  promises never to reassign, mapped once to an internal ``prn-`` ID. An
  email address or a display name is never an identity: both can change, and
  are kept only to show who a principal is.
* **Enrollment is explicit.** The owner enrolls principals and grants
  permissions with the local CLI. A valid token for anyone else is refused;
  no HTTP route creates a principal or changes a permission.
* **A disabled principal cannot authenticate**, whatever it was granted.
* **No permission implies another.** ``approvals:read`` does not allow
  deciding, ``approvals:decide`` does not allow reconciling an uncertain
  action (``actions:reconcile``), and creating projects does not allow
  reading them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum

from .errors import DomainValidationError
from .identity import Actor
from .ids import PRINCIPAL_ID_RE
from .serialization import check_enum, check_optional_text, check_pattern, check_timestamp, parse_timestamp


class Permission(StrEnum):
    """Everything an API caller can be allowed to do. Each API operation requires exactly one."""

    PROJECTS_READ = "projects:read"  # projects, their summaries, and the template catalog
    PROJECTS_CREATE = "projects:create"
    RUNS_READ = "runs:read"
    MILESTONES_READ = "milestones:read"
    APPROVALS_READ = "approvals:read"
    APPROVALS_DECIDE = "approvals:decide"
    ACTIONS_RECONCILE = "actions:reconcile"  # settle an uncertain action's outcome (reconcileAction)


ALL_PERMISSIONS = frozenset(Permission)


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


#: An OpenID Connect issuer identifier: printable ASCII, no spaces (the API requires https in production).
ISSUER_RE = re.compile(r"^[!-~]{1,512}$")
#: OpenID Connect: "MUST NOT exceed 255 ASCII characters in length".
SUBJECT_RE = re.compile(r"^[!-~]{1,255}$")
MAX_DISPLAY_NAME = 200
MAX_EMAIL = 320


def check_permissions(values: Iterable[object], field: str = "permissions") -> frozenset[Permission]:
    """Permissions from the closed vocabulary; anything else is refused, never ignored."""
    if isinstance(values, (str, bytes)):
        raise DomainValidationError(f"{field}: expected a collection of permissions")
    return frozenset(check_enum(Permission, value, field) for value in values)


@dataclass(frozen=True)
class Principal:
    """An enrolled identity: who a token's (issuer, subject) is, inside Agent Factory."""

    id: str
    issuer: str
    subject: str
    status: PrincipalStatus
    created_at: str
    updated_at: str
    display_name: str | None = None  # shown to people; never used to identify
    email: str | None = None  # shown to people; never used to identify

    def __post_init__(self) -> None:
        check_pattern(self.id, PRINCIPAL_ID_RE, "principal.id")
        check_pattern(self.issuer, ISSUER_RE, "principal.issuer")
        check_pattern(self.subject, SUBJECT_RE, "principal.subject")
        object.__setattr__(self, "status", check_enum(PrincipalStatus, self.status, "principal.status"))
        check_timestamp(self.created_at, "principal.created_at")
        check_timestamp(self.updated_at, "principal.updated_at")
        if parse_timestamp(self.updated_at) < parse_timestamp(self.created_at):
            raise DomainValidationError("principal: updated_at is before created_at")
        check_optional_text(self.display_name, "principal.display_name", max_len=MAX_DISPLAY_NAME)
        check_optional_text(self.email, "principal.email", max_len=MAX_EMAIL)

    @property
    def active(self) -> bool:
        return self.status is PrincipalStatus.ACTIVE

    @property
    def external_identity(self) -> tuple[str, str]:
        return (self.issuer, self.subject)

    def with_profile(self, *, display_name: str | None, email: str | None, at: str) -> Principal:
        """The same identity with new display information (the issuer may change both at any time)."""
        return replace(self, display_name=display_name, email=email, updated_at=at)

    def with_status(self, status: PrincipalStatus, at: str) -> Principal:
        return replace(self, status=status, updated_at=at)

    def actor(self) -> Actor:
        """This principal as the verified actor of a decision."""
        return Actor.authenticated(self.id, self.display_name or self.id)

    @staticmethod
    def check_successor(old: Principal, new: Principal) -> None:
        """Profile and status may change; the ID, the (issuer, subject) identity, and creation time never do."""
        if (old.id, old.issuer, old.subject, old.created_at) != (new.id, new.issuer, new.subject, new.created_at):
            raise DomainValidationError(f"principal {old.id}: its identity never changes")
        if parse_timestamp(new.updated_at) < parse_timestamp(old.updated_at):
            raise DomainValidationError(f"principal {old.id}: updated_at went backwards")


__all__ = [
    "ALL_PERMISSIONS",
    "ISSUER_RE",
    "SUBJECT_RE",
    "Permission",
    "Principal",
    "PrincipalStatus",
    "check_permissions",
]

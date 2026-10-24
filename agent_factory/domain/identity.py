"""Who made a decision.

* ``USER``: an authenticated principal (domain/principals.py). Its ``id`` is
  the principal's ``prn-`` ID and it is always ``verified``: only the API
  creates one, from a verified token, never from anything a client sends.
* ``UNAUTHENTICATED``: the local CLI, with a free-text name. Never verified.
* ``SYSTEM``: an automated policy, e.g. expiry (future).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import DomainValidationError
from .ids import PRINCIPAL_ID_RE
from .serialization import check_bool, check_enum, check_fields, check_text


class ActorKind(StrEnum):
    UNAUTHENTICATED = "unauthenticated"  # local CLI, free-text name, nothing verified
    USER = "user"  # an authenticated principal
    SYSTEM = "system"  # an automated policy, e.g. expiry (future)


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    id: str
    display_name: str
    verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", check_enum(ActorKind, self.kind, "actor.kind"))
        check_text(self.id, "actor.id", max_len=200)
        check_text(self.display_name, "actor.display_name", max_len=200)
        check_bool(self.verified, "actor.verified")
        if self.kind is ActorKind.UNAUTHENTICATED and self.verified:
            raise DomainValidationError("actor: an unauthenticated actor cannot be marked verified")
        if self.kind is ActorKind.USER and not (self.verified and PRINCIPAL_ID_RE.fullmatch(self.id)):
            raise DomainValidationError("actor: a user is an authenticated principal (verified, with a prn- ID)")

    @classmethod
    def authenticated(cls, principal_id: str, display_name: str) -> Actor:
        """An authenticated principal. Only the API's authentication creates one."""
        return cls(kind=ActorKind.USER, id=principal_id, display_name=display_name, verified=True)

    @classmethod
    def unauthenticated(cls, name: str) -> Actor:
        name = (name or "").strip() or "unknown"
        return cls(kind=ActorKind.UNAUTHENTICATED, id=f"local:{name}", display_name=name, verified=False)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "id": self.id, "display_name": self.display_name, "verified": self.verified}

    @classmethod
    def from_dict(cls, data: Any) -> Actor:
        if not isinstance(data, dict):
            raise DomainValidationError("actor: expected an object")
        check_fields(data, required=("kind", "id", "display_name", "verified"), where="actor")
        return cls(kind=data["kind"], id=data["id"], display_name=data["display_name"], verified=data["verified"])

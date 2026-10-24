"""Authentication: from an HTTP request to an enrolled, active principal, or a refusal.

1. Exactly one ``Authorization: Bearer <token>`` header (RFC 6750).
2. The token verifies (``TokenVerifier``): signature, issuer, audience, times.
3. The token's (issuer, subject) is an enrolled principal: an identity the
   owner enrolled, never one created because a token arrived.
4. The principal is active: a disabled principal cannot authenticate, whatever
   its permissions.
5. Its permissions are read now, on every request, so a revoked permission or
   a disabled principal takes effect at once.

The token's name and email claims refresh the principal's display information;
they never change who the principal is. Refused credentials are counted per
client address (``ratelimit.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ...domain.identity import Actor
from ...domain.principals import Permission, Principal
from ...domain.repositories import ProductStore
from ...util import Clock, timestamp, utc_now
from .ratelimit import SlidingWindowLimiter
from .tokens import AuthenticationFailed, AuthFailure, TokenVerifier, VerifiedToken

DEFAULT_FAILED_ATTEMPTS_PER_MINUTE = 10
DEFAULT_DECISIONS_PER_MINUTE = 30


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited for {retry_after} s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """The trusted result of authentication: who is calling, and what they may do."""

    principal: Principal
    permissions: frozenset[Permission]
    token_expires_at: datetime

    @property
    def id(self) -> str:
        return self.principal.id

    def allows(self, permission: Permission) -> bool:
        return permission in self.permissions

    def actor(self) -> Actor:
        return self.principal.actor()


class Authenticator:
    def __init__(
        self,
        verifier: TokenVerifier,
        *,
        clock: Clock = utc_now,
        failures: SlidingWindowLimiter | None = None,
        decisions: SlidingWindowLimiter | None = None,
    ) -> None:
        self.verifier = verifier
        self.clock = clock
        self.failures = failures or SlidingWindowLimiter(DEFAULT_FAILED_ATTEMPTS_PER_MINUTE, timedelta(minutes=1))
        self.decisions = decisions or SlidingWindowLimiter(DEFAULT_DECISIONS_PER_MINUTE, timedelta(minutes=1))

    def authenticate(self, authorization: Sequence[str], client: str, store: ProductStore) -> AuthenticatedPrincipal:
        """Raises ``AuthenticationFailed`` (401), ``RateLimited`` (429), or ``KeysUnavailable`` (503)."""
        if not authorization:
            raise AuthenticationFailed(AuthFailure.MISSING)
        retry_after = self.failures.retry_after(client)
        if retry_after is not None:
            raise RateLimited(retry_after)
        try:
            verified = self.verifier.verify(_bearer_token(authorization))
            return self._principal(verified, store)
        except AuthenticationFailed:
            self.failures.record(client)
            raise

    def _principal(self, verified: VerifiedToken, store: ProductStore) -> AuthenticatedPrincipal:
        principal = store.principals.find(verified.issuer, verified.subject)
        if principal is None:
            raise AuthenticationFailed(AuthFailure.UNKNOWN_PRINCIPAL)  # never enrolled: no access
        if not principal.active:
            raise AuthenticationFailed(AuthFailure.DISABLED_PRINCIPAL)
        principal = self._refresh_profile(principal, verified, store)
        return AuthenticatedPrincipal(principal, store.principals.permissions(principal.id), verified.expires_at)

    def _refresh_profile(self, principal: Principal, verified: VerifiedToken, store: ProductStore) -> Principal:
        """Follow the provider's display name and email; the identity itself never changes."""
        name = verified.display_name or principal.display_name
        email = verified.email or principal.email
        if (name, email) == (principal.display_name, principal.email):
            return principal
        updated = principal.with_profile(display_name=name, email=email, at=timestamp(self.clock))
        store.principals.update(updated)
        return updated


def _bearer_token(authorization: Sequence[str]) -> str:
    """The token of the one ``Bearer`` Authorization header; anything else is malformed."""
    if len(authorization) != 1:
        raise AuthenticationFailed(AuthFailure.MALFORMED)
    scheme, _, token = authorization[0].strip().partition(" ")
    token = token.strip(" ")
    if scheme.lower() != "bearer" or not token or " " in token:
        raise AuthenticationFailed(AuthFailure.MALFORMED)
    return token


__all__ = ["AuthenticatedPrincipal", "Authenticator", "RateLimited"]

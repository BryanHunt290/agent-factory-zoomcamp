"""Verifying an access token: a JWT signed by the trusted OpenID Connect issuer.

The API is an OAuth 2.0 *resource server*: it never sees a password and never
issues tokens. It accepts ``Authorization: Bearer <token>`` (RFC 6750) and
trusts the token only after all of these hold (RFC 7519, RFC 8725):

* it is a compact JWS (three parts), not an unsigned or encrypted token;
* its ``alg`` is on the configured allowlist of *asymmetric* algorithms
  (``none`` and the HMAC family are never accepted, whatever the token
  says), and the key it names (``kid``) was published by the issuer for that
  algorithm: the key, not the token, decides how the signature is checked,
  which prevents algorithm-confusion attacks;
* the signature verifies, with keys of a safe minimum length;
* ``iss`` is exactly the configured issuer and ``aud`` contains the
  configured audience;
* ``exp``, ``iat``, and ``sub`` are present; it has not expired and is not
  used before ``nbf`` (a small leeway allows for clock skew); it was not
  issued in the future, and it was not issued for longer than
  ``max_lifetime``;
* ``sub`` is a valid OpenID Connect subject.

Every failure is one :class:`AuthFailure` category, logged (never with the
token) and answered with the same 401: the client learns only that its
credential was refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol

import jwt
from jwt import exceptions as jwt_errors

from ...domain.principals import MAX_DISPLAY_NAME, MAX_EMAIL, SUBJECT_RE
from .keys import UnknownKey

#: Algorithms a token may use, if configured: all asymmetric. The API holds no signing secret.
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
#: A compact JWS: base64url header . payload . signature (an encrypted JWE has five parts).
COMPACT_JWS_RE = re.compile(r"^[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}$")
MAX_TOKEN_LENGTH = 8192
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class AuthFailure(StrEnum):
    """Why a request was not authenticated. Logged; never shown to the client in more detail than 401."""

    MISSING = "missing_credential"
    MALFORMED = "malformed_credential"
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"
    UNKNOWN_KEY = "unknown_key"
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    INVALID_CLAIMS = "invalid_claims"
    UNKNOWN_PRINCIPAL = "unknown_principal"
    DISABLED_PRINCIPAL = "disabled_principal"


class AuthenticationFailed(Exception):
    def __init__(self, failure: AuthFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class KeySource(Protocol):
    def key(self, kid: str, algorithm: str) -> jwt.PyJWK: ...


@dataclass(frozen=True)
class TokenPolicy:
    """What a token must say to be accepted."""

    issuer: str
    audience: str
    algorithms: tuple[str, ...]
    leeway: timedelta = timedelta(seconds=30)
    max_lifetime: timedelta = timedelta(hours=24)
    #: If set, the header ``typ`` must equal it (RFC 9068 access tokens use "at+jwt").
    token_type: str | None = None

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("a token policy needs an issuer and an audience")
        if not self.algorithms or not set(self.algorithms) <= ASYMMETRIC_ALGORITHMS:
            raise ValueError(f"token algorithms must be a non-empty subset of {sorted(ASYMMETRIC_ALGORITHMS)}")
        if not timedelta(0) <= self.leeway <= timedelta(minutes=5):
            raise ValueError("the clock-skew leeway must be between 0 and 5 minutes")
        if self.max_lifetime <= timedelta(0):
            raise ValueError("the maximum token lifetime must be positive")


@dataclass(frozen=True)
class VerifiedToken:
    """The facts a verified token establishes. Profile fields are for display only."""

    issuer: str
    subject: str
    expires_at: datetime
    display_name: str | None = None
    email: str | None = None


def _display(value: Any, max_len: int) -> str | None:
    """A profile claim worth showing, or None. Never a reason to refuse a token."""
    if not isinstance(value, str) or not value.strip() or len(value) > max_len or _CONTROL.search(value):
        return None
    return value


class TokenVerifier:
    def __init__(self, policy: TokenPolicy, keys: KeySource) -> None:
        self.policy = policy
        self.keys = keys

    def verify(self, token: str) -> VerifiedToken:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or not COMPACT_JWS_RE.fullmatch(token):
            raise AuthenticationFailed(AuthFailure.MALFORMED)
        try:
            header = jwt.get_unverified_header(token)  # only to choose the key; nothing in it is trusted yet
        except jwt_errors.PyJWTError:
            raise AuthenticationFailed(AuthFailure.MALFORMED) from None
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in self.policy.algorithms:
            raise AuthenticationFailed(AuthFailure.UNSUPPORTED_ALGORITHM)
        if "crit" in header:  # no critical header extensions are understood, so none may be required
            raise AuthenticationFailed(AuthFailure.MALFORMED)
        if self.policy.token_type is not None and str(header.get("typ", "")).lower() != self.policy.token_type.lower():
            raise AuthenticationFailed(AuthFailure.INVALID_CLAIMS)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationFailed(AuthFailure.UNKNOWN_KEY)
        try:
            key = self.keys.key(kid, algorithm)  # KeysUnavailable propagates: 503, not the client's fault
        except UnknownKey:
            raise AuthenticationFailed(AuthFailure.UNKNOWN_KEY) from None
        claims = self._decode(token, key, algorithm)
        return self._facts(claims)

    def _decode(self, token: str, key: jwt.PyJWK, algorithm: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=self.policy.audience,
                issuer=self.policy.issuer,
                leeway=self.policy.leeway,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "enforce_minimum_key_length": True,
                },
            )
        except jwt_errors.ExpiredSignatureError:
            raise AuthenticationFailed(AuthFailure.EXPIRED) from None
        except jwt_errors.ImmatureSignatureError:
            raise AuthenticationFailed(AuthFailure.NOT_YET_VALID) from None
        except jwt_errors.InvalidIssuerError:
            raise AuthenticationFailed(AuthFailure.WRONG_ISSUER) from None
        except jwt_errors.InvalidAudienceError:
            raise AuthenticationFailed(AuthFailure.WRONG_AUDIENCE) from None
        except jwt_errors.InvalidSignatureError:
            raise AuthenticationFailed(AuthFailure.INVALID_SIGNATURE) from None
        except jwt_errors.InvalidAlgorithmError:
            raise AuthenticationFailed(AuthFailure.UNSUPPORTED_ALGORITHM) from None
        except jwt_errors.InvalidKeyError:
            raise AuthenticationFailed(AuthFailure.UNKNOWN_KEY) from None  # e.g. a key below the minimum length
        except jwt_errors.DecodeError:
            raise AuthenticationFailed(AuthFailure.MALFORMED) from None
        except jwt_errors.InvalidTokenError:  # missing or malformed claims, and anything else PyJWT refuses
            raise AuthenticationFailed(AuthFailure.INVALID_CLAIMS) from None

    def _facts(self, claims: dict[str, Any]) -> VerifiedToken:
        subject, issued, expires = claims.get("sub"), claims.get("iat"), claims.get("exp")
        if not isinstance(subject, str) or not SUBJECT_RE.fullmatch(subject):
            raise AuthenticationFailed(AuthFailure.INVALID_CLAIMS)
        if isinstance(issued, bool) or isinstance(expires, bool):
            raise AuthenticationFailed(AuthFailure.INVALID_CLAIMS)
        if not isinstance(issued, (int, float)) or not isinstance(expires, (int, float)):
            raise AuthenticationFailed(AuthFailure.INVALID_CLAIMS)
        if timedelta(seconds=expires - issued) > self.policy.max_lifetime:
            raise AuthenticationFailed(AuthFailure.INVALID_CLAIMS)
        return VerifiedToken(
            issuer=self.policy.issuer,
            subject=subject,
            expires_at=datetime.fromtimestamp(expires, tz=timezone.utc),
            display_name=_display(claims.get("name") or claims.get("preferred_username"), MAX_DISPLAY_NAME),
            email=_display(claims.get("email"), MAX_EMAIL),
        )


__all__ = [
    "ASYMMETRIC_ALGORITHMS",
    "AuthFailure",
    "AuthenticationFailed",
    "KeySource",
    "TokenPolicy",
    "TokenVerifier",
    "VerifiedToken",
]

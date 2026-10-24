"""Authentication configuration: production by default, refusing anything unsafe.

``AGENT_FACTORY_AUTH_MODE`` selects one of two modes. Production is the
default, so forgetting to configure authentication stops the API instead of
opening it.

**production** (the default): tokens from an external OpenID Connect provider.

* ``AGENT_FACTORY_AUTH_ISSUER``: the provider's issuer identifier, an https
  URL compared exactly with each token's ``iss``;
* ``AGENT_FACTORY_AUTH_AUDIENCE``: the audience the provider puts in tokens
  meant for this API;
* ``AGENT_FACTORY_AUTH_JWKS_URL``: the provider's https JWKS document;
* ``AGENT_FACTORY_AUTH_ALGORITHMS`` (optional, default ``RS256,ES256``):
  asymmetric algorithms only;
* ``AGENT_FACTORY_AUTH_TOKEN_TYPE`` (optional): a required ``typ`` header,
  e.g. ``at+jwt``.

**development** (explicit opt-in, DEVELOPMENT ONLY): tokens from the local
development issuer, whose public keys are in
``AGENT_FACTORY_AUTH_DEV_ISSUER_DIR`` (development.py).

The two modes never mix: production refuses the development directory and
issuer, development refuses production settings, and both kinds of setting
at once are refused in either mode.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from ...util import Clock, utc_now
from .authenticator import DEFAULT_DECISIONS_PER_MINUTE, DEFAULT_FAILED_ATTEMPTS_PER_MINUTE, Authenticator
from .development import DEV_ALGORITHM, DEV_AUDIENCE, DEV_ISSUER, load_public_jwks
from .keys import RemoteKeySet, StaticKeySet, fetch_https
from .ratelimit import SlidingWindowLimiter
from .tokens import TokenPolicy, TokenVerifier

MODE_ENV = "AGENT_FACTORY_AUTH_MODE"
ISSUER_ENV = "AGENT_FACTORY_AUTH_ISSUER"
AUDIENCE_ENV = "AGENT_FACTORY_AUTH_AUDIENCE"
JWKS_URL_ENV = "AGENT_FACTORY_AUTH_JWKS_URL"
ALGORITHMS_ENV = "AGENT_FACTORY_AUTH_ALGORITHMS"
TOKEN_TYPE_ENV = "AGENT_FACTORY_AUTH_TOKEN_TYPE"
DEV_ISSUER_DIR_ENV = "AGENT_FACTORY_AUTH_DEV_ISSUER_DIR"
PRODUCTION_SETTINGS = (ISSUER_ENV, AUDIENCE_ENV, JWKS_URL_ENV, ALGORITHMS_ENV, TOKEN_TYPE_ENV)
DEVELOPMENT_SETTINGS = (DEV_ISSUER_DIR_ENV,)
DEFAULT_ALGORITHMS = ("RS256", "ES256")


class AuthMode(StrEnum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"


class AuthConfigError(ValueError):
    """Authentication is not configured safely; the API refuses to start."""


def _https_url(value: str | None, what: str) -> str:
    parts = urlsplit(value or "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise AuthConfigError(f"the {what} must be an https URL without credentials or a fragment")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class AuthSettings:
    mode: AuthMode
    policy: TokenPolicy
    jwks_url: str | None = None  # production
    dev_issuer_dir: Path | None = None  # development
    failed_attempts_per_minute: int = DEFAULT_FAILED_ATTEMPTS_PER_MINUTE
    decisions_per_minute: int = DEFAULT_DECISIONS_PER_MINUTE

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", AuthMode(self.mode))
        if self.mode is AuthMode.PRODUCTION:
            if self.dev_issuer_dir is not None:
                raise AuthConfigError("production mode refuses the development issuer")
            if self.policy.issuer == DEV_ISSUER:
                raise AuthConfigError("production mode refuses the development issuer")
            _https_url(self.policy.issuer, "issuer")
            _https_url(self.jwks_url, "JWKS URL")
        else:
            if self.jwks_url is not None:
                raise AuthConfigError("development mode trusts only the local development issuer")
            if self.dev_issuer_dir is None:
                raise AuthConfigError(f"development mode needs {DEV_ISSUER_DIR_ENV}")
            if (self.policy.issuer, self.policy.audience) != (DEV_ISSUER, DEV_AUDIENCE):
                raise AuthConfigError("development mode trusts only the local development issuer")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AuthSettings:
        env = os.environ if environ is None else environ

        def value(name: str) -> str:
            return env.get(name, "").strip()

        mode_text = value(MODE_ENV).lower() or AuthMode.PRODUCTION.value
        try:
            mode = AuthMode(mode_text)
        except ValueError:
            raise AuthConfigError(f"{MODE_ENV} must be 'production' or 'development'") from None
        production = [name for name in PRODUCTION_SETTINGS if value(name)]
        development = [name for name in DEVELOPMENT_SETTINGS if value(name)]
        if production and development:
            raise AuthConfigError(
                "both production and development authentication settings are set "
                f"({', '.join(production + development)}); set exactly one kind"
            )
        if mode is AuthMode.DEVELOPMENT:
            if production:
                raise AuthConfigError(f"development mode refuses production settings ({', '.join(production)})")
            if not value(DEV_ISSUER_DIR_ENV):
                raise AuthConfigError(
                    f"development mode needs {DEV_ISSUER_DIR_ENV}; create the issuer with "
                    "`python -m agent_factory auth dev-issuer init`"
                )
            return cls(mode, development_policy(), dev_issuer_dir=Path(value(DEV_ISSUER_DIR_ENV)))
        if development:
            raise AuthConfigError(f"production mode refuses development settings ({', '.join(development)})")
        missing = [name for name in (ISSUER_ENV, AUDIENCE_ENV, JWKS_URL_ENV) if not value(name)]
        if missing:
            raise AuthConfigError(
                f"authentication is not configured: set {', '.join(missing)} for your identity provider "
                "(docs/authentication.md)"
            )
        algorithms = tuple(a.strip() for a in value(ALGORITHMS_ENV).split(",") if a.strip()) or DEFAULT_ALGORITHMS
        try:
            policy = TokenPolicy(
                issuer=value(ISSUER_ENV),
                audience=value(AUDIENCE_ENV),
                algorithms=algorithms,
                token_type=value(TOKEN_TYPE_ENV) or None,
            )
        except ValueError as exc:
            raise AuthConfigError(str(exc)) from None
        return cls(mode, policy, jwks_url=value(JWKS_URL_ENV))


def development_policy() -> TokenPolicy:
    return TokenPolicy(issuer=DEV_ISSUER, audience=DEV_AUDIENCE, algorithms=(DEV_ALGORITHM,))


def build_authenticator(
    settings: AuthSettings,
    *,
    clock: Clock = utc_now,
    fetch: Callable[[str, float], bytes] = fetch_https,
) -> Authenticator:
    """The authenticator for these settings: the provider's remote keys, or the development issuer's public keys."""
    if settings.mode is AuthMode.PRODUCTION:
        keys: RemoteKeySet | StaticKeySet = RemoteKeySet(settings.jwks_url or "", fetch=fetch)
    else:
        keys = StaticKeySet(load_public_jwks(settings.dev_issuer_dir or Path()))
    return Authenticator(
        TokenVerifier(settings.policy, keys),
        clock=clock,
        failures=SlidingWindowLimiter(settings.failed_attempts_per_minute, timedelta(minutes=1)),
        decisions=SlidingWindowLimiter(settings.decisions_per_minute, timedelta(minutes=1)),
    )


__all__ = [
    "AuthConfigError",
    "AuthMode",
    "AuthSettings",
    "build_authenticator",
    "development_policy",
]

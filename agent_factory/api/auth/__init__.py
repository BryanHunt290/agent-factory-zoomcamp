"""Authentication and authorization for the HTTP API (docs/authentication.md).

* **Authentication** ("who is making this request?"): the API is an OAuth 2.0
  resource server. It verifies signed JWT access tokens from a trusted OpenID
  Connect issuer (``tokens.py``, ``keys.py``) and maps the token's issuer and
  subject to an enrolled, active principal (``authenticator.py``).
* **Authorization** ("what may that identity do?"): every product operation
  requires exactly one permission, enforced on the server by
  ``require_permission`` (``policy.py``).
* **Configuration** (``settings.py``): production by default and fail
  closed; the local development issuer (``development.py``) is DEVELOPMENT
  ONLY and refused in production.
"""

from .authenticator import AuthenticatedPrincipal, Authenticator, RateLimited
from .keys import KeysUnavailable, RemoteKeySet, StaticKeySet
from .policy import PermissionCheck, decision_quota, reconciliation_quota, require_permission
from .ratelimit import SlidingWindowLimiter
from .settings import AuthConfigError, AuthMode, AuthSettings, build_authenticator
from .tokens import AuthenticationFailed, AuthFailure, TokenPolicy, TokenVerifier

__all__ = [
    "AuthConfigError",
    "AuthFailure",
    "AuthMode",
    "AuthSettings",
    "AuthenticatedPrincipal",
    "AuthenticationFailed",
    "Authenticator",
    "KeysUnavailable",
    "PermissionCheck",
    "RateLimited",
    "RemoteKeySet",
    "SlidingWindowLimiter",
    "StaticKeySet",
    "TokenPolicy",
    "TokenVerifier",
    "build_authenticator",
    "decision_quota",
    "reconciliation_quota",
    "require_permission",
]

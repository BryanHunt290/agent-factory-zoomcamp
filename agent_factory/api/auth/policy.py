"""Authorization: every product operation requires exactly one permission, checked on the server.

A route is protected only through :func:`require_permission`: a FastAPI
dependency that authenticates the request and then requires the
operation's permission, before the handler or any lookup runs. A test checks
that every operation under ``/api/v1`` has exactly one such check, and that
it is the permission ``openapi.yaml`` documents (``x-permission``).

Answers, in this order:

* **401** ``authentication_required``: no credential; ``invalid_token``: a
  credential was sent and refused (malformed, bad signature, expired, wrong
  issuer or audience, unknown or disabled principal). Both carry
  ``WWW-Authenticate: Bearer`` (RFC 6750) and say nothing about the account.
* **429** ``too_many_requests``: too many refused credentials from this
  address; ``Retry-After`` says when to try again.
* **503** ``authentication_unavailable``: the provider's keys cannot be
  obtained, so nothing can be verified. Fail closed.
* **403** ``forbidden``: authenticated, without the permission. The problem
  names the permission the operation requires, never the principal's others.

Hiding a button in a user interface is not authorization: this check runs on
every request, whatever the client shows or sends.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Request

from ...domain.principals import Permission
from ..errors import ApiError, ErrorCode
from .authenticator import AuthenticatedPrincipal, Authenticator, RateLimited
from .keys import KeysUnavailable
from .tokens import AuthenticationFailed, AuthFailure

log = logging.getLogger("agent_factory.api.auth")

REALM = 'Bearer realm="agent-factory"'
AUTHENTICATION_UNAVAILABLE_RETRY_S = 30
MISSING_DETAIL = "Send an access token: Authorization: Bearer <token>."
REFUSED_DETAIL = "The access token was not accepted. Sign in again to get a new one."
FORBIDDEN_DETAIL = "This identity does not have the permission this operation requires."
RATE_LIMITED_DETAIL = "Too many attempts. Try again after the number of seconds in Retry-After."
UNAVAILABLE_DETAIL = "Credentials cannot be verified right now. Try again shortly."


def unauthenticated(failure: AuthFailure) -> ApiError:
    if failure is AuthFailure.MISSING:
        return ApiError(ErrorCode.AUTHENTICATION_REQUIRED, MISSING_DETAIL, headers={"WWW-Authenticate": REALM})
    return ApiError(
        ErrorCode.INVALID_TOKEN, REFUSED_DETAIL, headers={"WWW-Authenticate": f'{REALM}, error="invalid_token"'}
    )


def too_many_requests(retry_after: int) -> ApiError:
    return ApiError(ErrorCode.TOO_MANY_REQUESTS, RATE_LIMITED_DETAIL, headers={"Retry-After": str(retry_after)})


class PermissionCheck:
    """A FastAPI dependency: authenticate the request, then require ``permission``."""

    def __init__(self, permission: Permission) -> None:
        self.permission = Permission(permission)

    def __repr__(self) -> str:
        return f"require_permission({self.permission.value!r})"

    def __call__(self, request: Request) -> AuthenticatedPrincipal:
        authenticator: Authenticator = request.app.state.authenticator
        context = request.app.state.context
        # request.state lives in the ASGI scope, so the access log line can report the outcome.
        request.state.permission = self.permission.value
        client = request.client.host if request.client else "unknown"
        try:
            with context.stores.session() as store:
                principal = authenticator.authenticate(request.headers.getlist("authorization"), client, store)
        except AuthenticationFailed as exc:
            request.state.auth_result = exc.failure.value
            log.warning("authentication refused: %s", exc.failure.value)
            raise unauthenticated(exc.failure) from None
        except RateLimited as exc:
            request.state.auth_result = "rate_limited"
            log.warning("authentication rate limited for %s s", exc.retry_after)
            raise too_many_requests(exc.retry_after) from None
        except KeysUnavailable:
            request.state.auth_result = "keys_unavailable"
            log.error("authentication unavailable: the identity provider's keys cannot be obtained")
            raise ApiError(
                ErrorCode.AUTHENTICATION_UNAVAILABLE,
                UNAVAILABLE_DETAIL,
                headers={"Retry-After": str(AUTHENTICATION_UNAVAILABLE_RETRY_S)},
            ) from None
        request.state.principal_id = principal.id
        if not principal.allows(self.permission):
            request.state.auth_result = "forbidden"
            log.warning("permission denied: %s lacks %s", principal.id, self.permission.value)
            raise ApiError(ErrorCode.FORBIDDEN, FORBIDDEN_DETAIL, required_permission=self.permission.value)
        request.state.auth_result = "allowed"
        return principal


#: One check per permission, so every route that requires a permission shares the same object.
_CHECKS = {permission: PermissionCheck(permission) for permission in Permission}


def require_permission(permission: Permission | str) -> PermissionCheck:
    return _CHECKS[Permission(permission)]


def _within_quota(request: Request, principal: AuthenticatedPrincipal, what: str) -> AuthenticatedPrincipal:
    """One per-principal quota for every sensitive human decision (429 beyond N per minute, per process)."""
    authenticator: Authenticator = request.app.state.authenticator
    retry_after = authenticator.decisions.acquire(principal.id)
    if retry_after is not None:
        request.state.auth_result = "rate_limited"
        log.warning("%s rate limit reached by %s", what, principal.id)
        raise too_many_requests(retry_after)
    return principal


def decision_quota(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission(Permission.APPROVALS_DECIDE))],
) -> AuthenticatedPrincipal:
    """The deciding principal, after ``approvals:decide`` and the decision quota."""
    return _within_quota(request, principal, "decision")


def reconciliation_quota(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_permission(Permission.ACTIONS_RECONCILE))],
) -> AuthenticatedPrincipal:
    """The reconciling principal, after ``actions:reconcile`` (never implied by ``approvals:decide``) and the same quota.

    Decisions and reconciliation answers share one budget per principal, so
    alternating between them does not double it.
    """
    return _within_quota(request, principal, "reconciliation")


__all__ = [
    "PermissionCheck", "decision_quota", "reconciliation_quota", "require_permission", "too_many_requests",
    "unauthenticated",
]  # fmt: skip

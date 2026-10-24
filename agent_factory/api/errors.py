"""API errors: stable codes and curated messages, independent of the web framework.

Services raise :class:`ApiError` with a code from the contract's ``ErrorCode``
enum and a fixed, human-readable ``detail``. ``handlers.py`` turns it into an
RFC 9457 problem response. Exception text never becomes a ``detail``: it can
contain file paths, SQL, connection details, or sandbox internals. Logs carry
the error type and the same ``request_id`` without sensitive exception text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any

MAX_FIELD_ERRORS = 50


class ErrorCode(StrEnum):
    """The contract's ErrorCode enum. Codes are added within v1, never changed or removed."""

    MALFORMED_REQUEST = "malformed_request"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    INVALID_CURSOR = "invalid_cursor"
    AUTHENTICATION_REQUIRED = "authentication_required"  # no credential was sent
    INVALID_TOKEN = "invalid_token"  # a credential was sent and refused, for whatever reason
    FORBIDDEN = "forbidden"  # authenticated, without the operation's permission
    TOO_MANY_REQUESTS = "too_many_requests"
    PROJECT_NOT_FOUND = "project_not_found"
    RUN_NOT_FOUND = "run_not_found"
    APPROVAL_NOT_FOUND = "approval_not_found"
    ROUTE_NOT_FOUND = "route_not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    ACTION_FINGERPRINT_MISMATCH = "action_fingerprint_mismatch"
    APPROVAL_ALREADY_DECIDED = "approval_already_decided"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_CANCELLED = "approval_cancelled"
    RECONCILIATION_NOT_REQUIRED = "reconciliation_not_required"
    RECONCILIATION_STALE = "reconciliation_stale"
    RECONCILIATION_ALREADY_DECIDED = "reconciliation_already_decided"
    IDEMPOTENCY_KEY_IN_USE = "idempotency_key_in_use"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    VALIDATION_FAILED = "validation_failed"
    UNKNOWN_TEMPLATE = "unknown_template"
    INTERNAL_ERROR = "internal_error"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    AUTHENTICATION_UNAVAILABLE = "authentication_unavailable"  # the identity provider's keys cannot be obtained


#: code -> (HTTP status, title)
SPEC: Mapping[ErrorCode, tuple[int, str]] = {
    ErrorCode.MALFORMED_REQUEST: (400, "Malformed request"),
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: (400, "Idempotency key required"),
    ErrorCode.INVALID_CURSOR: (400, "Invalid cursor"),
    ErrorCode.AUTHENTICATION_REQUIRED: (401, "Authentication required"),
    ErrorCode.INVALID_TOKEN: (401, "Invalid credentials"),
    ErrorCode.FORBIDDEN: (403, "Forbidden"),
    ErrorCode.TOO_MANY_REQUESTS: (429, "Too many requests"),
    ErrorCode.PROJECT_NOT_FOUND: (404, "Project not found"),
    ErrorCode.RUN_NOT_FOUND: (404, "Run not found"),
    ErrorCode.APPROVAL_NOT_FOUND: (404, "Approval not found"),
    ErrorCode.ROUTE_NOT_FOUND: (404, "Not found"),
    ErrorCode.METHOD_NOT_ALLOWED: (405, "Method not allowed"),
    ErrorCode.ACTION_FINGERPRINT_MISMATCH: (409, "Action fingerprint mismatch"),
    ErrorCode.APPROVAL_ALREADY_DECIDED: (409, "Approval already decided"),
    ErrorCode.APPROVAL_EXPIRED: (409, "Approval expired"),
    ErrorCode.APPROVAL_CANCELLED: (409, "Approval cancelled"),
    ErrorCode.RECONCILIATION_NOT_REQUIRED: (409, "Reconciliation not required"),
    ErrorCode.RECONCILIATION_STALE: (409, "Reconciliation is stale"),
    ErrorCode.RECONCILIATION_ALREADY_DECIDED: (409, "Reconciliation already decided"),
    ErrorCode.IDEMPOTENCY_KEY_IN_USE: (409, "Idempotency key in use"),
    # 409, not 422: the request conflicts with the server state already bound to the key (owner decision).
    ErrorCode.IDEMPOTENCY_KEY_REUSED: (409, "Idempotency key reused"),
    ErrorCode.VALIDATION_FAILED: (422, "Validation failed"),
    ErrorCode.UNKNOWN_TEMPLATE: (422, "Unknown template"),
    ErrorCode.INTERNAL_ERROR: (500, "Internal error"),
    ErrorCode.STORAGE_UNAVAILABLE: (503, "Storage unavailable"),
    ErrorCode.AUTHENTICATION_UNAVAILABLE: (503, "Authentication unavailable"),
}

INTERNAL_DETAIL = "An unexpected error occurred. Quote the request_id when reporting it."
UNAVAILABLE_DETAIL = "The service cannot reach its database. Retry shortly using the same request and idempotency key."


class ApiError(Exception):
    """An error reported to the client: a code, a curated detail, and optional extras."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        errors: Sequence[Mapping[str, str]] | None = None,
        approval: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        required_permission: str | None = None,
    ) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.errors = list(errors)[:MAX_FIELD_ERRORS] if errors else None
        self.approval = dict(approval) if approval is not None else None
        self.headers = dict(headers or {})
        self.required_permission = required_permission

    @property
    def status(self) -> int:
        return SPEC[self.code][0]


def field_error(location: str, pointer: str, message: str) -> dict[str, str]:
    """One entry of a problem's ``errors``; long client-supplied names are cut to the contract's limits."""
    return {"location": location, "pointer": pointer[:200], "message": message[:300]}


def validation_failed(errors: Iterable[Mapping[str, str]], detail: str = "The request is invalid.") -> ApiError:
    return ApiError(ErrorCode.VALIDATION_FAILED, detail, errors=list(errors))


def malformed(detail: str) -> ApiError:
    return ApiError(ErrorCode.MALFORMED_REQUEST, detail)


def not_found(code: ErrorCode) -> ApiError:
    return ApiError(code, NOT_FOUND_DETAIL[code])


NOT_FOUND_DETAIL: Mapping[ErrorCode, str] = {
    ErrorCode.PROJECT_NOT_FOUND: "No project has this ID.",
    ErrorCode.RUN_NOT_FOUND: "This project has no run with this ID.",
    ErrorCode.APPROVAL_NOT_FOUND: "This project has no approval request with this ID.",
}

#: Fields a client may never send, with the reason it is shown.
SERVER_CONTROLLED: Mapping[str, str] = {
    "decided_at": "Not allowed; the server sets the decision time.",
    "decided_by": "Not allowed; the server records who decided.",
    "actor": "Not allowed; the server records who decided.",
    "verified": "Not allowed; only the server can verify an identity.",
    "id": "Not allowed; the server assigns IDs.",
    "created_at": "Not allowed; the server sets the time.",
    "status": "Not allowed; the server sets the status.",
}


def _pointer(location: str, loc: Sequence[Any]) -> str:
    if location == "body":
        return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in loc)
    name = str(loc[0]) if loc else ""
    return "Idempotency-Key" if location == "header" and name.lower() == "idempotency-key" else name


def _message(error: Mapping[str, Any], field: str) -> str:
    kind = error.get("type")
    if kind == "missing":
        return "Required."
    if kind == "extra_forbidden":
        return SERVER_CONTROLLED.get(field, "Not allowed.")
    return str(error.get("msg", "Invalid value.")).removeprefix("Value error, ")


def field_errors(errors: Iterable[Mapping[str, Any]], *, location: str | None = None) -> list[dict[str, str]]:
    """Pydantic validation errors as the contract's ``FieldError`` list.

    ``location`` is given for the errors of a body model, whose ``loc`` is a
    path inside the body. FastAPI's parameter errors carry it in ``loc[0]``.
    Messages are Pydantic's, which describe the rule, never the input value.
    """
    out = []
    for e in errors:
        loc = list(e.get("loc", ()))
        where = location or (str(loc.pop(0)) if loc else "body")
        if where not in ("body", "query", "path", "header"):
            where = "body"
        field = str(loc[-1]) if loc else ""
        # Unknown keys can themselves contain secrets or filesystem paths.
        # The empty JSON Pointer identifies the enclosing body without echoing them.
        pointer = "" if e.get("type") == "extra_forbidden" and field not in SERVER_CONTROLLED else _pointer(where, loc)
        out.append(field_error(where, pointer, _message(e, field)))
        if len(out) == MAX_FIELD_ERRORS:
            break
    return out

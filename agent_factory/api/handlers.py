"""Problem responses (RFC 9457) and the mapping of every error onto one.

Every 4xx and 5xx response is ``application/problem+json``::

    {"type": "urn:agent-factory:problem:<code>", "title": ..., "status": ...,
     "detail": ..., "instance": "<request path>", "code": "<code>",
     "request_id": "req_...", "errors": [...], "approval": {...}}

The mapping lives here, in one place:

| Raised | Response |
| --- | --- |
| ``ApiError`` (from services) | its code |
| path parameter that cannot be an ID | 404 ``*_not_found`` |
| query parameter invalid | 422 ``validation_failed`` with ``errors`` |
| no route / wrong method | 404 ``route_not_found`` / 405 ``method_not_allowed`` |
| ``StorageUnavailableError``, ``SchemaNotReadyError`` | 503 ``storage_unavailable`` + ``Retry-After`` |
| any other ``StorageError`` or ``DomainError``, invalid response | 500 ``internal_error`` |
| anything else | 500 ``internal_error`` (request-context middleware) |
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..domain.errors import DomainError, SchemaNotReadyError, StorageError, StorageUnavailableError
from .errors import (
    INTERNAL_DETAIL,
    SPEC,
    UNAVAILABLE_DETAIL,
    ApiError,
    ErrorCode,
    field_errors,
    malformed,
    not_found,
    validation_failed,
)
from .observability import current_request_id

log = logging.getLogger("agent_factory.api")

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_PREFIX = "urn:agent-factory:problem:"
RETRY_AFTER_S = 5

#: Path parameter -> its not-found code. An ID that cannot be valid identifies
#: nothing, so a malformed path ID is a 404, exactly like an unknown one.
PATH_PARAMETERS = {
    "project_id": ErrorCode.PROJECT_NOT_FOUND,
    "run_id": ErrorCode.RUN_NOT_FOUND,
    "approval_id": ErrorCode.APPROVAL_NOT_FOUND,
}


def instance_of(request: Request) -> str | None:
    """A recognized resource path, without reflecting arbitrary client-controlled text."""
    if request.scope.get("route") is None:
        return None
    for value in request.path_params.values():
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            return None
    raw = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    text = quote(raw.decode("latin-1"), safe="/%:@!$&'()*+,;=-._~")
    if len(text) > 500:
        text = text[:500]
        cut = text.rfind("%", len(text) - 2)  # never end inside a %XX escape
        text = text[:cut] if cut != -1 else text
    return text


def problem_response(request: Request, error: ApiError) -> JSONResponse:
    status, title = SPEC[error.code]
    content = {
        "type": PROBLEM_TYPE_PREFIX + error.code.value,
        "title": title,
        "status": status,
        "detail": error.detail,
        "code": error.code.value,
        "request_id": current_request_id() or "req_unassigned",
    }
    instance = instance_of(request)
    if instance is not None:
        content["instance"] = instance
    if error.errors:
        content["errors"] = error.errors
    if error.approval is not None:
        content["approval"] = error.approval
    if error.required_permission is not None:
        content["required_permission"] = error.required_permission
    headers = dict(error.headers)
    if error.code is ErrorCode.STORAGE_UNAVAILABLE:
        headers.setdefault("Retry-After", str(RETRY_AFTER_S))
    return JSONResponse(content, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return problem_response(request, exc)


async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    invalid_path = {str(e["loc"][1]) for e in errors if len(e.get("loc", ())) > 1 and e["loc"][0] == "path"}
    for name, code in PATH_PARAMETERS.items():
        if name in invalid_path:
            return problem_response(request, not_found(code))
    return problem_response(request, validation_failed(field_errors(errors)))


async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        error = ApiError(ErrorCode.ROUTE_NOT_FOUND, "No API operation exists at this path.")
    elif exc.status_code == 405:
        allow = (exc.headers or {}).get("Allow")
        error = ApiError(
            ErrorCode.METHOD_NOT_ALLOWED,
            "This path does not support this HTTP method.",
            headers={"Allow": allow} if allow else None,
        )
    elif 400 <= exc.status_code < 500:
        error = malformed("The request could not be understood.")
    else:
        log.error("the framework raised HTTP %s", exc.status_code)
        error = ApiError(ErrorCode.INTERNAL_ERROR, INTERNAL_DETAIL)
    return problem_response(request, error)


async def _storage_unavailable(request: Request, exc: StorageError) -> JSONResponse:
    log.warning("storage unavailable (%s)", type(exc).__name__)
    return problem_response(request, ApiError(ErrorCode.STORAGE_UNAVAILABLE, UNAVAILABLE_DETAIL))


async def _internal(request: Request, exc: Exception) -> JSONResponse:
    log.error("unexpected %s while handling the request", type(exc).__name__)
    return problem_response(request, ApiError(ErrorCode.INTERNAL_ERROR, INTERNAL_DETAIL))


def install_error_handlers(app: FastAPI) -> None:
    """Register the mapping above. Unlisted exceptions (bugs) reach the request-context middleware."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(StorageUnavailableError, _storage_unavailable)
    app.add_exception_handler(SchemaNotReadyError, _storage_unavailable)
    app.add_exception_handler(StorageError, _internal)
    app.add_exception_handler(DomainError, _internal)
    app.add_exception_handler(ResponseValidationError, _internal)


__all__ = ["PROBLEM_MEDIA_TYPE", "install_error_handlers", "instance_of", "problem_response"]

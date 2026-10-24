"""Dependency injection: what a route handler receives, built per request.

FastAPI calls these functions and passes their results to the handlers, so
a handler declares what it needs (``ProjectServiceDep``, a validated body)
instead of constructing it. Tests replace any of them through
``app.dependency_overrides``.

Request bodies are read here, strictly:

* ``Content-Type`` must be ``application/json``. Anything else is refused
  (400), which also stops a web page in the user's browser from posting a
  "simple" cross-site form or ``text/plain`` request to this unauthenticated
  local API.
* At most 64 KiB; UTF-8; no duplicated keys (ambiguous: which one counts?);
  no ``NaN`` or ``Infinity``.
* Then the Pydantic request model validates it: closed (unknown fields such
  as ``decided_at`` are refused), strict types, the contract's limits.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ValidationError

from .errors import ApiError, ErrorCode, field_error, field_errors, malformed, validation_failed
from .schemas.common import IDEMPOTENCY_KEY_PATTERN
from .services import ApiContext, ApprovalApiService, MilestoneService, ProjectService, RunService

MAX_BODY_BYTES = 64 * 1024
_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)
M = TypeVar("M", bound=BaseModel)


def get_context(request: Request) -> ApiContext:
    return request.app.state.context


ContextDep = Annotated[ApiContext, Depends(get_context)]


def project_service(ctx: ContextDep) -> ProjectService:
    return ProjectService(ctx)


def run_service(ctx: ContextDep) -> RunService:
    return RunService(ctx)


def milestone_service(ctx: ContextDep) -> MilestoneService:
    return MilestoneService(ctx)


def approval_service(ctx: ContextDep) -> ApprovalApiService:
    return ApprovalApiService(ctx)


ProjectServiceDep = Annotated[ProjectService, Depends(project_service)]
RunServiceDep = Annotated[RunService, Depends(run_service)]
MilestoneServiceDep = Annotated[MilestoneService, Depends(milestone_service)]
ApprovalServiceDep = Annotated[ApprovalApiService, Depends(approval_service)]


# ------------------------------------------------------------------ bodies


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (
        not declared.isascii() or not declared.isdigit() or len(declared) > 10 or int(declared) > MAX_BODY_BYTES
    ):
        raise malformed(f"The request body must be at most {MAX_BODY_BYTES // 1024} KiB.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise malformed(f"The request body must be at most {MAX_BODY_BYTES // 1024} KiB.")
        chunks.append(chunk)
    return b"".join(chunks)


def json_body(model: type[M]) -> Callable[[Request], Awaitable[M]]:
    """A dependency that reads, parses, and validates a JSON request body as ``model``."""

    async def dependency(request: Request) -> M:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise malformed("The request body must be JSON, sent with Content-Type: application/json.")
        raw = await _read_body(request)
        try:
            data = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates, parse_constant=_no_constants
            )
        except _DuplicateKey:
            raise malformed("The request body repeats a key.") from None
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise malformed("The request body is not valid JSON.") from None
        if not isinstance(data, dict):
            raise validation_failed([field_error("body", "", "Expected a JSON object.")])
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise validation_failed(field_errors(exc.errors(include_input=False), location="body")) from None

    return dependency


# --------------------------------------------------------------- headers


def idempotency_key(key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    """The required Idempotency-Key header: 400 when missing, 422 when malformed."""
    if key is None:
        raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "Send an Idempotency-Key header, e.g. a new UUID.")
    if not 16 <= len(key) <= 128 or not _KEY_RE.fullmatch(key):
        raise validation_failed(
            [field_error("header", "Idempotency-Key", "Must be 16-128 letters, digits, hyphens, or underscores.")]
        )
    return key


IdempotencyKeyDep = Annotated[str, Depends(idempotency_key)]

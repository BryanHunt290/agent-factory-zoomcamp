"""What every service needs, assembled once at application start-up."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ...domain.errors import DomainValidationError
from ...templates import ProjectTemplate
from ...util import Clock, timestamp, utc_now
from ..errors import ApiError, ErrorCode
from ..pagination import INVALID_CURSOR_DETAIL, CursorCodec, InvalidCursor
from ..redaction import TextSanitizer
from ..stores import StoreProvider

@dataclass(frozen=True)
class ApiContext:
    stores: StoreProvider
    cursors: CursorCodec
    templates: Mapping[str, ProjectTemplate]
    sanitize: TextSanitizer = TextSanitizer()
    clock: Clock = utc_now
    idempotency_ttl: timedelta = timedelta(hours=24)

    def now(self) -> str:
        return timestamp(self.clock)


class Service:
    """Base for the per-resource services: cursor handling shared by every listing."""

    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx

    def _position(self, cursor: str | None, operation: str, filters: Mapping[str, Any]) -> list[Any] | None:
        if cursor is None:
            return None
        try:
            return self.ctx.cursors.decode(cursor, operation, filters)
        except InvalidCursor as exc:
            raise ApiError(ErrorCode.INVALID_CURSOR, INVALID_CURSOR_DETAIL[exc.reason]) from None

    def _next_cursor(self, more: bool, operation: str, filters: Mapping[str, Any], position: list[Any]) -> str | None:
        return self.ctx.cursors.encode(operation, filters, position) if more else None


def bad_position() -> ApiError:
    """A signed cursor whose position no longer fits the query (e.g. after an upgrade)."""
    return ApiError(ErrorCode.INVALID_CURSOR, INVALID_CURSOR_DETAIL["malformed"])


POSITION_ERRORS = (DomainValidationError, TypeError, ValueError)

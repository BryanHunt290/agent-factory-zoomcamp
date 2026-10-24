"""The outermost middleware: request ID, Host check, access log, and the last-resort 500.

It wraps everything else, so every response, including errors raised by the
framework itself, carries ``X-Request-Id`` and is logged exactly once.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import LOOPBACK_HOSTS
from .errors import INTERNAL_DETAIL, ApiError, ErrorCode, malformed
from .handlers import problem_response
from .observability import REQUEST_ID, new_request_id

log = logging.getLogger("agent_factory.api")
access_log = logging.getLogger("agent_factory.api.access")


def host_of(header: str) -> str:
    """The host part of a Host header value (no port), lower-case; IPv6 without brackets."""
    value = header.strip().lower()
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\[\]]+)\](?::[0-9]{1,5})?", value)
        return match.group(1) if match else ""
    if value.count(":") > 1:
        return ""  # IPv6 literals must use brackets in a Host header.
    match = re.fullmatch(r"([^:\s]+)(?::[0-9]{1,5})?", value)
    return match.group(1) if match else ""


def route_of(scope: Scope) -> str:
    """The matched route's template (``/api/v1/projects/{project_id}``), never the raw path."""
    route = scope.get("route")
    return getattr(route, "path", None) or "unmatched"


class RequestContextMiddleware:
    """Request ID, Host allowlist, access log line, and a problem response for unhandled errors.

    **Host allowlist.** The API listens on the loopback interface. A web page
    the user visits could still try to reach it through *DNS rebinding*: its
    own domain name is re-pointed at 127.0.0.1, so the browser treats the API
    as that page's origin. The request then carries the attacker's domain in
    its ``Host`` header, which is refused here (and it would still need a
    valid access token).
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str] = LOOPBACK_HOSTS) -> None:
        self.app = app
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        status: int | None = None
        # Request state lives in the scope: the authorization check (auth/policy.py) records its outcome here.
        state = scope.setdefault("state", {})

        async def send_with_context(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                headers["X-Request-Id"] = request_id
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        try:
            if self._host_allowed(scope):
                await self.app(scope, receive, send_with_context)
            else:
                error = malformed("The Host header is not accepted by this server.")
                await problem_response(Request(scope), error)(scope, receive, send_with_context)
        except Exception as exc:
            # Exception strings and tracebacks may contain request bodies or
            # secrets unknown to a redactor. The correlation ID and type suffice.
            log.error("unhandled request error (%s)", type(exc).__name__)
            if status is None:  # nothing sent yet: answer with a problem instead of dropping the connection
                error = ApiError(ErrorCode.INTERNAL_ERROR, INTERNAL_DETAIL)
                await problem_response(Request(scope), error)(scope, receive, send_with_context)
            else:
                raise  # a partially sent response cannot safely be replaced
        finally:
            access_log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": scope.get("method"),
                    "route": route_of(scope),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    **{k: state[k] for k in ("principal_id", "permission", "auth_result") if k in state},
                },
            )
            REQUEST_ID.reset(token)

    def _host_allowed(self, scope: Scope) -> bool:
        values = [value for name, value in scope.get("headers", ()) if name.lower() == b"host"]
        return len(values) == 1 and host_of(values[0].decode("latin-1")) in self.allowed_hosts

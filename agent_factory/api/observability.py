"""Request IDs and structured, redacted logs for the HTTP API.

* **Request IDs.** Every request gets a new random ID (``req_`` + 20
  letters and digits). It is returned in the ``X-Request-Id`` header and in
  every error body, and attached to every log line written while the request
  is handled. A user quotes it, an operator searches the logs for it. An ID a
  client sends is ignored, so log entries cannot be forged or confused.
* **Structured logs.** One JSON object per line: time (UTC), level, logger,
  message, request ID, and for access lines method, route template, status,
  duration, and the authorization outcome: the principal ID once
  authenticated, the permission checked, and a safe result category
  (``allowed``, ``forbidden``, ``expired``, ...).
* **Never logged:** request or response bodies, headers (``Authorization``
  among them), query strings, bearer tokens, the database URL or password,
  cursor secrets, or the environment. A redactor also removes known secret
  values, any URL carrying credentials, and anything shaped like a JWT or a
  bearer credential from every line, including tracebacks.

These HTTP logs go to the server process's standard error. They are separate
from Agent Factory's execution log, the hash-chained per-run audit trail in
each workspace, which the API never writes to.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import string
import sys
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from ..domain.serialization import format_timestamp

REQUEST_ID: ContextVar[str | None] = ContextVar("agent_factory_request_id", default=None)
_ALPHABET = string.ascii_letters + string.digits
REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9]{8,64}$")

#: scheme://user:password@host — the credentials part of any URL.
_CREDENTIAL_URL = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)[^\s/?#@\"']*@")
#: Anything shaped like a JWT (base64url JSON header "eyJ..." and two more parts), and bearer credentials.
JWT_SHAPED = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
BEARER_CREDENTIAL = re.compile(r"\b[Bb][Ee][Aa][Rr][Ee][Rr]\s+[A-Za-z0-9._~+/-]+=*")


def new_request_id() -> str:
    return "req_" + "".join(secrets.choice(_ALPHABET) for _ in range(20))


def current_request_id() -> str | None:
    return REQUEST_ID.get()


class Redactor:
    """Replaces known secret values, URL credentials, and bearer tokens with ``***``."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        # Longest first, so a password inside a URL is replaced together with the URL.
        self._secrets = sorted({s for s in secret_values if s}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for value in self._secrets:
            text = text.replace(value, "***")
        text = BEARER_CREDENTIAL.sub("Bearer ***", text)
        text = JWT_SHAPED.sub("***", text)
        return _CREDENTIAL_URL.sub(r"\1***@", text)


class JsonFormatter(logging.Formatter):
    """One redacted JSON object per log record."""

    EXTRA_FIELDS = ("method", "route", "status", "duration_ms", "principal_id", "permission", "auth_result")

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or Redactor()

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": format_timestamp(datetime.fromtimestamp(record.created, tz=timezone.utc)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or REQUEST_ID.get()
        if request_id:
            entry["request_id"] = request_id
        for name in self.EXTRA_FIELDS:
            if hasattr(record, name):
                entry[name] = getattr(record, name)
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        clean = {k: self.redactor(v) if isinstance(v, str) else v for k, v in entry.items()}
        return json.dumps(clean, ensure_ascii=False, default=str)


def configure_logging(level: str = "info", redactor: Redactor | None = None) -> None:
    """Route every log record of the API process through one JSON handler on standard error."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(redactor))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # The pool logs every borrowed connection at INFO; only its warnings matter here.
    logging.getLogger("psycopg.pool").setLevel(max(logging.WARNING, root.level))
    # uvicorn's own access log would duplicate ours and include query strings.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True

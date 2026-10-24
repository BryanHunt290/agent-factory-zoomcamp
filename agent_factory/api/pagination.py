"""Cursor (keyset) pagination: opaque, tamper-evident cursors bound to their query.

A cursor carries the *position* of the last item on a page: its sort key,
such as ``(occurred_at, run_id, sequence)`` for milestones. The next page is
"the items after this position", which an index serves directly; there are no
offsets and no total counts.

Format::

    base64url( version(1 byte) || HMAC-SHA256(32 bytes) || payload )
    payload = canonical JSON {"o": operation, "f": filter digest, "k": position, "x": expiry}

* **Opaque.** Clients must not build or decode cursors (the contract says
  so). The payload is signed, not encrypted: it holds only the operation
  name, a digest of the filters, the last item's sort key (values the page
  already showed), and an expiry time. Nothing secret.
* **Tamper-evident.** The HMAC covers the version and the payload. A cursor
  that was edited, truncated, or signed with another key is refused.
* **Bound to its query.** The digest covers the operation and every filter
  (everything except ``limit`` and ``cursor``). Reusing a cursor with other
  filters is refused instead of silently returning the wrong items. The page
  size may change between pages.
* **Expiring.** A cursor is valid for a limited time (24 hours by default).

Any failure is reported the same way: 400 ``invalid_cursor``, "start again
without a cursor".

The signing key comes from ``AGENT_FACTORY_API_CURSOR_SECRET_FILE`` (at least
32 bytes), or is generated at start-up. A generated key makes every cursor
invalid after a restart, which is harmless (clients restart their listing)
but means several API processes behind one address must share a key file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..domain.serialization import canonical
from ..util import Clock, utc_now

VERSION = 1
MAX_CURSOR_LENGTH = 512
MIN_SECRET_BYTES = 32
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_MAC_CONTEXT = b"agent-factory-cursor\x00"
_MAC_BYTES = 32


class InvalidCursor(Exception):
    """The cursor is unusable. ``reason`` is "malformed", "filters", or "expired"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def filter_digest(operation: str, filters: Mapping[str, Any]) -> str:
    """A digest of the operation and its normalised filters (sorted lists, canonical timestamps)."""
    material = canonical({"operation": operation, "filters": dict(filters)})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class CursorCodec:
    """Signs and verifies cursors with one key."""

    def __init__(self, secret: bytes, *, ttl: timedelta = timedelta(hours=24), clock: Clock = utc_now) -> None:
        if len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"the cursor signing key must be at least {MIN_SECRET_BYTES} bytes")
        if ttl <= timedelta(0):
            raise ValueError("the cursor lifetime must be positive")
        self._secret = bytes(secret)
        self._ttl = ttl
        self._clock = clock

    @classmethod
    def from_secret_file(cls, path: Path | None, **kwargs: Any) -> CursorCodec:
        """Key from ``path`` (stripped of surrounding whitespace), or a fresh random key if ``path`` is None."""
        if path is None:
            return cls(secrets.token_bytes(MIN_SECRET_BYTES), **kwargs)
        secret = Path(path).read_bytes().strip()
        if len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"the cursor secret file must hold at least {MIN_SECRET_BYTES} bytes")
        return cls(secret, **kwargs)

    @property
    def secret_for_redaction(self) -> str:
        """The key as text, so log redaction can recognise it. Never logged or returned."""
        return self._secret.decode("latin-1")

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._secret, _MAC_CONTEXT + bytes([VERSION]) + payload, hashlib.sha256).digest()

    def encode(self, operation: str, filters: Mapping[str, Any], position: Sequence[Any]) -> str:
        expires = int((self._clock() + self._ttl).timestamp())
        payload = canonical(
            {"o": operation, "f": filter_digest(operation, filters), "k": list(position), "x": expires}
        ).encode("utf-8")
        token = base64.urlsafe_b64encode(bytes([VERSION]) + self._mac(payload) + payload).rstrip(b"=").decode("ascii")
        if len(token) > MAX_CURSOR_LENGTH:  # pragma: no cover - positions are short by construction
            raise ValueError("cursor too long")
        return token

    def decode(self, token: str, operation: str, filters: Mapping[str, Any]) -> list[Any]:
        """The position inside ``token``, if it is authentic, unexpired, and issued for this query."""
        if not isinstance(token, str) or not _CURSOR_RE.fullmatch(token):
            raise InvalidCursor("malformed")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (binascii.Error, ValueError):
            raise InvalidCursor("malformed") from None
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
            raise InvalidCursor("malformed")
        if len(raw) <= 1 + _MAC_BYTES or raw[0] != VERSION:
            raise InvalidCursor("malformed")
        mac, payload = raw[1 : 1 + _MAC_BYTES], raw[1 + _MAC_BYTES :]
        if not hmac.compare_digest(mac, self._mac(payload)):
            raise InvalidCursor("malformed")
        try:
            data = json.loads(payload)
        except ValueError:  # pragma: no cover - only a holder of the key could produce this
            raise InvalidCursor("malformed") from None
        if (
            not isinstance(data, dict)
            or set(data) != {"o", "f", "k", "x"}
            or not isinstance(data.get("k"), list)
            or not data["k"]
            or type(data.get("x")) is not int
        ):
            raise InvalidCursor("malformed")
        if data.get("o") != operation or data.get("f") != filter_digest(operation, filters):
            raise InvalidCursor("filters")
        if self._clock().timestamp() >= data["x"]:
            raise InvalidCursor("expired")
        return data["k"]


INVALID_CURSOR_DETAIL = {
    "malformed": "The cursor is not valid; start again without a cursor.",
    "filters": "The cursor was issued for different filters; start again without a cursor.",
    "expired": "The cursor has expired; start again without a cursor.",
}

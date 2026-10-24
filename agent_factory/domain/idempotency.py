"""Idempotency records: one stored response per (operation, client key).

An HTTP client that retries a request with the same ``Idempotency-Key`` must
get the original result back instead of causing the effect twice. The API
stores, together with the effect and in the same transaction, a record of:

* the operation (``scope``) and the client's key;
* a fingerprint of the request (operation plus canonical body), so a reused
  key with a *different* request is detected;
* the response that was returned (status and JSON body);
* when the record stops protecting retries.

Records hold no secrets: request bodies of idempotent operations are public
project data, and the stored response is what the client already received.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import DomainValidationError
from .ids import IDEMPOTENCY_KEY_RE, TYPE_RE
from .serialization import canonical, check_int, check_json_object, check_pattern, check_timestamp, parse_timestamp

REQUEST_HASH_PREFIX = "sha256:"
REQUEST_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def request_hash(scope: str, body: Mapping[str, Any]) -> str:
    """Deterministic fingerprint of one request: its operation and its canonical JSON body."""
    material = canonical({"scope": scope, "body": dict(body)})
    return REQUEST_HASH_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyRecord:
    scope: str
    key: str
    request_hash: str
    response_status: int
    response_body: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    expires_at: str = ""

    def __post_init__(self) -> None:
        check_pattern(self.scope, TYPE_RE, "idempotency.scope")
        check_pattern(self.key, IDEMPOTENCY_KEY_RE, "idempotency.key")
        check_pattern(self.request_hash, REQUEST_HASH_RE, "idempotency.request_hash")
        check_int(self.response_status, "idempotency.response_status", minimum=200)
        if self.response_status > 299:
            raise DomainValidationError("idempotency.response_status: only successful responses are stored")
        object.__setattr__(
            self, "response_body", check_json_object(self.response_body, "idempotency.response_body", max_bytes=64 * 1024)
        )
        object.__setattr__(self, "created_at", check_timestamp(self.created_at, "idempotency.created_at"))
        object.__setattr__(self, "expires_at", check_timestamp(self.expires_at, "idempotency.expires_at"))
        if parse_timestamp(self.expires_at) <= parse_timestamp(self.created_at):
            raise DomainValidationError("idempotency.expires_at: must be later than created_at")

    def is_expired(self, at: str) -> bool:
        return parse_timestamp(at) >= parse_timestamp(self.expires_at)

    def response(self) -> dict[str, Any]:
        """An independent copy of the stored response body."""
        return copy.deepcopy(self.response_body)

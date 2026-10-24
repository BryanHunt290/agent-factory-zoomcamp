"""A deterministic notification provider for tests and local development. It contacts nothing.

It stands in for an SMS gateway or an email service and records, for every
call: the notification ID, the attempt, the redacted destination, the exact
text a person would receive, a simulated provider message ID, and the
outcome. ``delivered`` is what reached the "person"; a call that failed
before acceptance delivers nothing.

Outcomes are scripted, one per call in order (then ``default``):

=========================  ==========================  ===============================
script entry               delivered?                  the dispatcher sees
=========================  ==========================  ===============================
``success``                yes                         accepted, with a provider ID
``transient``              no                          transient: provider_unavailable
``rate_limited``           no                          transient: rate_limited
``permanent``              no                          permanent: invalid_destination
``misconfigured``          no                          permanent: provider_rejected_configuration
``timeout``                no                          unknown: timeout
``accepted_response_lost`` yes                         unknown: timeout
``hang_before_send``       no (blocks until killed)    nothing: the process is killed
``hang_after_accept``      yes (then blocks)           nothing: the process is killed
=========================  ==========================  ===============================

With ``honor_idempotency`` it behaves like a provider that supports
idempotency keys: a second acceptance with the same key returns the first
message ID and delivers nothing new. Real providers differ; see
docs/notifications.md.

With ``record`` set, every call is also appended to that JSON-lines file and
flushed to disk before ``send`` returns or blocks, so a test can read what a
killed dispatcher process handed over.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..domain.notifications import FailureCategory, NotificationChannel, redact_address
from .provider import NotificationProvider, OutboundMessage, ProviderError

OUTCOMES = frozenset({
    "success", "transient", "rate_limited", "permanent", "misconfigured", "timeout", "accepted_response_lost",
    "hang_before_send", "hang_after_accept",
})  # fmt: skip
_DELIVERS = frozenset({"success", "accepted_response_lost", "hang_after_accept"})


class FakeNotificationProvider(NotificationProvider):
    name = "fake"
    channels = frozenset(NotificationChannel)

    def __init__(
        self,
        script: Iterable[str] = (),
        *,
        default: str = "success",
        record: Path | None = None,
        honor_idempotency: bool = False,
        release: threading.Event | None = None,
    ) -> None:
        self._script = deque(script)
        for outcome in (*self._script, default):
            if outcome not in OUTCOMES:
                raise ValueError(f"unknown fake outcome {outcome!r}")
        self._default = default
        self._record = record
        self._honor = honor_idempotency
        self._release = release  # in-process tests: a "hang" waits for this instead of blocking for ever
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self._accepted: dict[str, str] = {}  # idempotency key -> provider message ID

    @classmethod
    def from_script_file(cls, path: Path, **kwargs: Any) -> FakeNotificationProvider:
        """A script file: ``{"script": [...], "default": "success", "honor_idempotency": false}``."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("script", []), default=data.get("default", "success"),
                   honor_idempotency=bool(data.get("honor_idempotency", False)), **kwargs)  # fmt: skip

    def _next(self) -> str:
        with self._lock:
            return self._script.popleft() if self._script else self._default

    def _write(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.calls.append(entry)
            if entry["delivered"]:
                self.delivered.append(entry)
            if self._record is not None:
                with open(self._record, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, sort_keys=True) + "\n")
                    f.flush()
                    os.fsync(f.fileno())

    def _hang(self) -> None:
        if self._release is not None:
            self._release.wait()
            return
        while True:  # a process stuck inside the provider call, until the test kills it
            time.sleep(3600)

    def send(self, message: OutboundMessage, *, timeout: float) -> str:
        outcome = self._next()
        if outcome == "hang_before_send":
            self._write(self._entry(message, outcome, None, delivered=False))
            self._hang()
            raise ProviderError.unknown(FailureCategory.TIMEOUT)
        provider_id = None
        duplicate = False
        if outcome in _DELIVERS:
            with self._lock:
                previous = self._accepted.get(message.idempotency_key) if self._honor else None
                provider_id = previous or "fake-" + secrets.token_hex(8)
                duplicate = previous is not None
                self._accepted.setdefault(message.idempotency_key, provider_id)
        self._write(self._entry(message, outcome, provider_id, delivered=outcome in _DELIVERS and not duplicate,
                                duplicate=duplicate))  # fmt: skip
        if outcome == "success":
            return provider_id  # type: ignore[return-value]
        if outcome == "hang_after_accept":
            self._hang()
            raise ProviderError.unknown(FailureCategory.TIMEOUT)
        raise {
            "transient": ProviderError.transient(FailureCategory.PROVIDER_UNAVAILABLE),
            "rate_limited": ProviderError.transient(FailureCategory.RATE_LIMITED),
            "permanent": ProviderError.permanent(FailureCategory.INVALID_DESTINATION),
            "misconfigured": ProviderError.permanent(FailureCategory.PROVIDER_REJECTED_CONFIGURATION),
            "timeout": ProviderError.unknown(FailureCategory.TIMEOUT),
            "accepted_response_lost": ProviderError.unknown(FailureCategory.TIMEOUT),
        }[outcome]

    @staticmethod
    def _entry(message: OutboundMessage, outcome: str, provider_id: str | None, *, delivered: bool,
               duplicate: bool = False) -> dict[str, Any]:  # fmt: skip
        return {
            "notification_id": message.notification_id,
            "attempt": message.attempt,
            "channel": message.channel.value,
            "to": redact_address(message.channel, message.address),
            "title": message.title,
            "text": message.text,
            "idempotency_key": message.idempotency_key,
            "provider_message_id": provider_id,
            "outcome": outcome,
            "delivered": delivered,
            "duplicate_suppressed": duplicate,
            "at": time.time(),
        }


def read_record(path: Path) -> list[dict[str, Any]]:
    """Every call a fake provider recorded in ``path`` (complete lines only)."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")[:-1]
    return [json.loads(line) for line in lines if line.strip()]


__all__ = ["OUTCOMES", "FakeNotificationProvider", "read_record"]

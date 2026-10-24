"""Dispatcher events: what a notification dispatcher did, one safe record each.

Like worker events: identifiers, attempt numbers, counts, closed-vocabulary
categories and reasons. Never an address, a message text, a provider's error
text, a database URL, or a credential. Field names are a closed set too, so an
address cannot slip in under a new name.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from ..domain.notifications import FailureCategory
from ..util import Clock, timestamp, utc_now

LOGGER = logging.getLogger("agent_factory.notifications")

EVENTS = frozenset({
    "dispatcher_started",
    "intents_created",  # the attention sweep created notification intents the outbox did not have yet
    "notification_claimed",
    "notification_sent",
    "notification_retry_scheduled",
    "notification_failed",
    "notification_skipped",
    "notification_deferred",  # storm protection: over the destination's rate limit, due again later
    "notification_recovered",  # taken back from a dispatcher whose lease expired
    "stale_dispatcher_rejected",  # this dispatcher no longer owned the notification; nothing was recorded
    "database_unavailable",
    "dispatcher_stopped",
})  # fmt: skip

FIELDS = frozenset({
    "notification_id", "project_id", "approval_id", "attention_kind", "channel", "attempt", "claim_token",
    "category", "outcome", "reason", "next_attempt_at", "provider", "provider_message_id", "count", "error_type",
    "retry_in_s", "lease_s", "poll_s", "sweep_s", "max_attempts", "sent", "failed", "skipped", "host", "pid",
    "link", "operation", "unknown_attempt",
})  # fmt: skip

REASONS = frozenset({"stop_requested", "idle", "max_sends", "error", "begin_attempt", "finish", "defer", "skip"})

_SAFE_VALUE = (str, int, float, bool, type(None))


class DispatcherEvents:
    def __init__(self, dispatcher_id: str, *, sink: Callable[[dict[str, Any]], None] | None = None, clock: Clock = utc_now) -> None:
        self.dispatcher_id = dispatcher_id
        self._sink = sink
        self._clock = clock
        self._lock = threading.Lock()
        self.recent: deque[dict[str, Any]] = deque(maxlen=2000)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        if event not in EVENTS:
            raise ValueError(f"unknown dispatcher event {event!r}")
        for name, value in fields.items():
            if name not in FIELDS:
                raise ValueError(f"dispatcher events carry no field {name!r}")
            if not isinstance(value, _SAFE_VALUE):
                raise TypeError(f"event field {name!r} must be a plain value, not {type(value).__name__}")
        if "category" in fields and fields["category"] is not None:
            FailureCategory(fields["category"])  # a closed vocabulary: never free text
        if "reason" in fields and fields["reason"] not in REASONS:
            raise ValueError(f"unknown event reason {fields['reason']!r}")
        record = {"event": event, "dispatcher_id": self.dispatcher_id, "at": timestamp(self._clock), **fields}
        LOGGER.info(json.dumps(record, sort_keys=True))
        with self._lock:
            self.recent.append(record)
            if self._sink is not None:
                self._sink(record)
        return record

    def named(self, event: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.recent if r["event"] == event]


__all__ = ["EVENTS", "FIELDS", "DispatcherEvents"]

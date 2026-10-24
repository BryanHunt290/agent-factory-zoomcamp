"""Worker events: what a worker did, as one safe, structured record each.

Events are for operators and logs (logger ``agent_factory.worker``), not for
the product: no milestone is ever created from them, and heartbeats are
logged at DEBUG only. A record holds identifiers (worker, project, run),
generations (fencing tokens), database times, counts, and curated reasons;
never a database URL, a credential, a file path, or exception text. An error
appears only as its type name.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from ..util import Clock, timestamp, utc_now

LOGGER = logging.getLogger("agent_factory.worker")

EVENTS = frozenset({
    "worker_started",  # the worker registered its identity and settings
    "run_claimed",  # a new ownership generation began
    "lease_renewed",  # a heartbeat was confirmed (DEBUG)
    "run_released",  # the owner gave the run back, and said whether it wants another worker
    "lease_expired",  # a lease ran out: found by the owner's heartbeat, or by a worker taking over
    "run_taken_over",  # a claim replaced an owner whose lease had expired
    "stale_worker_rejected",  # a worker's operation was refused because it no longer owns the run
    "database_unavailable",  # the worker could not reach the database; it waits before trying again
    "worker_stopped",  # the worker exited its loop, and why
})  # fmt: skip

#: Reasons are a closed vocabulary, so an event can never carry free text from an exception.
REASONS = frozenset({
    # run_released
    "yielded", "shutdown", "waiting_for_person", "blocked", "finished", "run_busy", "workspace_unavailable",
    "unsupported_run", "storage_error", "error", "decision_refused", "operator_done",
    # stale_worker_rejected / lease_expired (domain.claims.OwnershipLost.REASONS)
    "superseded", "expired", "released", "missing", "unconfirmed", "wrong_run",
    # worker_stopped
    "stop_requested", "max_runs", "idle",
})  # fmt: skip

_SAFE_VALUE = (str, int, float, bool, type(None))


class WorkerEvents:
    """Emits events for one worker; keeps the last few in memory for tests and diagnostics."""

    def __init__(self, worker_id: str, *, sink: Callable[[dict[str, Any]], None] | None = None, clock: Clock = utc_now) -> None:
        self.worker_id = worker_id
        self._sink = sink
        self._clock = clock
        self._lock = threading.Lock()
        self.recent: deque[dict[str, Any]] = deque(maxlen=1000)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        if event not in EVENTS:
            raise ValueError(f"unknown worker event {event!r}")
        for name, value in fields.items():
            if not isinstance(value, _SAFE_VALUE):
                raise TypeError(f"event field {name!r} must be a plain value, not {type(value).__name__}")
        if "reason" in fields and fields["reason"] not in REASONS:
            raise ValueError(f"unknown event reason {fields['reason']!r}")
        record = {"event": event, "worker_id": self.worker_id, "at": timestamp(self._clock), **fields}
        LOGGER.log(logging.DEBUG if event == "lease_renewed" else logging.INFO, json.dumps(record, sort_keys=True))
        with self._lock:
            self.recent.append(record)
            if self._sink is not None:
                self._sink(record)
        return record

    def named(self, event: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.recent if r["event"] == event]


__all__ = ["EVENTS", "LOGGER", "REASONS", "WorkerEvents"]

"""A small in-process rate limiter for the sensitive paths.

Two limits use it (docs/authentication.md#rate-limits):

* **Refused credentials, per client address.** After too many invalid tokens
  from one address, further requests carrying a credential get 429 until the
  window passes, before any verification work is done. An address is not an
  identity (many people can share one, behind a proxy), so this protects the
  verifier; it does not identify anyone.
* **Decisions, per principal.** A principal can record a bounded number of
  approval decisions per window.

**What is guaranteed:** the limits hold within one API process. The API runs
as a single process on the loopback interface today, so that is the whole
service. Several processes or hosts would each count separately: a deployment
must move these limits to shared state or a gateway (no Redis was added for
this milestone).
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import timedelta


class SlidingWindowLimiter:
    """At most ``limit`` events per ``window`` for each key, in this process; memory is bounded."""

    def __init__(
        self,
        limit: int,
        window: timedelta,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if limit < 1 or window <= timedelta(0) or max_keys < 1:
            raise ValueError("a rate limit needs a positive limit, window, and key bound")
        self.limit = limit
        self.window_s = window.total_seconds()
        self._clock = clock
        self._max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may act again, if it is at its limit now; otherwise None."""
        with self._lock:
            events = self._current(key, self._clock())
            if events is None or len(events) < self.limit:
                return None
            return max(1, math.ceil(events[0] + self.window_s - self._clock()))

    def record(self, key: str) -> None:
        with self._lock:
            self._record(key, self._clock())

    def acquire(self, key: str) -> int | None:
        """Atomically: record an event for ``key`` if it is under its limit (None), else the seconds to wait."""
        with self._lock:
            now = self._clock()
            events = self._current(key, now)
            if events is not None and len(events) >= self.limit:
                return max(1, math.ceil(events[0] + self.window_s - now))
            self._record(key, now)
            return None

    def _record(self, key: str, now: float) -> None:
        events = self._current(key, now)
        if events is None:
            events = self._events[key] = deque()
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)  # forget the least recently active key
        self._events.move_to_end(key)
        events.append(now)
        while len(events) > self.limit:
            events.popleft()

    def _current(self, key: str, now: float) -> deque[float] | None:
        events = self._events.get(key)
        if events is None:
            return None
        while events and events[0] <= now - self.window_s:
            events.popleft()
        if not events:
            del self._events[key]
            return None
        return events


__all__ = ["SlidingWindowLimiter"]

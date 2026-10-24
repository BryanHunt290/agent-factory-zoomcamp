"""The notification dispatcher: send what the outbox holds, at least once, never from a transaction.

::

    loop:  sweep (now and then) -> recover expired leases -> claim due notifications -> for each:
           still needed? -> under the rate limit? -> begin attempt (write-ahead, COMMIT)
           -> provider.send (no transaction) -> finish (fenced, COMMIT)

* **Separate from everything that creates attention.** Graph nodes, API
  handlers, and the dashboard never send anything: the recorder writes an
  intent in the attention event's own transaction, and this process sends it.
  It runs as its own process (``notifications dispatch``), so neither the
  browser, the API, nor a worker has to be alive for a notification to go out.
* **The sweep** is a backstop for the transactional outbox. It reads the
  attention inbox and enqueues an intent for any item that has none (enqueue
  is idempotent). An uncertain action becomes durable in the execution
  ledger's own write-ahead transaction, which the engine keeps free of
  product concerns; if a process dies before the next sync records its
  intent, the sweep creates it. It also notifies destinations added while an
  item was already waiting.
* **Fail closed on the database.** No provider is ever called without a
  committed attempt row. If PostgreSQL is unreachable, nothing is sent; the
  dispatcher waits with a growing delay and resumes when it returns.
* **Honest outcomes.** A send that ends without an answer is recorded as
  ``unknown`` and retried; so is one whose dispatcher dies mid-call (found
  when its lease expires). Delivery is at least once: a person may receive a
  duplicate, never silence because Agent Factory guessed.
"""

from __future__ import annotations

import ipaddress
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from ..domain.errors import StorageError
from ..domain.notification_repositories import MAX_LEASE, NotificationOutbox
from ..domain.notifications import (
    AttemptOutcome,
    Delivery,
    FailureCategory,
    Notification,
    NotificationIntent,
    NotificationStatus,
    RateLimit,
    RetryPolicy,
    later,
)
from ..domain.repositories import ProductStore
from .events import DispatcherEvents
from .provider import NotificationProvider, OutboundMessage, ProviderError


class DashboardUrlError(ValueError):
    pass


def check_dashboard_url(url: str) -> str:
    """The dashboard's origin as a notification may link to it: no credentials, query, or fragment.

    Plain ``http`` only for this machine (loopback): anything reachable from
    elsewhere must be ``https``. The link never authorizes: opening it without
    a session leads to sign-in.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DashboardUrlError("the dashboard URL must be an http(s) URL with a host")
    if parts.username or parts.password or "@" in parts.netloc:
        raise DashboardUrlError("the dashboard URL must not contain credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise DashboardUrlError("the dashboard URL must be an origin only: no path, query string, or fragment")
    if parts.scheme == "http":
        host = parts.hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise DashboardUrlError("a dashboard reachable from other machines must use https")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class DispatcherSettings:
    lease: timedelta = timedelta(seconds=60)
    send_timeout: float = 20.0  # seconds; must end well inside the lease
    poll_interval: float = 2.0
    batch: int = 10
    sweep_interval: float = 60.0  # seconds between attention sweeps
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    rate: RateLimit = field(default_factory=RateLimit)
    max_backoff: float = 30.0  # the longest wait while the database is unreachable
    dashboard_url: str | None = None
    idle_exit: float | None = None  # stop after this many seconds without work (tests, local development)
    max_sends: int | None = None  # stop after this many provider calls (tests)

    def __post_init__(self) -> None:
        if not timedelta(0) < self.lease <= MAX_LEASE:
            raise ValueError("lease must be positive and at most five minutes")
        if not 0 < self.send_timeout < self.lease.total_seconds() / 2:
            raise ValueError("send_timeout must be positive and less than half the lease")
        if self.poll_interval <= 0 or self.sweep_interval <= 0 or self.batch < 1:
            raise ValueError("poll_interval and sweep_interval must be positive, batch at least 1")
        if self.max_backoff < self.poll_interval:
            raise ValueError("max_backoff must be at least poll_interval")
        if self.dashboard_url is not None:
            object.__setattr__(self, "dashboard_url", check_dashboard_url(self.dashboard_url))


@dataclass
class DispatcherReport:
    sends: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    stopped_because: str = ""


_WAIT_SLICE = 0.2


class NotificationDispatcher:
    def __init__(
        self,
        dispatcher_id: str,
        settings: DispatcherSettings,
        *,
        open_outbox: Callable[[], NotificationOutbox],
        provider: NotificationProvider,
        open_store: Callable[[], ProductStore] | None = None,
        events: DispatcherEvents | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.dispatcher_id = dispatcher_id
        self.settings = settings
        self._open_outbox = open_outbox
        self._open_store = open_store
        self.provider = provider
        self.events = events or DispatcherEvents(dispatcher_id)
        self._monotonic = monotonic
        self._rng = rng or random.Random()
        self._stop = threading.Event()
        self._outbox: NotificationOutbox | None = None
        self._store: ProductStore | None = None
        self._failures = 0
        self._last_sweep: float | None = None
        self.report = DispatcherReport()

    # ------------------------------------------------------------- control

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> DispatcherReport:
        self.events.emit(
            "dispatcher_started", provider=self.provider.name, lease_s=self.settings.lease.total_seconds(),
            poll_s=self.settings.poll_interval, sweep_s=self.settings.sweep_interval,
            max_attempts=self.settings.retry.max_attempts, link=self.settings.dashboard_url is not None,
        )  # fmt: skip
        reason = "stop_requested"
        idle_since = self._monotonic()
        try:
            while not self._stop.is_set():
                if self.settings.max_sends is not None and self.report.sends >= self.settings.max_sends:
                    reason = "max_sends"
                    break
                worked = self.run_once()
                if worked:
                    idle_since = self._monotonic()
                    continue
                if self.settings.idle_exit is not None and self._monotonic() - idle_since >= self.settings.idle_exit:
                    reason = "idle"
                    break
                self._wait(self._idle_delay())
        except BaseException:
            reason = "error"
            raise
        finally:
            self._close()
            self.report.stopped_because = reason
            self.events.emit("dispatcher_stopped", reason=reason, sent=self.report.sent, failed=self.report.failed,
                             skipped=self.report.skipped)  # fmt: skip
        return self.report

    def run_once(self) -> bool:
        """One pass: sweep if due, recover, claim, send. True if anything was claimed or recovered."""
        try:
            self._sweep_if_due()
            outbox = self._outbox_session()
            recovered = outbox.recover_expired(self.settings.retry, self._rng)
            for n in recovered:
                self.events.emit(
                    "notification_recovered", notification_id=n.id, attempt=n.attempts, claim_token=n.claim_token,
                    outcome=n.status.value, next_attempt_at=n.next_attempt_at,
                    unknown_attempt=n.failure_category is FailureCategory.OUTCOME_UNKNOWN,
                )  # fmt: skip
            claimed = outbox.claim_due(self.dispatcher_id, self.settings.lease, self.settings.batch)
            self._failures = 0
            for n in claimed:
                if self._stop.is_set():
                    break  # the rest are recovered when their lease expires; none was attempted
                self._process(outbox, n)
            return bool(claimed or recovered)
        except StorageError as exc:
            self._database_unavailable(exc)
            return False

    # ------------------------------------------------------------- one notification

    def _process(self, outbox: NotificationOutbox, n: Notification) -> None:
        where = {"notification_id": n.id, "claim_token": n.claim_token}
        self.events.emit("notification_claimed", **where, project_id=n.project_id, approval_id=n.approval_id,
                         attention_kind=n.attention_kind.value, channel=n.channel.value)  # fmt: skip
        reason = outbox.still_needed(n)
        if reason is not None:
            if outbox.skip(n, reason):
                self.report.skipped += 1
                self.events.emit("notification_skipped", **where, category=reason.value)
            else:
                self.events.emit("stale_dispatcher_rejected", **where, reason="skip")
            return
        now = outbox.now()
        window_start = later(now, -self.settings.rate.window)
        recent = outbox.attempts_since(n.destination_id, window_start)
        if len(recent) >= self.settings.rate.limit:
            until = later(recent[-self.settings.rate.limit], self.settings.rate.window)
            if outbox.defer(n, until):
                self.events.emit("notification_deferred", **where, category=FailureCategory.RATE_LIMITED.value, next_attempt_at=until)
            else:
                self.events.emit("stale_dispatcher_rejected", **where, reason="defer")
            return
        destination = outbox.destination(n.destination_id)
        started = outbox.begin_attempt(n)  # write-ahead: committed before the provider hears anything
        if started is None:
            self.events.emit("stale_dispatcher_rejected", **where, reason="begin_attempt")
            return
        if destination is None or destination.channel not in self.provider.channels:
            delivery = Delivery(AttemptOutcome.PERMANENT_FAILURE, FailureCategory.UNSUPPORTED_DESTINATION)
        else:
            message = OutboundMessage(
                notification_id=n.id, attempt=started.attempts, channel=destination.channel, address=destination.address,
                title=n.title, text=self._text(n),
            )  # fmt: skip
            self.report.sends += 1
            delivery = self._send(message)
        finished = outbox.finish(started, delivery, self.settings.retry, self._rng)
        if finished is None:
            # Another dispatcher took it over meanwhile: this result is not recorded, and that attempt
            # counts as unknown there. Never "fixed up" here.
            self.events.emit("stale_dispatcher_rejected", **where, reason="finish", outcome=delivery.outcome.value)
            return
        self._report(finished, delivery)

    def _send(self, message: OutboundMessage) -> Delivery:
        try:
            provider_id = self.provider.send(message, timeout=self.settings.send_timeout)
        except ProviderError as exc:
            return exc.delivery
        except Exception:  # noqa: BLE001 - an adapter bug may strike after the request left: never assume it did not
            return Delivery(AttemptOutcome.UNKNOWN, FailureCategory.OUTCOME_UNKNOWN)
        return Delivery(AttemptOutcome.ACCEPTED, provider_message_id=provider_id)

    def _text(self, n: Notification) -> str:
        url = f"{self.settings.dashboard_url}{n.link_path}" if self.settings.dashboard_url else None
        return f"{n.body}\n\n{url}" if url else n.body

    def _report(self, n: Notification, delivery: Delivery) -> None:
        where = {"notification_id": n.id, "attempt": n.attempts, "outcome": delivery.outcome.value}
        if n.status is NotificationStatus.SENT:
            self.report.sent += 1
            self.events.emit("notification_sent", **where, provider_message_id=n.provider_message_id)
        elif n.status is NotificationStatus.FAILED:
            self.report.failed += 1
            self.events.emit("notification_failed", **where, category=n.failure_category.value)  # type: ignore[union-attr]
        else:
            self.events.emit("notification_retry_scheduled", **where, category=delivery.failure_category.value,  # type: ignore[union-attr]
                             next_attempt_at=n.next_attempt_at)  # fmt: skip

    # ------------------------------------------------------------- the attention sweep

    def _sweep_if_due(self) -> None:
        if self._open_store is None:
            return
        now_m = self._monotonic()
        if self._last_sweep is not None and now_m - self._last_sweep < self.settings.sweep_interval:
            return
        created = self.sweep()
        self._last_sweep = now_m
        if created:
            self.events.emit("intents_created", count=created)

    def sweep(self) -> int:
        """Enqueue an intent for every attention item that has none yet; how many notifications were created."""
        store = self._store_session()
        outbox = self._outbox_session()
        now = outbox.now()
        created, after = 0, None
        while True:
            with store.snapshot():
                page = store.approvals.page_attention(limit=100, at=now, after=after)
                names = {p.id: p.name for p in store.projects.get_many({i.project_id for i in page}).values()}
            for item in page:
                with store.transaction():
                    created += len(store.notifications.enqueue(NotificationIntent.for_item(item, names.get(item.project_id, ""), at=now)))
            if len(page) < 100:
                return created
            after = page[-1].position

    # ------------------------------------------------------------- sessions

    def _outbox_session(self) -> NotificationOutbox:
        if self._outbox is None:
            self._outbox = self._open_outbox()
        return self._outbox

    def _store_session(self) -> ProductStore:
        if self._store is None:
            self._store = self._open_store()  # type: ignore[misc]
        return self._store

    def _close(self) -> None:
        outbox, self._outbox = self._outbox, None
        store, self._store = self._store, None
        for session in (outbox, store):
            if session is not None:
                try:
                    session.close()
                except StorageError:
                    pass  # closing a broken session: nothing else to do

    def _database_unavailable(self, exc: StorageError) -> None:
        self._close()  # the sessions may be broken; the next pass opens new ones
        self._failures += 1
        self.events.emit("database_unavailable", error_type=type(exc).__name__, retry_in_s=self._idle_delay())

    def _idle_delay(self) -> float:
        if not self._failures:
            return self.settings.poll_interval
        return min(self.settings.max_backoff, self.settings.poll_interval * 2 ** (self._failures - 1))

    def _wait(self, seconds: float) -> None:
        deadline = self._monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(_WAIT_SLICE, remaining))


__all__ = [
    "DashboardUrlError",
    "DispatcherReport",
    "DispatcherSettings",
    "NotificationDispatcher",
    "check_dashboard_url",
]

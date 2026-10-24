"""Shared helpers for the notification tests (docs/notifications.md)."""

from __future__ import annotations

import random

from agent_factory.domain.notification_repositories import InMemoryNotificationOutbox
from agent_factory.domain.notifications import Destination, DestinationStatus, NotificationChannel, new_destination_id, normalize_address
from agent_factory.notifications import DispatcherEvents, DispatcherSettings, FakeNotificationProvider, NotificationDispatcher

ENROLLED = "2026-10-01T00:00:00.000000Z"
EMAIL = "Dana.Owner@Example.TEST"
PHONE = "+1 (555) 010-4477"
DASHBOARD = "http://127.0.0.1:4173"


def destination(store, principal, channel: str = "email", address: str = EMAIL) -> Destination:
    ch = NotificationChannel(channel)
    d = Destination(
        id=new_destination_id(), principal_id=principal.id, channel=ch, address=normalize_address(ch, address),
        status=DestinationStatus.ACTIVE, created_at=ENROLLED, updated_at=ENROLLED,
    )  # fmt: skip
    store.notifications.add_destination(d)
    return d


def memory_dispatcher(flow, provider: FakeNotificationProvider | None = None, *, events: list | None = None,
                      dispatcher_id: str | None = None, sweep: bool = True, **settings):  # fmt: skip
    """A dispatcher over the flow's in-memory store, on the flow's movable clock."""
    from agent_factory.domain.notifications import new_dispatcher_id

    outbox = InMemoryNotificationOutbox(flow.store.unit_of_work, flow.clock)
    did = dispatcher_id or new_dispatcher_id()
    merged = {"poll_interval": 0.01, "max_backoff": 0.05, "dashboard_url": DASHBOARD, **settings}
    sink = events.append if events is not None else None
    return NotificationDispatcher(
        did, DispatcherSettings(**merged), open_outbox=lambda: outbox, provider=provider or FakeNotificationProvider(),
        open_store=(lambda: flow.store) if sweep else None, events=DispatcherEvents(did, sink=sink),
        rng=random.Random(7),
    ), outbox  # fmt: skip

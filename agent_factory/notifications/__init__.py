"""Attention notifications: the dispatcher, the provider boundary, and the fake provider (docs/notifications.md).

Only the dispatcher process uses this package. The engine, agents, tools,
sandbox, API, and dashboard never import it: nothing but the dispatcher can
hand a message to a provider, and a provider's credentials (when a real one
exists) live only in the dispatcher's environment.
"""

from .dispatcher import DashboardUrlError, DispatcherReport, DispatcherSettings, NotificationDispatcher, check_dashboard_url
from .events import DispatcherEvents
from .fake import FakeNotificationProvider, read_record
from .provider import NotificationProvider, OutboundMessage, ProviderError

__all__ = [
    "DashboardUrlError",
    "DispatcherEvents",
    "DispatcherReport",
    "DispatcherSettings",
    "FakeNotificationProvider",
    "NotificationDispatcher",
    "NotificationProvider",
    "OutboundMessage",
    "ProviderError",
    "check_dashboard_url",
    "read_record",
]

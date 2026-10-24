"""The provider-neutral boundary between the dispatcher and whatever delivers a message.

A provider adapter turns one :class:`OutboundMessage` into one call to an
external service (an SMS gateway, an email service, a push service) and
reports what happened in Agent Factory's own vocabulary:

* it returns the provider's message ID: the provider **accepted** the message;
* it raises :class:`ProviderError` with a :class:`FailureCategory` and an
  :class:`AttemptOutcome`: a **transient** failure (retry later), a
  **permanent** one (never retry), or an **unknown** outcome (the request may
  have been accepted: a timeout after sending, a lost response).

Adapters never log or raise the address or the message text, and never put
a provider's own error text into the error: only the category. The domain
and the outbox know nothing about any particular provider.

``OutboundMessage.idempotency_key`` is the notification's ID, the same for
every attempt. An adapter whose provider supports idempotency keys passes it
on, so a retry after an unknown outcome is not delivered twice; for one that
does not, a duplicate is possible and accepted (docs/notifications.md).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain.notifications import AttemptOutcome, Delivery, FailureCategory, NotificationChannel, redact_address


@dataclass(frozen=True)
class OutboundMessage:
    notification_id: str
    attempt: int
    channel: NotificationChannel
    address: str
    title: str
    text: str  # the body and the dashboard link: everything the person receives

    @property
    def idempotency_key(self) -> str:
        return self.notification_id

    def __repr__(self) -> str:  # never print the address or the text by accident
        return (
            f"OutboundMessage(notification_id={self.notification_id!r}, attempt={self.attempt}, "
            f"channel={self.channel.value!r}, address={redact_address(self.channel, self.address)!r})"
        )


class ProviderError(Exception):
    """A send that did not end in acceptance. The message is a fixed phrase, never provider text."""

    def __init__(self, category: FailureCategory, outcome: AttemptOutcome) -> None:
        super().__init__(f"notification not accepted: {category.value} ({outcome.value})")
        self.category = category
        self.outcome = outcome

    @classmethod
    def transient(cls, category: FailureCategory = FailureCategory.PROVIDER_UNAVAILABLE) -> ProviderError:
        return cls(category, AttemptOutcome.TRANSIENT_FAILURE)

    @classmethod
    def permanent(cls, category: FailureCategory = FailureCategory.INVALID_DESTINATION) -> ProviderError:
        return cls(category, AttemptOutcome.PERMANENT_FAILURE)

    @classmethod
    def unknown(cls, category: FailureCategory = FailureCategory.TIMEOUT) -> ProviderError:
        """The provider may have accepted it: a timeout after the request left, or a lost response."""
        return cls(category, AttemptOutcome.UNKNOWN)

    @property
    def delivery(self) -> Delivery:
        return Delivery(self.outcome, self.category)


class NotificationProvider(ABC):
    """Delivers one message on the channels it supports."""

    name: str = "provider"
    channels: frozenset[NotificationChannel] = frozenset()

    @abstractmethod
    def send(self, message: OutboundMessage, *, timeout: float) -> str:
        """Hand ``message`` to the provider within ``timeout`` seconds; the provider's message ID.

        Raises ``ProviderError`` for anything but acceptance.
        """


__all__ = ["NotificationProvider", "OutboundMessage", "ProviderError"]

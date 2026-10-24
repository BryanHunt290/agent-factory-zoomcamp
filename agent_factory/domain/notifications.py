"""Attention notifications: telling a person that something needs them (docs/notifications.md).

A notification **alerts**; it never **authorizes**. It says "Agent Factory
needs your attention" and links to the dashboard, where the person signs in
and sees the exact action. Nothing in a notification, its link, or a reply to
it can approve, reject, or answer anything.

The records here:

* :class:`Destination`: where one principal wants to be told (a channel and an
  address), owned by that principal. Addresses are normalized, shown only
  redacted, and never copied into a notification.
* :class:`Notification`: one durable *intent* in the outbox: "tell this
  destination about this attention event". It is created in the same
  transaction that records the attention event where the engine allows it
  (the transactional outbox), and its identity is derived from the event and
  the destination, so recording the event again never creates another.
* :class:`NotificationAttempt`: one try at handing the notification to a
  provider, with an honest outcome: accepted, a transient or permanent
  failure, or **unknown** (the provider may have accepted it, but Agent
  Factory never learned the answer).

Delivery is **at least once, with duplicate suppression where possible**:
an attempt whose outcome is unknown is retried, so a person may occasionally
receive the same alert twice. A lost alert could leave a run waiting all
night; a duplicate is only annoying.
"""

from __future__ import annotations

import hashlib
import random
import re
import secrets
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum

from .attention import AttentionItem, AttentionKind
from .errors import DomainValidationError
from .executions import EXECUTION_ID_RE
from .ids import APPROVAL_ID_RE, PRINCIPAL_ID_RE, PROJECT_ID_RE, RUN_ID_RE
from .principals import Permission
from .serialization import (
    canonical,
    check_enum,
    check_int,
    check_optional_timestamp,
    check_pattern,
    check_text,
    check_timestamp,
    format_timestamp,
    parse_timestamp,
)

SCHEMA_VERSION = 1

NOTIFICATION_ID_RE = re.compile(r"^ntf-[0-9a-f]{20}$")
DESTINATION_ID_RE = re.compile(r"^nds-[0-9a-f]{20}$")
DISPATCHER_ID_RE = re.compile(r"^ndp-[0-9a-f]{20}$")
DEDUP_KEY_RE = re.compile(r"^(decision_required|reconciliation_required):[A-Za-z0-9:_.-]{1,200}$")
#: A link is a path inside the dashboard, never a full URL: the dispatcher adds the configured origin.
LINK_PATH_RE = re.compile(r"^/projects/prj-[0-9a-f]{20}/approvals/apr-[0-9a-f]{16}$")
PROVIDER_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@<>+=/-]{1,200}$")

MAX_TITLE = 80
MAX_BODY = 500
MAX_PROJECT_NAME_IN_MESSAGE = 60


class NotificationChannel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"  # reserved for a future mobile or browser push channel; no provider exists yet


class DestinationStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class DeliveryPolicy(StrEnum):
    """When a destination is told. v1 has one policy, chosen for overnight autonomy.

    ``immediate``: every urgent notification is sent as soon as it is due, day
    or night. Quiet hours and digests are designed (docs/notifications.md) but
    not built; when they are, urgent attention must still never be silently
    suppressed.
    """

    IMMEDIATE = "immediate"


class Severity(StrEnum):
    URGENT = "urgent"  # a run is paused until a person acts: approvals and reconciliations


class NotificationStatus(StrEnum):
    PENDING = "pending"  # due at next_attempt_at (first try, or a retry)
    SENDING = "sending"  # a dispatcher holds it under a lease
    SENT = "sent"  # a provider accepted it (not proof that the person read it)
    FAILED = "failed"  # given up: a permanent failure, or the attempt limit
    SKIPPED = "skipped"  # no longer needed before it was sent (resolved, destination disabled, ...)

    @property
    def is_final(self) -> bool:
        return self in (NotificationStatus.SENT, NotificationStatus.FAILED, NotificationStatus.SKIPPED)


class AttemptOutcome(StrEnum):
    ACCEPTED = "accepted"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN = "unknown"  # the provider may have accepted it; Agent Factory cannot tell


class FailureCategory(StrEnum):
    """A closed vocabulary: a provider's own error text is never stored or shown."""

    # transient: retried with backoff
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    OUTCOME_UNKNOWN = "outcome_unknown"  # an attempt ended without an answer (a crash, a lost response)
    # permanent: never retried
    INVALID_DESTINATION = "invalid_destination"
    PROVIDER_REJECTED_CONFIGURATION = "provider_rejected_configuration"
    UNSUPPORTED_DESTINATION = "unsupported_destination"
    MAX_ATTEMPTS = "max_attempts"
    # skipped: the notification was no longer needed
    ATTENTION_RESOLVED = "attention_resolved"
    DESTINATION_DISABLED = "destination_disabled"
    RECIPIENT_NOT_PERMITTED = "recipient_not_permitted"

    @property
    def transient(self) -> bool:
        return self in TRANSIENT_FAILURES


TRANSIENT_FAILURES = frozenset({
    FailureCategory.TIMEOUT, FailureCategory.PROVIDER_UNAVAILABLE, FailureCategory.RATE_LIMITED,
    FailureCategory.OUTCOME_UNKNOWN,
})  # fmt: skip
PERMANENT_FAILURES = frozenset({
    FailureCategory.INVALID_DESTINATION, FailureCategory.PROVIDER_REJECTED_CONFIGURATION,
    FailureCategory.UNSUPPORTED_DESTINATION, FailureCategory.MAX_ATTEMPTS,
})  # fmt: skip
SKIP_REASONS = frozenset({
    FailureCategory.ATTENTION_RESOLVED, FailureCategory.DESTINATION_DISABLED, FailureCategory.RECIPIENT_NOT_PERMITTED,
})  # fmt: skip

#: Who may be told about each kind of attention: whoever may act on it. Never anyone else.
PERMISSION_FOR = {
    AttentionKind.DECISION_REQUIRED: Permission.APPROVALS_DECIDE,
    AttentionKind.RECONCILIATION_REQUIRED: Permission.ACTIONS_RECONCILE,
}

REASON_TEXT = {
    AttentionKind.DECISION_REQUIRED: "Approval required",
    AttentionKind.RECONCILIATION_REQUIRED: "Action outcome needs verification",
}


# ------------------------------------------------------------------ identities


def new_destination_id() -> str:
    return "nds-" + secrets.token_hex(10)


def new_dispatcher_id() -> str:
    return "ndp-" + secrets.token_hex(10)


def dedup_key(item: AttentionItem) -> str:
    """The stable identity of one attention event: the same event always gives the same key.

    * an approval request waiting for a decision: one event per request;
    * an uncertain action: one event per uncertain *attempt* (attempt 2 is a new event).
    """
    if item.kind is AttentionKind.DECISION_REQUIRED:
        return f"decision_required:{item.project_id}:{item.approval_id}"
    return f"reconciliation_required:{item.project_id}:{item.execution_id}:{item.attempt}"


def notification_id(key: str, destination_id: str) -> str:
    """Deterministic: one notification per (attention event, destination), however often it is recorded."""
    digest = hashlib.sha256(canonical(["notification", key, destination_id]).encode("utf-8")).hexdigest()
    return "ntf-" + digest[:20]


def link_path(project_id: str, approval_id: str) -> str:
    """Where in the dashboard to look. A path only: no token, no secret, no host, no query string."""
    return f"/projects/{project_id}/approvals/{approval_id}"


# ------------------------------------------------------------------ content


_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def safe_project_name(name: str) -> str:
    """A project's name as it may appear in a notification: one plain line, clipped.

    Control and bidirectional-override characters are removed and whitespace
    collapsed, so a name cannot forge extra lines ("Reason: ...") or reorder
    the text. It is the only free text a notification carries.
    """
    cleaned = " ".join(_UNSAFE.sub(" ", name).split())
    if len(cleaned) > MAX_PROJECT_NAME_IN_MESSAGE:
        cleaned = cleaned[: MAX_PROJECT_NAME_IN_MESSAGE - 1] + "…"
    return cleaned or "(unnamed project)"


@dataclass(frozen=True)
class Content:
    """What a notification says. Deliberately minimal: no tool, no parameters, no paths, no secrets."""

    title: str
    body: str

    def with_link(self, url: str | None) -> str:
        """The message as sent: the body, then the dashboard link (which authorizes nothing)."""
        return f"{self.body}\n\n{url}" if url else self.body


def content_for(kind: AttentionKind, project_name: str) -> Content:
    return Content(
        title="Agent Factory needs your attention",
        body=(
            "Agent Factory needs your attention.\n\n"
            f"Project: {safe_project_name(project_name)}\n"
            f"Reason: {REASON_TEXT[kind]}\n\n"
            "Open Agent Factory to review."
        ),
    )


# ------------------------------------------------------------------ destinations


EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
PUSH_RE = re.compile(r"^push:[A-Za-z0-9_-]{16,200}$")


def normalize_address(channel: NotificationChannel, raw: str) -> str:
    """The canonical form of an address, or ``DomainValidationError`` (whose text never contains it).

    * email: trimmed; the domain lower-cased (the local part is kept as given);
    * sms: E.164 (``+`` and 8 to 15 digits), with spaces, dots, dashes, and brackets removed;
    * push: an opaque device or subscription handle (reserved).
    """
    channel = check_enum(NotificationChannel, channel, "channel")
    if not isinstance(raw, str):
        raise DomainValidationError("address: expected text")
    value = raw.strip()
    if channel is NotificationChannel.EMAIL:
        local, at, domain = value.rpartition("@")
        value = f"{local}{at}{domain.lower()}"
        if len(value) > 254 or not EMAIL_RE.fullmatch(value):
            raise DomainValidationError("address: not a valid email address")
    elif channel is NotificationChannel.SMS:
        value = re.sub(r"[\s().-]", "", value)
        if value.startswith("00"):
            value = "+" + value[2:]
        if not E164_RE.fullmatch(value):
            raise DomainValidationError("address: not a phone number in international form (+ country code)")
    elif not PUSH_RE.fullmatch(value):
        raise DomainValidationError("address: not a push handle")
    return value


def redact_address(channel: NotificationChannel, address: str) -> str:
    """An address as diagnostics may show it: enough to recognise your own, too little to use."""
    if channel is NotificationChannel.EMAIL:
        local, _, domain = address.rpartition("@")
        return f"{local[:1]}***@{domain}"
    if channel is NotificationChannel.SMS:
        return f"+***{address[-4:]}"
    return "push:***"


@dataclass(frozen=True)
class Destination:
    """Where one principal is told. Owned by the principal; never chosen by an API client."""

    id: str
    principal_id: str
    channel: NotificationChannel
    address: str  # normalized; never logged, never copied into a notification, never in an API response
    status: DestinationStatus
    created_at: str
    updated_at: str
    policy: DeliveryPolicy = DeliveryPolicy.IMMEDIATE

    def __post_init__(self) -> None:
        check_pattern(self.id, DESTINATION_ID_RE, "destination.id")
        check_pattern(self.principal_id, PRINCIPAL_ID_RE, "destination.principal_id")
        object.__setattr__(self, "channel", check_enum(NotificationChannel, self.channel, "destination.channel"))
        object.__setattr__(self, "status", check_enum(DestinationStatus, self.status, "destination.status"))
        object.__setattr__(self, "policy", check_enum(DeliveryPolicy, self.policy, "destination.policy"))
        if normalize_address(self.channel, self.address) != self.address:
            raise DomainValidationError("destination.address: not in normalized form")
        check_timestamp(self.created_at, "destination.created_at")
        check_timestamp(self.updated_at, "destination.updated_at")
        if parse_timestamp(self.updated_at) < parse_timestamp(self.created_at):
            raise DomainValidationError("destination: updated_at is before created_at")

    @property
    def redacted(self) -> str:
        return redact_address(self.channel, self.address)

    @property
    def active(self) -> bool:
        return self.status is DestinationStatus.ACTIVE

    def with_status(self, status: DestinationStatus, at: str) -> Destination:
        return replace(self, status=status, updated_at=at)

    def __repr__(self) -> str:  # never print the address by accident
        return f"Destination(id={self.id!r}, principal_id={self.principal_id!r}, channel={self.channel.value!r}, address={self.redacted!r}, status={self.status.value!r})"

    @staticmethod
    def check_successor(old: Destination, new: Destination) -> None:
        """Only the status (and policy) may change: a different address is a different destination."""
        if (old.id, old.principal_id, old.channel, old.address, old.created_at) != (
            new.id, new.principal_id, new.channel, new.address, new.created_at,
        ):  # fmt: skip
            raise DomainValidationError(f"destination {old.id}: only its status may change")
        if parse_timestamp(new.updated_at) < parse_timestamp(old.updated_at):
            raise DomainValidationError(f"destination {old.id}: updated_at went backwards")


# ------------------------------------------------------------------ the outbox


@dataclass(frozen=True)
class NotificationIntent:
    """An attention event that should be notified: the outbox fans it out to every eligible destination.

    Eligible: an active destination of an active principal that holds the
    permission to act on this kind of attention (``PERMISSION_FOR``).
    """

    item: AttentionItem
    content: Content
    created_at: str

    @classmethod
    def for_item(cls, item: AttentionItem, project_name: str, *, at: str) -> NotificationIntent:
        return cls(item=item, content=content_for(item.kind, project_name), created_at=check_timestamp(at, "at"))

    @property
    def key(self) -> str:
        return dedup_key(self.item)

    @property
    def permission(self) -> Permission:
        return PERMISSION_FOR[self.item.kind]

    def notification_for(self, destination: Destination) -> Notification:
        item = self.item
        return Notification(
            id=notification_id(self.key, destination.id),
            dedup_key=self.key,
            project_id=item.project_id,
            run_id=item.run_id,
            approval_id=item.approval_id,
            attention_kind=item.kind,
            execution_id=item.execution_id,
            attention_attempt=item.attempt,
            destination_id=destination.id,
            principal_id=destination.principal_id,
            channel=destination.channel,
            severity=Severity.URGENT,
            title=self.content.title,
            body=self.content.body,
            link_path=link_path(item.project_id, item.approval_id),
            status=NotificationStatus.PENDING,
            attempts=0,
            created_at=self.created_at,
            available_at=self.created_at,
            next_attempt_at=self.created_at,
            updated_at=self.created_at,
        )


@dataclass(frozen=True)
class Notification:
    """One outbox record: tell one destination about one attention event, until sent, failed, or skipped."""

    id: str
    dedup_key: str
    project_id: str
    run_id: str
    approval_id: str
    attention_kind: AttentionKind
    destination_id: str
    principal_id: str
    channel: NotificationChannel
    severity: Severity
    title: str
    body: str
    link_path: str
    status: NotificationStatus
    attempts: int
    created_at: str
    available_at: str
    updated_at: str
    next_attempt_at: str | None = None
    execution_id: str | None = None
    attention_attempt: int | None = None
    last_attempt_at: str | None = None
    sent_at: str | None = None
    provider_message_id: str | None = None
    failure_category: FailureCategory | None = None
    claimed_by: str | None = None
    claim_token: int = 0
    lease_expires_at: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        check_pattern(self.id, NOTIFICATION_ID_RE, "notification.id")
        check_pattern(self.dedup_key, DEDUP_KEY_RE, "notification.dedup_key")
        check_pattern(self.project_id, PROJECT_ID_RE, "notification.project_id")
        check_pattern(self.run_id, RUN_ID_RE, "notification.run_id")
        check_pattern(self.approval_id, APPROVAL_ID_RE, "notification.approval_id")
        object.__setattr__(self, "attention_kind", check_enum(AttentionKind, self.attention_kind, "notification.attention_kind"))
        check_pattern(self.destination_id, DESTINATION_ID_RE, "notification.destination_id")
        check_pattern(self.principal_id, PRINCIPAL_ID_RE, "notification.principal_id")
        object.__setattr__(self, "channel", check_enum(NotificationChannel, self.channel, "notification.channel"))
        object.__setattr__(self, "severity", check_enum(Severity, self.severity, "notification.severity"))
        check_text(self.title, "notification.title", max_len=MAX_TITLE)
        check_text(self.body, "notification.body", max_len=MAX_BODY, multiline=True)
        check_pattern(self.link_path, LINK_PATH_RE, "notification.link_path")
        object.__setattr__(self, "status", check_enum(NotificationStatus, self.status, "notification.status"))
        check_int(self.attempts, "notification.attempts", minimum=0)
        for name in ("created_at", "available_at", "updated_at"):
            check_timestamp(getattr(self, name), f"notification.{name}")
        for name in ("next_attempt_at", "last_attempt_at", "sent_at", "lease_expires_at"):
            check_optional_timestamp(getattr(self, name), f"notification.{name}")
        if self.failure_category is not None:
            object.__setattr__(self, "failure_category", check_enum(FailureCategory, self.failure_category, "notification.failure_category"))
        if self.provider_message_id is not None:
            check_pattern(self.provider_message_id, PROVIDER_MESSAGE_ID_RE, "notification.provider_message_id")
        if self.claimed_by is not None:
            check_pattern(self.claimed_by, DISPATCHER_ID_RE, "notification.claimed_by")
        check_int(self.claim_token, "notification.claim_token", minimum=0)
        if self.schema_version != SCHEMA_VERSION:
            raise DomainValidationError(f"notification: schema version {self.schema_version} is not supported")
        reconciliation = self.attention_kind is AttentionKind.RECONCILIATION_REQUIRED
        if reconciliation != (self.execution_id is not None and self.attention_attempt is not None):
            raise DomainValidationError("notification: a reconciliation names its execution and attempt, a decision neither")
        if self.execution_id is not None:
            check_pattern(self.execution_id, EXECUTION_ID_RE, "notification.execution_id")
            check_int(self.attention_attempt, "notification.attention_attempt", minimum=1)
        sending = self.status is NotificationStatus.SENDING
        if sending != (self.claimed_by is not None) or sending != (self.lease_expires_at is not None):
            raise DomainValidationError("notification: exactly a sending notification has a dispatcher and a lease")
        if (self.status is NotificationStatus.SENT) != (self.sent_at is not None):
            raise DomainValidationError("notification: exactly a sent notification has sent_at")
        if (self.status in (NotificationStatus.PENDING, NotificationStatus.SENDING)) != (self.next_attempt_at is not None):
            raise DomainValidationError("notification: exactly an unfinished notification has next_attempt_at")
        if self.status in (NotificationStatus.FAILED, NotificationStatus.SKIPPED) and self.failure_category is None:
            raise DomainValidationError("notification: a failed or skipped notification says why")
        if self.status is NotificationStatus.SKIPPED and self.failure_category not in SKIP_REASONS:
            raise DomainValidationError("notification: not a reason to skip")

    @property
    def is_final(self) -> bool:
        return self.status.is_final


@dataclass(frozen=True)
class NotificationAttempt:
    """One hand-over to a provider. ``ended_at``/``outcome`` stay empty while it is in flight."""

    notification_id: str
    attempt: int
    dispatcher_id: str
    claim_token: int
    started_at: str
    ended_at: str | None = None
    outcome: AttemptOutcome | None = None
    failure_category: FailureCategory | None = None
    provider_message_id: str | None = None

    def __post_init__(self) -> None:
        check_pattern(self.notification_id, NOTIFICATION_ID_RE, "attempt.notification_id")
        check_int(self.attempt, "attempt.attempt", minimum=1)
        check_pattern(self.dispatcher_id, DISPATCHER_ID_RE, "attempt.dispatcher_id")
        check_int(self.claim_token, "attempt.claim_token", minimum=1)
        check_timestamp(self.started_at, "attempt.started_at")
        check_optional_timestamp(self.ended_at, "attempt.ended_at")
        if self.outcome is not None:
            object.__setattr__(self, "outcome", check_enum(AttemptOutcome, self.outcome, "attempt.outcome"))
        if self.failure_category is not None:
            object.__setattr__(self, "failure_category", check_enum(FailureCategory, self.failure_category, "attempt.failure_category"))
        if (self.ended_at is None) != (self.outcome is None):
            raise DomainValidationError("attempt: an attempt ends exactly when its outcome is known (or known to be unknown)")
        if self.provider_message_id is not None:
            check_pattern(self.provider_message_id, PROVIDER_MESSAGE_ID_RE, "attempt.provider_message_id")

    @property
    def open(self) -> bool:
        return self.ended_at is None


# ------------------------------------------------------------------ retries


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries with exponential backoff and jitter (docs/notifications.md, "retries").

    The delay before attempt ``n + 1`` after ``n`` attempts is ``base * 2^(n-1)``,
    capped at ``cap``, then jittered to between half and all of it ("equal
    jitter"), so many failures at once do not retry in lock-step. After
    ``max_attempts`` attempts a notification fails for good; the attention item
    it was about stays in the dashboard regardless.
    """

    max_attempts: int = 8
    base: timedelta = timedelta(seconds=30)
    cap: timedelta = timedelta(minutes=30)
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base <= timedelta(0) or self.cap < self.base:
            raise ValueError("backoff: base must be positive and cap at least base")

    def delay(self, attempts: int, rng: random.Random | None = None) -> timedelta:
        """How long to wait after ``attempts`` attempts before the next one."""
        exponent = max(0, attempts - 1)
        full = min(self.cap, self.base * (2 ** min(exponent, 30)))
        if not self.jitter:
            return full
        half = full / 2
        return half + (full - half) * (rng or random).random()

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts


@dataclass(frozen=True)
class RateLimit:
    """Storm protection per destination: at most ``limit`` attempts in any ``window``.

    Over the limit, a notification waits (``next_attempt_at`` moves to when
    the window allows it); it is never dropped. Deduplication already keeps
    repeated internal events for one attention event to one notification.
    """

    limit: int = 20
    window: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if self.limit < 1 or self.window <= timedelta(0):
            raise ValueError("rate limit: limit must be at least 1 and the window positive")


def later(moment: str, delta: timedelta) -> str:
    return format_timestamp(parse_timestamp(moment) + delta)


@dataclass(frozen=True)
class Delivery:
    """How a dispatcher's attempt ended, ready to be recorded."""

    outcome: AttemptOutcome
    failure_category: FailureCategory | None = None
    provider_message_id: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is AttemptOutcome.ACCEPTED and self.failure_category is not None:
            raise DomainValidationError("delivery: an accepted attempt has no failure")
        if self.outcome is not AttemptOutcome.ACCEPTED and self.failure_category is None:
            raise DomainValidationError("delivery: a failed or unknown attempt says why")
        if self.outcome is AttemptOutcome.PERMANENT_FAILURE and self.failure_category not in PERMANENT_FAILURES:
            raise DomainValidationError("delivery: not a permanent failure category")
        if self.outcome in (AttemptOutcome.TRANSIENT_FAILURE, AttemptOutcome.UNKNOWN) and self.failure_category not in TRANSIENT_FAILURES:
            raise DomainValidationError("delivery: not a transient failure category")


def after_attempt(
    n: Notification, delivery: Delivery, *, at: str, policy: RetryPolicy, rng: random.Random | None = None
) -> Notification:
    """The notification once an attempt has ended, released from its dispatcher (one consistent record)."""
    released = {"claimed_by": None, "lease_expires_at": None, "updated_at": at}
    if delivery.outcome is AttemptOutcome.ACCEPTED:
        return replace(
            n, **released, status=NotificationStatus.SENT, sent_at=at, next_attempt_at=None,
            provider_message_id=delivery.provider_message_id, failure_category=None,
        )  # fmt: skip
    if delivery.outcome is AttemptOutcome.PERMANENT_FAILURE:
        return replace(n, **released, status=NotificationStatus.FAILED, next_attempt_at=None, failure_category=delivery.failure_category)
    if policy.exhausted(n.attempts):
        return replace(n, **released, status=NotificationStatus.FAILED, next_attempt_at=None, failure_category=FailureCategory.MAX_ATTEMPTS)
    return replace(
        n, **released, status=NotificationStatus.PENDING, next_attempt_at=later(at, policy.delay(n.attempts, rng)),
        failure_category=delivery.failure_category,
    )  # fmt: skip

# ------------------------------------------------------------------ what the dashboard sees


class NotificationState(StrEnum):
    """One attention event's notifications, summed up for a person (the API's NotificationState)."""

    QUEUED = "queued"  # not attempted yet
    SENDING = "sending"
    RETRYING = "retrying"  # attempted, not sent yet, will try again
    SENT = "sent"  # a provider accepted it for at least one destination
    FAILED = "failed"  # every destination gave up
    SKIPPED = "skipped"  # no longer needed before it was sent


@dataclass(frozen=True)
class NotificationSummary:
    """The notifications for one attention event, without any recipient or address."""

    attention_kind: AttentionKind
    attention_attempt: int | None
    state: NotificationState
    deliveries: int
    sent: int
    failed: int
    created_at: str
    last_attempt_at: str | None
    sent_at: str | None
    outcome_unknown: bool  # some attempt's outcome is unknown: a duplicate may have arrived
    failure_category: FailureCategory | None = None


def summarize(notifications: list[Notification], unknown_ids: set[str] = frozenset()) -> list[NotificationSummary]:  # type: ignore[assignment]
    """One summary per attention event, oldest first. ``unknown_ids``: notifications with an unknown attempt."""
    groups: dict[str, list[Notification]] = {}
    for n in sorted(notifications, key=lambda n: (parse_timestamp(n.created_at), n.dedup_key, n.id)):
        groups.setdefault(n.dedup_key, []).append(n)
    summaries = []
    for members in groups.values():
        statuses = [m.status for m in members]
        sent = sum(s is NotificationStatus.SENT for s in statuses)
        failed = sum(s is NotificationStatus.FAILED for s in statuses)
        if sent:
            state = NotificationState.SENT
        elif NotificationStatus.SENDING in statuses:
            state = NotificationState.SENDING
        elif NotificationStatus.PENDING in statuses:
            pending = [m for m in members if m.status is NotificationStatus.PENDING]
            state = NotificationState.RETRYING if any(m.attempts for m in pending) else NotificationState.QUEUED
        elif failed:
            state = NotificationState.FAILED
        else:
            state = NotificationState.SKIPPED
        attempted = [m.last_attempt_at for m in members if m.last_attempt_at]
        sent_times = [m.sent_at for m in members if m.sent_at]
        reasons = [m.failure_category for m in members if m.failure_category is not None and m.status is not NotificationStatus.SENT]
        first = members[0]
        summaries.append(
            NotificationSummary(
                attention_kind=first.attention_kind,
                attention_attempt=first.attention_attempt,
                state=state,
                deliveries=len(members),
                sent=sent,
                failed=failed,
                created_at=first.created_at,
                last_attempt_at=max(attempted, key=parse_timestamp) if attempted else None,
                sent_at=min(sent_times, key=parse_timestamp) if sent_times else None,
                outcome_unknown=any(m.id in unknown_ids for m in members),
                failure_category=reasons[-1] if reasons and state is not NotificationState.SENT else None,
            )
        )
    return summaries


__all__ = [
    "PERMISSION_FOR",
    "SCHEMA_VERSION",
    "AttemptOutcome",
    "Content",
    "DeliveryPolicy",
    "Delivery",
    "Destination",
    "DestinationStatus",
    "FailureCategory",
    "Notification",
    "NotificationAttempt",
    "NotificationChannel",
    "NotificationIntent",
    "NotificationState",
    "NotificationStatus",
    "NotificationSummary",
    "RateLimit",
    "RetryPolicy",
    "Severity",
    "after_attempt",
    "content_for",
    "dedup_key",
    "link_path",
    "new_destination_id",
    "new_dispatcher_id",
    "normalize_address",
    "notification_id",
    "redact_address",
    "safe_project_name",
    "summarize",
]

"""Attention notifications, in memory (docs/notifications.md). PostgreSQL and real processes:
tests/test_postgres_notifications.py; the browser: tests/test_dashboard_notifications.py.

What this file proves, with a real executor, recorder, and worker loop over the in-memory store, and the
fake provider:

* the transactional outbox: an approval or a reconciliation creates one intent per eligible destination,
  in the same transaction as the attention event; re-syncing creates nothing; a rollback keeps neither;
* deduplication by attention event; eligibility by permission, never by anything a client sends;
* the dispatcher: sent, retried with backoff, failed for good, attempt limit, unknown outcomes, storm
  protection, skipping what no longer needs anyone, fencing of a stale dispatcher;
* the content: minimal, no tool, parameter, path, token, or secret; the link authorizes nothing;
* the boundaries: nothing but the dispatcher can reach a provider; no API route sends anything.
"""

import ast
import json
import random
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from decision_support import RECONCILE, Flow, person
from notification_support import DASHBOARD, EMAIL, PHONE, destination, memory_dispatcher

from agent_factory.domain.attention import AttentionItem, AttentionKind
from agent_factory.domain.errors import DomainValidationError
from agent_factory.domain.notifications import (
    AttemptOutcome,
    Delivery,
    DestinationStatus,
    FailureCategory,
    NotificationChannel,
    NotificationState,
    NotificationStatus,
    RetryPolicy,
    content_for,
    dedup_key,
    normalize_address,
    notification_id,
    redact_address,
    safe_project_name,
    summarize,
)
from agent_factory.domain.principals import Permission, PrincipalStatus
from agent_factory.notifications import FakeNotificationProvider, check_dashboard_url
from agent_factory.notifications.events import DispatcherEvents

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "agent_factory"


@pytest.fixture
def flow(tmp_path):
    f = Flow.memory(tmp_path, kind="unknown-notice")  # its approved notice ends with an unknown outcome
    f.email = destination(f.store, f.dana)
    return f


def pending(flow):
    return [n for n in flow.store.notifications.list_for_approval(flow.project_id, flow.request().id)]


# ------------------------------------------------------------------ content and identity


def test_the_content_is_minimal_and_carries_no_action_details():
    body = content_for(AttentionKind.DECISION_REQUIRED, "Client ETL Pipeline").body
    assert body == (
        "Agent Factory needs your attention.\n\nProject: Client ETL Pipeline\nReason: Approval required\n\n"
        "Open Agent Factory to review."
    )
    assert "Reason: Action outcome needs verification" in content_for(AttentionKind.RECONCILIATION_REQUIRED, "x").body


def test_a_project_name_cannot_forge_lines_or_reorder_text():
    forged = "Calc\nReason: Approved already‮evil\x07"
    cleaned = safe_project_name(forged)
    assert "\n" not in cleaned and "‮" not in cleaned and "\x07" not in cleaned
    assert len(safe_project_name("x" * 500)) == 60
    body = content_for(AttentionKind.DECISION_REQUIRED, forged).body
    assert body.count("Reason:") == 2 and body.splitlines()[3] == "Reason: Approval required"  # the forged one stays on the project line


def test_one_attention_event_has_one_identity_and_a_new_attempt_is_a_new_event():
    base = {"project_id": "prj-" + "1" * 20, "run_id": "run-1", "approval_id": "apr-" + "2" * 16, "since": "2026-10-05T01:00:00Z",
            "tool": "send_notice", "category": "external_communication"}  # fmt: skip
    decision = AttentionItem(kind=AttentionKind.DECISION_REQUIRED, **base)
    first = AttentionItem(kind=AttentionKind.RECONCILIATION_REQUIRED, execution_id="axe-" + "3" * 20, attempt=1, **base)
    second = replace(first, attempt=2)
    assert dedup_key(decision) == dedup_key(replace(decision, since="2026-10-05T02:00:00Z"))  # same event, recorded again
    assert len({dedup_key(decision), dedup_key(first), dedup_key(second)}) == 3
    assert notification_id(dedup_key(decision), "nds-" + "a" * 20) == notification_id(dedup_key(decision), "nds-" + "a" * 20)
    assert notification_id(dedup_key(decision), "nds-" + "a" * 20) != notification_id(dedup_key(decision), "nds-" + "b" * 20)


def test_addresses_are_normalized_validated_and_shown_only_redacted():
    assert normalize_address(NotificationChannel.EMAIL, " Dana.Owner@Example.TEST ") == "Dana.Owner@example.test"
    assert normalize_address(NotificationChannel.SMS, "+1 (555) 010-4477") == "+15550104477"
    assert normalize_address(NotificationChannel.SMS, "0044 20 7946 0958") == "+442079460958"
    for channel, bad in ((NotificationChannel.EMAIL, "not-an-address"), (NotificationChannel.SMS, "555-0104"),
                         (NotificationChannel.SMS, "+0123456789"), (NotificationChannel.EMAIL, "a@b")):  # fmt: skip
        with pytest.raises(DomainValidationError) as caught:
            normalize_address(channel, bad)
        assert bad not in str(caught.value)  # the error never echoes the address
    assert redact_address(NotificationChannel.EMAIL, "Dana.Owner@example.test") == "D***@example.test"
    assert redact_address(NotificationChannel.SMS, "+15550104477") == "+***4477"


def test_a_destination_never_prints_its_address(flow):
    text = repr(flow.email) + str(flow.email)
    assert "Dana.Owner" not in text and "D***@example.test" in text


def test_the_dashboard_link_is_an_origin_without_credentials_or_secrets():
    assert check_dashboard_url("http://127.0.0.1:4173/") == "http://127.0.0.1:4173"
    assert check_dashboard_url("https://agent-factory.example.test") == "https://agent-factory.example.test"
    for bad in ("http://dashboard.example.test", "https://user:pw@x.test", "https://x.test/?token=abc", "https://x.test/#t",
                "https://x.test/some/path", "javascript:alert(1)", "ftp://x.test"):  # fmt: skip
        with pytest.raises(ValueError):
            check_dashboard_url(bad)


def test_retry_delays_double_up_to_the_cap_with_bounded_jitter():
    exact = RetryPolicy(max_attempts=8, base=timedelta(seconds=30), cap=timedelta(minutes=30), jitter=False)
    assert [exact.delay(n).total_seconds() for n in range(1, 9)] == [30, 60, 120, 240, 480, 960, 1800, 1800]
    jittered = RetryPolicy()
    rng = random.Random(1)
    for n in range(1, 12):
        full = exact.delay(n)
        assert full / 2 <= jittered.delay(n, rng) <= full
    assert exact.exhausted(8) and not exact.exhausted(7)


# ------------------------------------------------------------------ the transactional outbox


def test_an_approval_creates_one_intent_per_eligible_destination_with_the_attention_event(flow):
    flow.paused()
    [n] = pending(flow)
    request = flow.request()
    assert (n.attention_kind, n.status, n.attempts, n.destination_id) == (
        AttentionKind.DECISION_REQUIRED, NotificationStatus.PENDING, 0, flow.email.id,
    )  # fmt: skip
    assert n.dedup_key == f"decision_required:{flow.project_id}:{request.id}"
    assert n.link_path == f"/projects/{flow.project_id}/approvals/{request.id}"
    assert "notice" not in n.body.lower() and "billing" not in n.body  # no tool, no parameters


def test_syncing_the_same_state_again_creates_nothing(flow):
    flow.paused()
    before = pending(flow)
    ex = flow.executor()
    for _ in range(5):
        ex.recorder.sync(flow.checkpoint())  # 5 repeated internal events about the same approval
    assert pending(flow) == before


def test_a_reconciliation_creates_its_own_intent_and_a_new_attempt_another(flow):
    flow.uncertain()
    rows = pending(flow)
    kinds = sorted((n.attention_kind.value, n.attention_attempt) for n in rows)
    assert kinds == [("decision_required", None), ("reconciliation_required", 1)]
    [rec] = [n for n in rows if n.attention_kind is AttentionKind.RECONCILIATION_REQUIRED]
    assert rec.execution_id == flow.execution().id
    assert "Reason: Action outcome needs verification" in rec.body


def test_only_people_who_may_act_are_notified(flow):
    reviewer = person(flow.store, "reader", {Permission.APPROVALS_READ})  # may look, may not decide
    destination(flow.store, reviewer, address="reader@example.test")
    disabled = person(flow.store, "gone", {Permission.APPROVALS_DECIDE})
    destination(flow.store, disabled, address="gone@example.test")
    flow.store.principals.update(disabled.with_status(PrincipalStatus.DISABLED, "2026-10-02T00:00:00Z"))
    off = destination(flow.store, flow.dana, "sms", PHONE)
    flow.store.notifications.update_destination(off.with_status(DestinationStatus.DISABLED, "2026-10-02T00:00:00Z"))
    reconciler = person(flow.store, "rita", RECONCILE - {Permission.APPROVALS_DECIDE})
    rita = destination(flow.store, reconciler, address="rita@example.test")
    flow.uncertain()
    by_kind = {(n.attention_kind.value, n.destination_id) for n in pending(flow)}
    assert by_kind == {
        ("decision_required", flow.email.id),
        ("reconciliation_required", flow.email.id),  # dana holds both permissions
        ("reconciliation_required", rita.id),  # rita may reconcile, not decide
    }


def test_a_rollback_keeps_neither_the_attention_event_nor_its_notification(flow):
    flow.create()
    ex = flow.executor()
    store = flow.store

    class Crash(Exception):
        pass

    original = store.notifications.enqueue

    def enqueue_then_crash(intent):
        original(intent)
        raise Crash()

    store.notifications.enqueue = enqueue_then_crash  # the process dies inside the sync's transaction
    try:
        with pytest.raises(Crash):
            flow.work()
    finally:
        store.notifications.enqueue = original
    assert store.unit_of_work.requests == {} and store.unit_of_work.notifications == {}  # neither half survived
    flow.clock.advance(seconds=31)  # the crashed run is offered again after the worker's retry delay
    flow.work()  # the next owner syncs the committed checkpoint: both appear, together
    assert len(pending(flow)) == 1


def test_the_sweep_creates_a_missing_intent_once(flow):
    flow.paused()
    flow.store.unit_of_work.notifications.clear()  # as if the attention event was committed without its intent
    dispatcher, _ = memory_dispatcher(flow)
    assert dispatcher.sweep() == 1
    assert dispatcher.sweep() == 0
    assert len(pending(flow)) == 1


# ------------------------------------------------------------------ dispatch


def test_a_notification_is_sent_once_with_a_link_that_authorizes_nothing(flow):
    flow.paused()
    provider = FakeNotificationProvider()
    dispatcher, _ = memory_dispatcher(flow, provider)
    assert dispatcher.run_once() is True
    [n] = pending(flow)
    assert (n.status, n.attempts, n.failure_category) == (NotificationStatus.SENT, 1, None)
    [call] = provider.delivered
    assert call["to"] == "D***@example.test" and call["title"] == "Agent Factory needs your attention"
    link = call["text"].splitlines()[-1]
    assert link == f"{DASHBOARD}/projects/{flow.project_id}/approvals/{flow.request().id}"
    assert "?" not in link and "token" not in link.lower()
    assert dispatcher.run_once() is False and len(provider.calls) == 1  # nothing left to send


def test_a_transient_failure_is_retried_with_backoff_reusing_the_same_notification(flow):
    flow.paused()
    provider = FakeNotificationProvider(["transient", "rate_limited", "success"])
    dispatcher, _ = memory_dispatcher(flow, provider, retry=RetryPolicy(jitter=False))
    dispatcher.run_once()
    [n] = pending(flow)
    assert (n.status, n.attempts, n.failure_category) == (NotificationStatus.PENDING, 1, FailureCategory.PROVIDER_UNAVAILABLE)
    assert n.next_attempt_at == "2026-10-05T09:00:30.000000Z"  # 30 s after the first attempt
    assert dispatcher.run_once() is False  # not due yet
    flow.clock.advance(seconds=30)
    dispatcher.run_once()
    [n] = pending(flow)
    assert (n.attempts, n.next_attempt_at) == (2, "2026-10-05T09:01:30.000000Z")  # then 60 s
    flow.clock.advance(seconds=60)
    dispatcher.run_once()
    [n] = pending(flow)
    assert (n.status, n.attempts) == (NotificationStatus.SENT, 3)
    assert {c["notification_id"] for c in provider.calls} == {n.id} and len(provider.delivered) == 1
    assert [a.outcome for a in flow.store.notifications.attempts(n.id)] == [
        AttemptOutcome.TRANSIENT_FAILURE, AttemptOutcome.TRANSIENT_FAILURE, AttemptOutcome.ACCEPTED,
    ]  # fmt: skip


def test_a_permanent_failure_is_never_retried_and_the_approval_still_waits(flow):
    flow.paused()
    provider = FakeNotificationProvider(["permanent"])
    dispatcher, _ = memory_dispatcher(flow, provider)
    dispatcher.run_once()
    flow.clock.advance(hours=12)
    dispatcher.run_once()
    [n] = pending(flow)
    assert (n.status, n.attempts, n.failure_category) == (NotificationStatus.FAILED, 1, FailureCategory.INVALID_DESTINATION)
    assert len(provider.calls) == 1
    assert flow.request().status.value == "pending" and flow.checkpoint().pending_approval is not None


def test_the_attempt_limit_is_enforced(flow):
    flow.paused()
    provider = FakeNotificationProvider(default="transient")
    dispatcher, _ = memory_dispatcher(flow, provider, retry=RetryPolicy(max_attempts=3, base=timedelta(seconds=1),
                                                                         cap=timedelta(seconds=1), jitter=False))  # fmt: skip
    for _ in range(10):
        dispatcher.run_once()
        flow.clock.advance(seconds=1)
    [n] = pending(flow)
    assert (n.status, n.attempts, n.failure_category) == (NotificationStatus.FAILED, 3, FailureCategory.MAX_ATTEMPTS)
    assert len(provider.calls) == 3


def test_a_lost_response_is_an_unknown_outcome_retried_honestly(flow):
    flow.paused()
    provider = FakeNotificationProvider(["accepted_response_lost", "success"])
    dispatcher, _ = memory_dispatcher(flow, provider, retry=RetryPolicy(jitter=False))
    dispatcher.run_once()
    [n] = pending(flow)
    assert (n.status, n.failure_category) == (NotificationStatus.PENDING, FailureCategory.TIMEOUT)
    assert flow.store.notifications.attempts(n.id)[0].outcome is AttemptOutcome.UNKNOWN
    flow.clock.advance(seconds=30)
    dispatcher.run_once()
    # At least once: the person may have received it twice. Agent Factory never guesses "it arrived".
    assert len(provider.delivered) == 2 and {c["idempotency_key"] for c in provider.calls} == {n.id}
    [summary] = summarize(pending(flow), flow.store.notifications.with_unknown_outcome([n.id]))
    assert (summary.state, summary.outcome_unknown) == (NotificationState.SENT, True)


def test_a_provider_that_honors_idempotency_suppresses_the_duplicate(flow):
    flow.paused()
    provider = FakeNotificationProvider(["accepted_response_lost", "success"], honor_idempotency=True)
    dispatcher, _ = memory_dispatcher(flow, provider)
    dispatcher.run_once()
    flow.clock.advance(minutes=2)
    dispatcher.run_once()
    assert len(provider.calls) == 2 and len(provider.delivered) == 1 and provider.calls[1]["duplicate_suppressed"]


def test_an_adapter_bug_is_never_taken_to_mean_nothing_was_sent(flow):
    flow.paused()

    class Broken(FakeNotificationProvider):
        def send(self, message, *, timeout):
            raise RuntimeError("bug after the request left")

    dispatcher, _ = memory_dispatcher(flow, Broken())
    dispatcher.run_once()
    [n] = pending(flow)
    assert flow.store.notifications.attempts(n.id)[0].outcome is AttemptOutcome.UNKNOWN
    assert n.status is NotificationStatus.PENDING


def test_what_no_longer_needs_anyone_is_skipped_not_sent(flow):
    flow.paused()
    flow.decide("approved")  # decided before the dispatcher got to it
    provider = FakeNotificationProvider()
    dispatcher, _ = memory_dispatcher(flow, provider)
    dispatcher.run_once()
    [n] = pending(flow)
    assert (n.status, n.failure_category) == (NotificationStatus.SKIPPED, FailureCategory.ATTENTION_RESOLVED)
    assert provider.calls == []


def test_a_disabled_destination_is_skipped(flow):
    flow.paused()
    flow.store.notifications.update_destination(flow.email.with_status(DestinationStatus.DISABLED, "2026-10-05T08:00:00Z"))
    provider = FakeNotificationProvider()
    dispatcher, _ = memory_dispatcher(flow, provider)
    dispatcher.run_once()
    assert pending(flow)[0].failure_category is FailureCategory.DESTINATION_DISABLED and provider.calls == []


def test_an_expired_approval_is_not_notified(flow):
    flow.paused()
    flow.clock.advance(hours=12, seconds=1)
    provider = FakeNotificationProvider()
    dispatcher, _ = memory_dispatcher(flow, provider, sweep=False)
    dispatcher.run_once()
    assert pending(flow)[0].failure_category is FailureCategory.ATTENTION_RESOLVED and provider.calls == []


def test_a_storm_for_one_destination_is_deferred_not_dropped(flow):
    from agent_factory.domain.approvals import ApprovalRequest, ExactAction, risk_for
    from agent_factory.domain.notifications import RateLimit

    flow.paused()
    for n in range(2):  # two more distinct approvals waiting in the same run (as if from other visits)
        action = ExactAction(tool="send_notice", category="external_communication", params={"to": f"team-{n}@example.test"})
        request = ApprovalRequest(
            id=f"apr-{n:016x}", project_id=flow.project_id, run_id="run-1", node="notify", agent="developer", action=action,
            action_fingerprint=action.fingerprint, reason="fixture", risk=risk_for(action.category), requested_at=flow.now(),
            expires_at="2026-10-05T21:00:00.000000Z",
        )  # fmt: skip
        flow.store.approvals.add_request(request)
    provider = FakeNotificationProvider()
    events: list = []
    dispatcher, _ = memory_dispatcher(flow, provider, events=events, rate=RateLimit(limit=2, window=timedelta(hours=1)))
    assert dispatcher.sweep() == 2  # one intent per distinct approval; none suppressed
    dispatcher.run_once()
    assert len(provider.calls) == 2  # the limit; the third waits instead of being dropped
    [deferred] = [e for e in events if e["event"] == "notification_deferred"]
    assert deferred["next_attempt_at"] == "2026-10-05T10:00:00.000000Z"
    flow.clock.at("2026-10-05T10:00:00Z")
    dispatcher.run_once()
    assert len(provider.delivered) == 3

def test_a_stale_dispatcher_records_nothing(flow):
    flow.paused()
    provider = FakeNotificationProvider()
    events: list = []
    dispatcher, outbox = memory_dispatcher(flow, provider, events=events, lease=timedelta(seconds=30), send_timeout=5)
    [claimed] = outbox.claim_due(dispatcher.dispatcher_id, timedelta(seconds=30))
    started = outbox.begin_attempt(claimed)
    flow.clock.advance(seconds=31)  # this dispatcher stalls past its lease...
    [recovered] = outbox.recover_expired(RetryPolicy(jitter=False))  # ...and another takes the notification back
    assert recovered.claim_token == claimed.claim_token + 1 and recovered.status is NotificationStatus.PENDING
    assert outbox.finish(started, Delivery(AttemptOutcome.ACCEPTED, provider_message_id="late"), RetryPolicy()) is None
    assert outbox.skip(started, FailureCategory.ATTENTION_RESOLVED) is False and outbox.defer(started, flow.now()) is False
    [attempt] = flow.store.notifications.attempts(claimed.id)
    assert (attempt.outcome, attempt.failure_category) == (AttemptOutcome.UNKNOWN, FailureCategory.OUTCOME_UNKNOWN)
    assert outbox.begin_attempt(started) is None  # nor can it begin another
    # Even claimed again under the same dispatcher ID, the old generation's result is refused: only the token tells them apart.
    flow.clock.advance(minutes=5)
    [again] = outbox.claim_due(dispatcher.dispatcher_id, timedelta(seconds=30))
    assert again.claimed_by == started.claimed_by and again.claim_token > started.claim_token
    assert outbox.finish(started, Delivery(AttemptOutcome.ACCEPTED, provider_message_id="late"), RetryPolicy()) is None
    assert outbox.begin_attempt(started) is None and outbox.skip(started, FailureCategory.ATTENTION_RESOLVED) is False


def test_a_dispatcher_whose_lease_ran_out_may_not_begin_an_attempt(flow):
    flow.paused()
    dispatcher, outbox = memory_dispatcher(flow, lease=timedelta(seconds=30), send_timeout=5)
    [claimed] = outbox.claim_due(dispatcher.dispatcher_id, timedelta(seconds=30))
    flow.clock.advance(seconds=30)  # the lease ended (nobody has recovered it yet)
    assert outbox.begin_attempt(claimed) is None
    assert flow.store.notifications.attempts(claimed.id) == []


def test_a_dispatcher_lost_before_it_began_an_attempt_sent_nothing_and_is_retried_at_once(flow):
    flow.paused()
    dispatcher, outbox = memory_dispatcher(flow, lease=timedelta(seconds=30), send_timeout=5)
    [claimed] = outbox.claim_due(dispatcher.dispatcher_id, timedelta(seconds=30))
    flow.clock.advance(seconds=30)
    [recovered] = outbox.recover_expired(RetryPolicy())
    assert (recovered.status, recovered.attempts, recovered.next_attempt_at) == (NotificationStatus.PENDING, 0, flow.now())
    assert flow.store.notifications.attempts(claimed.id) == []


def test_two_claimers_never_hold_the_same_notification(flow):
    flow.paused()
    _, outbox = memory_dispatcher(flow)
    first = outbox.claim_due("ndp-" + "1" * 20, timedelta(seconds=30))
    second = outbox.claim_due("ndp-" + "2" * 20, timedelta(seconds=30))
    assert len(first) == 1 and second == []


def test_dispatcher_events_never_carry_an_address_or_the_message(flow):
    flow.uncertain()
    events: list = []
    provider = FakeNotificationProvider(["timeout", "transient", "permanent"])
    dispatcher, _ = memory_dispatcher(flow, provider, events=events)
    for _ in range(3):
        dispatcher.run_once()
        flow.clock.advance(hours=1)
    text = json.dumps(events)
    assert events and EMAIL.lower() not in text.lower() and "dana.owner" not in text.lower()
    assert "Open Agent Factory" not in text and "http" not in text
    with pytest.raises(ValueError):
        DispatcherEvents("ndp-" + "0" * 20).emit("notification_sent", address="x@example.test")


def test_a_notification_failure_never_removes_the_attention_item(flow):
    flow.paused()
    dispatcher, _ = memory_dispatcher(flow, FakeNotificationProvider(default="permanent"))
    dispatcher.run_once()
    inbox = flow.store.approvals.page_attention(limit=10, at=flow.now())
    assert [i.approval_id for i in inbox] == [flow.request().id]


def test_the_summary_tells_queued_retrying_sent_failed_and_skipped_apart(flow):
    flow.paused()
    [n] = pending(flow)
    assert summarize([n])[0].state is NotificationState.QUEUED
    assert summarize([replace(n, attempts=1, failure_category=FailureCategory.TIMEOUT)])[0].state is NotificationState.RETRYING
    sent = replace(n, status=NotificationStatus.SENT, sent_at=n.created_at, next_attempt_at=None, attempts=1)
    failed = replace(n, id="ntf-" + "f" * 20, status=NotificationStatus.FAILED, next_attempt_at=None,
                     failure_category=FailureCategory.INVALID_DESTINATION)  # fmt: skip
    assert summarize([sent, failed])[0].state is NotificationState.SENT  # someone was told
    assert summarize([failed])[0].state is NotificationState.FAILED
    skipped = replace(n, status=NotificationStatus.SKIPPED, next_attempt_at=None, failure_category=FailureCategory.ATTENTION_RESOLVED)
    assert summarize([skipped])[0].state is NotificationState.SKIPPED


def test_the_overnight_story_in_memory(tmp_path):
    """1:00 approval required -> notified; 1:05 unrelated milestone -> nothing new; 2:00 reconciliation -> notified."""
    first = Flow.memory(tmp_path / "a", kind="unknown-notice")
    first.clock.at("2026-10-05T01:00:00Z")
    email = destination(first.store, first.dana)
    provider = FakeNotificationProvider()
    first.paused()
    dispatcher, _ = memory_dispatcher(first, provider)
    dispatcher.run_once()
    assert len(provider.delivered) == 1
    first.clock.at("2026-10-05T01:05:00Z")
    first.executor().recorder.sync(first.checkpoint())  # another internal event about the same paused run
    dispatcher.run_once()
    assert len(provider.delivered) == 1
    first.clock.at("2026-10-05T01:10:00Z")
    first.decide("approved")  # (the night's other event: the approved notice's outcome turns out unknown at 2:00)
    first.clock.at("2026-10-05T02:00:00Z")
    first.work()
    dispatcher.run_once()
    kinds = [c["text"].splitlines()[3] for c in provider.delivered]
    assert kinds == ["Reason: Approval required", "Reason: Action outcome needs verification"]
    assert {n.destination_id for n in pending(first)} == {email.id}


# ------------------------------------------------------------------ boundaries


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def _absolute_imports(path: Path) -> set[str]:
    """Every module ``path`` imports, with relative imports resolved."""
    package = list(path.relative_to(REPO).with_suffix("").parts)[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found |= {f"{module}.{a.name}" for a in node.names}
    return found


def test_only_the_dispatcher_and_the_operator_cli_can_reach_a_provider():
    allowed = {PACKAGE / "cli.py"} | set((PACKAGE / "notifications").glob("*.py"))
    offenders = [
        f"{path.relative_to(REPO)}: {name}"
        for path in PACKAGE.rglob("*.py") if path not in allowed
        for name in _absolute_imports(path) if name == "agent_factory.notifications" or name.startswith("agent_factory.notifications.")
    ]  # fmt: skip
    assert offenders == []

def test_agents_tools_engine_sandbox_and_api_never_import_the_provider_package():
    for folder in ("agents", "tools", "engine", "sandbox", "api", "graph", "safety", "worker", "providers", "templates"):
        for path in (PACKAGE / folder).rglob("*.py"):
            for name in _imports(path):
                assert not name.endswith("notifications") or name.endswith("domain.notifications"), f"{path}: {name}"
                assert "notifications.provider" not in name and "notifications.fake" not in name, f"{path}: {name}"


def test_no_tool_can_send_a_notification():
    text = "\n".join(p.read_text(encoding="utf-8") for p in (PACKAGE / "tools").rglob("*.py"))
    assert "notif" not in text.lower() and "sms" not in text.lower() and "send_email" not in text.lower()


def test_the_api_has_no_route_that_sends_anything():
    import yaml

    spec = yaml.safe_load((REPO / "openapi.yaml").read_text(encoding="utf-8"))
    for path, item in spec["paths"].items():
        assert not re.search(r"send|sms|email|notif|message|push", path, re.IGNORECASE), path
        for method in item:
            if method in ("post", "put", "patch", "delete"):
                assert item[method]["operationId"] in {"createProject", "decideApproval", "reconcileAction"}


def test_provider_credentials_can_never_reach_the_sandbox():
    from agent_factory.sandbox.validation import DEFAULT_ENV_ALLOWLIST, validate_env

    names = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE",
             "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY", "SES_SMTP_PASSWORD", "AGENT_FACTORY_NOTIFICATIONS_MODE"]  # fmt: skip
    for name in names:
        problems = validate_env({name: "x"}, DEFAULT_ENV_ALLOWLIST | {name})
        assert any("looks like a credential" in p or "never passed" in p for p in problems), (name, problems)

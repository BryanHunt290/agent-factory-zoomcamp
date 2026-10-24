"""The notification outbox and dispatcher against a real PostgreSQL, with real processes that die.

    set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\\test-database.url
    .venv\\Scripts\\python -m pytest -m postgres --run-postgres tests/test_postgres_notifications.py -v

* the transactional outbox through a real worker on a fenced session; atomicity; deduplication that
  survives reconnection; the database's own constraints;
* concurrency: dispatchers racing for the same notifications on separate connections;
* fencing: a dispatcher whose notification was recovered records nothing;
* crash boundaries, each with a real ``notifications dispatch`` process (or a claiming child) killed like a
  crash at that point, and a second dispatcher process that recovers:

      1  attention committed, its intent missing            -> the sweep creates it
      2  intent committed, no dispatcher yet                 -> sent when a dispatcher starts
      3  claimed, killed before any attempt                  -> recovered, sent; nothing was lost
      4  attempt begun, killed before the provider accepted  -> recovered as "unknown", sent once
      5  provider accepted, killed before the result         -> recovered as "unknown", sent AGAIN (honest duplicate)
      6  result recorded, then killed                        -> nothing is sent again
      7  retry scheduled, then killed                        -> the next dispatcher retries on time

Every result is read from PostgreSQL and from the fake provider's record file; nothing is assumed.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from dashboard_support import Child, clean_env, enroll, seed_approval, seed_project, wait_until

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parent.parent
CHILD = Path(__file__).resolve().parent / "notification_child.py"


# ------------------------------------------------------------------ helpers


def owner(store, name="Owner", subject=None):
    from agent_factory.domain.principals import ALL_PERMISSIONS

    return enroll(store, subject or f"owner-{time.monotonic_ns()}", ALL_PERMISSIONS, name)


def add_destination(store, principal, address="owner@example.test"):
    from notification_support import destination

    return destination(store, principal, "email", address)


def waiting_approval(store, name="Overnight project", path="legacy_calc.py", enqueue=True):
    """A run waiting for one approval (and, unless ``enqueue`` is False, its notification intents)."""
    from agent_factory.domain.attention import AttentionItem, AttentionKind
    from agent_factory.domain.notifications import NotificationIntent
    from agent_factory.util import timestamp, utc_now

    project = seed_project(store, name)
    request = seed_approval(store, project, path=path)
    if enqueue:
        item = AttentionItem(kind=AttentionKind.DECISION_REQUIRED, project_id=project.id, run_id=request.run_id,
                             approval_id=request.id, since=request.requested_at, tool=request.action.tool,
                             category=request.action.category, expires_at=request.expires_at)  # fmt: skip
        with store.transaction():
            store.notifications.enqueue(NotificationIntent.for_item(item, project.name, at=timestamp(utc_now)))
    return project, request


def rows(store, project, request):
    return store.notifications.list_for_approval(project.id, request.id)


class Stack:
    """Dispatcher processes for one test, on its schema; each killable like a crash."""

    def __init__(self, tmp_path, config) -> None:
        self.tmp = tmp_path
        self.config = config
        self.logs = tmp_path / "logs"
        self.logs.mkdir(exist_ok=True)
        self.record = tmp_path / "provider.jsonl"
        self.children: list[Child] = []

    def env(self):
        return clean_env(AGENT_FACTORY_DATABASE_URL=self.config.url, AGENT_FACTORY_DATABASE_SCHEMA=self.config.schema,
                         AGENT_FACTORY_NOTIFICATIONS_MODE="development")  # fmt: skip

    def dispatcher(self, *script: str, default="success", lease=2.0, extra=()) -> Child:
        import json

        argv = [sys.executable, "-m", "agent_factory", "notifications", "dispatch", "--provider", "fake",
                "--fake-record", str(self.record), "--dashboard-url", "http://127.0.0.1:4173", "--poll", "0.1",
                "--lease", str(lease), "--send-timeout", str(lease / 4), "--retry-base", "1", "--retry-cap", "2",
                "--max-backoff", "0.5", "--sweep", "0.5", *extra]  # fmt: skip
        if script or default != "success":
            path = self.tmp / f"script-{time.monotonic_ns()}.json"
            path.write_text(json.dumps({"script": list(script), "default": default}), encoding="utf-8")
            argv += ["--fake-script", str(path)]
        child = Child(f"dispatcher-{len(self.children)}", argv, REPO, self.env(), self.logs)  # unique: one log file each
        self.children.append(child)
        return child

    def claimer(self, lease=2.0) -> Child:
        child = Child(f"claimer-{len(self.children)}", [sys.executable, str(CHILD), "claim-then-wait", str(lease)], REPO, self.env(), self.logs)
        self.children.append(child)
        return child

    def calls(self):
        from agent_factory.notifications import read_record

        return read_record(self.record)

    def close(self):
        for child in self.children:
            child.kill()


@pytest.fixture
def stack(tmp_path, pg_schema):
    s = Stack(tmp_path, pg_schema)
    yield s
    s.close()


def events(child: Child, name: str) -> list[dict]:
    return [e for e in child.events() if e.get("event") == name]


# ------------------------------------------------------------------ the transactional outbox


def test_a_real_worker_records_the_approval_and_its_intent_together(tmp_path, pg_schema, pg_store):
    from decision_support import Flow
    from notification_support import destination

    from agent_factory.domain.attention import AttentionKind

    flow = Flow.postgres(tmp_path, pg_schema, pg_store, "unknown-notice")
    email = destination(pg_store, flow.dana)
    flow.paused()  # a real worker, on a session fenced by its claim
    request = flow.request()
    [n] = pg_store.notifications.list_for_approval(flow.project_id, request.id)
    assert (n.attention_kind, n.destination_id, n.status.value) == (AttentionKind.DECISION_REQUIRED, email.id, "pending")
    flow.decide("approved")
    flow.work()  # sent with an unknown outcome: reconciliation required
    kinds = sorted(n.attention_kind.value for n in pg_store.notifications.list_for_approval(flow.project_id, request.id))
    assert kinds == ["decision_required", "reconciliation_required"]


def test_recording_the_same_event_again_on_new_connections_creates_nothing(pg_open, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    first = rows(pg_store, project, request)
    from agent_factory.notifications import DispatcherSettings, FakeNotificationProvider, NotificationDispatcher
    from agent_factory.persistence.notifications import PostgresNotificationOutbox

    for _ in range(3):  # three restarts, each sweeping the inbox again
        store = pg_open()
        dispatcher = NotificationDispatcher(
            "ndp-" + "1" * 20, DispatcherSettings(), open_outbox=lambda s=store: PostgresNotificationOutbox(s.unit_of_work),
            provider=FakeNotificationProvider(), open_store=lambda s=store: s,
        )  # fmt: skip
        assert dispatcher.sweep() == 0
    assert rows(pg_store, project, request) == first and len(first) == 1


def test_a_rollback_keeps_neither_the_attention_event_nor_its_intent(pg_store):
    from agent_factory.domain.attention import AttentionItem, AttentionKind
    from agent_factory.domain.notifications import NotificationIntent

    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project = seed_project(pg_store, "Rolled back")

    class Crash(Exception):
        pass

    with pytest.raises(Crash), pg_store.transaction():
        request = seed_approval(pg_store, project)
        item = AttentionItem(kind=AttentionKind.DECISION_REQUIRED, project_id=project.id, run_id="run-1", approval_id=request.id,
                             since=request.requested_at, tool="delete_file", category="destructive_delete")  # fmt: skip
        assert pg_store.notifications.enqueue(NotificationIntent.for_item(item, "x", at=request.requested_at))
        raise Crash()
    assert pg_store.approvals.get_request(project.id, request.id) is None
    assert pg_store.notifications.list_for_approval(project.id, request.id) == []


def test_the_database_refuses_an_unsafe_or_inconsistent_notification(pg_schema, pg_store):
    from agent_factory.persistence.postgres import connect
    import psycopg

    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    [n] = rows(pg_store, project, request)
    conn = connect(pg_schema)
    try:
        for statement in (
            "UPDATE notifications SET link_path = link_path || '?token=abc' WHERE id = %s",  # a link that could carry a secret
            "UPDATE notifications SET status = 'sending' WHERE id = %s",  # sending without an owner and a lease
            "UPDATE notifications SET status = 'skipped', next_attempt_at = NULL, failure_category = 'timeout' WHERE id = %s",
            "UPDATE notifications SET status = 'sending', claimed_by = 'ndp-" + "1" * 20 + "', "
            "lease_expires_at = updated_at + interval '6 minutes' WHERE id = %s",  # a lease over five minutes
        ):  # fmt: skip
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement, (n.id,))
        with pytest.raises(psycopg.errors.UniqueViolation):  # one notification per event and destination
            conn.execute("INSERT INTO notifications SELECT 'ntf-' || repeat('0', 20), dedup_key, schema_version, project_id, "
                         "run_id, approval_id, attention_kind, execution_id, attention_attempt, destination_id, principal_id, "
                         "channel, severity, title, body, link_path, status, attempts, created_at, available_at, "
                         "next_attempt_at, last_attempt_at, sent_at, provider_message_id, failure_category, claimed_by, "
                         "claim_token, lease_expires_at, updated_at FROM notifications WHERE id = %s", (n.id,))  # fmt: skip
    finally:
        conn.close()


def test_the_claim_statement_is_served_by_its_index(pg_schema):
    import re

    from agent_factory.persistence.notifications import CLAIM_DUE
    from agent_factory.persistence.postgres import connect

    conn = connect(pg_schema)
    try:
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.execute(
            "EXPLAIN " + CLAIM_DUE, {"dispatcher_id": "ndp-" + "1" * 20, "lease": timedelta(seconds=60), "limit": 10}))  # fmt: skip
    finally:
        conn.close()
    assert re.search(r"\b(using|on) notifications_due_idx\b", plan), plan


# ------------------------------------------------------------------ concurrency


def test_racing_claimers_never_share_a_notification(pg_schema, pg_store):
    from agent_factory.persistence.notifications import open_notification_outbox

    principal = owner(pg_store)
    add_destination(pg_store, principal)
    for i in range(12):
        waiting_approval(pg_store, name=f"P{i}", path=f"f{i}.py")
    barrier = threading.Barrier(4)
    claimed: list[list[str]] = [[] for _ in range(4)]

    def claim(k: int) -> None:
        outbox = open_notification_outbox(pg_schema)
        try:
            barrier.wait()
            while batch := outbox.claim_due(f"ndp-{k:020x}", timedelta(seconds=60), limit=2):
                claimed[k] += [n.id for n in batch]
        finally:
            outbox.close()

    threads = [threading.Thread(target=claim, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    every = [i for ids in claimed for i in ids]
    assert len(every) == 12 and len(set(every)) == 12  # each claimed exactly once, by one dispatcher


def test_two_dispatcher_processes_deliver_each_notification_once(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    approvals = [waiting_approval(pg_store, name=f"Run {i}", path=f"f{i}.py") for i in range(8)]
    first, second = stack.dispatcher(), stack.dispatcher()
    wait_until(lambda: all(n.status.value == "sent" for p, r in approvals for n in rows(pg_store, p, r)), 60, "all sent")
    calls = stack.calls()
    assert sorted(c["notification_id"] for c in calls) == sorted(n.id for p, r in approvals for n in rows(pg_store, p, r))
    assert len(events(first, "notification_sent")) + len(events(second, "notification_sent")) == 8


def test_a_stale_dispatcher_records_nothing(pg_schema, pg_store):
    from agent_factory.domain.notifications import AttemptOutcome, Delivery, RetryPolicy
    from agent_factory.persistence.notifications import open_notification_outbox
    from agent_factory.persistence.postgres import connect

    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    stale, other = open_notification_outbox(pg_schema), open_notification_outbox(pg_schema)
    try:
        [claimed] = stale.claim_due("ndp-" + "a" * 20, timedelta(seconds=60))
        started = stale.begin_attempt(claimed)
        conn = connect(pg_schema)
        conn.execute("UPDATE notifications SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (claimed.id,))
        conn.close()  # the stale dispatcher stalled past its lease (in PostgreSQL's time)
        [recovered] = other.recover_expired(RetryPolicy(jitter=False))
        assert recovered.claim_token == claimed.claim_token + 1
        assert stale.finish(started, Delivery(AttemptOutcome.ACCEPTED, provider_message_id="late"), RetryPolicy()) is None
        assert stale.begin_attempt(started) is None
        [attempt] = pg_store.notifications.attempts(claimed.id)
        assert attempt.outcome is AttemptOutcome.UNKNOWN
        [n] = rows(pg_store, project, request)
        assert n.status.value == "pending" and n.provider_message_id is None
        # Claimed again under the SAME dispatcher ID: the old generation's result is still refused (the token decides).
        conn = connect(pg_schema)
        conn.execute("UPDATE notifications SET next_attempt_at = now() WHERE id = %s", (n.id,))
        conn.close()
        [again] = other.claim_due(claimed.claimed_by, timedelta(seconds=60))
        assert again.claimed_by == started.claimed_by and again.claim_token > started.claim_token
        assert stale.finish(started, Delivery(AttemptOutcome.ACCEPTED, provider_message_id="late"), RetryPolicy()) is None
        assert stale.begin_attempt(started) is None
        assert rows(pg_store, project, request)[0].status.value == "sending"  # still the new generation's
    finally:
        stale.close()
        other.close()


def test_retry_timing_is_decided_by_the_database_clock(pg_schema, pg_store):
    from agent_factory.domain.notifications import RetryPolicy
    from agent_factory.domain.serialization import parse_timestamp
    from agent_factory.notifications import DispatcherSettings, FakeNotificationProvider, NotificationDispatcher
    from agent_factory.persistence.notifications import open_notification_outbox

    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    outbox = open_notification_outbox(pg_schema)
    settings = DispatcherSettings(retry=RetryPolicy(max_attempts=2, base=timedelta(seconds=30), jitter=False))
    dispatcher = NotificationDispatcher("ndp-" + "b" * 20, settings, open_outbox=lambda: outbox,
                                        provider=FakeNotificationProvider(default="transient"))  # fmt: skip
    dispatcher.run_once()
    [n] = rows(pg_store, project, request)
    assert n.status.value == "pending" and n.attempts == 1
    # The delay counts from the end of the attempt (PostgreSQL's now() when its result was recorded).
    assert parse_timestamp(n.next_attempt_at) - parse_timestamp(n.updated_at) == timedelta(seconds=30)
    assert parse_timestamp(n.updated_at) >= parse_timestamp(n.last_attempt_at)
    assert dispatcher.run_once() is False  # not due yet
    outbox.close()


# ------------------------------------------------------------------ crash boundaries (real processes)


def test_boundary_1_an_attention_event_without_its_intent_is_found_by_the_sweep(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store, enqueue=False)  # committed without its intent
    assert rows(pg_store, project, request) == []
    dispatcher = stack.dispatcher()
    wait_until(lambda: [n.status.value for n in rows(pg_store, project, request)] == ["sent"], 60, "the sweep's notification sent")
    assert len(stack.calls()) == 1 and events(dispatcher, "intents_created")[0]["count"] == 1


def test_boundary_2_an_intent_waits_for_a_dispatcher(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    time.sleep(1.0)  # nothing runs: the intent simply waits, durably
    assert [n.status.value for n in rows(pg_store, project, request)] == ["pending"] and stack.calls() == []
    stack.dispatcher()
    wait_until(lambda: [n.status.value for n in rows(pg_store, project, request)] == ["sent"], 60, "sent")


def test_boundary_3_killed_after_claiming_before_any_attempt(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    claimer = stack.claimer(lease=2.0)
    wait_until(lambda: events(claimer, "claimed"), 30, "the claim")
    claimer.kill()  # like a crash: claimed, nothing attempted
    [n] = rows(pg_store, project, request)
    assert (n.status.value, n.attempts) == ("sending", 0)
    stack.dispatcher()
    wait_until(lambda: rows(pg_store, project, request)[0].status.value == "sent", 60, "recovered and sent")
    [n] = rows(pg_store, project, request)
    assert n.attempts == 1 and [a.outcome.value for a in pg_store.notifications.attempts(n.id)] == ["accepted"]
    assert len(stack.calls()) == 1  # nothing was lost, nothing duplicated


def test_boundary_4_killed_after_the_attempt_began_before_the_provider_accepted(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    first = stack.dispatcher("hang_before_send")
    wait_until(lambda: [c["outcome"] for c in stack.calls()] == ["hang_before_send"], 60, "the send to begin")
    first.kill()
    stack.dispatcher()
    wait_until(lambda: rows(pg_store, project, request)[0].status.value == "sent", 60, "recovered and sent")
    [n] = rows(pg_store, project, request)
    # The first attempt's outcome is honestly unknown (Agent Factory cannot tell it never left), then a retry.
    assert [a.outcome.value for a in pg_store.notifications.attempts(n.id)] == ["unknown", "accepted"]
    assert [c["delivered"] for c in stack.calls()] == [False, True]  # the person got it once


def test_boundary_5_killed_after_the_provider_accepted_before_the_result_was_recorded(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    first = stack.dispatcher("hang_after_accept")
    wait_until(lambda: [c["outcome"] for c in stack.calls()] == ["hang_after_accept"], 60, "the provider to accept")
    first.kill()
    [n] = rows(pg_store, project, request)
    assert (n.status.value, n.attempts) == ("sending", 1)  # Agent Factory does not know it was accepted
    stack.dispatcher()
    wait_until(lambda: rows(pg_store, project, request)[0].status.value == "sent", 60, "recovered and sent")
    [n] = rows(pg_store, project, request)
    assert [a.outcome.value for a in pg_store.notifications.attempts(n.id)] == ["unknown", "accepted"]
    # At least once: the person received it twice. That duplicate is the price of never losing an alert.
    assert [c["delivered"] for c in stack.calls()] == [True, True]
    assert {c["idempotency_key"] for c in stack.calls()} == {n.id}  # what a provider with idempotency keys would use


def test_boundary_6_killed_after_the_result_was_recorded(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    first = stack.dispatcher()
    wait_until(lambda: rows(pg_store, project, request)[0].status.value == "sent", 60, "sent")
    first.kill()
    second = stack.dispatcher(extra=("--idle-exit", "2"))
    wait_until(lambda: not second.alive, 60, "the second dispatcher to find nothing to do")
    assert len(stack.calls()) == 1 and events(second, "notification_claimed") == []


def test_boundary_7_killed_after_scheduling_a_retry(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    first = stack.dispatcher("transient")
    wait_until(lambda: events(first, "notification_retry_scheduled"), 60, "a retry to be scheduled")
    first.kill()
    [n] = rows(pg_store, project, request)
    assert (n.status.value, n.attempts, n.failure_category.value) == ("pending", 1, "provider_unavailable")
    stack.dispatcher()
    wait_until(lambda: rows(pg_store, project, request)[0].status.value == "sent", 60, "the retry")
    [n] = rows(pg_store, project, request)
    assert [a.outcome.value for a in pg_store.notifications.attempts(n.id)] == ["transient_failure", "accepted"]
    assert [c["delivered"] for c in stack.calls()] == [False, True]


def test_a_permanent_failure_stops_and_the_approval_still_waits(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal)
    project, request = waiting_approval(pg_store)
    dispatcher = stack.dispatcher(default="permanent", extra=("--idle-exit", "3"))
    wait_until(lambda: not dispatcher.alive, 60, "the dispatcher to finish")
    [n] = rows(pg_store, project, request)
    assert (n.status.value, n.attempts, n.failure_category.value) == ("failed", 1, "invalid_destination")
    assert len(stack.calls()) == 1
    assert pg_store.approvals.get_request(project.id, request.id).status.value == "pending"
    from agent_factory.util import timestamp, utc_now

    assert [i.approval_id for i in pg_store.approvals.page_attention(limit=10, at=timestamp(utc_now))] == [request.id]


def test_dispatcher_output_never_contains_the_address(stack, pg_store):
    principal = owner(pg_store)
    add_destination(pg_store, principal, address="secret.person@example.test")
    waiting_approval(pg_store)
    dispatcher = stack.dispatcher("timeout", "transient", extra=("--idle-exit", "4"))
    wait_until(lambda: not dispatcher.alive, 90, "the dispatcher to finish")
    output = dispatcher.output()
    assert "secret.person" not in output and "notification_sent" in output
    assert "secret.person" not in stack.record.read_text(encoding="utf-8")  # even the fake records only "s***@example.test"

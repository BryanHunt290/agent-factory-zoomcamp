"""Attention notifications with the real stack: Chrome, the API, PostgreSQL, workers, and a real dispatcher process.

    set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\\test-database.url
    .venv\\Scripts\\python -m pytest -m browser --run-browser --run-postgres tests/test_dashboard_notifications.py -v
    rem the PostgreSQL scenario stops and starts this project's database container:
    .venv\\Scripts\\python -m pytest -m browser --run-browser --run-postgres --run-docker-live tests/test_dashboard_notifications.py -v

The dispatcher is ``python -m agent_factory notifications dispatch`` with the fake provider (DEVELOPMENT ONLY):
it contacts nothing and appends every message it would have sent to a record file, which these tests read as
"the person's phone". Nothing mocks the API, the database, the workers, or the dispatcher. Like the reliability
suite, every scenario records what it observed (DASHBOARD_EVIDENCE_DIR) for docs/dashboard-reliability.md.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from dashboard_support import (
    DESKTOP,
    MOBILE,
    RECORDED,
    TERMINAL,
    calculator_run,
    database_reachable,
    decide,
    docker,
    guarded_container,
    http,
    notice_run,
    wait_until,
)
from notification_support import destination

from agent_factory.notifications import read_record

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

OWNER_ADDRESS = "dashboard.owner@example.test"


@pytest.fixture
def evidence(request):
    record: dict = {"scenario": request.node.name}
    yield record
    out = os.environ.get("DASHBOARD_EVIDENCE_DIR")
    if out:
        directory = Path(out)
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
        (directory / f"{name}.json").write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


@pytest.fixture
def phone(dashboard, tmp_path):
    """The owner's notification destination, and the record of what the fake provider delivered to it."""
    destination(dashboard.store, dashboard.owner, "email", OWNER_ADDRESS)
    return tmp_path / "phone.jsonl"


def delivered(record: Path) -> list[dict]:
    return [c for c in read_record(record) if c["delivered"]]


def reasons(record: Path) -> list[str]:
    return [line for c in delivered(record) for line in c["text"].splitlines() if line.startswith("Reason:")]


def notifications(dashboard, project_id: str, approval_id: str) -> list[dict]:
    return dashboard.approval(project_id, approval_id)["notifications"]


def approval_of(dashboard, project_id: str) -> str:
    status, body = dashboard.call("GET", f"/api/v1/approvals?status=pending&project_id={project_id}")
    assert status == 200 and body["items"], body
    return body["items"][0]["id"]


def assert_safe(call: dict, dashboard) -> None:
    """A delivered message: minimal, with a link that is a plain dashboard path."""
    text = call["text"]
    link = text.splitlines()[-1]
    parts = urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}" == dashboard.url and not parts.query and not parts.fragment
    assert re.fullmatch(r"/projects/prj-[0-9a-f]{20}/approvals/apr-[0-9a-f]{16}", parts.path)
    for forbidden in (dashboard.token, "Bearer", "legacy_calc", "delete_file", "billing@", dashboard.config.url,
                      "sha256:", "\\", "password"):  # fmt: skip
        assert forbidden not in text
    assert call["to"] == "d***@example.test" and OWNER_ADDRESS not in json.dumps(call)


# ------------------------------------------------------------------ the overnight story


def test_overnight_two_attention_events_two_notifications_and_the_morning_view(dashboard, browse, tmp_path, phone, evidence):
    """No browser open all night. 1: approval required -> notified. Then a milestone of another run -> nothing.
    2: an action's outcome unknown -> notified separately. Morning: the dashboard shows both, with history."""
    dispatcher = dashboard.dispatcher(phone)
    evidence["initial_state"] = "no browser open; a worker, the API, PostgreSQL, and the dispatcher running"

    project_id, legacy, calc_worker = calculator_run(dashboard, tmp_path / "calc", name="Client ETL Pipeline")
    approval_id = wait_until(lambda: dashboard.call("GET", f"/api/v1/approvals?status=pending&project_id={project_id}")[1]
                             .get("items"), 120, "the approval")[0]["id"]  # fmt: skip
    wait_until(lambda: len(delivered(phone)) == 1, 60, "the approval notification")
    assert reasons(phone) == ["Reason: Approval required"]
    assert_safe(delivered(phone)[0], dashboard)
    assert "Project: Client ETL Pipeline" in delivered(phone)[0]["text"]

    # The calculator run now waits for a person, so its worker has nothing left to do. Stop it: workers of different
    # kinds share one queue, and a `worker run` that claims the notice fixture's run cannot open its workspace and
    # releases it for 30 s (found as a flaky failure in a repeated run; docs/dashboard-reliability.md).
    calc_worker.kill()
    # Another run makes progress (milestones, its own approval request); the first approval is not notified again.
    flow, notice_worker = notice_run(dashboard, tmp_path / "notice", "unknown-notice")
    notice_approval = wait_until(lambda: dashboard.call("GET", f"/api/v1/approvals?status=pending&project_id={flow.project_id}")[1]
                                 .get("items"), 120, "the notice's approval")[0]["id"]  # fmt: skip
    wait_until(lambda: len(delivered(phone)) == 2, 60, "the notice's approval notification")
    status, _ = dashboard.call("POST", f"/api/v1/projects/{flow.project_id}/approvals/{notice_approval}/decision",
                               {"outcome": "approved", "action_fingerprint": dashboard.approval(flow.project_id, notice_approval)["action_fingerprint"]})  # fmt: skip
    assert status == 201  # approved the evening before, from the API

    # The approved notice is sent, and its outcome is unknown: a separate attention event, a separate notification.
    wait_until(lambda: len(delivered(phone)) == 3, 120, "the reconciliation notification")
    assert reasons(phone) == ["Reason: Approval required", "Reason: Approval required", "Reason: Action outcome needs verification"]
    assert len({c["notification_id"] for c in delivered(phone)}) == 3  # three events, three notifications, no repeats

    # Morning: open the dashboard. Both items are waiting, each with its notification history.
    browser = browse(MOBILE)
    browser.sign_in(dashboard.token, path="/attention")
    inbox = browser.page.get_by_role("main")
    browser.page.get_by_role("link", name=re.compile("Approval required")).first.wait_for(timeout=30_000)
    browser.page.get_by_role("link", name=re.compile("Reconciliation required")).first.wait_for(timeout=30_000)
    inbox_text = " ".join(inbox.inner_text().split())  # read now: the locator follows the page as it navigates
    browser.open(f"/projects/{project_id}/approvals/{approval_id}")
    browser.page.get_by_test_id("notifications").get_by_text("Notification sent").wait_for(timeout=15_000)
    browser.open(f"/projects/{flow.project_id}/approvals/{notice_approval}")
    history = browser.page.get_by_test_id("notifications")
    history.get_by_text("Whether attempt 1 happened:").wait_for(timeout=15_000)
    assert history.inner_text().count("Notification sent") == 2
    seen = {"inbox": inbox_text[:400], "notice_history": " ".join(history.inner_text().split())}
    api_view = notifications(dashboard, flow.project_id, notice_approval)
    assert [(n["about"], n["state"]) for n in api_view] == [("decision_required", "sent"), ("reconciliation_required", "sent")]
    assert dispatcher.alive and notice_worker.alive
    browser.assert_clean(dashboard.token)
    evidence.update(
        failure_injected="none: the browser was closed all night",
        user_saw={"phone": reasons(phone), "morning": seen},
        continued_running="the notice's worker and the dispatcher (the calculator's worker was stopped once its run waited); no browser",
        recovery="none needed",
        final_state={"notifications_delivered": len(delivered(phone)), "api_history": api_view},
        data_lost=False, action_duplicated=False,
    )  # fmt: skip


def test_the_link_leads_through_sign_in_to_the_exact_approval(dashboard, browse, tmp_path, phone, evidence):
    dashboard.dispatcher(phone)
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    wait_until(lambda: delivered(phone), 120, "the notification")
    link = delivered(phone)[0]["text"].splitlines()[-1]
    path = urlsplit(link).path
    browser = browse(MOBILE)
    browser.open(path)  # no session in this browser: the link alone opens nothing
    browser.page.get_by_role("heading", name="Sign in").wait_for()
    assert "Approve" not in browser.text()
    browser.page.get_by_label("Development access token").fill(dashboard.token)
    browser.page.get_by_role("button", name="Sign in").click()
    browser.page.get_by_role("heading", name="Approval required", exact=True).wait_for(timeout=15_000)
    assert urlsplit(browser.page.url).path == path  # returned to the item the notification was about
    decide(browser.page, "approved")
    browser.page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(project_id, TERMINAL) == "completed"
    assert not legacy.exists()
    browser.assert_clean(dashboard.token)
    evidence.update(initial_state="a notification delivered with a dashboard link", failure_injected="none",
                    user_saw=["the sign-in screen", "after sign-in: the exact approval"], recovery="none needed",
                    final_state={"run": "completed"}, data_lost=False, action_duplicated=False)  # fmt: skip


# ------------------------------------------------------------------ failures


def test_frontend_down_notifications_still_go_out(dashboard, tmp_path, phone, evidence):
    dashboard.stop_frontend()
    dashboard.dispatcher(phone)
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    wait_until(lambda: delivered(phone), 120, "the notification with the frontend down")
    assert http(dashboard.url + "/", timeout=1)[0] is None
    approval_id = approval_of(dashboard, project_id)
    assert [n["state"] for n in notifications(dashboard, project_id, approval_id)] == ["sent"]
    evidence.update(initial_state="frontend server stopped", failure_injected="frontend server killed",
                    user_saw="the notification arrived (the link works again once the frontend is back)",
                    continued_running="API, PostgreSQL, worker, dispatcher", recovery="none needed",
                    final_state={"notification": "sent"}, data_lost=False, action_duplicated=False)  # fmt: skip


def test_api_down_already_durable_notifications_still_go_out(dashboard, tmp_path, phone, evidence):
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    approval_id = wait_until(lambda: dashboard.call("GET", f"/api/v1/approvals?status=pending&project_id={project_id}")[1]
                             .get("items"), 120, "the approval")[0]["id"]  # fmt: skip
    [row] = dashboard.store.notifications.list_for_approval(project_id, approval_id)
    assert row.status.value == "pending"  # durable before any dispatcher exists
    dashboard.stop_api()
    dashboard.dispatcher(phone)
    wait_until(lambda: delivered(phone), 60, "the notification with the API down")
    assert dashboard.store.notifications.list_for_approval(project_id, approval_id)[0].status.value == "sent"
    evidence.update(initial_state="an approval and its notification intent stored; no dispatcher yet",
                    failure_injected="API killed", user_saw="the notification arrived", continued_running="PostgreSQL, worker, dispatcher",
                    recovery="none needed", final_state={"notification": "sent"}, data_lost=False, action_duplicated=False)  # fmt: skip


def test_worker_down_after_the_attention_event_the_notification_still_goes_out(dashboard, tmp_path, phone, evidence):
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    approval_id = wait_until(lambda: dashboard.call("GET", f"/api/v1/approvals?status=pending&project_id={project_id}")[1]
                             .get("items"), 120, "the approval")[0]["id"]  # fmt: skip
    worker.kill()  # like a crash, right after it recorded the approval (and its intent)
    dashboard.dispatcher(phone)
    wait_until(lambda: delivered(phone), 60, "the notification with no worker")
    assert [n["state"] for n in notifications(dashboard, project_id, approval_id)] == ["sent"]
    evidence.update(initial_state="a worker paused a run for approval", failure_injected="the worker killed",
                    user_saw="the notification arrived", continued_running="API, PostgreSQL, dispatcher",
                    recovery="none needed for the notification; a worker resumes the run after the decision",
                    final_state={"notification": "sent", "run": dashboard.run_status(project_id)}, data_lost=False,
                    action_duplicated=False)  # fmt: skip


def test_dispatcher_down_the_outbox_waits_and_the_dashboard_shows_it(dashboard, browse, tmp_path, phone, evidence):
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    approval_id = wait_until(lambda: dashboard.call("GET", f"/api/v1/approvals?status=pending&project_id={project_id}")[1]
                             .get("items"), 120, "the approval")[0]["id"]  # fmt: skip
    browser = browse(DESKTOP)
    browser.sign_in(dashboard.token, path=f"/projects/{project_id}/approvals/{approval_id}")
    browser.page.get_by_test_id("notifications").get_by_text("Notification queued").wait_for(timeout=15_000)
    queued = " ".join(browser.page.get_by_test_id("notifications").inner_text().split())
    first = dashboard.dispatcher(phone, script=_script(tmp_path, ["hang_after_accept"]))
    wait_until(lambda: read_record(phone), 60, "the first send")
    first.kill()  # the provider accepted, the dispatcher died before recording it
    dashboard.dispatcher(phone)
    wait_until(lambda: notifications(dashboard, project_id, approval_id)[0]["state"] == "sent", 60, "the recovery")
    browser.page.get_by_test_id("notifications").get_by_text("possibly more than once").wait_for(timeout=15_000)
    shown = " ".join(browser.page.get_by_test_id("notifications").inner_text().split())
    [n] = notifications(dashboard, project_id, approval_id)
    assert n["outcome_unknown"] is True and len(delivered(phone)) == 2  # at least once: a duplicate, honestly shown
    browser.assert_clean(dashboard.token)
    evidence.update(initial_state="an approval waiting; no dispatcher", failure_injected="the dispatcher killed after the provider accepted",
                    user_saw=[queued, shown], continued_running="API, PostgreSQL, worker",
                    recovery="a second dispatcher recovered the expired lease and sent again",
                    final_state=n, data_lost=False, action_duplicated="the notification (never the approved action): delivered twice, shown as 'possibly more than once'")  # fmt: skip


def _script(tmp_path: Path, outcomes: list[str]) -> Path:
    path = tmp_path / f"script-{len(list(tmp_path.glob('script-*')))}.json"
    path.write_text(json.dumps({"script": outcomes}), encoding="utf-8")
    return path


@pytest.mark.docker_live
def test_postgresql_down_the_dispatcher_fails_closed_and_resumes(dashboard, tmp_path, phone, evidence):
    container = guarded_container(dashboard.config)
    dispatcher = dashboard.dispatcher(phone, retry_base=10, script=_script(tmp_path, ["transient"]))
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    wait_until(lambda: [e for e in dispatcher.events() if e["event"] == "notification_retry_scheduled"], 120, "a retry to be scheduled")
    before = len(read_record(phone))
    stopped = docker("stop", "-t", "10", container)
    try:
        assert stopped.returncode == 0, stopped.stderr
        wait_until(lambda: [e for e in dispatcher.events() if e["event"] == "database_unavailable"], 60, "the dispatcher to notice")
        assert dispatcher.alive and len(read_record(phone)) == before  # nothing is sent without the outbox
        outages = len([e for e in dispatcher.events() if e["event"] == "database_unavailable"])
    finally:
        started = docker("start", container)
        assert started.returncode == 0, started.stderr
    wait_until(lambda: database_reachable(dashboard.config), 120, "PostgreSQL to accept connections again", poll=0.5)
    wait_until(lambda: delivered(phone), 120, "the retry after PostgreSQL returned")
    from agent_factory.persistence.postgres import open_postgres_store

    fresh = open_postgres_store(dashboard.config)
    try:
        [request] = fresh.approvals.page_pending(limit=10, project_id=project_id)  # a fresh session: the old one died with the server
        [n] = fresh.notifications.list_for_approval(project_id, request.id)
        outcomes = [a.outcome.value for a in fresh.notifications.attempts(n.id)]
    finally:
        fresh.close()
    assert n.status.value == "sent" and outcomes == ["transient_failure", "accepted"]
    assert len(delivered(phone)) == 1 and dispatcher.alive
    evidence.update(initial_state="an approval whose first notification attempt failed transiently (retry in 10 s)",
                    failure_injected=f"PostgreSQL stopped (docker stop {container})",
                    user_saw="nothing until PostgreSQL returned; then the notification",
                    continued_running=f"the dispatcher, retrying the database ({outages} database_unavailable events), sending nothing",
                    recovery="PostgreSQL started; the dispatcher reconnected and sent the retry",
                    final_state={"notification": n.status.value, "attempts": outcomes}, data_lost=False, action_duplicated=False)  # fmt: skip

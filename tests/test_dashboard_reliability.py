"""Failure and recovery with the real dashboard: Chrome, the API, PostgreSQL, and workers (marker browser).

    set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\\test-database.url
    .venv\\Scripts\\python -m pytest -m browser --run-browser --run-postgres tests/test_dashboard_reliability.py -v
    rem the PostgreSQL scenario stops and starts this project's database container, so it also needs Docker:
    .venv\\Scripts\\python -m pytest -m browser --run-browser --run-postgres --run-docker-live tests/test_dashboard_reliability.py -v

Each scenario breaks something real while a person is using the dashboard: a process is killed like a
crash (the frontend server, the API, a worker), this project's PostgreSQL container is stopped, the
browser quits, or the browser's network misbehaves (a request refused, held until the dashboard gives up,
delayed, delivered twice, or answered by the API with the answer lost on the way back). No API answer is
ever fabricated: a shaped request reaches the real API, or nothing does.

Every scenario checks, and records for docs/dashboard-reliability.md (one JSON file per scenario in
DASHBOARD_EVIDENCE_DIR, when set): what was running, the failure, what the person saw, what kept
running, the recovery, the final authoritative state, whether anything was lost, and whether any action
ran twice.
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dashboard_support import (
    BLACKHOLE_PROXY,
    DESKTOP,
    MOBILE,
    RECORDED,
    TERMINAL,
    UNCERTAIN,
    Browsing,
    Hold,
    calculator_run,
    confirm_answer,
    confirm_decision,
    database_reachable,
    decide,
    docker,
    guarded_container,
    http,
    notice_run,
    open_attention,
    seed_approval,
    seed_project,
    start_answer,
    start_decision,
    wait_until,
)

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

UNCONFIRMED = "We couldn't confirm whether your approval reached Agent Factory."
NOT_RECORDED = "Your approval did not reach Agent Factory. Nothing was recorded. You can send it again."
ON_RECORD = "Your approval reached Agent Factory and is on record."
SENDING = "Sending your approval to Agent Factory…"
#: A calculator run whose deletion was approved once by the owner and performed once.
CALCULATOR_DONE = {"run": "completed", "decision": "approved by Dashboard Owner", "executions": [["succeeded", 1]],
                   "legacy_calc.py": "deleted"}  # fmt: skip
#: A notice run whose notice was approved once by the owner and sent once.
NOTICE_DONE = {"run": "completed", "decision": "approved by Dashboard Owner", "executions": [["succeeded", 1]], "notices_sent": 1}


@pytest.fixture
def evidence(request):
    """What the scenario observed; written to DASHBOARD_EVIDENCE_DIR (if set) for the reliability report."""
    record: dict = {"scenario": request.node.name}
    yield record
    out = os.environ.get("DASHBOARD_EVIDENCE_DIR")
    if out:
        directory = Path(out)
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
        (directory / f"{name}.json").write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def words(text: str) -> str:
    return " ".join(text.split())


def seen(page) -> dict:
    """What the person sees right now: the stale-data banner, the system status, the decision controls."""
    banner = page.get_by_test_id("stale-banner")
    status = page.locator("[data-health]")
    approve = page.get_by_role("button", name="Approve…")
    return {
        "banner": words(banner.inner_text()) if banner.count() else None,
        "system_status": words(status.inner_text()) if status.count() else None,
        "decisions": None if not approve.count() else ("disabled" if approve.is_disabled() else "enabled"),
    }


def calculator_outcome(store, project_id, approval_id, legacy) -> dict:
    decision = store.approvals.get_decision(project_id, approval_id)
    return {
        "run": store.runs.get(project_id, "run-1").status.value,
        "decision": None if decision is None else f"{decision.outcome.value} by {decision.actor.display_name}",
        "executions": [[e.state.value, e.attempts] for e in store.executions.list_for_run(project_id, "run-1")],
        "legacy_calc.py": "present" if legacy.exists() else "deleted",
    }


def notice_outcome(store, flow, approval_id) -> dict:
    decision = store.approvals.get_decision(flow.project_id, approval_id)
    return {
        "run": store.runs.get(flow.project_id, "run-1").status.value,
        "decision": None if decision is None else f"{decision.outcome.value} by {decision.actor.display_name}",
        "executions": [[e.state.value, e.attempts] for e in store.executions.list_for_run(flow.project_id, "run-1")],
        "notices_sent": flow.notices(),
    }


def paused_calculator(dashboard, browse, tmp_path, viewport=MOBILE):
    """A calculator run paused for approval by a real worker, the approval open in the dashboard."""
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    browser = browse(viewport)
    browser.sign_in(dashboard.token)
    approval_id = open_attention(browser.page, "Approval required")
    return project_id, legacy, worker, browser, approval_id


def paused_notice(dashboard, browse, tmp_path, kind="notice"):
    """The notice fixture's run paused for approval by a real worker loop, the approval open in the dashboard."""
    flow, worker = notice_run(dashboard, tmp_path, kind)
    browser = browse(MOBILE)
    browser.sign_in(dashboard.token)
    approval_id = open_attention(browser.page, "Approval required")
    return flow, worker, browser, approval_id


def next_poll(page, approval_id: str) -> None:
    """Wait until the page has just reloaded the approval from the API (it does every 5 s)."""
    with page.expect_response(lambda r: r.url.endswith(f"/approvals/{approval_id}") and r.request.method == "GET",
                              timeout=15_000):  # fmt: skip
        pass


# ------------------------------------------------------------------ 1. frontend server restart


def test_frontend_server_restart(dashboard, browse, tmp_path, evidence):
    project_id, legacy, worker, browser, approval_id = paused_calculator(dashboard, browse, tmp_path)
    page = browser.page
    evidence["initial_state"] = {"run": dashboard.run_status(project_id), "page": "the approval, open on a phone",
                                 "seen": seen(page)}  # fmt: skip

    dashboard.stop_frontend()
    evidence["failure_injected"] = "the frontend server (vite preview: the bundle and the /api proxy) was killed like a crash"
    page.get_by_test_id("stale-banner").wait_for(timeout=30_000)
    page.locator('[data-health="unreachable"]').wait_for(timeout=30_000)
    saw = seen(page)
    assert saw["banner"].startswith("Connection lost — showing data from ") and "Decisions are disabled" in saw["banner"]
    assert saw["decisions"] == "disabled" and "legacy_calc.py" in page.get_by_role("main").inner_text()  # last data kept
    evidence["user_saw"] = saw
    assert worker.alive and dashboard.api.alive and http(dashboard.api_url + "/readyz")[0] == 200
    assert dashboard.run_status(project_id) == "awaiting_approval"
    evidence["continued_running"] = "the API (ready), PostgreSQL, and the worker; the run kept waiting for its approval"

    dashboard.start_frontend()
    page.get_by_test_id("stale-banner").wait_for(state="detached", timeout=45_000)
    page.locator('[data-health="operational"]').wait_for(timeout=45_000)
    assert seen(page)["decisions"] == "enabled"  # the open page reconnected by itself
    page.reload()  # a full load from the new server process: this tab is still signed in
    page.get_by_role("button", name="Approve…").wait_for()
    decide(page, "approved")
    page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(project_id, TERMINAL) == "completed"
    evidence["recovery"] = ("frontend server started again on the same port; the open page reconnected without a "
                            "reload; after a reload the tab was still signed in; the person approved")  # fmt: skip
    final = calculator_outcome(dashboard.store, project_id, approval_id, legacy)
    assert final == CALCULATOR_DONE and len(browser.posted("/decision")) == 1
    evidence.update(final_state=final, data_lost=False, action_duplicated=False)
    browser.assert_clean(dashboard.token)


# ------------------------------------------------------------------ 2. browser closed


def test_browser_quits_and_reopens(dashboard, chrome, tmp_path, evidence):
    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    path = None

    def chrome_process():  # its own Chrome process, which quits completely when closed
        return chrome.browser_type.launch(channel="chrome", headless=True, proxy=BLACKHOLE_PROXY)

    first_chrome = chrome_process()
    first = Browsing(first_chrome, dashboard.url, MOBILE)
    first.sign_in(dashboard.token)
    approval_id = open_attention(first.page, "Approval required")
    path = f"/projects/{project_id}/approvals/{approval_id}"
    start_decision(first.page, "approved")
    evidence["initial_state"] = "calculator run waiting for approval; the confirmation step open in Chrome on a phone"
    first_chrome.close()  # quits before the person confirms
    evidence["failure_injected"] = "Chrome quit (twice: with the confirmation open, and right after a decision was recorded)"
    assert dashboard.approval(project_id, approval_id)["decision"] is None and first.posted("/decision") == []
    assert dashboard.run_status(project_id) == "awaiting_approval" and worker.alive

    second_chrome = chrome_process()
    try:
        second = Browsing(second_chrome, dashboard.url, MOBILE)
        second.open(path)
        second.page.get_by_role("heading", name="Sign in").wait_for()  # the tab's sign-in ended with the browser
        second.sign_in(dashboard.token, path=path)
        decide(second.page, "approved")
        second.page.get_by_text(RECORDED["approved"]).wait_for()
        second.assert_clean(dashboard.token)
    finally:
        second_chrome.close()  # quits at once; the worker carries on without any browser
    assert dashboard.wait_for_run(project_id, TERMINAL) == "completed"
    evidence["continued_running"] = "the API, PostgreSQL, and the worker, which finished the run with no browser open"

    third_chrome = chrome_process()
    try:
        third = Browsing(third_chrome, dashboard.url, DESKTOP)
        third.sign_in(dashboard.token, path=path)
        third.page.get_by_text("Action succeeded (attempt 1)").wait_for()
        third.page.get_by_text("Approved by Dashboard Owner").first.wait_for()
        evidence["user_saw"] = {
            "after_reopening": "the sign-in screen (the token lived only in the closed tab)",
            "after_signing_in": words(third.page.get_by_role("heading", level=1).inner_text()),
        }
        third.assert_clean(dashboard.token)
    finally:
        third_chrome.close()
    evidence["recovery"] = "reopened Chrome, signed in again; everything shown came from the API"
    final = calculator_outcome(dashboard.store, project_id, approval_id, legacy)
    assert final == CALCULATOR_DONE and len(second.posted("/decision")) == 1
    evidence.update(final_state=final, data_lost=False, action_duplicated=False)


# ------------------------------------------------------------------ 3. API restart


def test_api_restart(dashboard, browse, tmp_path, evidence):
    project_id, legacy, worker, browser, approval_id = paused_calculator(dashboard, browse, tmp_path)
    page = browser.page
    evidence["initial_state"] = {"run": dashboard.run_status(project_id), "seen": seen(page)}

    dashboard.stop_api()
    evidence["failure_injected"] = "the API process was killed like a crash"
    page.get_by_test_id("stale-banner").wait_for(timeout=30_000)
    page.locator('[data-health="unreachable"]').wait_for(timeout=30_000)
    saw = seen(page)
    assert saw["banner"].startswith("Connection lost — showing data from ") and saw["decisions"] == "disabled"
    assert saw["system_status"].endswith("API unavailable")
    evidence["user_saw"] = saw
    assert worker.alive and database_reachable(dashboard.config)
    assert dashboard.store.runs.get(project_id, "run-1").status.value == "awaiting_approval"  # straight from PostgreSQL
    evidence["continued_running"] = "PostgreSQL and the worker; the run kept waiting for its approval"

    dashboard.start_api()
    page.get_by_test_id("stale-banner").wait_for(state="detached", timeout=45_000)
    page.locator('[data-health="operational"]').wait_for(timeout=45_000)
    assert seen(page)["decisions"] == "enabled"
    decide(page, "approved")
    page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(project_id, TERMINAL) == "completed"
    evidence["recovery"] = "API started again on the same port; the page reconnected by itself; the person approved"
    final = calculator_outcome(dashboard.store, project_id, approval_id, legacy)
    assert final == CALCULATOR_DONE and len(browser.posted("/decision")) == 1
    evidence.update(final_state=final, data_lost=False, action_duplicated=False)
    browser.assert_clean(dashboard.token)


# ------------------------------------------------------------------ 4. PostgreSQL outage (Docker)


@pytest.mark.docker_live
def test_database_outage(dashboard, browse, tmp_path, evidence):
    from agent_factory.persistence.postgres import open_postgres_store

    container = guarded_container(dashboard.config)  # this project's container, serving the test database
    project_id, legacy, worker, browser, approval_id = paused_calculator(dashboard, browse, tmp_path)
    page = browser.page
    evidence["initial_state"] = {"run": dashboard.run_status(project_id), "seen": seen(page)}

    stopped = docker("stop", "-t", "10", container)
    try:
        assert stopped.returncode == 0, stopped.stderr
        evidence["failure_injected"] = f"PostgreSQL stopped (docker stop {container})"
        assert not database_reachable(dashboard.config)
        page.locator('[data-health="database_unavailable"]').wait_for(timeout=30_000)
        page.get_by_test_id("stale-banner").wait_for(timeout=30_000)
        saw = seen(page)
        assert saw["banner"].startswith("Agent Factory is temporarily unavailable — showing data from ")
        assert saw["decisions"] == "disabled" and saw["system_status"].endswith("Database unavailable")
        assert http(dashboard.api_url + "/readyz") == (503, {"status": "not_ready", "reason": "database_unavailable"})
        evidence["user_saw"] = saw
        wait_until(lambda: any(e["event"] == "database_unavailable" for e in worker.events()), 30, "the worker to notice")
        assert dashboard.api.alive and worker.alive  # both wait for the database instead of failing
        evidence["continued_running"] = "the API (answering 503 and not ready) and the worker (retrying with backoff)"
    finally:
        started = docker("start", container)
        assert started.returncode == 0, started.stderr
    wait_until(lambda: database_reachable(dashboard.config), 120, "PostgreSQL to accept connections again", poll=0.5)
    page.get_by_test_id("stale-banner").wait_for(state="detached", timeout=60_000)
    page.locator('[data-health="operational"]').wait_for(timeout=60_000)
    decide(page, "approved")
    page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(project_id, TERMINAL, timeout=180) == "completed"
    evidence["recovery"] = "PostgreSQL started again; the API, the worker, and the page reconnected by themselves; the person approved"
    fresh = open_postgres_store(dashboard.config)  # the test's own session did not survive the restart
    try:
        final = calculator_outcome(fresh, project_id, approval_id, legacy)
    finally:
        fresh.close()
    assert final == CALCULATOR_DONE and len(browser.posted("/decision")) == 1
    outages = sum(1 for e in worker.events() if e["event"] == "database_unavailable")
    evidence.update(final_state=final, data_lost=False, action_duplicated=False, worker_database_retries=outages)
    browser.assert_clean(dashboard.token)


# ------------------------------------------------------------------ 5. worker death and takeover


def test_worker_killed_after_sending_and_another_takes_over(dashboard, browse, tmp_path, evidence):
    lease = ("--lease", "3", "--heartbeat", "1")
    flow, first = notice_run(dashboard, tmp_path, "notice", *lease, "--hold-after-effect")
    browser = browse(MOBILE)
    browser.sign_in(dashboard.token)
    page = browser.page
    approval_id = open_attention(page, "Approval required")
    decide(page, "approved")
    page.get_by_text(RECORDED["approved"]).wait_for()
    wait_until(lambda: (flow.oracle / "holding").is_file(), 60, "worker 1 to send the notice")
    assert flow.notices() == 1
    page.get_by_text("Action sent (attempt 1)").wait_for(timeout=15_000)
    assert "Being sent" in page.get_by_role("main").inner_text()
    first_id = next(e["worker_id"] for e in first.events() if e["event"] == "fixture_worker_ready")
    evidence["initial_state"] = "notice approved in the dashboard; worker 1 sent it and had not yet recorded the outcome"

    first.kill()
    evidence["failure_injected"] = "worker 1 killed like a crash after the notice left, before its outcome was recorded"
    second = dashboard.fixture_worker(flow.root, "notice", *lease)
    page.get_by_role("heading", name="Reconciliation required", exact=True).wait_for(timeout=60_000)
    shown = page.get_by_role("main").inner_text()
    assert UNCERTAIN in shown and "Outcome unknown (attempt 1)" in shown
    assert flow.notices() == 1  # the new owner did not send it again: it cannot know whether the first one arrived
    [taken] = [e for e in second.events() if e["event"] == "run_taken_over"]
    assert taken["previous_worker_id"] == first_id
    evidence["user_saw"] = ["Action sent (attempt 1) / Being sent, while worker 1 held the run",
                            "after the takeover: Reconciliation required — " + UNCERTAIN]  # fmt: skip
    evidence["continued_running"] = f"the API, PostgreSQL, and the dashboard; worker 2 took the run over (generation {taken['fencing_token']})"

    start_answer(page, "it_happened")  # the person checked the receiving system: the notice did arrive
    confirm_answer(page, "it_happened")
    page.get_by_text(RECORDED["answer"]).wait_for()
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    page.get_by_text("Resolved: succeeded").wait_for(timeout=15_000)
    timeline = page.locator(".timeline").inner_text()
    assert "(generation" in timeline
    evidence["recovery"] = "worker 2 took over after the lease expired; the person answered 'It happened'; worker 2 finished the run"
    final = notice_outcome(dashboard.store, flow, approval_id)
    assert final == NOTICE_DONE
    evidence.update(final_state=final, data_lost=False, action_duplicated=False)
    browser.assert_clean(dashboard.token)


# ------------------------------------------------------------------ 6. network-like interruptions


def test_network_refuses_the_decision(dashboard, browse, tmp_path, evidence):
    flow, worker, browser, approval_id = paused_notice(dashboard, browse, tmp_path)
    page = browser.page
    refused = []

    def refuse_first(route):
        if route.request.method == "POST" and not refused:
            refused.append(route.request.url)
            route.abort("connectionrefused")  # never reaches the API
        else:
            route.fallback()

    page.route("**/decision", refuse_first)
    decide(page, "approved")
    page.get_by_text(NOT_RECORDED).wait_for()
    assert dashboard.approval(flow.project_id, approval_id)["decision"] is None and flow.notices() == 0
    evidence.update(initial_state="notice run waiting for approval", failure_injected="the decision's connection refused",
                    user_saw=NOT_RECORDED)  # fmt: skip
    page.get_by_role("button", name="Send the same decision again").click()
    page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    bodies = browser.posted("/decision")
    assert len(bodies) == 2 and bodies[0] == bodies[1]  # the person sent the identical decision again
    final = notice_outcome(dashboard.store, flow, approval_id)
    assert final == NOTICE_DONE
    evidence.update(recovery="the person sent the same decision again", final_state=final, data_lost=False,
                    action_duplicated=False)  # fmt: skip
    browser.assert_clean(dashboard.token)


def test_the_decision_times_out_before_reaching_the_api(dashboard, browse, tmp_path, evidence):
    flow, worker, browser, approval_id = paused_notice(dashboard, browse, tmp_path)
    page = browser.page
    Hold(page, "**/decision", method="POST", count=1)  # the first never leaves; the dashboard gives up after 10 s
    decide(page, "approved")
    page.get_by_text(SENDING).wait_for()
    page.get_by_text(NOT_RECORDED).wait_for(timeout=30_000)
    assert dashboard.approval(flow.project_id, approval_id)["decision"] is None and flow.notices() == 0
    evidence.update(initial_state="notice run waiting for approval",
                    failure_injected="the decision held on the network until the dashboard's 10 s timeout",
                    user_saw=[SENDING, NOT_RECORDED])  # fmt: skip
    page.get_by_role("button", name="Send the same decision again").click()
    page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    final = notice_outcome(dashboard.store, flow, approval_id)
    assert final == NOTICE_DONE
    evidence.update(recovery="the person sent the same decision again", final_state=final, data_lost=False,
                    action_duplicated=False)  # fmt: skip
    browser.assert_clean(dashboard.token)


def test_a_slow_network_delays_the_decision_and_more_taps_send_nothing(dashboard, browse, tmp_path, evidence):
    flow, worker, browser, approval_id = paused_notice(dashboard, browse, tmp_path)
    page = browser.page
    hold = Hold(page, "**/decision", method="POST")
    decide(page, "approved")
    page.get_by_text(SENDING).wait_for()
    confirm = page.get_by_role("button", name="Yes, approve this action")
    assert confirm.is_disabled()
    confirm.click(force=True)  # impatient taps while the first request is in flight
    confirm.dblclick(force=True)
    page.wait_for_timeout(3_000)
    assert len(hold.held) == 1 and len(browser.posted("/decision")) == 1
    assert dashboard.approval(flow.project_id, approval_id)["decision"] is None  # it has not arrived yet
    evidence.update(initial_state="notice run waiting for approval",
                    failure_injected="the decision delayed 3 s on the network; the person tapped again meanwhile",
                    user_saw=[SENDING, "the confirm button disabled while sending"])  # fmt: skip
    hold.release()
    page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    final = notice_outcome(dashboard.store, flow, approval_id)
    assert final == NOTICE_DONE and len(browser.posted("/decision")) == 1
    evidence.update(recovery="the delayed request arrived", final_state=final, data_lost=False, action_duplicated=False)
    browser.assert_clean(dashboard.token)


def test_the_api_records_the_decision_but_its_answer_is_lost(dashboard, browse, tmp_path, evidence):
    flow, worker, browser, approval_id = paused_notice(dashboard, browse, tmp_path)
    page = browser.page
    answered = []

    def lose_the_answer(route):
        if route.request.method == "POST":
            answered.append(route.fetch().status)  # the real API receives it and records the decision...
            route.abort("connectionreset")  # ...and its answer never reaches the browser
        else:
            route.fallback()

    page.route("**/decision", lose_the_answer)
    decide(page, "approved")
    page.get_by_text(ON_RECORD).wait_for()
    assert answered == [201]
    assert page.get_by_role("button", name="Send the same decision again").count() == 0  # nothing to resend
    evidence.update(initial_state="notice run waiting for approval",
                    failure_injected="the API recorded the decision (201); the connection dropped before the answer arrived",
                    user_saw=ON_RECORD)  # fmt: skip
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    final = notice_outcome(dashboard.store, flow, approval_id)
    assert final == NOTICE_DONE and len(browser.posted("/decision")) == 1
    evidence.update(recovery="none needed: the dashboard asked the API and found the decision on record",
                    final_state=final, data_lost=False, action_duplicated=False)  # fmt: skip
    browser.assert_clean(dashboard.token)


def test_the_network_delivers_the_decision_twice(dashboard, browse, tmp_path, evidence):
    flow, worker, browser, approval_id = paused_notice(dashboard, browse, tmp_path)
    page = browser.page
    statuses = []

    def deliver_twice(route):
        if route.request.method == "POST":
            first = route.fetch()
            second = route.fetch()  # the identical request arrives again
            statuses.extend([first.status, second.status])
            route.fulfill(response=second)
        else:
            route.fallback()

    page.route("**/decision", deliver_twice)
    decide(page, "approved")
    page.get_by_text("Agent Factory recorded your approval. It was already on record.").wait_for()
    assert statuses == [201, 200]  # recorded once; the repeat was a no-op
    evidence.update(initial_state="notice run waiting for approval",
                    failure_injected="the decision delivered to the API twice", user_saw=RECORDED["approved"] + " It was already on record.")  # fmt: skip
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    final = notice_outcome(dashboard.store, flow, approval_id)
    assert final == NOTICE_DONE
    evidence.update(recovery="none needed", final_state=final, data_lost=False, action_duplicated=False)
    browser.assert_clean(dashboard.token)


def test_a_retry_answer_delivered_twice_with_its_answer_lost_authorizes_one_retry(dashboard, browse, tmp_path, evidence):
    flow, worker, browser, approval_id = paused_notice(dashboard, browse, tmp_path, kind="unknown-notice")
    page = browser.page
    decide(page, "approved")
    page.get_by_text(RECORDED["approved"]).wait_for()
    page.get_by_role("heading", name="Reconciliation required", exact=True).wait_for(timeout=60_000)
    assert flow.notices() == 1
    statuses = []

    def twice_then_lost(route):
        if route.request.method == "POST":
            statuses.append(route.fetch().status)
            statuses.append(route.fetch().status)  # delivered twice...
            route.abort("connectionreset")  # ...and the answer lost
        else:
            route.fallback()

    page.route("**/reconciliation", twice_then_lost)
    start_answer(page, "did_not_happen_retry")
    confirm_answer(page, "did_not_happen_retry")
    page.get_by_text("Your answer reached Agent Factory and is on record.").wait_for()
    assert statuses == [201, 200]
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    execution = flow.execution()
    assert (execution.state.value, execution.attempts) == ("succeeded", 2)
    assert flow.notices() == 2  # the first attempt and exactly one authorized retry
    assert len(dashboard.store.executions.reconciliations(flow.project_id, "run-1", execution.id)) == 1
    evidence.update(
        initial_state="an approved notice whose first attempt has an unknown outcome",
        failure_injected="the 'authorize one retry' answer delivered twice, and its answer lost",
        user_saw="Your answer reached Agent Factory and is on record.", recovery="none needed",
        final_state={"run": "completed", "execution": ["succeeded", 2], "notices_sent": 2, "answers_applied": 1},
        data_lost=False, action_duplicated=False,
    )  # fmt: skip
    browser.assert_clean(dashboard.token)


# ------------------------------------------------------------------ 7. a decision sent as the API goes down


def test_a_decision_sent_as_the_api_goes_down(dashboard, browse, tmp_path, evidence):
    project_id, legacy, worker, browser, approval_id = paused_calculator(dashboard, browse, tmp_path)
    page = browser.page
    start_decision(page, "approved")
    next_poll(page, approval_id)  # the page has just confirmed the approval is current...
    dashboard.stop_api()  # ...and the API crashes before the person's tap
    confirm_decision(page, "approved")
    page.get_by_text(UNCONFIRMED).wait_for(timeout=15_000)
    page.get_by_test_id("stale-banner").wait_for(timeout=30_000)
    assert page.get_by_role("button", name="Send the same decision again").count() == 0  # nothing resent, or resendable
    saw = [words(page.get_by_test_id("submission").inner_text()), seen(page)["banner"]]
    evidence.update(initial_state="calculator run waiting for approval; the confirmation open on a phone",
                    failure_injected="the API killed after the page's last refresh, just before the person confirmed",
                    user_saw=saw)  # fmt: skip
    assert worker.alive and dashboard.store.runs.get(project_id, "run-1").status.value == "awaiting_approval"
    assert dashboard.store.approvals.get_decision(project_id, approval_id) is None
    evidence["continued_running"] = "PostgreSQL and the worker; the run kept waiting"

    dashboard.start_api()
    page.get_by_text(NOT_RECORDED).wait_for(timeout=45_000)  # settled from the API's state once it answered
    page.get_by_role("button", name="Send the same decision again").click()
    page.get_by_text(RECORDED["approved"]).wait_for()
    assert dashboard.wait_for_run(project_id, TERMINAL) == "completed"
    bodies = browser.posted("/decision")
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    final = calculator_outcome(dashboard.store, project_id, approval_id, legacy)
    assert final == CALCULATOR_DONE
    evidence.update(recovery="API restarted; the dashboard found nothing on record and offered the same decision again",
                    final_state=final, data_lost=False, action_duplicated=False)  # fmt: skip
    browser.assert_clean(dashboard.token)


# ------------------------------------------------------------------ 8. stale approvals


def test_a_decision_already_made_elsewhere_is_refused_and_shown(dashboard, browse, tmp_path, evidence):
    from agent_factory.domain.principals import Permission

    flow, worker = notice_run(dashboard, tmp_path, "notice")
    permissions = {Permission.APPROVALS_READ, Permission.APPROVALS_DECIDE, Permission.PROJECTS_READ, Permission.RUNS_READ}
    reviewer = dashboard.person("second-reviewer", permissions, "Second Reviewer")
    phone, desk = browse(MOBILE), browse(DESKTOP)
    phone.sign_in(dashboard.token)
    approval_id = open_attention(phone.page, "Approval required")
    desk.sign_in(reviewer, path=f"/projects/{flow.project_id}/approvals/{approval_id}")
    start_decision(desk.page, "rejected")  # the second reviewer is about to reject...
    next_poll(desk.page, approval_id)
    hold = Hold(desk.page, f"**/approvals/{approval_id}")  # ...and their screen gets no newer data for now
    decide(phone.page, "approved")  # meanwhile the owner approves on the phone
    phone.page.get_by_text(RECORDED["approved"]).wait_for()
    confirm_decision(desk.page, "rejected")  # the rejection arrives second
    desk.page.get_by_text("This approval was already decided. Refreshing to show the decision on record.").wait_for()
    notice = words(desk.page.get_by_test_id("submission").inner_text())
    assert "Nothing was recorded by this attempt." in notice and "Support details" in notice
    hold.release()
    desk.page.get_by_role("heading", name="Approved", exact=True).wait_for(timeout=15_000)
    assert "Approved by Dashboard Owner" in desk.page.get_by_role("main").inner_text()
    assert dashboard.wait_for_run(flow.project_id, TERMINAL) == "completed"
    final = notice_outcome(dashboard.store, flow, approval_id)
    assert final == NOTICE_DONE
    assert [b["outcome"] for b in desk.posted("/decision")] == ["rejected"]  # sent once, refused (409)
    evidence.update(
        initial_state="one approval open in two browsers (owner on a phone, a second reviewer on a desktop)",
        failure_injected="the reviewer's screen was out of date: the owner approved first, then the reviewer rejected",
        user_saw=notice, continued_running="everything", recovery="the reviewer's page refreshed to the decision on record",
        final_state=final, data_lost=False, action_duplicated=False,
    )  # fmt: skip
    phone.assert_clean(dashboard.token)
    desk.assert_clean(reviewer)


def test_an_approval_that_expires_while_open_cannot_be_approved(dashboard, browse, evidence):
    project = seed_project(dashboard.store, "Expiring calculator")
    request = seed_approval(dashboard.store, project, requested_ago=60, expires_in=60 + 30)  # expires 30 s from now
    expires = datetime.fromisoformat(request.expires_at.replace("Z", "+00:00"))
    browser = browse(MOBILE)
    browser.sign_in(dashboard.token, path=f"/projects/{project.id}/approvals/{request.id}")
    page = browser.page
    start_decision(page, "approved")
    while True:  # a fresh view of the approval with at most ~10 s left (it is reloaded every 5 s)
        next_poll(page, request.id)
        remaining = (expires - datetime.now(UTC)).total_seconds()
        assert remaining > 1, "the approval expired before the scenario could hold the page's view"
        if remaining <= 10:
            break
    hold = Hold(page, f"**/approvals/{request.id}")  # from now on the page sees nothing newer
    page.wait_for_timeout((remaining + 0.5) * 1000)  # the approval expires while the person reads it
    confirm_decision(page, "approved")
    page.get_by_text("This approval expired before your decision reached Agent Factory. Refreshing.").wait_for()
    notice = words(page.get_by_test_id("submission").inner_text())
    assert "Nothing was recorded by this attempt." in notice
    hold.release()
    page.get_by_role("heading", name="Expired", exact=True).wait_for(timeout=15_000)
    assert "Expired before anyone decided. The workflow continues without this action." in page.get_by_role("main").inner_text()
    view = dashboard.approval(project.id, request.id)
    assert view["status"] == "expired" and view["decision"] is None and view["attention"] is None
    assert dashboard.store.approvals.get_request(project.id, request.id).status.value == "expired"  # recorded by the API
    evidence.update(
        initial_state="an approval open on a phone, 30 s before it expires",
        failure_injected="the page's view went stale (no newer data) and the approval expired before the person confirmed",
        user_saw=notice, recovery="the page refreshed to 'Expired'; no decision can be made",
        final_state={"approval": "expired", "decision": None}, data_lost=False, action_duplicated=False,
    )  # fmt: skip
    browser.assert_clean(dashboard.token)

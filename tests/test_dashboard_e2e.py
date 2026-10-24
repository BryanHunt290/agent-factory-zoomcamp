"""Complete workflows in the dashboard: a person in Chrome, the real API, PostgreSQL, and real workers.

    set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\\test-database.url
    .venv\\Scripts\\python -m pytest -m browser --run-browser --run-postgres tests/test_dashboard_e2e.py

Nothing is mocked. Calculator runs are created as an operator creates them (the project through the API,
then ``worker enqueue``) and executed by ``python -m agent_factory worker run``. The uncertain-action flow
runs the action fixture's notice gateway in tests/dashboard_worker.py: a real ``Worker`` loop whose only
fake is the external system, a file that counts the notices actually sent. The person signs in with a
development token and decides in the browser. Outcomes are checked against authoritative state only:
the API, the database, the workspace on disk, and the notice counter.
"""

import re

import pytest
from dashboard_support import (
    CHOICE_LABEL,
    DESKTOP,
    MOBILE,
    RECORDED,
    TOKEN_KEY_HINT,
    UNCERTAIN,
    calculator_run,
    confirm_answer,
    decide,
    notice_run,
    open_attention,
    seed_approval,
    seed_project,
    start_answer,
)

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


@pytest.mark.parametrize(
    ("outcome", "viewport"), [("approved", MOBILE), ("rejected", DESKTOP)], ids=["approve-on-a-phone", "reject-on-a-desktop"]
)
def test_a_decision_in_the_dashboard_is_consumed_by_the_real_worker(dashboard, browse, tmp_path, outcome, viewport):
    from agent_factory.domain.identity import ActorKind

    project_id, legacy, worker = calculator_run(dashboard, tmp_path)
    browser = browse(viewport)
    browser.sign_in(dashboard.token)
    page = browser.page
    approval_id = open_attention(page, "Approval required")  # the worker planned, developed, and paused
    before = dashboard.approval(project_id, approval_id)
    assert before["attention"] == "decision_required" and before["action"]["params"] == {"path": "legacy_calc.py"}
    shown = page.get_by_role("main").inner_text()
    for fact in ("What will happen", "legacy_calc.py", "Why it is requested", before["reason"], "Risk", before["risk"],
                 "Overnight calculator", "run-1", "developer", "Expires"):  # fmt: skip
        assert fact in shown, fact
    page.get_by_text("Technical details").click()
    assert before["action_fingerprint"] in page.get_by_role("main").inner_text()  # secondary, but there

    comment = f"{outcome.capitalize()} from the dashboard test."
    decide(page, outcome, comment)
    page.get_by_text(RECORDED[outcome]).wait_for()
    assert dashboard.wait_for_run(project_id, {"completed", "failed", "blocked"}) == "completed"
    page.get_by_text("Completed").first.wait_for(timeout=15_000)  # the page follows the run by itself

    # One tap sent one request, carrying the fingerprint exactly as the API supplied it.
    assert browser.posted("/decision") == [{"outcome": outcome, "action_fingerprint": before["action_fingerprint"], "comment": comment}]
    store = dashboard.store
    decision = store.approvals.get_decision(project_id, approval_id)
    assert (decision.outcome.value, decision.comment) == (outcome, comment)
    assert (decision.actor.kind, decision.actor.id, decision.actor.verified) == (ActorKind.USER, dashboard.owner.id, True)
    executions = store.executions.list_for_run(project_id, "run-1")
    if outcome == "approved":
        [execution] = executions
        assert (execution.state.value, execution.attempts) == ("succeeded", 1)
        assert not legacy.exists()  # the approved action ran, once
        page.get_by_text("Action succeeded (attempt 1)").wait_for(timeout=15_000)
    else:
        assert executions == [] and legacy.is_file()  # nothing was attempted
        assert "Nothing has been attempted." in page.get_by_role("main").inner_text()
    released = [e["reason"] for e in worker.events() if e["event"] == "run_released"]
    assert released[-1] == "finished", released
    view = dashboard.approval(project_id, approval_id)
    assert view["attention"] is None and view["decision"]["decided_by"]["id"] == dashboard.owner.id
    browser.assert_clean(dashboard.token)


CHOICES = {  # choice -> (button, notices sent in total, final execution state, the timeline's last line)
    "it_happened": ("Confirm answer", 1, "succeeded", "Resolved: succeeded"),
    "did_not_happen": ("Confirm answer", 1, "failed", "Resolved: did not take effect"),
    "did_not_happen_retry": ("Authorize one retry", 2, "succeeded", "Resolved: succeeded"),
}


@pytest.mark.parametrize("choice", list(CHOICES))
def test_an_uncertain_action_is_reconciled_in_the_dashboard(dashboard, browse, tmp_path, choice):
    button, notices, final_state, resolved = CHOICES[choice]
    flow, worker = notice_run(dashboard, tmp_path, "unknown-notice")
    browser = browse(MOBILE)
    browser.sign_in(dashboard.token)
    page = browser.page
    approval_id = open_attention(page, "Approval required")
    decide(page, "approved")
    page.get_by_text(RECORDED["approved"]).wait_for()

    # The worker sends the notice; the gateway accepts it and then times out: nobody can tell whether it arrived.
    page.get_by_role("heading", name="Reconciliation required", exact=True).wait_for(timeout=60_000)
    assert flow.notices() == 1
    main = page.get_by_role("main")
    assert UNCERTAIN in main.inner_text() and "Outcome unknown (attempt 1)" in main.inner_text()
    for label in CHOICE_LABEL.values():
        assert not page.get_by_label(label).is_checked()  # no answer is preselected; retry is never the default
    assert page.get_by_role("button", name="Review answer…").is_disabled()
    if choice == "did_not_happen_retry":  # a new authorization needs its own acknowledgement
        page.get_by_label(CHOICE_LABEL[choice]).check()
        page.get_by_role("button", name="Review answer…").click()
        assert "or it lapses and nothing is sent" in main.inner_text()  # the review restates the 15-minute window
        assert page.get_by_role("button", name=button).is_disabled()
        page.get_by_role("button", name="Change answer").click()
    start_answer(page, choice)
    confirm_answer(page, choice)
    page.get_by_text(RECORDED["answer"]).wait_for()

    assert dashboard.wait_for_run(flow.project_id, {"completed", "failed", "blocked"}) == "completed"
    page.get_by_text(resolved).wait_for(timeout=15_000)
    assert flow.notices() == notices  # "it happened" and "don't retry" send nothing more; a retry sends one more
    execution = flow.execution()
    assert execution.state.value == final_state
    [applied] = dashboard.store.executions.reconciliations(flow.project_id, "run-1", execution.id)
    assert (applied.actor.id, applied.choice.value) == (dashboard.owner.id, choice)
    [body] = browser.posted("/reconciliation")
    assert (body["choice"], body["attempt"], body["execution_id"]) == (choice, 1, execution.id)
    assert body["action_fingerprint"] == dashboard.approval(flow.project_id, approval_id)["action_fingerprint"]
    assert [e["reason"] for e in worker.events() if e["event"] == "run_released"][-1] == "finished"
    browser.assert_clean(dashboard.token)


def test_every_browser_shows_the_servers_state_and_the_browser_holds_none(dashboard, browse):
    """A second browser sees the first one's decision without reloading; clearing a browser's storage or
    refreshing changes nothing on the server; nothing about the workflow is stored in the browser."""
    from agent_factory.domain.principals import Permission

    project = seed_project(dashboard.store, "Shared calculator")
    request = seed_approval(dashboard.store, project)
    path = f"/projects/{project.id}/approvals/{request.id}"
    reviewer = dashboard.person("second-reviewer", {Permission.APPROVALS_READ, Permission.APPROVALS_DECIDE,
                                                    Permission.PROJECTS_READ, Permission.RUNS_READ}, "Second Reviewer")  # fmt: skip
    phone, desk = browse(MOBILE), browse(DESKTOP)
    phone.sign_in(dashboard.token, path=path)
    desk.sign_in(reviewer, path=path)
    desk.page.get_by_role("button", name="Approve…").wait_for()

    decide(phone.page, "approved", "Approved on the phone.")
    phone.page.get_by_text(RECORDED["approved"]).wait_for()
    # The other browser follows by itself (it polls), and no longer offers a decision.
    desk.page.get_by_role("heading", name="Approved", exact=True).wait_for(timeout=15_000)
    assert "Approved by Dashboard Owner (signed in)" in desk.page.get_by_role("main").inner_text()
    assert desk.page.get_by_role("button", name="Approve…").count() == 0

    for browser, token in ((phone, dashboard.token), (desk, reviewer)):
        stored = browser.page.evaluate(
            """async () => ({
                session: {...sessionStorage}, local: {...localStorage},
                databases: indexedDB.databases ? (await indexedDB.databases()).map((d) => d.name) : [],
                caches: self.caches ? await caches.keys() : [],
            })"""
        )
        assert stored == {"session": {TOKEN_KEY_HINT: token}, "local": {}, "databases": [], "caches": []}
        assert browser.context.cookies() == []

    phone.page.evaluate("sessionStorage.clear()")  # the person clears this tab's storage
    phone.page.reload()
    phone.page.get_by_role("heading", name="Sign in").wait_for()
    desk.page.reload()  # an ordinary refresh elsewhere
    desk.page.get_by_role("heading", name="Approved", exact=True).wait_for()
    view = dashboard.approval(project.id, request.id)
    assert view["status"] == "approved" and view["decision"]["decided_by"]["id"] == dashboard.owner.id  # unchanged

    phone.sign_in(dashboard.token, path=path)  # signing in again shows the same, server-held state
    phone.page.get_by_role("heading", name="Approved", exact=True).wait_for()
    assert "Approved on the phone." in phone.page.get_by_role("main").inner_text()
    assert len(desk.posted("/decision")) == 0 and len(phone.posted("/decision")) == 1
    phone.assert_clean(dashboard.token)
    desk.assert_clean(reviewer)


def test_the_attention_inbox_tells_approvals_and_reconciliations_apart(dashboard, browse):
    from dashboard_support import seed_uncertain

    from agent_factory.state import RunStatus

    waiting = seed_project(dashboard.store, "Waiting calculator")
    seed_approval(dashboard.store, waiting)
    uncertain = seed_project(dashboard.store, "Notice sender", status=RunStatus.AWAITING_RECONCILIATION)
    notice = seed_approval(dashboard.store, uncertain, path="notices/billing.txt")
    seed_uncertain(dashboard.store, uncertain, notice, dashboard.owner.actor())
    browser = browse(MOBILE)
    browser.sign_in(dashboard.token, path="/attention")
    page = browser.page
    approval = page.get_by_role("link", name=re.compile("Approval required"))
    reconciliation = page.get_by_role("link", name=re.compile("Reconciliation required"))
    approval.wait_for()
    assert "Waiting calculator" in approval.inner_text() and "Notice sender" in reconciliation.inner_text()
    assert "1 approval required, 1 reconciliation required." in page.get_by_role("main").inner_text()
    nav = page.get_by_role("navigation", name="Main")
    assert re.search(r"Needs attention\s*2", nav.inner_text())
    reconciliation.click()
    page.get_by_role("heading", name="Reconciliation required", exact=True).wait_for()
    assert UNCERTAIN in page.get_by_role("main").inner_text()
    browser.assert_clean(dashboard.token)

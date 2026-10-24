"""The dashboard in a real browser, against the real API (marker browser).

    set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\\test-database.url
    .venv\\Scripts\\python -m pytest -m browser --run-browser --run-postgres tests/test_dashboard_browser.py

Each test gets the real API (`api serve`, development authentication, a fresh PostgreSQL test schema) and
the real frontend server (`vite preview` of the production bundle), and drives the installed Google
Chrome at a phone (375 x 812) or desktop viewport. Data is seeded straight into the test schema. The
complete workflows with real workers are in test_dashboard_e2e.py; failure and recovery in
test_dashboard_reliability.py.
"""

import re
import urllib.request
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from dashboard_support import (
    DESKTOP,
    MOBILE,
    TOKEN_KEY_HINT,
    at,
    axe_violations,
    overflow,
    seed_approval,
    seed_milestone,
    seed_project,
    seed_uncertain,
)

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


# ------------------------------------------------------------------ sign-in (LOCAL DEVELOPMENT)


def test_sign_in_exercises_the_development_path_and_keeps_the_token_in_this_tab_only(dashboard, browse):
    seed_project(dashboard.store, "Calculator")
    browser = browse(MOBILE)
    auth_headers = []
    browser.page.on(
        "request",
        lambda request: auth_headers.append(request.headers.get("authorization", "")) if "/api/" in request.url else None,
    )
    browser.sign_in(dashboard.token)
    browser.page.get_by_text("Calculator").first.wait_for()
    assert "LOCAL DEVELOPMENT" in browser.text()
    storage = browser.page.evaluate("({ session: {...sessionStorage}, local: {...localStorage} })")
    assert storage["session"] == {TOKEN_KEY_HINT: dashboard.token} and storage["local"] == {}
    assert browser.context.cookies() == []
    assert dashboard.token not in browser.page.content()
    assert auth_headers and all(header == f"Bearer {dashboard.token}" for header in auth_headers)
    browser.page.reload()  # an ordinary refresh in the same tab keeps the sign-in
    browser.page.get_by_role("navigation", name="Main").wait_for()
    other = browse(MOBILE)  # another tab or browser: no token
    other.open("/")
    other.page.get_by_role("heading", name="Sign in").wait_for()
    browser.assert_clean(dashboard.token)
    other.assert_clean(dashboard.token)


def test_a_token_the_api_refuses_never_signs_in(dashboard, browse):
    stranger = dashboard.mint("not-enrolled", "Stranger")
    browser = browse(DESKTOP)
    browser.open("/")
    browser.page.get_by_label("Development access token").fill(stranger)
    browser.page.get_by_role("button", name="Sign in").click()
    browser.page.get_by_text("That token was not accepted").wait_for()
    assert browser.page.evaluate("sessionStorage.length") == 0
    browser.assert_clean(stranger)


def test_an_expired_session_returns_to_sign_in(dashboard, browse):
    short = dashboard.mint("dashboard-owner", "Dashboard Owner", lifetime=timedelta(seconds=1))
    browser = browse(MOBILE)
    browser.sign_in(short)
    # The API accepts a token for 30 s past its expiry (clock-skew leeway, api/auth/tokens.py); the first
    # poll after that is refused, and the dashboard forgets the token.
    browser.page.get_by_role("heading", name="Sign in").wait_for(timeout=60_000)
    assert "Your session is no longer valid." in browser.text()
    assert browser.page.evaluate("sessionStorage.length") == 0
    browser.assert_clean(short)


def test_disabling_the_person_ends_their_session_at_the_next_poll(dashboard, browse):
    from agent_factory.domain.principals import PrincipalStatus

    browser = browse(MOBILE)
    browser.sign_in(dashboard.token)
    owner = dashboard.store.principals.get(dashboard.owner.id)
    dashboard.store.principals.update(owner.with_status(PrincipalStatus.DISABLED, at()))
    browser.page.get_by_role("heading", name="Sign in").wait_for(timeout=20_000)
    assert "Your session is no longer valid." in browser.text()
    assert browser.page.evaluate("sessionStorage.length") == 0
    browser.assert_clean(dashboard.token)


# ------------------------------------------------------------------ seeded screens


def seeded(dashboard):
    """Two projects: one waiting for approval (long names and paths), one with an uncertain action."""
    from agent_factory.domain.milestones import MilestoneStatus
    from agent_factory.state import RunStatus

    store = dashboard.store
    long_name = "Calculator with a deliberately long project name that must wrap on a phone screen"
    waiting = seed_project(store, long_name, description="A calculator built overnight by the fake provider.")
    request = seed_approval(
        store, waiting, path="src/very/deeply/nested/directory/structure/legacy_calc_module_v1.py",
        reason="The deprecated module " + "is replaced by calculator.py " * 4,
    )  # fmt: skip
    seed_milestone(store, waiting, 1, title="Plan ready", summary="Implement calculator.py with four functions.", ago=900)
    seed_milestone(store, waiting, 2, title="Tests passing", summary="5 tests, FAILED (1 failing)",
                   status=MilestoneStatus.FAILED, milestone_type="tests_passing", ago=600)  # fmt: skip
    seed_milestone(store, waiting, 3, title="Implementation ready", summary="Fix subtract.", milestone_type="implementation_ready", ago=300)
    uncertain = seed_project(store, "Notice sender", status=RunStatus.AWAITING_RECONCILIATION)
    notice = seed_approval(store, uncertain, path="notices/billing.txt")
    execution = seed_uncertain(store, uncertain, notice, dashboard.owner.actor())
    return waiting, request, uncertain, notice, execution


def screens(waiting, request, uncertain, notice):
    return {
        "dashboard": "/",
        "attention": "/attention",
        "project": f"/projects/{waiting.id}",
        "approval": f"/projects/{waiting.id}/approvals/{request.id}",
        "reconciliation": f"/projects/{uncertain.id}/approvals/{notice.id}",
    }


@pytest.mark.parametrize("viewport", [MOBILE, DESKTOP], ids=["phone-375x812", "desktop-1280x800"])
def test_every_screen_fits_the_viewport_and_its_controls_are_usable(dashboard, browse, viewport):
    waiting, request, uncertain, notice, _ = seeded(dashboard)
    paths = screens(waiting, request, uncertain, notice)
    browser = browse(viewport)
    browser.sign_in(dashboard.token)
    page = browser.page
    for name, path in paths.items():
        page.goto(path)
        page.get_by_role("main").wait_for()
        page.wait_for_load_state("networkidle")
        assert overflow(page) == 0, f"{name}: horizontal scrolling at {viewport}"
    page.goto(paths["approval"])
    for label in ("Approve…", "Reject…"):
        button = page.get_by_role("button", name=label)
        button.scroll_into_view_if_needed()
        box = button.bounding_box()
        assert button.is_visible() and box["height"] >= 44 and box["width"] >= 44, (label, box)
    for section in ("What will happen", "Why it is requested", "Risk", "Context"):
        assert page.get_by_role("heading", name=section).is_visible(), section
    page.goto(paths["reconciliation"])
    for choice in ("It happened", "It didn't happen — don't retry", "It didn't happen — authorize one retry"):
        label = page.get_by_text(choice, exact=True)
        label.scroll_into_view_if_needed()
        assert label.is_visible() and label.bounding_box()["height"] >= 24, choice
    assert page.get_by_role("heading", name="Timeline").is_visible()
    assert overflow(page) == 0
    page.goto("/attention")
    item = page.get_by_role("link", name=re.compile("Reconciliation required"))
    assert item.bounding_box()["height"] >= 44
    browser.assert_clean(dashboard.token)


@pytest.mark.parametrize("viewport", [MOBILE, DESKTOP], ids=["phone", "desktop"])
def test_every_screen_passes_automated_accessibility_checks(dashboard, browse, viewport):
    waiting, request, uncertain, notice, _ = seeded(dashboard)
    browser = browse(viewport)
    browser.open("/")
    browser.page.get_by_role("heading", name="Sign in").wait_for()
    assert axe_violations(browser.page) == [], "sign-in"
    browser.sign_in(dashboard.token)
    for name, path in screens(waiting, request, uncertain, notice).items():
        browser.page.goto(path)
        browser.page.wait_for_load_state("networkidle")
        assert axe_violations(browser.page) == [], name
    browser.page.get_by_label("It didn't happen — authorize one retry").check()
    browser.page.get_by_role("button", name="Review answer…").click()
    assert axe_violations(browser.page) == [], "reconciliation review"


def test_the_decision_can_be_made_with_the_keyboard_alone(dashboard, browse):
    waiting, request, *_ = seeded(dashboard)
    browser = browse(DESKTOP)
    browser.sign_in(dashboard.token, path=f"/projects/{waiting.id}/approvals/{request.id}")
    page = browser.page
    page.get_by_role("button", name="Reject…").wait_for()
    for _ in range(80):  # Tab until the first decision button has focus: reachable without a mouse
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement && document.activeElement.textContent") == "Approve…":
            break
    else:
        raise AssertionError("Approve… is not reachable with Tab")
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none", "no visible focus"
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")  # Reject…
    page.get_by_role("heading", name="Confirm rejection").wait_for()
    assert page.evaluate("document.activeElement.textContent") == "Confirm rejection"  # focus moved to the step
    page.wait_for_timeout(450)
    page.keyboard.press("Tab")  # comment
    page.keyboard.press("Tab")  # confirm
    page.keyboard.press("Enter")
    page.get_by_text("Agent Factory recorded your rejection.").wait_for()
    status, body = dashboard.call("GET", f"/api/v1/projects/{waiting.id}/approvals/{request.id}")
    assert status == 200 and body["decision"]["outcome"] == "rejected"
    assert body["decision"]["decided_by"]["display_name"] == "Dashboard Owner" and body["decision"]["decided_by"]["verified"]


def test_html_like_content_from_the_api_renders_as_inert_text(dashboard, browse):
    # No "/" in the payload: the API's own sanitizer rewrites path-like text, and this test is about the UI,
    # so it uses strings the API passes through unchanged.
    payload = "<img src=x onerror=alert(1)><svg onload=alert(2)><iframe srcdoc=x>"
    store = dashboard.store
    project = seed_project(store, f"XSS {payload}", description=payload)
    request = seed_approval(store, project, reason=f"Because {payload}")
    seed_milestone(store, project, 1, title=f"Plan {payload}", summary=payload)
    browser = browse(DESKTOP)
    browser.sign_in(dashboard.token)
    page = browser.page
    shown = {
        "/": f"XSS {payload}",
        "/attention": f"XSS {payload}",
        f"/projects/{project.id}": f"Plan {payload}",
        f"/projects/{project.id}/approvals/{request.id}": f"Because {payload}",
    }
    for path, text in shown.items():
        page.goto(path)
        page.wait_for_load_state("networkidle")
        page.get_by_text(text).first.wait_for()  # displayed character for character, as text
        assert page.locator("main img, main svg, main iframe, main script").count() == 0, path
    browser.assert_clean(dashboard.token)  # including: no alert() dialog ever opened


def test_the_served_bundle_holds_no_secret_and_is_served_with_a_strict_policy(dashboard, dashboard_dist):
    from agent_factory.api.auth.development import PRIVATE_KEY_FILE

    key_lines = (dashboard.issuer_dir / PRIVATE_KEY_FILE).read_text(encoding="ascii").splitlines()
    secrets = {
        "database URL": dashboard.config.url,
        "database password": urlsplit(dashboard.config.url).password,
        "development token": dashboard.token,
        "signing key": key_lines[1],
    }
    patterns = [r"postgres(ql)?://", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"AKIA[0-9A-Z]{16}",
                r"aws_secret_access_key", r"AGENT_FACTORY_", r"database\.url", r"signing-key\.pem"]  # fmt: skip
    files = [path for path in dashboard_dist.rglob("*") if path.is_file()]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, value in secrets.items():
            assert value and value not in text, f"{name} in {path.name}"
        for pattern in patterns:
            assert not re.search(pattern, text), f"{pattern} in {path.name}"
    with urllib.request.urlopen(dashboard.url + "/", timeout=5) as response:
        headers = response.headers
        html = response.read().decode()
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe" not in csp
    assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff"
    assert re.findall(r"<script(?![^>]*\bsrc=)", html) == []


@pytest.mark.parametrize(
    "origin", ["http://192.168.1.10:8000", "http://api.example.test", "https://127.0.0.1:8000", "http://127.0.0.1:8000/api"]
)
def test_the_frontend_server_refuses_to_proxy_to_anything_but_the_loopback_api(dashboard_dist, origin):
    import subprocess

    from dashboard_support import FRONTEND, VITE, clean_env, free_port, node

    argv = [node(), str(VITE), "preview", "--port", str(free_port()), "--strictPort", "--outDir", str(dashboard_dist)]
    done = subprocess.run(argv, cwd=FRONTEND, env=clean_env(AGENT_FACTORY_API_ORIGIN=origin), capture_output=True,
                          text=True, timeout=60)  # fmt: skip
    assert done.returncode != 0, done.stdout
    assert "(loopback only)" in done.stdout + done.stderr


def test_a_missing_permission_is_explained_and_records_nothing(dashboard, browse):
    from agent_factory.domain.principals import Permission

    waiting, request, *_ = seeded(dashboard)
    reader = dashboard.person("reader", {Permission.APPROVALS_READ, Permission.PROJECTS_READ}, "Read Only")
    browser = browse(MOBILE)
    browser.sign_in(reader, path=f"/projects/{waiting.id}/approvals/{request.id}")
    browser.page.get_by_role("button", name="Approve…").click()
    browser.page.wait_for_timeout(450)
    browser.page.get_by_role("button", name="Yes, approve this action").click()
    browser.page.get_by_text("You don't have permission to perform this action.").wait_for()
    assert "approvals:decide" in browser.text()
    assert "recorded your approval" not in browser.text()
    status, body = dashboard.call("GET", f"/api/v1/projects/{waiting.id}/approvals/{request.id}")
    assert status == 200 and body["decision"] is None
    browser.assert_clean(reader)

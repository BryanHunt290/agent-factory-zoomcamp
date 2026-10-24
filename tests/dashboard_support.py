"""The real stack for the dashboard's browser, E2E, and reliability tests (marker browser).

    API       python -m agent_factory api serve     development authentication, a fresh test schema
    frontend  node vite.js preview                  the built bundle; proxies /api, /healthz, /readyz to the API
    workers   python -m agent_factory worker run    calculator runs: production code
              python tests/dashboard_worker.py      action-fixture runs (a notice whose outcome can be unknown)
    browser   the installed Google Chrome           Playwright; every non-loopback request goes to a dead proxy

Nothing here mocks the API, the database, or the workers: every process is real and can be killed like a
crash (``Child.kill``). The development issuer's key lives in the test's temporary directory, and tokens
are minted exactly as ``auth dev-issuer token`` mints them.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
VITE = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
AXE = FRONTEND / "node_modules" / "axe-core" / "axe.min.js"
FIXTURE_WORKER = Path(__file__).resolve().parent / "dashboard_worker.py"
OWNER_SUBJECT = "dashboard-owner"
OWNER_NAME = "Dashboard Owner"
MOBILE = {"width": 375, "height": 812}
DESKTOP = {"width": 1280, "height": 800}
#: Chrome sends every non-loopback request here, where nothing listens: the browser cannot reach the internet.
BLACKHOLE_PROXY = {"server": "http://127.0.0.1:9", "bypass": "127.0.0.1,localhost"}


def node() -> str:
    found = shutil.which("node")
    if not found:
        raise RuntimeError("Node.js is not on PATH: the dashboard tests need it (docs/frontend.md)")
    return found


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(url: str, *, method: str = "GET", token: str | None = None, body: Any = None, headers: dict | None = None,
         timeout: float = 5.0) -> tuple[int | None, Any]:
    """(status, parsed JSON or text) for a loopback request; (None, reason) when nothing answers."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, str(error)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")


def wait_until(condition: Callable[[], Any], timeout: float, what: str, poll: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:g} s waiting for {what}")
        time.sleep(poll)


class Child:
    """A real process with its output in a log file. ``kill`` ends it at once, like a crash."""

    def __init__(self, name: str, argv: list[str], cwd: Path, env: dict[str, str], logs: Path) -> None:
        self.name = name
        self.log_path = logs / f"{name}-{time.monotonic_ns()}.log"
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=self._log, stderr=subprocess.STDOUT)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.alive:
            self.proc.kill()
        try:
            self.proc.wait(timeout=30)
        finally:
            self._log.close()

    def output(self) -> str:
        if not self._log.closed:
            self._log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def events(self) -> list[dict]:
        """The JSON event lines a worker printed (complete lines only: the last may still be written)."""
        lines = self.output().split("\n")[:-1]
        return [json.loads(line) for line in lines if line.strip().startswith("{")]


def build_dashboard(out: Path) -> Path:
    """Build the production bundle into ``out`` (each test session builds its own)."""
    if not VITE.is_file():
        raise RuntimeError("frontend/node_modules is missing: cd frontend && npm ci (docs/frontend.md)")
    done = subprocess.run(
        [node(), str(VITE), "build", "--outDir", str(out), "--emptyOutDir", "--logLevel", "warn"],
        cwd=FRONTEND, capture_output=True, text=True, timeout=300, env=clean_env(),
    )  # fmt: skip
    if done.returncode != 0:
        raise RuntimeError(f"vite build failed:\n{done.stdout}\n{done.stderr}")
    return out


def clean_env(**extra: str) -> dict[str, str]:
    """This process's environment without any Agent Factory setting, plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_FACTORY_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def enroll(store, subject: str, permissions, name: str):
    from agent_factory.api.auth.development import DEV_ISSUER
    from agent_factory.domain.ids import new_principal_id
    from agent_factory.domain.principals import Principal, PrincipalStatus
    from agent_factory.util import timestamp, utc_now

    now = timestamp(utc_now)
    principal = Principal(
        id=new_principal_id(), issuer=DEV_ISSUER, subject=subject, status=PrincipalStatus.ACTIVE, created_at=now,
        updated_at=now, display_name=name,
    )  # fmt: skip
    store.principals.add(principal)
    if permissions:
        store.principals.grant(principal.id, permissions, at=now)
    return principal


class DashboardStack:
    """The API, the frontend server, and workers for one test, on a fresh PostgreSQL test schema."""

    def __init__(self, root: Path, config, store, dist: Path) -> None:
        from agent_factory.api.auth.development import DevelopmentIssuer
        from agent_factory.domain.principals import ALL_PERMISSIONS

        self.root = root
        self.config = config
        self.store = store
        self.dist = dist
        self.logs = root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.issuer_dir = root / "dev-issuer"
        self.issuer = DevelopmentIssuer.create(self.issuer_dir)
        self.owner = enroll(store, OWNER_SUBJECT, ALL_PERMISSIONS, OWNER_NAME)
        self.token = self.mint(OWNER_SUBJECT, OWNER_NAME)
        self.api_port = free_port()
        self.frontend_port = free_port()
        self.api: Child | None = None
        self.frontend: Child | None = None
        self.children: list[Child] = []

    # -------------------------------------------------------------- identity

    def mint(self, subject: str, name: str, lifetime: timedelta = timedelta(hours=3)) -> str:
        return self.issuer.mint(subject, lifetime=lifetime, display_name=name)

    def person(self, subject: str, permissions, name: str) -> str:
        """Enroll another person with ``permissions``; their token."""
        enroll(self.store, subject, permissions, name)
        return self.mint(subject, name)

    # -------------------------------------------------------------- processes

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.frontend_port}"

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def spawn(self, name: str, argv: list[str], cwd: Path, env: dict[str, str]) -> Child:
        child = Child(name, argv, cwd, env, self.logs)
        self.children.append(child)
        return child

    def database_env(self) -> dict[str, str]:
        return {"AGENT_FACTORY_DATABASE_URL": self.config.url, "AGENT_FACTORY_DATABASE_SCHEMA": self.config.schema}

    def start_api(self, wait: bool = True) -> Child:
        env = clean_env(
            AGENT_FACTORY_AUTH_MODE="development", AGENT_FACTORY_AUTH_DEV_ISSUER_DIR=str(self.issuer_dir),
            **self.database_env(),
        )  # fmt: skip
        self.api = self.spawn("api", [sys.executable, "-m", "agent_factory", "api", "serve", "--port", str(self.api_port)], REPO, env)
        if wait:
            wait_until(lambda: http(self.api_url + "/readyz")[0] == 200 or not self.api.alive, 60, "the API to be ready")
            assert self.api.alive, self.api.output()
        return self.api

    def stop_api(self) -> None:
        assert self.api is not None
        self.api.kill()
        wait_until(lambda: http(self.api_url + "/healthz", timeout=1)[0] is None, 15, "the API to stop answering")

    def start_frontend(self) -> Child:
        argv = [node(), str(VITE), "preview", "--port", str(self.frontend_port), "--strictPort", "--outDir", str(self.dist)]
        self.frontend = self.spawn("frontend", argv, FRONTEND, clean_env(AGENT_FACTORY_API_ORIGIN=self.api_url))
        wait_until(lambda: http(self.url + "/")[0] == 200 or not self.frontend.alive, 60, "the frontend server")
        assert self.frontend.alive, self.frontend.output()
        return self.frontend

    def stop_frontend(self) -> None:
        assert self.frontend is not None
        self.frontend.kill()
        wait_until(lambda: http(self.url + "/", timeout=1)[0] is None, 15, "the frontend server to stop answering")

    def calculator_worker(self, workspace_base: Path, *, lease: int = 10, heartbeat: int = 3) -> Child:
        argv = [sys.executable, "-m", "agent_factory", "worker", "run", "--workspace-base", str(workspace_base),
                "--poll", "0.2", "--lease", str(lease), "--heartbeat", str(heartbeat)]  # fmt: skip
        return self.spawn("calculator-worker", argv, REPO, clean_env(**self.database_env()))

    def fixture_worker(self, flow_root: Path, kind: str, *extra: str) -> Child:
        env = clean_env(AGENT_FACTORY_TEST_DATABASE_URL=self.config.url, DASHBOARD_WORKER_SCHEMA=self.config.schema)
        return self.spawn("fixture-worker", [sys.executable, str(FIXTURE_WORKER), str(flow_root), kind, *extra], REPO, env)

    def dispatcher(self, record: Path, *extra: str, retry_base: float = 1, script: Path | None = None) -> Child:
        """``notifications dispatch`` with the fake provider (DEVELOPMENT ONLY), linking to this dashboard."""
        argv = [sys.executable, "-m", "agent_factory", "notifications", "dispatch", "--provider", "fake", "--fake-record",
                str(record), "--dashboard-url", self.url, "--poll", "0.2", "--lease", "4", "--send-timeout", "1",
                "--retry-base", str(retry_base), "--retry-cap", str(retry_base), "--max-backoff", "1", "--sweep", "1",
                *(["--fake-script", str(script)] if script else []), *extra]  # fmt: skip
        env = clean_env(AGENT_FACTORY_NOTIFICATIONS_MODE="development", **self.database_env())
        return self.spawn(f"dispatcher-{len(self.children)}", argv, REPO, env)

    def cli(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        env = clean_env(**self.database_env())
        return subprocess.run([sys.executable, "-m", "agent_factory", *args], cwd=REPO, env=env, capture_output=True,
                              text=True, timeout=timeout)  # fmt: skip

    # -------------------------------------------------------------- the API, as a test sees it

    def call(self, method: str, path: str, body: Any = None, token: str | None = None, headers: dict | None = None):
        return http(self.api_url + path, method=method, body=body, token=token or self.token, headers=headers)

    def run_status(self, project_id: str, run_id: str = "run-1") -> str | None:
        """The run's status as the API reports it; None while the API does not answer with it."""
        status, body = self.call("GET", f"/api/v1/projects/{project_id}/runs/{run_id}")
        return body["status"] if status == 200 else None

    def wait_for_run(self, project_id: str, statuses: set[str], timeout: float = 120, run_id: str = "run-1") -> str:
        return wait_until(lambda: (s := self.run_status(project_id, run_id)) in statuses and s, timeout,
                          f"run {run_id} to be {sorted(statuses)}", poll=0.25)  # fmt: skip

    def approval(self, project_id: str, approval_id: str) -> dict:
        status, body = self.call("GET", f"/api/v1/projects/{project_id}/approvals/{approval_id}")
        assert status == 200, body
        return body

    def create_project(self, name: str = "Dashboard calculator") -> str:
        status, body = self.call(
            "POST", "/api/v1/projects", {"name": name, "template": "calculator", "description": "Created by a test"},
            headers={"Idempotency-Key": f"dashboard-test-{time.monotonic_ns()}"},
        )  # fmt: skip
        assert status == 201, body
        return body["id"]

    def close(self) -> None:
        for child in reversed(self.children):
            try:
                child.kill()
            except Exception:  # noqa: BLE001 - teardown keeps going; the log shows what happened
                pass


class Browsing:
    """One browser context (its own storage, like a separate browser), recording everything risky."""

    def __init__(self, browser, base_url: str, viewport: dict[str, int]) -> None:
        mobile = viewport["width"] < 600
        self.base_url = base_url
        self.context = browser.new_context(
            viewport=viewport, base_url=base_url, is_mobile=mobile, has_touch=mobile, device_scale_factor=2 if mobile else 1,
            service_workers="block",
        )  # fmt: skip
        self.requests: list[str] = []
        self.posts: list[tuple[str, Any]] = []  # (path, JSON body) of every POST the page sent
        self.console: list[str] = []
        self.dialogs: list[str] = []
        self.errors: list[str] = []
        self.context.on("request", self._record)
        self.closed = False
        self.page = self.context.new_page()
        self.page.on("console", lambda message: self.console.append(message.text))
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

        def on_dialog(dialog):
            self.dialogs.append(dialog.message)
            dialog.dismiss()

        self.page.on("dialog", on_dialog)

    def _record(self, request) -> None:
        from urllib.parse import urlsplit

        self.requests.append(request.url)
        if request.method == "POST":
            self.posts.append((urlsplit(request.url).path, json.loads(request.post_data or "null")))

    def posted(self, suffix: str) -> list[Any]:
        """The bodies of the POSTs whose path ends with ``suffix`` (e.g. "/decision"), in order."""
        return [body for path, body in self.posts if path.endswith(suffix)]

    def open(self, path: str = "/") -> None:
        self.page.goto(path)

    def sign_in(self, token: str, path: str = "/") -> None:
        self.page.goto(path)
        self.page.get_by_label("Development access token").fill(token)
        self.page.get_by_role("button", name="Sign in").click()
        self.page.get_by_role("navigation", name="Main").wait_for(timeout=15_000)

    def text(self) -> str:
        return self.page.locator("body").inner_text()

    def assert_clean(self, *secrets: str) -> None:
        """Only same-origin loopback requests; no secret in a URL or the console; no dialog; no page error."""
        from urllib.parse import urlparse

        for url in self.requests:
            parsed = urlparse(url)
            assert parsed.scheme in {"http", "data"}, url
            if parsed.scheme == "http":
                assert parsed.hostname == "127.0.0.1" and url.startswith(self.base_url), f"request outside the dashboard origin: {url}"
            for secret in secrets:
                assert secret not in url
        for line in self.console:
            assert "Bearer" not in line
            for secret in secrets:
                assert secret not in line
        assert self.dialogs == [], self.dialogs
        assert self.errors == [], self.errors

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.context.close()


def overflow(page) -> int:
    """How many CSS pixels the page is wider than the viewport (0: no horizontal scrolling)."""
    return page.evaluate("Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)")


def axe_violations(page) -> list[dict]:
    """axe-core's WCAG 2.1 A/AA violations on the current page (axe-core from frontend/node_modules).

    axe is evaluated through the DevTools protocol, which page CSP does not govern: the dashboard's
    policy forbids inline scripts, and stays strict during the check.
    """
    page.evaluate(AXE.read_text(encoding="utf-8"))
    result = page.evaluate(
        "async () => (await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}})).violations"
    )
    return [{"id": v["id"], "impact": v["impact"], "nodes": [n["target"] for n in v["nodes"]][:3]} for v in result]


# ------------------------------------------------- runs for a test (queued exactly as in production)


def calculator_run(dashboard, tmp_path, name="Overnight calculator"):
    """A calculator project created through the API, its run queued, and a real worker started."""
    base = tmp_path / "workspaces"
    project_id = dashboard.create_project(name)
    queued = dashboard.cli("worker", "enqueue", "--project-id", project_id, "--run-id", "run-1", "--scenario", "approval",
                           "--workspace-base", str(base), "--no-git")  # fmt: skip
    assert queued.returncode == 0, queued.stderr
    legacy = base / project_id / "project" / "legacy_calc.py"
    assert legacy.is_file()
    return project_id, legacy, dashboard.calculator_worker(base)


def notice_run(dashboard, tmp_path, kind, *worker_args):
    """The action fixture's run (one approval-gated notice), queued, with a real worker loop started."""
    from decision_support import Flow

    flow = Flow.postgres(tmp_path / "flow", dashboard.config, dashboard.store, kind, enroll=False)
    flow.create()
    return flow, dashboard.fixture_worker(flow.root, kind, *worker_args)


# ------------------------------------------------- what a person does in the dashboard

TERMINAL = {"completed", "failed", "blocked", "exhausted"}
#: The confirm buttons ignore taps for 400 ms after they appear (frontend/src/screens/approval/DecisionPanel.tsx).
CONFIRM_GUARD_MS = 450
RECORDED = {
    "approved": "Agent Factory recorded your approval.",
    "rejected": "Agent Factory recorded your rejection.",
    "answer": "Agent Factory recorded your answer.",
}
CHOICE_LABEL = {
    "it_happened": "It happened",
    "did_not_happen": "It didn't happen — don't retry",
    "did_not_happen_retry": "It didn't happen — authorize one retry",
}
RETRY_ACKNOWLEDGEMENT = "I checked that the action did not happen, and I authorize one new attempt for 15 minutes."
UNCERTAIN = "Agent Factory attempted this action but cannot prove whether it completed."


def open_attention(page, kind: str, timeout_s: float = 90) -> str:
    """Open the attention inbox, wait for an item of ``kind`` ("Approval required" or "Reconciliation
    required"), and open it. The approval's ID."""
    import re

    page.goto("/attention")
    link = page.get_by_role("link", name=re.compile(kind)).first
    link.wait_for(timeout=timeout_s * 1000)
    link.click()
    page.wait_for_url(re.compile(r"/approvals/apr-[0-9a-f]{16}$"))
    page.get_by_role("heading", name=kind, exact=True).wait_for()
    return page.url.rsplit("/", 1)[-1]


def start_decision(page, outcome: str, comment: str = "") -> None:
    """The first, deliberate step: "Approve…" or "Reject…", a comment, then past the confirm guard."""
    approving = outcome == "approved"
    page.get_by_role("button", name="Approve…" if approving else "Reject…").click()
    page.get_by_role("heading", name="Confirm approval" if approving else "Confirm rejection").wait_for()
    if comment:
        page.get_by_label("Comment (optional)").fill(comment)
    page.wait_for_timeout(CONFIRM_GUARD_MS)


def confirm_decision(page, outcome: str) -> None:
    page.get_by_role("button", name="Yes, approve this action" if outcome == "approved" else "Yes, reject this action").click()


def decide(page, outcome: str, comment: str = "") -> None:
    start_decision(page, outcome, comment)
    confirm_decision(page, outcome)


def start_answer(page, choice: str) -> None:
    """Pick an answer for an uncertain action and open its review (acknowledging a retry)."""
    page.get_by_label(CHOICE_LABEL[choice]).check()
    page.get_by_role("button", name="Review answer…").click()
    page.get_by_role("heading", name="Confirm your answer").wait_for()
    if choice == "did_not_happen_retry":
        page.get_by_label(RETRY_ACKNOWLEDGEMENT).check()
    page.wait_for_timeout(CONFIRM_GUARD_MS)


def confirm_answer(page, choice: str) -> None:
    page.get_by_role("button", name="Authorize one retry" if choice == "did_not_happen_retry" else "Confirm answer").click()


class Hold:
    """A slow network for one page: matching requests wait, unsent, until ``release`` lets them through.

    Nothing is answered on the API's behalf: a held request reaches the real API only when released, and
    the page gets the real answer. Release well within the dashboard's 10 s request timeout.
    """

    def __init__(self, page, pattern, method: str = "GET", count: int | None = None) -> None:
        self.page = page
        self.pattern = pattern
        self.method = method
        self.remaining = count  # hold only this many (None: all)
        self.held: list = []
        page.route(pattern, self._handle)

    def _handle(self, route) -> None:
        if route.request.method == self.method and (self.remaining is None or self.remaining > 0):
            if self.remaining is not None:
                self.remaining -= 1
            self.held.append(route)
        else:
            route.fallback()

    def release(self) -> None:
        self.page.unroute(self.pattern, self._handle)
        held, self.held = self.held, []
        for route in held:
            route.fallback()


def guarded_container(config) -> str:
    """This project's PostgreSQL container, after proving it is the one serving the test database.

    The same guard as tests/test_worker_restart_live.py: only a container named for this project
    (``agent-factory-postgres-*``) that publishes the test database's port may be restarted. Anything
    else fails the test; nothing else on the machine is touched.
    """
    name = os.environ.get("AGENT_FACTORY_TEST_POSTGRES_CONTAINER", "agent-factory-postgres-dev")
    if not name.startswith("agent-factory-postgres"):
        raise AssertionError(f"refusing to restart {name!r}: only this project's PostgreSQL container may be restarted")
    ports = docker("port", name, "5432/tcp")
    if ports.returncode != 0:
        raise AssertionError(f"container {name!r} is not available: {ports.stderr.strip()}")
    if f":{config.port}" not in ports.stdout:
        raise AssertionError(f"container {name!r} does not publish the test database's port {config.port}")
    return name


def docker(*args: str, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def database_reachable(config) -> bool:
    from agent_factory.domain.errors import StorageError
    from agent_factory.persistence.postgres import connect

    try:
        connect(config).close()
        return True
    except StorageError:
        return False


# ------------------------------------------------- seeding (current times, straight into the test schema)


def at(offset_seconds: float = 0) -> str:
    from datetime import datetime, timezone

    from agent_factory.domain.serialization import format_timestamp

    return format_timestamp(datetime.now(timezone.utc) + timedelta(seconds=offset_seconds))


def seed_project(store, name: str, *, status=None, run_id: str = "run-1", description: str = ""):
    """A calculator project with one run (default: waiting for approval)."""
    from agent_factory.domain.ids import new_project_id
    from agent_factory.domain.project import Project, Run
    from agent_factory.state import RunStatus
    from agent_factory.templates import CALCULATOR_TEMPLATE

    project = Project(id=new_project_id(), name=name, created_at=at(-3600), template="calculator", description=description,
                      milestone_policy=CALCULATOR_TEMPLATE.milestone_policy)  # fmt: skip
    store.projects.add(project)
    store.runs.add(Run(project_id=project.id, run_id=run_id, number=1, graph_name="calculator-v1",
                       status=status or RunStatus.AWAITING_APPROVAL, created_at=at(-3600), updated_at=at(-60)))  # fmt: skip
    return project


def seed_approval(store, project, *, run_id: str = "run-1", path: str = "legacy_calc.py", reason: str = "The old module is replaced.",
                  requested_ago: float = 60, expires_in: float = 12 * 3600):
    from agent_factory.domain.approvals import ApprovalRequest, ExactAction, risk_for
    from agent_factory.safety.approval import make_request_id

    action = ExactAction(tool="delete_file", category="destructive_delete", params={"path": path})
    request = ApprovalRequest(
        id=make_request_id(run_id, "developer", 1, action.tool, action.params), project_id=project.id, run_id=run_id,
        node="developer", agent="developer", action=action, action_fingerprint=action.fingerprint, reason=reason,
        risk=risk_for(action.category), requested_at=at(-requested_ago), expires_at=at(expires_in - requested_ago),
    )  # fmt: skip
    store.approvals.add_request(request)
    return request


def seed_milestone(store, project, sequence: int, *, run_id: str = "run-1", title: str = "Plan ready",
                   summary: str = "Plan recorded", status=None, milestone_type: str = "plan_ready", ago: float = 120):
    from agent_factory.domain.ids import milestone_id
    from agent_factory.domain.milestones import Milestone, MilestoneCategory, MilestoneSource, MilestoneStatus

    milestone = Milestone(
        id=milestone_id(project.id, run_id, f"step:{sequence}"), project_id=project.id, run_id=run_id, sequence=sequence,
        key=f"step:{sequence}", type=milestone_type, category=MilestoneCategory.WORKFLOW, title=title, summary=summary,
        status=status or MilestoneStatus.COMPLETED, created_at=at(-ago), occurred_at=at(-ago),
        source=MilestoneSource(node="planner", agent="planner", step=sequence),
    )  # fmt: skip
    store.milestones.add(milestone)
    return milestone


def seed_uncertain(store, project, request, actor):
    """The request approved by ``actor``, sent by a worker, ending without a known outcome (a timeout)."""
    from datetime import datetime, timezone

    from agent_factory.domain.approvals import ApprovalOutcome
    from agent_factory.domain.executions import AttemptResult, ExecutionLedger, ExecutionSpec, LedgerKind
    from agent_factory.domain.services import ApprovalService
    from agent_factory.safety.execution import EffectClass, ExecutionSemantics

    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    decided = ApprovalService(store.approvals, clock=clock).record(
        project_id=project.id, request_id=request.id, action_fingerprint=request.action_fingerprint,
        outcome=ApprovalOutcome.APPROVED, actor=actor,
    )  # fmt: skip
    ledger = ExecutionLedger(store.executions, kind=LedgerKind.PRODUCT_STORE, clock=clock)
    spec = ExecutionSpec(
        project_id=project.id, run_id=request.run_id, request_id=request.id, node=request.node, visit=1, agent=request.agent,
        action=request.action, semantics=ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT),
        authorized_at=decided.decision.decided_at, authorized_until=request.expires_at,
    )  # fmt: skip
    execution, _ = ledger.authorize(spec)
    dispatch = ledger.begin(execution, owner="wkr-" + "5e" * 10 + "@3")
    return ledger.finish(dispatch.execution, dispatch.attempt, result=AttemptResult.INDETERMINATE, detail="TimeoutError")

#: The sessionStorage key the dashboard keeps its development token under (frontend/src/auth/session.ts).
TOKEN_KEY_HINT = "agent-factory.local-development-token"

import ipaddress
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent_factory.demo import scenario_provider
from agent_factory.factory import build_executor
from agent_factory.providers import FakeProvider
from agent_factory.sandbox.docker import SubprocessDockerRunner
from agent_factory.sandbox.process import ProcessOutcome
from agent_factory.templates import CALCULATOR_TEMPLATE

REPO_ROOT = Path(__file__).resolve().parent.parent


class Harness:
    """Creates a calculator workspace and executors bound to it."""

    def __init__(self, root, git=False):
        self.template = CALCULATOR_TEMPLATE
        self.workspace = CALCULATOR_TEMPLATE.create_workspace(root, git=git)

    def executor(self, provider, template=None, sandbox=None):
        return build_executor(template or self.template, provider, self.workspace, sandbox=sandbox)

    def run(self, provider, run_id="run-1"):
        ex = self.executor(provider)
        return ex, ex.start(self.template.task, run_id=run_id)

    def run_scenario(self, name, run_id="run-1"):
        provider = scenario_provider(name)
        ex, state = self.run(provider, run_id)
        return ex, state, provider


class ForbiddenOperation(AssertionError):
    pass


_real_popen_init = subprocess.Popen.__init__
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname


def _guarded_popen_init(self, args, *a, **kw):
    first = args if isinstance(args, (str, bytes, os.PathLike)) else args[0]
    name = os.path.basename(os.fsdecode(first)).lower()
    if name.startswith("docker"):
        raise ForbiddenOperation(f"test attempted to launch the Docker CLI: {args!r}")
    return _real_popen_init(self, args, *a, **kw)


def _docker_cli_absent(self, args, **kwargs):
    return ProcessOutcome(None, b"", b"", False, False, False, False, 0.0, error="docker CLI disabled in Tier-0 tests")


def _check_address(address):
    host = address[0] if isinstance(address, tuple) else address
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        if host == "localhost":
            return
    raise ForbiddenOperation(f"test attempted a non-loopback network connection to {address!r}")


def _check_name(host):
    """Name resolution is network traffic too (a DNS query): only loopback names may be resolved."""
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
    if name is None or _is_loopback(str(name)):
        return
    raise ForbiddenOperation(f"test attempted to resolve a non-loopback name {name!r}")


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check_name(host)
    return _real_getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host, *args):
    _check_name(host)
    return _real_gethostbyname(host, *args)


def _guarded_connect(self, address):
    _check_address(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_address(address)
    return _real_connect_ex(self, address)


def _docker_only_popen_init(self, args, *a, **kw):
    """Live suites: the host may launch the docker CLI and nothing else.

    This guarantees hostile fixtures can never be executed on the Windows host.
    """
    first = args if isinstance(args, (str, bytes, os.PathLike)) else args[0]
    name = os.path.basename(os.fsdecode(first)).lower()
    if not name.startswith("docker"):
        raise ForbiddenOperation(f"live/adversarial tests may only launch the docker CLI, not {args!r}")
    return _real_popen_init(self, args, *a, **kw)


#: Executables a test in BOTH the browser and a docker suite may start (the PostgreSQL-restart reliability
#: scenario): the docker CLI, this interpreter (the API and workers), and Node.js (the frontend server and the
#: Playwright driver). Nothing else, and never anything that names a hostile fixture.
BROWSER_DOCKER_EXECUTABLES = frozenset({"node", "node.exe"})


def _aws_live_popen_init(self, args, *a, **kw):
    """The AWS live suite: no process at all, except the two read-only git commands that record the commit.

    Never the AWS CLI (no ``aws sso login``, no ``aws sts``), never a credential process, never Docker.
    """
    from aws_live_support import GIT_COMMANDS, git_argv

    argv = [os.fsdecode(args)] if isinstance(args, (str, bytes, os.PathLike)) else [os.fsdecode(x) for x in args]
    if argv not in [git_argv(REPO_ROOT, command) for command in GIT_COMMANDS]:
        raise ForbiddenOperation(f"the AWS live suite may start no process, only its two git reads, not {args!r}")
    return _real_popen_init(self, args, *a, **kw)


def _browser_and_docker_popen_init(self, args, *a, **kw):
    """Browser + docker_live tests: the docker-only guard, widened by an explicit, short allowlist.

    Hostile fixtures stay off the host: they run only in the adversarial suite, whose docker-only guard is
    unchanged, and any argument naming one is refused here too.
    """
    argv = [os.fsdecode(args)] if isinstance(args, (str, bytes, os.PathLike)) else [os.fsdecode(x) for x in args]
    if any("hostile" in part.lower() or part.lower().endswith(".hpy") for part in argv):
        raise ForbiddenOperation(f"a hostile fixture may never run on the host: {args!r}")
    name = os.path.basename(argv[0]).lower()
    interpreter = os.path.normcase(os.path.abspath(argv[0])) == os.path.normcase(os.path.abspath(sys.executable))
    if not (name.startswith("docker") or name in BROWSER_DOCKER_EXECUTABLES or interpreter):
        raise ForbiddenOperation(f"browser/live tests may only launch docker, node, or this interpreter, not {args!r}")
    return _real_popen_init(self, args, *a, **kw)


LIVE_OPTION = "--run-docker-live"
ADVERSARIAL_OPTION = "--run-docker-adversarial"
POSTGRES_OPTION = "--run-postgres"
STRESS_OPTION = "--run-stress"
BROWSER_OPTION = "--run-browser"
MODEL_LIVE_OPTION = "--run-model-live"
AWS_LIVE_OPTION = "--run-aws-live"
#: marker -> the opt-in flag that enables it
OPT_IN_SUITES = {
    "docker_live": LIVE_OPTION,
    "docker_adversarial": ADVERSARIAL_OPTION,
    "postgres": POSTGRES_OPTION,
    "stress": STRESS_OPTION,
    "browser": BROWSER_OPTION,
    "model_live": MODEL_LIVE_OPTION,
    "aws_live": AWS_LIVE_OPTION,
}
#: suites that run the docker CLI (and therefore get the docker-only process guard)
DOCKER_SUITES = frozenset({"docker_live", "docker_adversarial"})


def pytest_addoption(parser):
    parser.addoption(
        LIVE_OPTION,
        action="store_true",
        help="run live Docker smoke tests (marker docker_live); any skip or unavailability then FAILS",
    )
    parser.addoption(
        ADVERSARIAL_OPTION,
        action="store_true",
        help="run the live adversarial acceptance suite (marker docker_adversarial); any skip then FAILS",
    )
    parser.addoption(
        POSTGRES_OPTION,
        action="store_true",
        help="run PostgreSQL integration tests (marker postgres) against AGENT_FACTORY_TEST_DATABASE_URL(_FILE); "
        "any skip or unavailability then FAILS",
    )
    parser.addoption(
        STRESS_OPTION,
        action="store_true",
        help="run the stress tests (marker stress): slow repetitions that hunt transient failures; any skip then FAILS",
    )
    parser.addoption(
        MODEL_LIVE_OPTION,
        action="store_true",
        help="run the live model suite (marker model_live): REAL, possibly paid model calls with a tiny hard budget; "
        "needs AGENT_FACTORY_MODEL_* configuration (docs/model-providers.md); any skip then FAILS",
    )
    parser.addoption(
        AWS_LIVE_OPTION,
        action="store_true",
        help="run the AWS live verification (marker aws_live): REAL calls to STS and the SSO portal in us-west-2 only; "
        "needs the owner's authorization and IAM Identity Center sign-in (docs/aws-capabilities.md); any skip then FAILS",
    )
    parser.addoption(
        BROWSER_OPTION,
        action="store_true",
        help="run the dashboard browser tests (marker browser) in the installed Chrome via Playwright; "
        "any skip or unavailability then FAILS",
    )


def _refuse_a_temp_root_inside_the_repository(config):
    """Test workspaces are git repositories, so they must never be created inside this one (docs/testing.md)."""
    for source, value in (("--basetemp", config.option.basetemp), ("PYTEST_DEBUG_TEMPROOT", os.environ.get("PYTEST_DEBUG_TEMPROOT"))):
        if value:
            path = Path(value).resolve()
            if path == REPO_ROOT or REPO_ROOT in path.parents:
                raise pytest.UsageError(f"{source} {value} is inside the repository; use a directory outside it")


def pytest_configure(config):
    _refuse_a_temp_root_inside_the_repository(config)
    config.addinivalue_line("markers", "docker_live: requires a live local Docker engine and the built sandbox image")
    config.addinivalue_line(
        "markers", "docker_adversarial: live adversarial containment tests running hostile fixtures in the sandbox"
    )
    config.addinivalue_line(
        "markers", "postgres: integration tests against a real PostgreSQL test database (docs/database.md)"
    )
    config.addinivalue_line("markers", "stress: slow repetitions that hunt transient failures (docs/testing.md)")
    config.addinivalue_line(
        "markers", "model_live: real model-provider calls with a hard budget (docs/model-providers.md); opt-in only"
    )
    config.addinivalue_line(
        "markers", "aws_live: a real AWS capability verification (docs/aws-capabilities.md); opt-in only, owner-authorized"
    )
    config.addinivalue_line(
        "markers", "browser: dashboard tests in a real browser against the real API (docs/dashboard-reliability.md)"
    )


def _opt_in_suites(item):
    """(marker, opt-in flag) for every opt-in suite ``item`` belongs to; a test may need several."""
    names = {m.name for m in item.iter_markers()}
    return [(marker, option) for marker, option in OPT_IN_SUITES.items() if marker in names]


def _requested(item):
    """True if ``item`` belongs to opt-in suites and every one of them was requested."""
    suites = _opt_in_suites(item)
    return bool(suites) and all(item.config.getoption(option) for _, option in suites)


def pytest_collection_modifyitems(config, items):
    for item in items:
        missing = [(m, o) for m, o in _opt_in_suites(item) if not config.getoption(o)]
        if missing:
            marker, option = missing[0]
            item.add_marker(pytest.mark.skip(reason=f"opt-in suite not requested (use -m {marker} {option})"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if _requested(item) and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"live test skipped during a required live run: {report.longrepr}"


def install_aws_live_guards(monkeypatch):
    """The AWS live suite: no process but its git reads, and the network only to its three us-west-2 hosts on
    port 443. The guard records every refusal; the test fails on any. Nothing else lifts the network guard."""
    from aws_live_support import AwsLiveNetworkGuard

    monkeypatch.setattr(subprocess.Popen, "__init__", _aws_live_popen_init)
    guard = AwsLiveNetworkGuard(resolve=_real_getaddrinfo, connect=_real_connect)
    guard.install(monkeypatch)
    return guard


@pytest.fixture(autouse=True)
def _forbid_docker_and_network(monkeypatch, request):
    """Tier-0 guarantee: no test may start Docker or reach the network.

    Tests in an opted-in live suite may launch the docker CLI - and only the
    docker CLI. The in-process network guard applies to every test.
    """
    suites = {marker for marker, _ in _opt_in_suites(request.node)}
    docker_suite = bool(suites & DOCKER_SUITES)
    if "aws_live" in suites and _requested(request.node):
        request.node.aws_live_guard = install_aws_live_guards(monkeypatch)
        return
    if docker_suite and "browser" in suites and _requested(request.node):
        monkeypatch.setattr(subprocess.Popen, "__init__", _browser_and_docker_popen_init)
    elif docker_suite and _requested(request.node):
        monkeypatch.setattr(subprocess.Popen, "__init__", _docker_only_popen_init)
    else:
        monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_popen_init)
        # The real docker CLI runner behaves as if Docker were not installed, so the
        # default manager's DockerBackend is simply unavailable in Tier-0 tests.
        monkeypatch.setattr(SubprocessDockerRunner, "__call__", _docker_cli_absent)
    if "model_live" in suites and _requested(request.node):
        return  # the live model suite reaches the configured provider's API; nothing else lifts this guard
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", _guarded_gethostbyname)


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path / "ws")


# ------------------------------------------------- PostgreSQL integration tests


def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@pytest.fixture(scope="session")
def postgres_config():
    """The test database (AGENT_FACTORY_TEST_DATABASE_URL or ..._FILE), refusing anything that is not one."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.fail("psycopg is not installed in this interpreter: run with .venv\\Scripts\\python -m pytest (docs/database.md)")
    from agent_factory.persistence.config import TEST_PREFIX, DatabaseConfig, DatabaseConfigError

    try:
        config = DatabaseConfig.from_environment(prefix=TEST_PREFIX)
    except DatabaseConfigError as exc:
        pytest.fail(f"test database not configured: {exc}")
    if "test" not in config.dbname:
        pytest.fail(f"refusing to run against {config.redacted()}: the database name must contain 'test'")
    if not _is_loopback(config.host):
        pytest.fail(f"refusing to run against {config.redacted()}: the test database must be on this machine")
    return config


def _new_schema(postgres_config, migrated):
    from psycopg import sql

    from agent_factory.persistence.migrator import migrate
    from agent_factory.persistence.postgres import connect

    config = postgres_config.with_schema(f"af_test_{secrets.token_hex(6)}")
    conn = connect(config)
    if migrated:
        migrate(conn, config.schema)
    else:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(config.schema)))
    return config, conn


def _drop_schema(config, conn):
    import psycopg
    from psycopg import sql

    from agent_factory.persistence.postgres import connect

    drop = sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(config.schema))
    try:
        conn.execute(drop)
    except psycopg.OperationalError:  # the server restarted during the test (a live restart test): reconnect
        conn.close()
        conn = connect(config)
        conn.execute(drop)
    conn.close()


@pytest.fixture
def pg_schema(postgres_config):
    """A fresh, fully migrated schema in the test database (its config); dropped afterwards."""
    config, conn = _new_schema(postgres_config, migrated=True)
    try:
        yield config
    finally:
        _drop_schema(config, conn)


@pytest.fixture
def pg_empty_schema(postgres_config):
    """A fresh, empty (unmigrated) schema in the test database; dropped afterwards."""
    config, conn = _new_schema(postgres_config, migrated=False)
    try:
        yield config
    finally:
        _drop_schema(config, conn)


@pytest.fixture
def pg_open(pg_schema):
    """Open a new store connection to the test schema; each call is a reconnect. All are closed afterwards."""
    from agent_factory.persistence.postgres import open_postgres_store

    opened = []

    def open_store():
        store = open_postgres_store(pg_schema)
        opened.append(store)
        return store

    yield open_store
    for store in opened:
        store.close()


@pytest.fixture
def pg_store(pg_open):
    return pg_open()


def path_of(state):
    return [(h["node"], h["outcome"]) for h in state.history]


def events(executor, run_id="run-1"):
    return executor.log_for(run_id).entries()


__all__ = ["FakeProvider", "Harness", "events", "path_of"]


# ------------------------------------------------- dashboard browser tests (marker browser)


@pytest.fixture(scope="session")
def dashboard_dist(tmp_path_factory):
    """The production bundle, built once per test session into its own temporary directory."""
    from dashboard_support import build_dashboard

    try:
        return build_dashboard(tmp_path_factory.mktemp("dashboard-dist"))
    except RuntimeError as exc:
        pytest.fail(str(exc))


@pytest.fixture(scope="session")
def chrome():
    """The Google Chrome installed on this machine, driven by Playwright. No browser is downloaded."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.fail("playwright is not installed in this interpreter: pip install -r requirements-browser.txt")
    from dashboard_support import BLACKHOLE_PROXY

    manager = sync_playwright().start()
    try:
        browser = manager.chromium.launch(channel="chrome", headless=True, proxy=BLACKHOLE_PROXY)
    except Exception as exc:  # noqa: BLE001 - reported as a failure of the requested suite
        manager.stop()
        pytest.fail(f"cannot start the installed Google Chrome: {exc}")
    yield browser
    browser.close()
    manager.stop()


@pytest.fixture
def dashboard(tmp_path, pg_schema, pg_store, dashboard_dist):
    """The real API (development authentication) and frontend server on a fresh test schema."""
    from dashboard_support import DashboardStack

    stack = DashboardStack(tmp_path, pg_schema, pg_store, dashboard_dist)
    try:
        stack.start_api()
        stack.start_frontend()
        yield stack
    finally:
        stack.close()


@pytest.fixture
def browse(chrome, dashboard):
    """Open a browser context (a separate browser's storage) at a viewport; all are closed afterwards."""
    from dashboard_support import MOBILE, Browsing

    opened = []

    def open_context(viewport=MOBILE):
        context = Browsing(chrome, dashboard.url, viewport)
        opened.append(context)
        return context

    yield open_context
    for context in opened:
        context.close()

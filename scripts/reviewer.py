"""Local reviewer environment. No production identity or public deployment is configured.

Run ``python scripts/reviewer.py setup`` once (downloads pinned dependencies and
PostgreSQL), then ``python scripts/reviewer.py up``. Ctrl+C stops only the child
services started here; PostgreSQL and all reviewer data remain. ``smoke`` checks
the authenticated workflow without deciding the pending approval. ``check
--browser`` runs the existing offline, PostgreSQL, frontend and browser suites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "postgres:17@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f"
LABEL = "agent-factory.reviewer"
SUBJECT = "zoomcamp-reviewer"
ISSUER = "urn:agent-factory:development"
APP_ROLE = "agent_factory_app"
DATABASES = ("agent_factory_reviewer", "agent_factory_reviewer_test")
REQUIREMENTS = ("requirements-dev.txt", "requirements-postgres.txt", "requirements-api.txt", "requirements-browser.txt", "requirements-aws.txt")
PORTS = {"postgres": 55433, "api": 18000, "frontend": 14173}


class ReviewerError(RuntimeError):
    """Safe diagnostic: never include a connection string or subprocess output."""


def clean_env(**extra: str) -> dict[str, str]:
    """Child services cannot inherit live-provider, database, proxy or Python overrides."""
    prefixes = ("AGENT_FACTORY_", "AWS_", "ANTHROPIC_", "OPENAI_", "AZURE_", "PYTHON", "PIP_", "DOCKER_", "PG")
    forbidden = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "VIRTUAL_ENV", "NODE_OPTIONS", "NODE_PATH"}
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(prefixes) and key.upper() not in forbidden}
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull,
               PIP_DISABLE_PIP_VERSION_CHECK="1", NO_PROXY="127.0.0.1,localhost,::1", AWS_EC2_METADATA_DISABLED="true")
    env.update(extra)
    return env


def sanitize_process_environment() -> None:
    """Also constrain in-process libpq/product imports, not only subprocesses.

    libpq can use PGHOSTADDR, PGSERVICE, PGOPTIONS and similar inherited
    settings even when the explicit URL has a loopback hostname. Always clear
    those overrides before importing the driver or opening a product store.
    """
    environment = clean_env()
    os.environ.clear()
    os.environ.update(environment)


def write_private(path: Path, value: str, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL)
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def runtime_paths(root: Path = ROOT) -> dict[str, Path]:
    local = root / ".local" / "reviewer"
    return {"root": root, "local": local, "python": local / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
            "url": local / "database.url", "test_url": local / "test-database.url",
            "admin_url": local / "postgres-admin.url", "password": local / "postgres-admin.password",
            "issuer": local / "dev-issuer", "token": local / "token.txt", "logs": local / "logs"}


def dependency_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in (*REQUIREMENTS, "frontend/package.json", "frontend/package-lock.json"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def layout(paths: dict[str, Path]) -> dict:
    local = paths["local"]
    local.mkdir(parents=True, exist_ok=True)
    paths["logs"].mkdir(exist_ok=True)
    manifest = local / "instance.json"
    if not manifest.exists():
        identity = secrets.token_hex(8)
        # Workspaces are Git repositories and must be outside the source checkout.
        workspace = paths["root"].parent / f"agent-factory-reviewer-workspaces-{identity}"
        write_private(manifest, json.dumps({"instance": identity, "workspace": str(workspace), "ports": PORTS}, indent=2) + "\n")
    try:
        state = json.loads(manifest.read_text(encoding="utf-8"))
        if not re.fullmatch(r"[a-f0-9]{16}", state["instance"]):
            raise ValueError
        workspace = Path(state["workspace"]).resolve()
        root = paths["root"].resolve()
        expected_workspace = root.parent / f"agent-factory-reviewer-workspaces-{state['instance']}"
        if workspace != expected_workspace or workspace.is_relative_to(root):
            raise ValueError
        if state["ports"] != PORTS:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ReviewerError("Reviewer instance.json is invalid; preserve it and inspect the local setup.") from None
    state["name"] = f"agent-factory-postgres-reviewer-{state['instance']}"
    state["volume"] = state["name"] + "-data"
    state["network"] = "agent-factory-reviewer-" + state["instance"]
    return state


@contextmanager
def exclusive(paths: dict[str, Path]):
    """A process-lifetime lock; a crashed run does not leave a stale PID lock."""
    paths["local"].mkdir(parents=True, exist_ok=True)
    with (paths["local"] / "reviewer.lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ReviewerError("Another reviewer command is already running for this checkout.") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run(argv: list[str], paths: dict[str, Path], label: str, *, env: dict | None = None,
        cwd: Path | None = None, timeout: float = 120, allow_failure: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, cwd=cwd or paths["root"], env=env or clean_env(),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    # Application commands already redact credentials; Docker inspect output is
    # intentionally never logged because it can contain other environment values.
    if label and "inspect" not in argv:
        (paths["logs"] / f"{label}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode and not allow_failure:
        raise ReviewerError(f"{label or 'Command'} failed (exit {result.returncode}); inspect .local/reviewer/logs.")
    return result


def tools_on_path() -> tuple[str, str, str]:
    node, npm, docker = (shutil.which(name) for name in ("node", "npm.cmd" if os.name == "nt" else "npm", "docker"))
    if not all((node, npm, docker)):
        raise ReviewerError("Install Python 3.12, Node.js 24 (including npm), Docker Desktop/Engine and Git first.")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, env=clean_env(), timeout=15)
    if version.returncode or not re.fullmatch(r"v24\.\d+\.\d+\s*", version.stdout):
        raise ReviewerError("The reproducible reviewer environment requires Node.js 24 on PATH.")
    return node, npm, docker


def local_docker(docker: str, paths: dict[str, Path]) -> None:
    result = run([docker, "context", "inspect"], paths, "docker-context")
    try:
        endpoint = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith(("unix://", "npipe://")):
            raise ValueError
    except (ValueError, TypeError, IndexError, KeyError):
        raise ReviewerError("Reviewer setup requires a local Docker socket; remote Docker contexts are refused.") from None
    info = run([docker, "info", "--format", "{{.OSType}}"], paths, "docker-info")
    if info.stdout.strip() != "linux":
        raise ReviewerError("Reviewer PostgreSQL requires the Docker Linux-container engine.")


def setup(args, paths: dict[str, Path]) -> None:
    node, npm, docker = tools_on_path()
    local_docker(docker, paths)
    if not paths["python"].exists():
        run([sys.executable, "-m", "venv", str(paths["local"] / ".venv")], paths, "create-venv")
    config = (paths["local"] / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    if "include-system-site-packages = false" not in config:
        raise ReviewerError("Reviewer venv must be isolated from global packages; preserve and inspect the existing venv.")
    command = [str(paths["python"]), "-m", "pip", "install", "--no-input"]
    if args.offline:
        if not args.wheelhouse:
            raise ReviewerError("Offline setup requires --wheelhouse with the pinned Python wheels; npm must already be cached.")
        command += ["--no-index", "--find-links", str(Path(args.wheelhouse).resolve())]
    for name in REQUIREMENTS:
        command += ["-r", name]
    print("Installing pinned Python and frontend dependencies into the reviewer environment...")
    run(command, paths, "pip-install", timeout=900)
    run([str(paths["python"]), "-m", "pip", "check"], paths, "pip-check")
    npm_command = [npm, "ci", "--no-audit", "--no-fund"] + (["--offline"] if args.offline else [])
    run(npm_command, paths, "npm-ci", cwd=paths["root"] / "frontend", timeout=900)
    run([npm, "run", "build"], paths, "frontend-build", cwd=paths["root"] / "frontend", timeout=300)
    image = run([docker, "image", "inspect", IMAGE], paths, "image-inspect", allow_failure=True)
    if image.returncode:
        if args.offline:
            raise ReviewerError("Pinned PostgreSQL image is not cached; offline setup cannot pull it.")
        run([docker, "pull", IMAGE], paths, "postgres-pull", timeout=900)
    write_private(paths["local"] / "setup.json", json.dumps({"dependencies": dependency_digest(paths["root"]),
                  "python": "3.12", "node": "24"}) + "\n", replace=True)
    print("Setup complete. Start with: python scripts/reviewer.py up")


def require_setup(paths: dict[str, Path]) -> None:
    try:
        ready = json.loads((paths["local"] / "setup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReviewerError("Run python scripts/reviewer.py setup first (or setup --offline --wheelhouse DIR).") from None
    if ready.get("dependencies") != dependency_digest(paths["root"]) or not paths["python"].is_file():
        raise ReviewerError("Dependencies changed or the reviewer venv is missing; run reviewer.py setup again.")
    config = (paths["local"] / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    if "include-system-site-packages = false" not in config:
        raise ReviewerError("Reviewer venv is not isolated; preserve it and inspect the local setup.")
    if not (paths["root"] / "frontend" / "node_modules" / "vite" / "bin" / "vite.js").is_file():
        raise ReviewerError("Frontend dependencies are missing; run reviewer.py setup again.")


def port_available(port: int) -> None:
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise ReviewerError(f"Loopback port {port} is occupied; stop its owner explicitly before retrying.") from None


def assert_owned(value: dict, state: dict, *, container: bool = False) -> None:
    labels = value.get("Config", {}).get("Labels", {}) if container else value.get("Labels", {})
    if not labels or labels.get(LABEL) != state["instance"] or labels.get("agent-factory.service") != "postgres-reviewer":
        raise ReviewerError("A Docker resource with the reviewer name has different ownership labels; refusing to change it.")


def inspect(docker: str, kind: str, name: str, paths: dict[str, Path]) -> dict | None:
    result = run([docker, kind, "inspect", name], paths, "resource-inspect", allow_failure=True)
    if result.returncode:
        # A missing resource is the only accepted failure. Daemon failures must
        # not be mistaken for permission to create a replacement resource.
        missing = ("no such", "not found", "does not exist")
        if any(marker in result.stderr.lower() for marker in missing):
            return None
        raise ReviewerError("Docker resource inspection failed; no resources were changed.")
    try:
        return json.loads(result.stdout)[0]
    except (ValueError, IndexError, TypeError):
        raise ReviewerError("Docker returned invalid resource metadata.") from None


def ensure_secrets(paths: dict[str, Path]) -> None:
    files = [paths[key] for key in ("password", "admin_url", "url", "test_url")]
    present = [path.exists() for path in files]
    if any(present):
        if not all(present):
            raise ReviewerError("Reviewer secret files are incomplete; refusing to overwrite or regenerate credentials.")
        return
    admin, app = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    write_private(paths["password"], admin + "\n")
    write_private(paths["admin_url"], f"postgresql://af_admin:{admin}@127.0.0.1:{PORTS['postgres']}/postgres\n")
    for key, database in zip(("url", "test_url"), DATABASES, strict=True):
        write_private(paths[key], f"postgresql://{APP_ROLE}:{app}@127.0.0.1:{PORTS['postgres']}/{database}\n")


def database_container(paths: dict[str, Path], state: dict, docker: str) -> None:
    local_docker(docker, paths)
    existing = inspect(docker, "container", state["name"], paths)
    volume = inspect(docker, "volume", state["volume"], paths)
    network = inspect(docker, "network", state["network"], paths)
    for value in (volume, network):
        if value is not None:
            assert_owned(value, state)
    if existing is not None:
        assert_owned(existing, state, container=True)
        bindings = existing.get("HostConfig", {}).get("PortBindings", {})
        expected = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(PORTS["postgres"])}]}
        mounts = existing.get("Mounts", [])
        networks = existing.get("NetworkSettings", {}).get("Networks", {})
        if (existing.get("Config", {}).get("Image") != IMAGE or bindings != expected or
                set(networks) != {state["network"]} or volume is None or network is None or
                not any(m.get("Type") == "volume" and m.get("Name") == state["volume"] and
                        m.get("Destination") == "/var/lib/postgresql/data" for m in mounts)):
            raise ReviewerError("Reviewer container configuration differs from the expected image, loopback port or data volume.")
        if not all(paths[key].is_file() for key in ("password", "admin_url", "url", "test_url")):
            raise ReviewerError("Existing reviewer database is missing its credential files; refusing to regenerate them.")
        if existing.get("State", {}).get("Running"):
            return
        port_available(PORTS["postgres"])
        run([docker, "start", state["name"]], paths, "postgres-start")
        return
    port_available(PORTS["postgres"])
    # A retained volume needs its original credentials. Never initialize over it.
    if volume is not None and not all(paths[key].is_file() for key in ("password", "admin_url", "url", "test_url")):
        raise ReviewerError("Retained reviewer volume has no complete local credentials; refusing replacement credentials.")
    ensure_secrets(paths)
    labels = ["--label", f"{LABEL}={state['instance']}", "--label", "agent-factory.service=postgres-reviewer"]
    if volume is None:
        run([docker, "volume", "create", *labels, state["volume"]], paths, "volume-create")
    if network is None:
        run([docker, "network", "create", *labels, state["network"]], paths, "network-create")
    if "," in str(paths["password"]):
        raise ReviewerError("The reviewer checkout path cannot contain a comma (Docker bind-mount syntax).")
    run([docker, "run", "-d", "--name", state["name"], *labels, "--pull", "never", "--network", state["network"],
         "-p", f"127.0.0.1:{PORTS['postgres']}:5432", "--env", "POSTGRES_USER=af_admin", "--env", "POSTGRES_DB=postgres",
         "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-admin",
         "--mount", f"type=bind,source={paths['password']},target=/run/secrets/postgres-admin,readonly",
         "--mount", f"type=volume,source={state['volume']},target=/var/lib/postgresql/data", IMAGE], paths, "postgres-create")


def provision(paths: dict[str, Path]) -> None:
    urls = {key: paths[key].read_text(encoding="utf-8").strip() for key in ("admin_url", "url", "test_url")}
    try:
        parts = {key: urlsplit(url) for key, url in urls.items()}
        for key, database in (("admin_url", "postgres"), ("url", DATABASES[0]), ("test_url", DATABASES[1])):
            value = parts[key]
            # libpq query options can override the apparent URL host/port.
            # Reviewer-generated URLs need no query options or fragments.
            if value.scheme != "postgresql" or value.query or value.fragment:
                raise ReviewerError("Reviewer database URLs must use postgresql with no query options or fragments.")
            if value.hostname != "127.0.0.1" or value.port != PORTS["postgres"] or value.path != "/" + database:
                raise ReviewerError("Reviewer database files target an unexpected host, port or database.")
            if value.username != ("af_admin" if key == "admin_url" else APP_ROLE) or not value.password:
                raise ReviewerError("Reviewer database files have unexpected roles or missing passwords.")
    except ValueError:
        raise ReviewerError("Reviewer database URL files are malformed; no connection was attempted.") from None
    if parts["url"].password != parts["test_url"].password:
        raise ReviewerError("Reviewer application credentials do not match each other.")
    # Import the driver only after every file has passed the target checks.
    import psycopg
    from psycopg import sql

    deadline = time.monotonic() + 60
    while True:
        try:
            connection = psycopg.connect(urls["admin_url"], autocommit=True, connect_timeout=3)
            break
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise ReviewerError("Reviewer PostgreSQL did not become available within 60 seconds.") from None
            time.sleep(0.5)
    try:
        with connection as admin:
            role = sql.Identifier(APP_ROLE)
            exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone()
            verb = sql.SQL("ALTER ROLE {} WITH" if exists else "CREATE ROLE {} WITH").format(role)
            admin.execute(sql.SQL("{} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}")
                          .format(verb, sql.Literal(parts["url"].password)))
            for database in DATABASES:
                if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
                    admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), role))
                admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
    except psycopg.Error:
        raise ReviewerError("Reviewer PostgreSQL provisioning failed; credentials were not printed.") from None


def database_env(paths: dict[str, Path]) -> dict[str, str]:
    return clean_env(AGENT_FACTORY_DATABASE_URL_FILE=str(paths["url"]),
                     AGENT_FACTORY_DATABASE_SCHEMA="agent_factory",
                     AGENT_FACTORY_TEST_DATABASE_URL_FILE=str(paths["test_url"]))


def cli(paths: dict[str, Path], *args: str, env: dict | None = None, label: str = "cli"):
    return run([str(paths["python"]), "-m", "agent_factory", *args], paths, label, env=env or database_env(paths))


def ensure_database(paths: dict[str, Path], state: dict, docker: str) -> None:
    database_container(paths, state, docker)
    provision(paths)
    # Explicit bootstrap step, before services: the application still never migrates on startup.
    cli(paths, "db", "migrate", label="database-migrate")
    cli(paths, "db", "check", label="database-check")


def identity(paths: dict[str, Path]) -> dict[str, str]:
    from agent_factory.api.auth.development import DevelopmentIssuer
    from agent_factory.persistence.config import DatabaseConfig
    from agent_factory.persistence.postgres import open_postgres_store

    env = database_env(paths)
    env.update(AGENT_FACTORY_AUTH_MODE="development", AGENT_FACTORY_AUTH_DEV_ISSUER_DIR=str(paths["issuer"]))
    if not paths["issuer"].exists():
        cli(paths, "auth", "dev-issuer", "init", "--dir", str(paths["issuer"]), env=env, label="issuer-init")
    else:
        DevelopmentIssuer.load(paths["issuer"])  # Fail closed if key files are incomplete.
    store = open_postgres_store(DatabaseConfig.from_environment(env))
    try:
        principal = store.principals.find(ISSUER, SUBJECT)
        if principal is not None and not principal.active:
            raise ReviewerError("Reviewer principal was disabled; refusing to re-enable it automatically.")
    finally:
        store.close()
    if principal is None:
        cli(paths, "auth", "principal", "add", "--issuer", ISSUER, "--subject", SUBJECT,
            "--display-name", "Zoomcamp reviewer", "--grant-all", label="principal-enroll")
    cli(paths, "auth", "dev-issuer", "token", "--dir", str(paths["issuer"]), "--subject", SUBJECT,
        "--minutes", "180", "--out", str(paths["token"]), env=env, label="token-refresh")
    return env


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(port: int, path: str, *, token: str | None = None, body: dict | None = None, key: str | None = None):
    if not path.startswith("/") or path.startswith("//"):
        raise ReviewerError("Reviewer requests must use an absolute local path.")
    # Vite's SPA fallback serves HTML only when the caller accepts text/html.
    # API calls still prefer JSON and are decoded by their response content type.
    headers = {"Accept": "application/json, text/html"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(Request(f"http://127.0.0.1:{port}" + path, data=data, headers=headers), timeout=3) as response:
            content = response.read(2_000_000)
            return response.status, json.loads(content) if "json" in response.headers.get("Content-Type", "") else None
    except HTTPError as error:
        return error.code, None
    except (URLError, TimeoutError, ConnectionError):
        return None, None


def seed_project(paths: dict[str, Path], state: dict, token: str) -> str:
    saved = paths["local"] / "project.json"
    if saved.exists():
        try:
            project = json.loads(saved.read_text(encoding="utf-8"))["id"]
        except (OSError, ValueError, KeyError, TypeError):
            raise ReviewerError("Saved reviewer project identity is invalid; refusing to replace it.") from None
        if not isinstance(project, str) or not re.fullmatch(r"prj-[a-f0-9]{20}", project):
            raise ReviewerError("Saved reviewer project identity is invalid.")
        status, _ = http(PORTS["api"], f"/api/v1/projects/{project}", token=token)
        if status != 200:
            raise ReviewerError("The saved reviewer project cannot be read; refusing to create a duplicate.")
        return project
    status, body = http(PORTS["api"], "/api/v1/projects", token=token,
                        body={"name": "Zoomcamp review", "template": "calculator", "description": "Local deterministic approval workflow."},
                        key="reviewer-" + state["instance"])
    if status != 201 or not isinstance(body, dict) or not re.fullmatch(r"prj-[a-f0-9]{20}", body.get("id", "")):
        raise ReviewerError("Authenticated reviewer project creation failed.")
    write_private(saved, json.dumps({"id": body["id"]}) + "\n")
    return body["id"]


def ensure_run(paths: dict[str, Path], state: dict, token: str, project: str) -> str:
    """Reuse every existing state, including terminal states, without requeueing it."""
    run_id = "review-approval"
    existing, _ = http(PORTS["api"], f"/api/v1/projects/{project}/runs/{run_id}", token=token)
    if existing == 404:
        cli(paths, "worker", "enqueue", "--project-id", project, "--run-id", run_id, "--scenario", "approval",
            "--workspace-base", state["workspace"], "--no-git", label="enqueue")
    elif existing != 200:
        raise ReviewerError("Cannot inspect the reviewer run; no new run was queued.")
    return run_id


class Services:
    def __init__(self, paths: dict[str, Path]):
        self.paths = paths
        self.children: list[tuple[str, subprocess.Popen, object]] = []

    def start(self, name: str, argv: list[str], env: dict, cwd: Path | None = None) -> None:
        output = (self.paths["logs"] / f"{name}.log").open("w", encoding="utf-8")
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            child = subprocess.Popen(argv, cwd=cwd or self.paths["root"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, **options)
        except OSError:
            output.close()
            raise
        self.children.append((name, child, output))

    def healthy(self) -> None:
        for name, child, _ in self.children:
            if child.poll() is not None:
                raise ReviewerError(f"Reviewer {name} exited; inspect .local/reviewer/logs/{name}.log.")

    def wait_http(self, port: int, path: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self.healthy()
            if http(port, path)[0] == 200:
                return
            time.sleep(0.25)
        raise ReviewerError("A reviewer service did not become ready within 60 seconds.")

    def close(self) -> None:
        failed = []
        for name, child, output in reversed(self.children):
            try:
                if child.poll() is None:
                    if os.name == "nt":
                        # Terminate only this Popen's process tree, including a
                        # maintainer-authored fixture test if one is in flight.
                        result = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                env=clean_env(), timeout=15,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                        if result.returncode and child.poll() is None:
                            child.terminate()
                    else:
                        try:
                            os.killpg(child.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass  # It exited between poll and the group signal.
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            child.kill()
                        else:
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                # Cleanup must visit every owned child even if one disappeared,
                # taskkill timed out, or the OS refused a termination request.
                # Preserve only its safe service name, not exception details.
                failed.append(name)
                try:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Reported after all other children are visited.
            finally:
                try:
                    output.close()
                except OSError:
                    failed.append(name)
        if failed:
            names = ", ".join(sorted(set(failed)))
            raise ReviewerError(f"Reviewer cleanup encountered an error for: {names}. Cleanup was attempted for every owned service.")


def up(args, paths: dict[str, Path], state: dict) -> None:
    node, npm, docker = tools_on_path()
    port_available(PORTS["api"])
    port_available(PORTS["frontend"])
    ensure_database(paths, state, docker)
    env = identity(paths)
    run([npm, "run", "build"], paths, "frontend-build", cwd=paths["root"] / "frontend", timeout=300)
    services = Services(paths)
    try:
        services.start("api", [str(paths["python"]), "-m", "agent_factory", "api", "serve", "--host", "127.0.0.1",
                              "--port", str(PORTS["api"])], env)
        services.wait_http(PORTS["api"], "/readyz")
        token = paths["token"].read_text(encoding="utf-8").strip()
        if http(PORTS["api"], "/api/v1/projects")[0] != 401:
            raise ReviewerError("Authentication smoke check failed: anonymous project access was not refused.")
        project = seed_project(paths, state, token)
        run_id = ensure_run(paths, state, token, project)
        services.start("worker", [str(paths["python"]), "-m", "agent_factory", "worker", "run",
                                  "--workspace-base", state["workspace"], "--poll", "0.5"], database_env(paths))
        services.start("frontend", [node, str(paths["root"] / "frontend/node_modules/vite/bin/vite.js"), "preview",
                                    "--host", "127.0.0.1", "--port", str(PORTS["frontend"]), "--strictPort"],
                       clean_env(AGENT_FACTORY_API_ORIGIN=f"http://127.0.0.1:{PORTS['api']}"), paths["root"] / "frontend")
        services.wait_http(PORTS["frontend"], "/")
        if http(PORTS["frontend"], "/api/v1/projects", token=token)[0] != 200:
            raise ReviewerError("The frontend same-origin authenticated API proxy did not respond correctly.")
        print(f"Dashboard: http://127.0.0.1:{PORTS['frontend']}")
        print(f"Development token file (expires in 3 hours): {paths['token']}")
        print("Open the token file locally and paste its contents into Sign in. Tokens are never printed here.")
        print(f"Project: {project}; deterministic fake provider; PostgreSQL and workspaces are retained.")
        if args.command == "smoke":
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                services.healthy()
                status, body = http(PORTS["api"], f"/api/v1/projects/{project}/runs/{run_id}", token=token)
                if status == 200 and body.get("status") in {"awaiting_approval", "completed"}:
                    print(f"Smoke passed: authenticated UI proxy, anonymous rejection, durable run {body['status']}.")
                    return
                if status == 200 and body.get("status") in {"failed", "blocked", "exhausted"}:
                    raise ReviewerError(f"Reviewer smoke run stopped with status {body['status']}.")
                time.sleep(0.25)
            raise ReviewerError("Reviewer run did not reach its approval pause within 90 seconds.")
        print("Press Ctrl+C to stop these services. The reviewer database container remains running.")
        while True:
            services.healthy()
            time.sleep(0.5)
    finally:
        services.close()


def check(args, paths: dict[str, Path], state: dict) -> None:
    _, npm, docker = tools_on_path()
    ensure_database(paths, state, docker)
    env = database_env(paths)
    command = [str(paths["python"]), "-m", "pytest"]
    jobs = [(command, "tests-default", paths["root"]),
            (command + ["-m", "postgres and not docker_live and not browser", "--run-postgres"], "tests-postgres", paths["root"]),
            ([npm, "run", "check"], "frontend-check", paths["root"] / "frontend"),
            ([npm, "run", "build"], "frontend-build", paths["root"] / "frontend")]
    if args.browser:
        jobs.append((command + ["-m", "browser and not docker_live", "--run-browser", "--run-postgres"], "tests-browser", paths["root"]))
    for argv, label, cwd in jobs:
        print(f"Running {label}...")
        result = run(argv, paths, label, env=env, cwd=cwd, timeout=1800)
        # Print only the terminal summary, never arbitrary captured output.
        summary = [line for line in result.stdout.splitlines() if re.search(r"\b\d+ passed\b|Test Files|Tests ", line)]
        print(summary[-1] if summary else f"{label}: passed; details in .local/reviewer/logs/{label}.log")
    print("Reviewer checks passed. Docker live/adversarial/outage and live-provider suites remain separate opt-ins.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("setup", help="install pinned dependencies and cache the PostgreSQL image")
    bootstrap.add_argument("--offline", action="store_true", help="use a Python wheelhouse, npm cache and already-cached image")
    bootstrap.add_argument("--wheelhouse")
    sub.add_parser("up", help="start the local reviewer stack in the foreground")
    sub.add_parser("smoke", help="check the stack and approval pause, then stop only child services")
    checks = sub.add_parser("check", help="run offline, database and frontend checks")
    checks.add_argument("--browser", action="store_true", help="also use installed Google Chrome for browser checks")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        parser.error("Use Python 3.12 for the reviewer environment.")
    sanitize_process_environment()
    paths = runtime_paths()
    try:
        if args.command != "setup":
            require_setup(paths)
            if Path(sys.prefix).resolve() != (paths["local"] / ".venv").resolve():
                return subprocess.call([str(paths["python"]), str(Path(__file__).resolve()), *(argv or sys.argv[1:])],
                                       cwd=ROOT, env=clean_env())
        # Makes local product modules importable when this script is the entry point.
        sys.path.insert(0, str(ROOT))
        with exclusive(paths):
            state = layout(paths)
            if args.command == "setup":
                setup(args, paths)
            elif args.command == "check":
                check(args, paths, state)
            else:
                up(args, paths, state)
        return 0
    except KeyboardInterrupt:
        print("Reviewer services stopped. Database, volume, credentials and workspaces were retained.")
        return 0
    except (ReviewerError, OSError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, ReviewerError) else type(error).__name__
        print(f"Reviewer setup stopped: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

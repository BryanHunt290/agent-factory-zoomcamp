"""Compose-only local reviewer lifecycle; no public identity or live provider.

The separate prepare command binds four named volumes before PostgreSQL starts.
The application command explicitly provisions/migrates, then supervises its own
API, worker and frontend children. It never invokes Docker or removes data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import reviewer

ROOT = Path(__file__).resolve().parent.parent
UID = GID = 10001
MARKER = ".agent-factory-compose.json"
DATABASE = "agent_factory_compose"
ROLE = "agent_factory_app"
SECRET_FILES = ("postgres-admin.password", "app.password")


def volume_roots() -> dict[str, Path]:
    return {"state": Path("/state"), "workspaces": Path("/workspaces"),
            "credentials": Path("/credentials"), "database": Path("/database")}


def runtime_paths() -> dict[str, Path]:
    state = Path("/state")
    return {"root": ROOT, "local": state, "python": Path(sys.executable),
            "url": state / "database.url", "issuer": state / "dev-issuer",
            "token": state / "token.txt", "logs": state / "logs"}


def validate_owner(project: str, roots: dict[str, Path]) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise reviewer.ReviewerError("Compose project name is missing or invalid.")
    records = []
    for path in roots.values():
        marker = path / MARKER
        if marker.is_symlink() or not marker.is_file():
            raise reviewer.ReviewerError("Volume ownership is incomplete; preserve all volumes for inspection.")
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise reviewer.ReviewerError("Volume ownership record is invalid.") from None
        if (not isinstance(record, dict) or record.get("project") != project
                or not re.fullmatch(r"[a-f0-9]{32}", str(record.get("instance", "")))
                or record.get("version") != 1):
            raise reviewer.ReviewerError("Volume belongs to another instance or has invalid ownership.")
        records.append(record)
    if any(record != records[0] for record in records[1:]):
        raise reviewer.ReviewerError("Named volumes do not belong to the same instance.")
    return records[0]


def load_passwords(roots: dict[str, Path]) -> tuple[str, str]:
    passwords = []
    for name in SECRET_FILES:
        path = roots["credentials"] / name
        if path.is_symlink() or not path.is_file():
            raise reviewer.ReviewerError("Retained credentials are incomplete; no passwords were regenerated.")
        password = path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", password):
            raise reviewer.ReviewerError("Retained credentials are invalid; no passwords were changed.")
        passwords.append(password)
    return tuple(passwords)


def prepare(project: str, roots: dict[str, Path]) -> None:
    """Only empty volumes may be initialized. Partial/retained state is never reset."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise reviewer.ReviewerError("Compose project name is missing or invalid.")
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    markers = [(path / MARKER).exists() for path in roots.values()]
    if any(markers):
        validate_owner(project, roots)
        load_passwords(roots)
        print("Existing Compose volume ownership and credentials verified; data retained.")
        return
    if any(any(path.iterdir()) for path in roots.values()):
        raise reviewer.ReviewerError("Refusing nonempty, unowned volumes; existing data was not changed.")
    record = json.dumps({"version": 1, "project": project, "instance": secrets.token_hex(16)}) + "\n"
    for path in roots.values():
        reviewer.write_private(path / MARKER, record)
        os.chown(path / MARKER, UID, GID)
    for name in SECRET_FILES:
        path = roots["credentials"] / name
        reviewer.write_private(path, secrets.token_urlsafe(32) + "\n")
        os.chown(path, UID, GID)
    # The official postgres entrypoint initializes its own pgdata subdirectory.
    # It reads POSTGRES_PASSWORD_FILE as root before switching to postgres.
    for name in ("state", "workspaces", "credentials"):
        os.chown(roots[name], UID, GID)
        roots[name].chmod(0o700)
    print("Compose volumes initialized; credentials remain in private files.")


def database_env(paths: dict[str, Path]) -> dict[str, str]:
    return reviewer.clean_env(AGENT_FACTORY_DATABASE_URL_FILE=str(paths["url"]),
                              AGENT_FACTORY_DATABASE_SCHEMA="agent_factory")


def cli(paths: dict[str, Path], *args: str, env: dict | None = None, label: str = "bootstrap"):
    return reviewer.run([sys.executable, "-m", "agent_factory", *args], paths,
                        label, env=env or database_env(paths))


def provision(paths: dict[str, Path], roots: dict[str, Path]) -> None:
    """Only the internal Compose database and generated credentials are accepted."""
    admin_password, app_password = load_passwords(roots)
    expected = f"postgresql://{ROLE}:{app_password}@db:5432/{DATABASE}\n"
    if paths["url"].exists():
        if paths["url"].is_symlink() or paths["url"].read_text(encoding="utf-8") != expected:
            raise reviewer.ReviewerError("Database configuration differs from this Compose instance.")
    else:
        reviewer.write_private(paths["url"], expected)
    import psycopg
    from psycopg import sql

    try:
        with psycopg.connect(host="db", port=5432, dbname="postgres", user="af_admin",
                             password=admin_password, connect_timeout=10, autocommit=True) as conn:
            existing = conn.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
                                    "FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone()
            if existing is None:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                     "NOREPLICATION NOBYPASSRLS PASSWORD {}")
                             .format(sql.Identifier(ROLE), sql.Literal(app_password)))
            elif tuple(existing) != (False, False, False, False, False, True):
                raise reviewer.ReviewerError("Existing application database role has unexpected privileges.")
            owner = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone()
            if owner is None:
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(DATABASE), sql.Identifier(ROLE)))
            elif owner[0] != ROLE:
                raise reviewer.ReviewerError("Existing application database has a different owner.")
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(DATABASE)))
    except psycopg.Error:
        raise reviewer.ReviewerError("Compose database provisioning failed; credentials were not printed or replaced.") from None
    # Bootstrap is explicit and finishes before any application server starts.
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
        DevelopmentIssuer.load(paths["issuer"])
    store = open_postgres_store(DatabaseConfig.from_environment(env))
    try:
        principal = store.principals.find(reviewer.ISSUER, reviewer.SUBJECT)
        if principal is not None and not principal.active:
            raise reviewer.ReviewerError("Reviewer principal is disabled; it was not re-enabled.")
    finally:
        store.close()
    if principal is None:
        cli(paths, "auth", "principal", "add", "--issuer", reviewer.ISSUER, "--subject", reviewer.SUBJECT,
            "--display-name", "Zoomcamp reviewer", "--grant-all", label="principal-enroll")
    cli(paths, "auth", "dev-issuer", "token", "--dir", str(paths["issuer"]), "--subject", reviewer.SUBJECT,
        "--minutes", "180", "--out", str(paths["token"]), env=env, label="token-refresh")
    return env


def ensure_run(paths: dict[str, Path], workspace: Path, token: str, project: str) -> str:
    run_id = "review-approval"
    status, _ = reviewer.http(reviewer.PORTS["api"], f"/api/v1/projects/{project}/runs/{run_id}", token=token)
    if status == 404:
        cli(paths, "worker", "enqueue", "--project-id", project, "--run-id", run_id, "--scenario", "approval",
            "--workspace-base", str(workspace), "--no-git", label="enqueue")
    elif status != 200:
        raise reviewer.ReviewerError("Cannot inspect the saved run; no replacement was queued.")
    return run_id


def health(paths: dict[str, Path]) -> bool:
    try:
        ready = json.loads((paths["local"] / "ready.json").read_text(encoding="utf-8"))
        if not isinstance(ready, dict) or set(ready) != {"api", "worker", "frontend"}:
            return False
        for pid in ready.values():
            if type(pid) is not int or pid <= 1:
                return False
            os.kill(pid, 0)
    except (OSError, ValueError, TypeError):
        return False
    return (reviewer.http(reviewer.PORTS["api"], "/readyz")[0] == 200
            and reviewer.http(reviewer.PORTS["frontend"], "/")[0] == 200
            and reviewer.http(reviewer.PORTS["frontend"], "/api/v1/projects")[0] == 401)


def serve(project: str, roots: dict[str, Path]) -> None:
    paths = runtime_paths()
    ready = paths["local"] / "ready.json"
    # prepare verifies all four volumes before the database can start. The app
    # needs only its own state/workspace/credential mounts, never PGDATA access.
    app_roots = {name: path for name, path in roots.items() if name != "database"}
    state = validate_owner(project, app_roots)
    with reviewer.exclusive(paths):
        paths["logs"].mkdir(exist_ok=True)
        ready.unlink(missing_ok=True)
        if (paths["local"] / "project.json").exists() and not paths["issuer"].exists():
            raise reviewer.ReviewerError("The retained project is missing its signing identity; it was not replaced.")
        provision(paths, roots)
        env = identity(paths)
        services = reviewer.Services(paths)
        try:
            services.start("api", [sys.executable, "-m", "agent_factory", "api", "serve", "--host", "127.0.0.1",
                                   "--port", str(reviewer.PORTS["api"])], env)
            services.wait_http(reviewer.PORTS["api"], "/readyz")
            token = paths["token"].read_text(encoding="utf-8").strip()
            if reviewer.http(reviewer.PORTS["api"], "/api/v1/projects")[0] != 401:
                raise reviewer.ReviewerError("Anonymous product access was not refused.")
            project_id = reviewer.seed_project(paths, state, token)
            ensure_run(paths, roots["workspaces"], token, project_id)
            services.start("worker", [sys.executable, "-m", "agent_factory", "worker", "run",
                                      "--workspace-base", str(roots["workspaces"]), "--poll", "0.5"], database_env(paths))
            services.start("frontend", ["node", str(ROOT / "frontend/node_modules/vite/bin/vite.js"), "preview",
                                        "--configLoader", "native",
                                        "--host", "0.0.0.0", "--port", str(reviewer.PORTS["frontend"]), "--strictPort"],
                           reviewer.clean_env(AGENT_FACTORY_API_ORIGIN=f"http://127.0.0.1:{reviewer.PORTS['api']}"), ROOT / "frontend")
            services.wait_http(reviewer.PORTS["frontend"], "/")
            if reviewer.http(reviewer.PORTS["frontend"], "/api/v1/projects", token=token)[0] != 200:
                raise reviewer.ReviewerError("Authenticated frontend proxy did not become ready.")
            reviewer.write_private(ready, json.dumps({name: child.pid for name, child, _ in services.children}) + "\n")
            print("Compose reviewer ready. Copy /state/token.txt privately for sign-in; token expires in 3 hours.", flush=True)
            print("Deterministic fake provider only; untrusted execution requires an unavailable Docker sandbox.", flush=True)
            while True:
                services.healthy()
                time.sleep(0.5)
        finally:
            try:
                ready.unlink(missing_ok=True)
            finally:
                services.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "serve", "health", "token"))
    args = parser.parse_args(argv)
    project = os.environ.get("AF_COMPOSE_PROJECT", "")
    reviewer.sanitize_process_environment()
    sys.path.insert(0, str(ROOT))
    roots = volume_roots()
    paths = runtime_paths()
    try:
        if args.command == "health":
            return 0 if health(paths) else 1
        if args.command == "prepare":
            prepare(project, roots)
        elif args.command == "token":
            validate_owner(project, {name: path for name, path in roots.items() if name != "database"})
            if not paths["issuer"].exists():
                raise reviewer.ReviewerError("The signing identity is missing; token refresh cannot replace it.")
            identity(paths)
            print("Development token refreshed in /state/token.txt; copy it privately for sign-in.")
        else:
            def stop(_signum, _frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            serve(project, roots)
        return 0
    except KeyboardInterrupt:
        print("Compose application stopped; named-volume data and credentials retained.", flush=True)
        return 0
    except (reviewer.ReviewerError, OSError, ValueError, subprocess.SubprocessError) as error:
        safe = str(error) if isinstance(error, reviewer.ReviewerError) else type(error).__name__
        print("Compose startup stopped: " + safe, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

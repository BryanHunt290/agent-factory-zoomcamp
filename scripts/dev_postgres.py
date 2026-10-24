"""Local development PostgreSQL for Agent Factory: secrets and provisioning.

This is a developer helper, not part of Agent Factory. It prepares an isolated
local database server for development and for the PostgreSQL integration
tests. The full procedure is in docs/database.md:

    py -3.12 scripts/dev_postgres.py secrets                 # 1. generate .local/ files
    docker network create agent-factory-dev                  # 2. human-run docker commands
    docker run ... postgres:17@sha256:...                    #    (see docs/database.md)
    .venv\\Scripts\\python scripts/dev_postgres.py provision   # 3. app role + databases

Passwords are generated here and written only to git-ignored files under
``.local/``. They are never printed and never placed on a command line.
Agent Factory itself connects as ``agent_factory_app``, a role that is not a
superuser and owns only the two Agent Factory databases.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
HOST, PORT = "127.0.0.1", 55432
ADMIN_USER = "af_admin"
APP_ROLE = "agent_factory_app"
DEV_DB, TEST_DB = "agent_factory_dev", "agent_factory_test"

ADMIN_ENV = LOCAL / "postgres-admin.env"  # for `docker run --env-file`
ADMIN_URL = LOCAL / "postgres-admin.url"  # provisioning only
DEV_URL = LOCAL / "database.url"  # AGENT_FACTORY_DATABASE_URL_FILE
TEST_URL = LOCAL / "test-database.url"  # AGENT_FACTORY_TEST_DATABASE_URL_FILE


def _write_new(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as fh:  # "x": never overwrite
        fh.write(text)


def cmd_secrets(_: argparse.Namespace) -> int:
    existing = [p.name for p in (ADMIN_ENV, ADMIN_URL, DEV_URL, TEST_URL) if p.exists()]
    if existing:
        print(f"refusing to overwrite existing files in .local/: {existing}", file=sys.stderr)
        return 1
    LOCAL.mkdir(exist_ok=True)
    admin_pw, app_pw = secrets.token_urlsafe(32), secrets.token_urlsafe(32)  # URL-safe: no escaping needed
    _write_new(ADMIN_ENV, f"POSTGRES_USER={ADMIN_USER}\nPOSTGRES_PASSWORD={admin_pw}\nPOSTGRES_DB=postgres\n")
    _write_new(ADMIN_URL, f"postgresql://{ADMIN_USER}:{admin_pw}@{HOST}:{PORT}/postgres\n")
    _write_new(DEV_URL, f"postgresql://{APP_ROLE}:{app_pw}@{HOST}:{PORT}/{DEV_DB}\n")
    _write_new(TEST_URL, f"postgresql://{APP_ROLE}:{app_pw}@{HOST}:{PORT}/{TEST_DB}\n")
    print(f"wrote {ADMIN_ENV.name}, {ADMIN_URL.name}, {DEV_URL.name}, {TEST_URL.name} under .local/ (git-ignored)")
    return 0


def _connect_with_retry(url: str, timeout_s: float = 60.0):
    import psycopg

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return psycopg.connect(url, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.0)


def cmd_provision(_: argparse.Namespace) -> int:
    from psycopg import sql

    admin_url = ADMIN_URL.read_text(encoding="utf-8").strip()
    app = urlsplit(DEV_URL.read_text(encoding="utf-8").strip())
    test = urlsplit(TEST_URL.read_text(encoding="utf-8").strip())
    if app.username != APP_ROLE or test.username != APP_ROLE or app.password != test.password:
        print("the .local/ URL files do not match each other; regenerate them", file=sys.stderr)
        return 1
    with _connect_with_retry(admin_url) as admin:
        role = sql.Identifier(APP_ROLE)
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone()
        verb = sql.SQL("ALTER ROLE {} WITH" if exists else "CREATE ROLE {} WITH").format(role)
        admin.execute(
            sql.SQL("{} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(
                verb, sql.Literal(app.password)
            )
        )
        for name in (DEV_DB, TEST_DB):
            if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), role))
            # Only the owning role (and the server administrator) may connect.
            admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
        version = admin.execute("SHOW server_version").fetchone()[0]
    print(f"provisioned role {APP_ROLE} (not a superuser) and databases {DEV_DB}, {TEST_DB} on PostgreSQL {version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("secrets", help="generate .local/ secret files (never overwrites)").set_defaults(func=cmd_secrets)
    sub.add_parser("provision", help="create the app role and databases (idempotent)").set_defaults(func=cmd_provision)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

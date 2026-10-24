"""Versioned schema migrations for the PostgreSQL product store.

A deliberately small runner. The project has no ORM, and every schema change
is plain SQL; docs/database.md#migrations explains why this is not Alembic.

* Migrations are SQL files ``NNNN_name.sql`` in
  ``agent_factory/persistence/migrations``, numbered 1, 2, 3, ... with no gaps.
* ``<schema>.schema_migrations`` records each applied version, its name, the
  SHA-256 of its file, and when it was applied.
* :func:`migrate` applies pending migrations in order. Each one runs in its own
  transaction together with its tracking row. PostgreSQL DDL is
  transactional, so a failed migration leaves nothing behind. A session
  advisory lock keeps two migrators from interleaving.
* It refuses to run if the database records a migration this code does not
  have (the database is newer), or one whose file has changed since it was
  applied. History is never rewritten: schema changes are new files.

Agent Factory never migrates at start-up. ``python -m agent_factory db migrate``
is an explicit step; opening a store only checks that the schema is current.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from ..domain.errors import SchemaNotReadyError, StorageError
from ..domain.serialization import format_timestamp

PACKAGE = "agent_factory.persistence.migrations"
FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
TRACKING_TABLE = "schema_migrations"


class MigrationError(StorageError):
    """Migrations cannot be applied safely: changed history, a newer database, or a failed step."""


# SchemaNotReadyError is defined in domain.errors, with the other storage errors, so code
# that only maps errors (the API) need not import the driver; it is re-exported from here.


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str


@dataclass(frozen=True)
class MigrationStatus:
    schema: str
    available: tuple[Migration, ...]
    applied: tuple[AppliedMigration, ...]

    @property
    def latest_version(self) -> int:
        return self.available[-1].version if self.available else 0

    @property
    def current_version(self) -> int:
        return max((a.version for a in self.applied), default=0)

    @property
    def pending(self) -> tuple[Migration, ...]:
        done = {a.version for a in self.applied}
        return tuple(m for m in self.available if m.version not in done)

    @property
    def problems(self) -> list[str]:
        known = {m.version: m for m in self.available}
        found: list[str] = []
        for a in self.applied:
            m = known.get(a.version)
            if m is None:
                found.append(
                    f"the database records migration {a.version} ({a.name}), which this Agent Factory does not "
                    "have: the database is newer than this code"
                )
            elif m.checksum != a.checksum:
                found.append(f"migration {a.version} ({a.name}) has been edited since it was applied")
        versions = sorted(a.version for a in self.applied)
        if versions != list(range(1, len(versions) + 1)):
            found.append(f"applied migrations are not contiguous: {versions}")
        return found

    @property
    def up_to_date(self) -> bool:
        return not self.pending and not self.problems

    def describe(self) -> str:
        state = "up to date" if self.up_to_date else (
            "; ".join(self.problems) if self.problems else f"{len(self.pending)} pending"
        )
        return (
            f"schema {self.schema}: version {self.current_version} of {self.latest_version} "
            f"({len(self.applied)} applied) - {state}"
        )


def available_migrations() -> tuple[Migration, ...]:
    found = []
    for entry in resources.files(PACKAGE).iterdir():
        match = FILE_RE.match(entry.name)
        if not match:
            continue
        text = entry.read_text(encoding="utf-8").replace("\r\n", "\n")  # checksum independent of checkout line endings
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        found.append(Migration(int(match.group(1)), match.group(2), text, f"sha256:{digest}"))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration files must be numbered 1..N without gaps, found {versions}")
    return tuple(found)


def migration_status(conn: psycopg.Connection, schema: str) -> MigrationStatus:
    available = available_migrations()
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            "SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = %s AND tablename = %s", (schema, TRACKING_TABLE)
        )
        if cur.fetchone() is None:
            return MigrationStatus(schema, available, ())
        cur.execute(
            sql.SQL("SELECT version, name, checksum, applied_at FROM {}.{} ORDER BY version").format(
                sql.Identifier(schema), sql.Identifier(TRACKING_TABLE)
            )
        )
        applied = tuple(AppliedMigration(v, n, c, format_timestamp(at)) for v, n, c, at in cur.fetchall())
    return MigrationStatus(schema, available, applied)


def migrate(conn: psycopg.Connection, schema: str) -> list[Migration]:
    """Apply the pending migrations to ``schema`` and return them. Needs an autocommit connection."""
    if not conn.autocommit:
        raise MigrationError("migrate needs an autocommit connection: each migration is its own transaction")
    key = _lock_key(schema)
    conn.execute("SELECT pg_advisory_lock(%s)", (key,))
    try:
        with conn.transaction():
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {}.{} ("
                    " version integer PRIMARY KEY CHECK (version >= 1),"
                    " name text NOT NULL,"
                    " checksum text NOT NULL,"
                    " applied_at timestamptz NOT NULL DEFAULT now())"
                ).format(sql.Identifier(schema), sql.Identifier(TRACKING_TABLE))
            )
        status = migration_status(conn, schema)
        if status.problems:
            raise MigrationError("refusing to migrate: " + "; ".join(status.problems))
        applied = []
        for m in status.pending:
            try:
                with conn.transaction():
                    conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
                    conn.execute(m.sql.encode("utf-8"))  # no parameters: may contain several statements
                    conn.execute(
                        sql.SQL("INSERT INTO {}.{} (version, name, checksum) VALUES (%s, %s, %s)").format(
                            sql.Identifier(schema), sql.Identifier(TRACKING_TABLE)
                        ),
                        (m.version, m.name, m.checksum),
                    )
            except psycopg.Error as exc:
                reason = exc.diag.message_primary or str(exc)
                raise MigrationError(f"migration {m.version} ({m.name}) failed and was rolled back: {reason}") from exc
            applied.append(m)
        return applied
    finally:
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


def _lock_key(schema: str) -> int:
    digest = hashlib.sha256(f"agent_factory.migrate:{schema}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)

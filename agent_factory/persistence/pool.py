"""A bounded pool of PostgreSQL connections for long-running processes such as the API.

Opening a PostgreSQL connection costs a TCP handshake, authentication, and
session setup: more time than a typical API query takes. A pool keeps a few
connections open and lends one to each request.

* **Bounded.** At most ``max_size`` connections, however many requests
  arrive, so a burst of traffic cannot exhaust the database server. A request
  that cannot borrow one within ``timeout`` seconds fails fast with
  ``StorageUnavailableError`` (the API answers 503) instead of queueing
  without limit.
* **Explicit lifecycle.** Nothing connects at import or construction time.
  ``open()`` runs at application start-up and ``close()`` at shutdown. The
  pool opens without waiting for the database, so the process can start (and
  report itself not ready) while the database is down.
* **Health.** Every connection is checked before it is lent, so a connection
  broken by a database restart is replaced instead of failing a request.
* **Schema.** The first connection lent verifies that every migration is
  applied; readiness checks it again each time it is asked.

Each lent connection gets its own ``ProductStore``, so one request's
transactions never mix with another's.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from ..domain.errors import SchemaNotReadyError, StorageError, StorageUnavailableError
from ..domain.repositories import ProductStore
from .config import DatabaseConfig
from .health import READY, Readiness
from .migrator import migration_status
from .postgres import PostgresDatabase, _translated, connection_kwargs, require_current_schema, store_for

POOL_NAME = "agent-factory-db"
DEFAULT_APPLICATION_NAME = "agent-factory-api"


class PostgresStorePool:
    """Lends ``ProductStore`` sessions backed by pooled connections."""

    def __init__(
        self,
        config: DatabaseConfig,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 3.0,
        max_idle: float = 300.0,
        application_name: str = DEFAULT_APPLICATION_NAME,
    ) -> None:
        if not 0 <= min_size <= max_size or max_size < 1:
            raise ValueError("pool sizes must satisfy 0 <= min_size <= max_size and max_size >= 1")
        if timeout <= 0:
            raise ValueError("pool timeout must be positive")
        self.config = config
        self.timeout = timeout
        self._schema_verified = False
        self._schema_lock = threading.Lock()
        self._pool = ConnectionPool(
            config.url,
            kwargs=connection_kwargs(config, application_name=application_name),
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            max_idle=max_idle,
            open=False,
            name=POOL_NAME,
            check=ConnectionPool.check_connection,
        )

    # ------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Start the pool. Connections are made in the background; an unreachable database is not an error here."""
        self._pool.open(wait=False)

    def close(self, timeout: float = 5.0) -> None:
        """Close every connection. Requests still holding one get it closed when they return it."""
        self._pool.close(timeout=timeout)

    @property
    def closed(self) -> bool:
        return self._pool.closed

    def stats(self) -> dict[str, int]:
        """Pool counters (sizes, waiting requests, errors). Contains no connection details."""
        return dict(self._pool.get_stats())

    # -------------------------------------------------------------- lending

    @contextmanager
    def session(self, timeout: float | None = None) -> Iterator[ProductStore]:
        """Borrow a connection for the duration of the block, as a ``ProductStore``.

        Raises ``StorageUnavailableError`` if none becomes available in time
        (every connection busy, or the database unreachable), and
        ``SchemaNotReadyError`` if the schema is not fully migrated.
        """
        wait = self.timeout if timeout is None else timeout
        # Checkout and return can fail outside a repository call too. Translate
        # those driver errors at the same persistence boundary.
        with _translated(self.config):
            try:
                with self._pool.connection(timeout=wait) as conn:
                    self._verify_schema_once(conn)
                    yield store_for(PostgresDatabase(conn, self.config, owns_connection=False))
            except PoolTimeout:
                raise StorageUnavailableError(f"no database connection became available within {wait:g} s") from None
            except PoolClosed:
                raise StorageUnavailableError("the database connection pool is closed") from None

    def readiness(self, timeout: float = 1.0) -> Readiness:
        """Can a request be served now? Never raises; the reason never contains connection details."""
        try:
            with self._pool.connection(timeout=timeout) as conn:
                status = migration_status(conn, self.config.schema)
        except (PoolTimeout, PoolClosed, psycopg.Error, StorageError):
            return Readiness(False, "database_unavailable")
        if not status.up_to_date:
            return Readiness(False, "schema_not_current")
        return READY

    def _verify_schema_once(self, conn: psycopg.Connection) -> None:
        if self._schema_verified:
            return
        with self._schema_lock:
            if not self._schema_verified:
                require_current_schema(conn, self.config)
                self._schema_verified = True


__all__ = ["PostgresStorePool", "Readiness", "SchemaNotReadyError"]

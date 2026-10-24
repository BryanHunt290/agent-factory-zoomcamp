"""Durable storage for product records, in PostgreSQL.

Optional: Agent Factory runs without it (the in-memory store needs nothing).
The PostgreSQL store needs the ``psycopg`` driver (see docs/database.md),
which is imported only when a PostgreSQL module is used:

* :mod:`.config` - explicit connection configuration (no driver needed);
* :mod:`.migrator` - versioned schema migrations;
* :mod:`.postgres` - the PostgreSQL repositories and ``open_postgres_store``;
* :mod:`.claims` - run claims for workers, and sessions whose every write is
  fenced by a claim (docs/workers.md).

Nothing here is imported by the engine or the domain model. Code that needs
storage depends on the repository interfaces in ``agent_factory.domain``.
"""

from .config import DatabaseConfig, DatabaseConfigError

__all__ = ["DatabaseConfig", "DatabaseConfigError"]

"""Database connection configuration: explicit, never hardcoded, never printed.

The connection URL comes from exactly one of:

* ``AGENT_FACTORY_DATABASE_URL``: the URL itself;
* ``AGENT_FACTORY_DATABASE_URL_FILE``: the path of a file containing it. This
  keeps the password out of the process environment and shell history, and
  is how a secret manager typically hands credentials to a service later.

``AGENT_FACTORY_DATABASE_SCHEMA`` optionally selects the schema (default
``agent_factory``). The PostgreSQL integration tests read the same variables
with the ``AGENT_FACTORY_TEST_`` prefix, so a test run can never pick up the
development database by accident.

The URL carries a password, so a ``DatabaseConfig`` never shows it: ``repr``,
``str`` and every error message use :meth:`DatabaseConfig.redacted`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_PREFIX = "AGENT_FACTORY"
TEST_PREFIX = "AGENT_FACTORY_TEST"
DEFAULT_SCHEMA = "agent_factory"
#: Lower-case SQL identifier; also safe to place in a connection option.
SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SCHEMES = ("postgresql", "postgres")


class DatabaseConfigError(ValueError):
    """Database configuration is missing, ambiguous, or invalid. Messages never contain the password."""


@dataclass(frozen=True, repr=False)
class DatabaseConfig:
    url: str = field(repr=False)
    schema: str = DEFAULT_SCHEMA
    source: str = "explicit"

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise DatabaseConfigError(f"database URL is empty ({self.source})")
        object.__setattr__(self, "url", self.url.strip())
        try:
            parts = urlsplit(self.url)
            parts.port  # noqa: B018 - validates the port
        except ValueError:
            raise DatabaseConfigError(f"database URL is malformed ({self.source})") from None
        if parts.scheme not in _SCHEMES:
            raise DatabaseConfigError(f"database URL must start with postgresql:// ({self.source})")
        if not parts.hostname:
            raise DatabaseConfigError(f"database URL must name a host ({self.source})")
        if not parts.path.strip("/"):
            raise DatabaseConfigError(f"database URL must name a database ({self.source})")
        if not parts.username:
            raise DatabaseConfigError(f"database URL must name a user ({self.source})")
        if not isinstance(self.schema, str) or not SCHEMA_RE.fullmatch(self.schema):
            raise DatabaseConfigError(f"schema must be a lower-case identifier like 'agent_factory', got {self.schema!r}")

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def port(self) -> int:
        return urlsplit(self.url).port or 5432

    @property
    def dbname(self) -> str:
        return unquote(urlsplit(self.url).path.lstrip("/"))

    @property
    def user(self) -> str:
        return unquote(urlsplit(self.url).username or "")

    def redacted(self) -> str:
        """The connection target without the password, safe to print and log."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.dbname} (schema {self.schema})"

    def __repr__(self) -> str:
        return f"DatabaseConfig({self.redacted()}, source={self.source!r})"

    def __str__(self) -> str:
        return self.redacted()

    def with_schema(self, schema: str) -> DatabaseConfig:
        return replace(self, schema=schema)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, prefix: str = DEFAULT_PREFIX
    ) -> DatabaseConfig:
        env = os.environ if environ is None else environ
        url_var, file_var, schema_var = f"{prefix}_DATABASE_URL", f"{prefix}_DATABASE_URL_FILE", f"{prefix}_DATABASE_SCHEMA"
        url, path = env.get(url_var, "").strip(), env.get(file_var, "").strip()
        if url and path:
            raise DatabaseConfigError(f"both {url_var} and {file_var} are set; set exactly one")
        if not url and not path:
            raise DatabaseConfigError(f"no database configured: set {url_var} or {file_var} (see docs/database.md)")
        source = url_var
        if path:
            source = f"{file_var}={path}"
            try:
                url = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise DatabaseConfigError(f"cannot read {file_var} ({path}): {exc.strerror or exc}") from None
        schema = env.get(schema_var, "").strip() or DEFAULT_SCHEMA
        return cls(url=url, schema=schema, source=source)

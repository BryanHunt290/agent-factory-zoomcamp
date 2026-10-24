"""Run the API with uvicorn against the configured PostgreSQL database.

``python -m agent_factory api serve`` calls :func:`serve`. One process, one
event loop; route handlers are synchronous and run in a thread pool, each
borrowing a pooled database connection for its unit of work.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote, urlsplit

import uvicorn

from ..persistence.config import DatabaseConfig
from ..persistence.pool import PostgresStorePool
from .app import create_app
from .auth import AuthMode, AuthSettings, build_authenticator
from .config import ApiSettings
from .observability import Redactor, configure_logging
from .pagination import CursorCodec
from .redaction import TextSanitizer

log = logging.getLogger("agent_factory.api")


def secret_values(database: DatabaseConfig, cursors: CursorCodec) -> list[str]:
    """Every secret this process holds, so logs and responses can be scrubbed of them."""
    password = urlsplit(database.url).password
    values = [database.url, cursors.secret_for_redaction]
    if password:
        values += [password, unquote(password)]
    return values


def serve(settings: ApiSettings, database: DatabaseConfig, auth: AuthSettings, *, log_level: str = "info") -> None:
    settings.bind_warnings()  # refuses every non-loopback address: no deployment design exists yet
    cursors = CursorCodec.from_secret_file(settings.cursor_secret_file, ttl=settings.cursor_ttl)
    hidden = secret_values(database, cursors)
    configure_logging(log_level, Redactor(hidden))  # the redactor also removes anything shaped like a token
    authenticator = build_authenticator(auth)
    if auth.mode is AuthMode.DEVELOPMENT:
        log.warning("DEVELOPMENT AUTHENTICATION: tokens from the local development issuer are accepted")
    else:
        log.info("authentication: OpenID Connect issuer %s, audience %s", auth.policy.issuer, auth.policy.audience)
    pool = PostgresStorePool(
        database,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=settings.pool_timeout_s,
    )
    app = create_app(
        stores=pool, authenticator=authenticator, settings=settings, cursors=cursors, sanitizer=TextSanitizer(hidden)
    )
    log.info("database: %s", database.redacted())
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        lifespan="on",
        log_config=None,  # logging is configured above: JSON, redacted
        log_level=log_level,
        access_log=False,  # the request-context middleware writes the access log
        server_header=False,
        proxy_headers=False,  # no proxy in front: never trust X-Forwarded-* headers
        workers=None,
    )
    uvicorn.Server(config).run()

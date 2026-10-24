"""The FastAPI application: wiring only.

``create_app`` assembles the layers (routes, services, store provider), the
error mapping, and the request-context middleware. It opens nothing itself:
the store provider is opened when the application starts (the ASGI
*lifespan* start-up event) and closed when it shuts down.

There is no application without authentication: ``authenticator`` is
required, and every product route requires a permission (auth/policy.py).

FastAPI's generated documentation and schema endpoints (/docs, /redoc,
/openapi.json) are switched off. The checked-in ``openapi.yaml`` is the
contract; a second, generated description would drift from it. Tests check
the running application against openapi.yaml instead.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from ..templates import TEMPLATES, ProjectTemplate
from ..util import Clock, utc_now
from .auth import Authenticator
from .config import ApiSettings
from .handlers import install_error_handlers
from .middleware import RequestContextMiddleware
from .pagination import CursorCodec
from .redaction import TextSanitizer
from .routes import health_router, product_router
from .services import ApiContext
from .stores import StoreProvider

log = logging.getLogger("agent_factory.api")

#: info.version of the openapi.yaml this application implements.
CONTRACT_VERSION = "1.0.0-draft"


def create_app(
    *,
    stores: StoreProvider,
    authenticator: Authenticator,
    settings: ApiSettings | None = None,
    cursors: CursorCodec | None = None,
    sanitizer: TextSanitizer | None = None,
    templates: Mapping[str, ProjectTemplate] = TEMPLATES,
    clock: Clock = utc_now,
) -> FastAPI:
    if not isinstance(authenticator, Authenticator):
        raise TypeError("create_app needs an Authenticator: the API never runs without authentication")
    settings = settings or ApiSettings()
    context = ApiContext(
        stores=stores,
        cursors=cursors or CursorCodec.from_secret_file(settings.cursor_secret_file, ttl=settings.cursor_ttl, clock=clock),
        templates=dict(templates),
        sanitize=sanitizer or TextSanitizer(),
        clock=clock,
        idempotency_ttl=settings.idempotency_ttl,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await run_in_threadpool(stores.open)
            log.info("Agent Factory API started (loopback only; every product route requires a permission)")
            yield
        finally:
            await run_in_threadpool(stores.close)
            log.info("Agent Factory API stopped")

    app = FastAPI(
        title="Agent Factory API",
        version=CONTRACT_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.authenticator = authenticator
    install_error_handlers(app)
    app.include_router(product_router)
    app.include_router(health_router)
    app.add_middleware(RequestContextMiddleware, allowed_hosts=settings.allowed_hosts)
    return app

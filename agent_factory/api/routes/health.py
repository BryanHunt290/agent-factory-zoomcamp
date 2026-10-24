"""Infrastructure endpoints, outside the versioned product API and outside openapi.yaml.

* ``GET /healthz`` (liveness): is the process alive and its event loop
  responsive? It touches no dependency, so a database outage never makes a
  supervisor restart a healthy process (a restart would not fix the database).
* ``GET /readyz`` (readiness): can this instance serve requests now? It
  borrows a database connection and checks that the schema is fully
  migrated. A load balancer stops sending traffic while it answers 503.

Both answer with a fixed word, never with host names, versions, or errors.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ...domain.errors import StorageError
from ..dependencies import ContextDep

router = APIRouter(include_in_schema=False)

INFRASTRUCTURE_PATHS = ("/healthz", "/readyz")


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/readyz")
async def readyz(ctx: ContextDep) -> JSONResponse:
    try:
        readiness = await run_in_threadpool(ctx.stores.readiness)
    except StorageError:
        return JSONResponse(
            {"status": "not_ready", "reason": "database_unavailable"},
            status_code=503,
            headers={"Retry-After": "5"},
        )
    if readiness.ready:
        return JSONResponse({"status": "ready"})
    reason = "schema_not_current" if readiness.reason == "schema_not_current" else "database_unavailable"
    return JSONResponse({"status": "not_ready", "reason": reason}, status_code=503, headers={"Retry-After": "5"})

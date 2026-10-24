"""HTTP routes: thin handlers that translate between HTTP and the services.

A handler declares its parameters (validated by FastAPI), receives its
service by dependency injection, makes one service call, and sets status
code and headers. It contains no SQL, graph orchestration, Docker or
filesystem access, and no business-state transitions.

Only the contract's 11 operations exist under /api/v1, plus the two
documented infrastructure endpoints (/healthz, /readyz).
"""

from fastapi import APIRouter

from . import approvals, health, projects, runs
from .params import PREFIX

#: Routers carry their full paths (no include prefix), so a matched route's
#: template is complete wherever it is read, e.g. in the access log.
product_router = APIRouter()
product_router.include_router(projects.router)
product_router.include_router(runs.router)
product_router.include_router(approvals.router)

health_router = health.router

__all__ = ["PREFIX", "health_router", "product_router"]

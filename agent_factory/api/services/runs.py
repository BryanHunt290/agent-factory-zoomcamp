"""Runs: listRuns, getRun. Read-only: workers own execution."""

from __future__ import annotations

from ...domain.approvals import ApprovalStatus
from ...domain.repositories import ProductStore
from .. import schemas as api
from ..errors import ErrorCode, not_found
from . import views
from .context import POSITION_ERRORS, Service, bad_position


def _pending_by_run(store: ProductStore, project_id: str) -> dict[str, str]:
    """run_id -> the approval each paused run waits for (the latest, should a run have several)."""
    pending = store.approvals.list_requests(project_id, status=ApprovalStatus.PENDING)  # oldest first
    return {r.run_id: r.id for r in pending}


class RunService(Service):
    def list_runs(self, project_id: str, *, limit: int, cursor: str | None) -> api.RunPage:
        filters = {"project_id": project_id}
        position = self._position(cursor, "listRuns", filters)
        with self.ctx.stores.session() as store, store.snapshot():
            if store.projects.get(project_id) is None:
                raise not_found(ErrorCode.PROJECT_NOT_FOUND)
            try:
                before = int(position[0]) if position else None
                found = store.runs.page_for_project(project_id, limit=limit + 1, before_number=before)
            except (*POSITION_ERRORS, IndexError):
                raise bad_position() from None
            page = found[:limit]
            pending = _pending_by_run(store, project_id)
            usage = {r.run_id: store.model_usage.get(project_id, r.run_id) for r in page}
        more = len(found) > limit
        return api.RunPage(
            items=[views.run(r, pending.get(r.run_id), usage[r.run_id]) for r in page],
            next_cursor=self._next_cursor(more, "listRuns", filters, [page[-1].number] if page else []),
        )

    def get_run(self, project_id: str, run_id: str) -> api.Run:
        with self.ctx.stores.session() as store, store.snapshot():
            if store.projects.get(project_id) is None:
                raise not_found(ErrorCode.PROJECT_NOT_FOUND)
            run = store.runs.get(project_id, run_id)
            if run is None:
                raise not_found(ErrorCode.RUN_NOT_FOUND)
            pending = _pending_by_run(store, project_id)
            usage = store.model_usage.get(project_id, run_id)
        return views.run(run, pending.get(run_id), usage)

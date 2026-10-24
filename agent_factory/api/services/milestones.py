"""Milestones: listProjectMilestones, listRunMilestones. Read-only: Agent Factory produces them."""

from __future__ import annotations

from collections.abc import Sequence

from ...domain.repositories import MilestoneQuery
from ...domain.serialization import parse_timestamp
from .. import schemas as api
from ..errors import ErrorCode, field_error, not_found, validation_failed
from . import views
from .context import POSITION_ERRORS, Service, bad_position


class MilestoneService(Service):
    def list_milestones(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        statuses: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
        limit: int,
        cursor: str | None,
    ) -> api.MilestonePage:
        """One project's milestones, or one run's, chronologically: (occurred_at, run_id, sequence)."""
        if since is not None and until is not None and parse_timestamp(until) <= parse_timestamp(since):
            raise validation_failed([field_error("query", "until", "Must be later than since.")])
        operation = "listRunMilestones" if run_id is not None else "listProjectMilestones"
        filters = {
            "project_id": project_id,
            "run_id": run_id,
            "since": since,
            "until": until,
            "status": sorted(set(statuses)) if statuses else None,
            "category": sorted(set(categories)) if categories else None,
        }
        position = self._position(cursor, operation, filters)
        query = MilestoneQuery(
            project_id=project_id,
            run_id=run_id,
            since=since,
            until=until,
            statuses=frozenset(statuses) if statuses else None,
            categories=frozenset(categories) if categories else None,
        )
        with self.ctx.stores.session() as store, store.snapshot():
            if store.projects.get(project_id) is None:
                raise not_found(ErrorCode.PROJECT_NOT_FOUND)
            if run_id is not None and store.runs.get(project_id, run_id) is None:
                raise not_found(ErrorCode.RUN_NOT_FOUND)
            try:
                found = store.milestones.page(query, limit=limit + 1, after=tuple(position) if position else None)
            except POSITION_ERRORS:
                raise bad_position() from None
        page = found[:limit]
        last = page[-1] if page else None
        return api.MilestonePage(
            items=[views.milestone(m, self.ctx.sanitize) for m in page],
            next_cursor=self._next_cursor(
                len(found) > limit, operation, filters, [last.occurred_at, last.run_id, last.sequence] if last else []
            ),
        )

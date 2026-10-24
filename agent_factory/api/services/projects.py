"""Projects and templates: listProjects, getProject, createProject, listTemplates."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...domain.errors import ConflictError, DomainError, StorageError
from ...domain.idempotency import IdempotencyRecord, request_hash
from ...domain.ids import new_project_id
from ...domain.project import Project
from ...domain.serialization import format_timestamp, parse_timestamp
from .. import schemas as api
from ..errors import ApiError, ErrorCode, not_found
from . import views
from .context import POSITION_ERRORS, Service, bad_position

log = logging.getLogger("agent_factory.api")

#: The idempotency scope of createProject: keys of different operations never collide.
CREATE_PROJECT = "create_project"


@dataclass(frozen=True)
class CreateResult:
    project: api.Project
    status: int
    replayed: bool


class ProjectService(Service):
    def list_projects(self, *, activity_since: str | None, limit: int, cursor: str | None) -> api.ProjectSummaryPage:
        filters = {"activity_since": activity_since}
        position = self._position(cursor, "listProjects", filters)
        with self.ctx.stores.session() as store, store.snapshot():
            try:
                found = store.projects.page(limit=limit + 1, after=tuple(position) if position else None)
            except POSITION_ERRORS:
                raise bad_position() from None
            page = found[:limit]
            ids = [p.id for p in page]
            latest = store.runs.latest_for_projects(ids)
            pending = store.approvals.pending_counts(ids, at=self.ctx.now())
            recent = store.milestones.count_since(ids, activity_since) if activity_since else {}
        items = [
            views.project_summary(p, latest.get(p.id), pending.get(p.id, 0), activity_since, recent.get(p.id, 0))
            for p in page
        ]
        last = page[-1] if page else None
        more = len(found) > limit and last is not None
        return api.ProjectSummaryPage(
            items=items,
            next_cursor=self._next_cursor(more, "listProjects", filters, [last.created_at, last.id] if last else []),
        )

    def get_project(self, project_id: str, *, activity_since: str | None) -> api.Project:
        with self.ctx.stores.session() as store, store.snapshot():
            project = store.projects.get(project_id)
            if project is None:
                raise not_found(ErrorCode.PROJECT_NOT_FOUND)
            latest = store.runs.latest_for_project(project_id)
            pending = store.approvals.pending_counts([project_id], at=self.ctx.now()).get(project_id, 0)
            recent = store.milestones.count_since([project_id], activity_since) if activity_since else {}
        return views.project(project, latest, pending, activity_since, recent.get(project_id, 0))

    def list_templates(self) -> api.TemplateList:
        return api.TemplateList(items=[views.template(self.ctx.templates[name]) for name in sorted(self.ctx.templates)])

    def create_project(self, key: str, request: api.CreateProjectRequest) -> CreateResult:
        """Create a project exactly once per Idempotency-Key.

        The project and the record of the response are written in one
        transaction. The record's primary key settles concurrent requests with
        the same key: one insert wins; every other attempt rolls back entirely
        (its project included) and then replays the winner's response.
        """
        fingerprint = request_hash(CREATE_PROJECT, request.model_dump())
        now = self.ctx.now()
        with self.ctx.stores.session() as store:
            existing = store.idempotency.get(CREATE_PROJECT, key)
            if existing is not None and not existing.is_expired(now):
                return self._replay(existing, fingerprint)
            template = self.ctx.templates.get(request.template)
            if template is None:
                raise ApiError(ErrorCode.UNKNOWN_TEMPLATE, "No template has this name; GET /api/v1/templates lists them.")
            project = Project(
                id=new_project_id(),
                name=request.name,
                created_at=now,
                milestone_policy=template.milestone_policy,
                template=template.name,
                description=request.description,
            )
            response = views.project(project, None, 0, None, 0)
            record = IdempotencyRecord(
                scope=CREATE_PROJECT,
                key=key,
                request_hash=fingerprint,
                response_status=201,
                response_body=response.model_dump(mode="json"),
                created_at=now,
                expires_at=format_timestamp(parse_timestamp(now) + self.ctx.idempotency_ttl),
            )
            with store.transaction():
                if existing is not None:  # expired: the key is free again
                    store.idempotency.remove_expired(CREATE_PROJECT, key, at=now)
                try:
                    with store.transaction():  # a savepoint: losing the key undoes only this insert
                        store.idempotency.add(record)
                    claimed = True
                except ConflictError:
                    claimed = False
                if claimed:
                    store.projects.add(project)
            if not claimed:
                winner = store.idempotency.get(CREATE_PROJECT, key)
                if winner is None or winner.is_expired(now):
                    raise ApiError(
                        ErrorCode.IDEMPOTENCY_KEY_IN_USE,
                        "A request with this Idempotency-Key is still being processed; retry shortly.",
                    )
                return self._replay(winner, fingerprint)
            self._purge_expired(store, now)
        return CreateResult(response, 201, replayed=False)

    @staticmethod
    def _replay(record: IdempotencyRecord, fingerprint: str) -> CreateResult:
        if record.request_hash != fingerprint:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                "This Idempotency-Key was already used for a different request; use a new key.",
            )
        return CreateResult(api.Project.model_validate(record.response()), record.response_status, replayed=True)

    @staticmethod
    def _purge_expired(store, now: str) -> None:
        """Best effort: expired keys are deleted a few at a time; a failure here must not fail the request."""
        try:
            store.idempotency.purge_expired(at=now, limit=100)
        except (StorageError, DomainError) as exc:
            log.warning("could not purge expired idempotency keys (%s)", type(exc).__name__)

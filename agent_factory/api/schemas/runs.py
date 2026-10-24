"""Runs (contract schemas: Run, RunPage, ModelUsage, ModelUsageByModel)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from .common import ApiModel, ApprovalId, Cursor, ProjectId, RunId, RunStatus, Timestamp


Count = Annotated[int, Field(ge=0)]


class ModelUsageByModel(ApiModel):
    provider: Annotated[str, StringConstraints(max_length=64)]
    model: Annotated[str, StringConstraints(max_length=128)]
    calls: Count
    input_tokens: Count
    output_tokens: Count
    cached_tokens: Count


class ModelUsage(ApiModel):
    """Counts only: never a prompt, an answer, or a credential. No cost without configured prices."""

    calls: Count
    failed_calls: Count
    input_tokens: Count
    output_tokens: Count
    cached_tokens: Count
    estimated_cost_usd: Annotated[float, Field(ge=0)] | None
    by_model: Annotated[list[ModelUsageByModel], Field(max_length=20)]


class Run(ApiModel):
    project_id: ProjectId
    run_id: RunId
    number: Annotated[int, Field(ge=1)]
    workflow: Annotated[str, StringConstraints(max_length=64)]
    status: RunStatus
    #: Always one of a fixed set of safe messages (api/redaction.py), never raw engine text.
    status_reason: Annotated[str, StringConstraints(max_length=300)] | None
    pending_approval_id: ApprovalId | None
    created_at: Timestamp
    updated_at: Timestamp
    model_usage: ModelUsage


class RunPage(ApiModel):
    items: list[Run]
    next_cursor: Cursor | None

"""Problem details (RFC 9457): the one error format (contract schemas: Problem, FieldError)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .approvals import ApprovalRequest
from .common import ApiModel, PermissionName


class FieldError(ApiModel):
    location: Literal["body", "query", "path", "header"]
    pointer: Annotated[str, StringConstraints(max_length=200)]
    message: Annotated[str, StringConstraints(max_length=300)]


class Problem(ApiModel):
    type: Annotated[str, StringConstraints(pattern=r"^urn:agent-factory:problem:[a-z_]+$")]
    title: Annotated[str, StringConstraints(max_length=200)]
    status: Annotated[int, Field(ge=400, le=599)]
    detail: Annotated[str, StringConstraints(max_length=1000)] | None = None
    instance: Annotated[str, StringConstraints(max_length=500)] | None = None
    code: str
    request_id: Annotated[str, StringConstraints(pattern=r"^req_[A-Za-z0-9]{8,64}$")]
    errors: Annotated[list[FieldError], Field(max_length=50)] | None = None
    approval: ApprovalRequest | None = None
    required_permission: PermissionName | None = None

"""Human approval gates.

When a gated action is attempted and no decision exists yet, the tool
gateway raises :class:`ApprovalRequired`. The executor then pauses the run
(status ``awaiting_approval``), checkpoints it, and stops. A human records a
decision (``Executor.decide_approval`` or ``agent-factory approve``) and
resumes the run; the node is re-executed and, because request ids are
deterministic, the gateway finds the decision for the same action.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..util import canonical_json, sha256_hex


class PermissionDenied(Exception):
    """The policy or a human refused the action."""


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    run_id: str
    node: str
    agent: str
    tool: str
    category: str
    params: dict[str, Any]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalRequired(Exception):
    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        super().__init__(
            f"{request.agent} wants to run {request.tool} ({request.category}); "
            f"approval request {request.request_id}"
        )


def make_request_id(run_id: str, node: str, visit: int, tool: str, params: dict[str, Any]) -> str:
    """Stable id for an action, so a re-executed node maps onto the same decision."""
    material = canonical_json(
        {"run": run_id, "node": node, "visit": visit, "tool": tool, "params": params}
    )
    return "apr-" + sha256_hex(material)[:16]

"""What needs a person: the attention inbox (docs/product-domain.md#attention).

Two kinds of item, both decided by the server so that a client never has to
inspect engine state:

* ``decision_required``: an approval request that is pending and has not
  expired. Someone with ``approvals:decide`` can approve or reject it.
* ``reconciliation_required``: an approved action whose outcome is uncertain
  (its execution is ``uncertain``) and for whose uncertain attempt no
  reconciliation decision has been recorded yet. Someone with
  ``actions:reconcile`` must say whether it happened.

Items are ordered oldest first by ``since`` (when the item began needing a
person), then project, then item key, which is also the keyset position of a
page.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .ids import APPROVAL_ID_RE, PROJECT_ID_RE, RUN_ID_RE
from .serialization import check_enum, check_optional_timestamp, check_pattern, check_text, check_timestamp, parse_timestamp


class AttentionKind(StrEnum):
    DECISION_REQUIRED = "decision_required"
    RECONCILIATION_REQUIRED = "reconciliation_required"


#: Keyset position: (since, project_id, approval_id) of the last item already seen.
AttentionPosition = tuple[str, str, str]


@dataclass(frozen=True)
class AttentionItem:
    kind: AttentionKind
    project_id: str
    run_id: str
    approval_id: str
    since: str  # pending: requested_at; uncertain: when the execution became uncertain
    tool: str
    category: str
    execution_id: str | None = None  # reconciliation items
    attempt: int | None = None  # reconciliation items: the uncertain attempt
    expires_at: str | None = None  # decision items

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", check_enum(AttentionKind, self.kind, "kind"))
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.run_id, RUN_ID_RE, "run_id")
        check_pattern(self.approval_id, APPROVAL_ID_RE, "approval_id")
        object.__setattr__(self, "since", check_timestamp(self.since, "since"))
        check_text(self.tool, "tool", max_len=64)
        check_text(self.category, "category", max_len=64)
        object.__setattr__(self, "expires_at", check_optional_timestamp(self.expires_at, "expires_at"))

    @property
    def position(self) -> AttentionPosition:
        return (self.since, self.project_id, self.approval_id)


def attention_order(item: AttentionItem) -> tuple:
    return (parse_timestamp(item.since), item.project_id, item.approval_id)


__all__ = ["AttentionItem", "AttentionKind", "AttentionPosition", "attention_order"]

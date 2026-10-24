"""Execution semantics: what may happen when a tool's action is repeated.

Approval answers "may this exact action run?". Execution semantics answer a
different question: "if Agent Factory loses track of an attempt, is it safe
to try again?" (docs/design/action-execution.md).

Every tool belongs to one class:

* ``READ_ONLY`` (class A): the action observes and changes nothing that
  outlives it. Repeating it is harmless.
* ``IDEMPOTENT`` (class B): repeating the dispatch leaves the same end state
  and has no further effect, either by the nature of the operation
  (``NATURAL``: "make sure this file is absent") or because the target
  deduplicates an idempotency key Agent Factory supplies (``KEYED``).
* ``NON_IDEMPOTENT`` (class C): the target cannot suppress a duplicate and
  cannot prove whether an uncertain attempt took effect. An uncertain attempt
  is never repeated automatically; a person decides.

A tool that declares nothing is treated as class C, the most conservative
class. A tool in a dangerous category must declare its semantics explicitly:
:func:`check_tool_semantics` refuses it otherwise, when it is registered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .permissions import DANGEROUS_CATEGORIES, ActionCategory

if TYPE_CHECKING:
    from ..tools.base import Tool

MAX_AUTOMATIC_ATTEMPTS = 10


class ExecutionSemanticsError(ValueError):
    """A tool's execution semantics are missing where required, or inconsistent."""


class EffectClass(StrEnum):
    READ_ONLY = "read_only"  # class A: safe to retry
    IDEMPOTENT = "idempotent"  # class B: a repeat has the same end state
    NON_IDEMPOTENT = "non_idempotent"  # class C: a repeat may duplicate the effect


class Idempotency(StrEnum):
    NATURAL = "natural"  # repeating the operation yields the same end state by definition
    KEYED = "keyed"  # the target deduplicates an idempotency key Agent Factory supplies


@dataclass(frozen=True)
class ExecutionSemantics:
    """How retries and crashes are handled for one tool.

    ``max_attempts`` is the number of dispatches one authorization allows
    automatically. ``key_retention`` is how long a ``KEYED`` target promises
    to deduplicate a key: a retry is attempted only while the first attempt
    is still inside that window. ``replayable_result`` says the tool's result
    is small, JSON-safe data that may be recorded and replayed to a node that
    re-runs; tools whose results are not replayable get only ok/summary back.
    """

    effect: EffectClass
    idempotency: Idempotency | None = None
    key_retention: timedelta | None = None
    max_attempts: int = 1
    replayable_result: bool = True
    rationale: str = ""

    def __post_init__(self) -> None:
        try:
            effect = EffectClass(self.effect)
        except ValueError:
            raise ExecutionSemanticsError(f"unknown effect class {self.effect!r}") from None
        object.__setattr__(self, "effect", effect)
        if self.idempotency is not None:
            try:
                object.__setattr__(self, "idempotency", Idempotency(self.idempotency))
            except ValueError:
                raise ExecutionSemanticsError(f"unknown idempotency {self.idempotency!r}") from None
        if (effect is EffectClass.IDEMPOTENT) != (self.idempotency is not None):
            raise ExecutionSemanticsError("idempotency (natural or keyed) is required for, and only for, idempotent tools")
        keyed = self.idempotency is Idempotency.KEYED
        if keyed != (self.key_retention is not None):
            raise ExecutionSemanticsError("key_retention is required for, and only for, keyed idempotency")
        if self.key_retention is not None and self.key_retention <= timedelta(0):
            raise ExecutionSemanticsError("key_retention must be positive")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ExecutionSemanticsError("max_attempts must be an integer")
        if not 1 <= self.max_attempts <= MAX_AUTOMATIC_ATTEMPTS:
            raise ExecutionSemanticsError(f"max_attempts must be between 1 and {MAX_AUTOMATIC_ATTEMPTS}")
        if effect is EffectClass.NON_IDEMPOTENT and self.max_attempts != 1:
            raise ExecutionSemanticsError("a non-idempotent tool gets exactly one automatic attempt")
        if not isinstance(self.replayable_result, bool):
            raise ExecutionSemanticsError("replayable_result must be true or false")

    @property
    def key_retention_s(self) -> int | None:
        return None if self.key_retention is None else int(self.key_retention.total_seconds())

    def describe(self) -> str:
        if self.effect is EffectClass.IDEMPOTENT:
            return f"idempotent ({self.idempotency.value})"  # type: ignore[union-attr]
        return self.effect.value.replace("_", "-")


#: What an undeclared tool is treated as: class C, one attempt, nothing replayed beyond ok/summary.
CONSERVATIVE_DEFAULT = ExecutionSemantics(
    effect=EffectClass.NON_IDEMPOTENT,
    replayable_result=False,
    rationale="undeclared: treated as the most conservative class",
)


def semantics_of(tool: Tool) -> ExecutionSemantics:
    """The tool's declared semantics, or the conservative default."""
    declared = getattr(tool, "execution", None)
    return declared if declared is not None else CONSERVATIVE_DEFAULT


def check_tool_semantics(tool: Tool) -> None:
    """Refuse a tool whose declaration is missing where it is required, or contradicts its category."""
    declared: Any = getattr(tool, "execution", None)
    category = ActionCategory(tool.category)
    if category is ActionCategory.CLOUD_READ and (
        not isinstance(declared, ExecutionSemantics) or declared.effect is not EffectClass.READ_ONLY
    ):
        # A cloud read never mutates. A tool that may change anything is not a cloud read, whatever it is called.
        raise ExecutionSemanticsError(f"tool {tool.name!r} is a cloud_read tool and must declare read-only semantics")
    if declared is None:
        if category in DANGEROUS_CATEGORIES:
            raise ExecutionSemanticsError(
                f"tool {tool.name!r} is in the dangerous category {category.value!r} and must declare its "
                "execution semantics (read_only, idempotent, or non_idempotent)"
            )
        return
    if not isinstance(declared, ExecutionSemantics):
        raise ExecutionSemanticsError(f"tool {tool.name!r}: execution must be an ExecutionSemantics")
    if category in DANGEROUS_CATEGORIES and declared.effect is EffectClass.READ_ONLY:
        raise ExecutionSemanticsError(
            f"tool {tool.name!r} is in the dangerous category {category.value!r}; it cannot be declared read-only"
        )


class ReconciliationRequired(Exception):
    """An approved action's outcome is uncertain and a person must decide before the run continues.

    Raised by the tool gateway; the executor pauses the run as
    ``awaiting_reconciliation``. Agents must not catch it, exactly like
    ``ApprovalRequired``. The executor also checks after every node, so a
    swallowed signal still pauses the run.
    """

    def __init__(self, execution_id: str, attempt: int, reason: str) -> None:
        self.execution_id = execution_id
        self.attempt = attempt
        self.reason = reason
        super().__init__(f"the outcome of action {execution_id} (attempt {attempt}) is uncertain: {reason}")


class ExecutionLedgerUnavailable(Exception):
    """The durable record of approved actions cannot be read or written. Nothing was dispatched
    unless the record already says so; the run blocks and is resumable."""


__all__ = [
    "CONSERVATIVE_DEFAULT",
    "MAX_AUTOMATIC_ATTEMPTS",
    "EffectClass",
    "ExecutionLedgerUnavailable",
    "ExecutionSemantics",
    "ExecutionSemanticsError",
    "Idempotency",
    "ReconciliationRequired",
    "check_tool_semantics",
    "semantics_of",
]

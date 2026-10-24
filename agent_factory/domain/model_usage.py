"""A run's model usage, as product data (docs/model-providers.md, "usage accounting").

The source of truth is the run's usage ledger (``model_usage.jsonl`` next to its
checkpoint), written before and after every model call. Each checkpoint carries
a summary of it (``state.metadata["model_usage"]``), and the recorder stores that
summary here, so the API and the dashboard can show calls, tokens, and (only
when prices are configured) an estimated cost. It holds counts only: never a
prompt, an answer, or a credential.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .ids import PROJECT_ID_RE, RUN_ID_RE
from .serialization import check_int, check_pattern, check_timestamp

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}$")
MAX_MODELS = 20


@dataclass(frozen=True)
class ModelUsageByModel:
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int

    def __post_init__(self) -> None:
        check_pattern(self.provider, NAME_RE, "provider")
        check_pattern(self.model, NAME_RE, "model")
        for name in ("calls", "input_tokens", "output_tokens", "cached_tokens"):
            check_int(getattr(self, name), name, minimum=0)

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cached_tokens": self.cached_tokens}  # fmt: skip


@dataclass(frozen=True)
class RunModelUsage:
    project_id: str
    run_id: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    updated_at: str
    estimated_cost_usd: float | None = None
    by_model: tuple[ModelUsageByModel, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        check_pattern(self.project_id, PROJECT_ID_RE, "project_id")
        check_pattern(self.run_id, RUN_ID_RE, "run_id")
        for name in ("calls", "failed_calls", "input_tokens", "output_tokens", "cached_tokens"):
            check_int(getattr(self, name), name, minimum=0)
        check_timestamp(self.updated_at, "updated_at")
        if self.estimated_cost_usd is not None and (not isinstance(self.estimated_cost_usd, (int, float)) or self.estimated_cost_usd < 0):
            raise ValueError("estimated_cost_usd must be a non-negative number or None")
        if len(self.by_model) > MAX_MODELS:
            raise ValueError(f"at most {MAX_MODELS} models per run")

    @classmethod
    def from_summary(cls, project_id: str, run_id: str, summary: Mapping[str, Any], *, at: str) -> RunModelUsage:
        """From the checkpoint's summary of the usage ledger (``UsageTotals.to_dict``)."""
        return cls(
            project_id=project_id, run_id=run_id, calls=int(summary.get("calls", 0)),
            failed_calls=int(summary.get("failed_calls", 0)), input_tokens=int(summary.get("input_tokens", 0)),
            output_tokens=int(summary.get("output_tokens", 0)), cached_tokens=int(summary.get("cached_tokens", 0)),
            estimated_cost_usd=summary.get("estimated_usd"), updated_at=at,
            by_model=tuple(ModelUsageByModel(**{k: m[k] for k in ("provider", "model", "calls", "input_tokens",
                                                                  "output_tokens", "cached_tokens")})
                           for m in summary.get("by_model", [])[:MAX_MODELS]),
        )  # fmt: skip

    def same_counts(self, other: RunModelUsage | None) -> bool:
        if other is None:
            return False
        keys = ("calls", "failed_calls", "input_tokens", "output_tokens", "cached_tokens", "estimated_cost_usd", "by_model")
        return all(getattr(self, k) == getattr(other, k) for k in keys)


__all__ = ["ModelUsageByModel", "RunModelUsage"]

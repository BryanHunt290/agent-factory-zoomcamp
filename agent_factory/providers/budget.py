"""Model budgets and the usage ledger (docs/model-providers.md).

**Budgets** are hard limits, checked before every call, never after:

* per call: input tokens (estimated before sending) and output tokens (the
  ``max_output_tokens`` the provider is told);
* per agent visit: model calls (transport retries and corrective re-asks count);
* per run: model calls, input tokens, output tokens, and, only when prices are
  configured, an estimated dollar ceiling.

A call that would exceed a limit is not made: ``ModelBudgetExhausted`` blocks
the run (fail closed). Nothing keeps spending silently.

**The usage ledger** is the run's ``model_usage.jsonl``, an append-only,
hash-chained, fsync'd log next to its checkpoint, so budgets survive a
restart:

* ``call_started`` is written **before** a request is sent, reserving its
  worst case (the estimated input and the full output cap);
* ``call_finished`` records what the provider reported, and the outcome.

A call started but never finished (the process died, or the provider never
answered) stays counted at its reservation: the provider may have processed
and billed it. Totals are computed per ``request_id``, so nothing is counted
twice, however often the ledger is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import ProviderError

USAGE_LOG = "model_usage.jsonl"


class ModelBudgetExhausted(ProviderError):
    """A model call would exceed a budget: it was not made. The run blocks until a person acts."""

    category = "budget_exhausted"


@dataclass(frozen=True)
class Pricing:
    """Configured prices in US dollars per million tokens. Never built in: prices change."""

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None

    def cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        cached_rate = self.cached_input_per_mtok if self.cached_input_per_mtok is not None else self.input_per_mtok
        fresh = max(0, input_tokens - cached_tokens)
        return (fresh * self.input_per_mtok + cached_tokens * cached_rate + output_tokens * self.output_per_mtok) / 1_000_000


@dataclass(frozen=True)
class ModelBudget:
    max_input_tokens_per_call: int = 120_000  # an upper-bound estimate: a full 80,000-character context fits
    max_output_tokens_per_call: int = 8_000
    max_calls_per_visit: int = 4  # one call, two transport retries, one corrective re-ask
    max_calls_per_run: int = 30
    max_input_tokens_per_run: int = 400_000
    max_output_tokens_per_run: int = 80_000
    max_estimated_usd_per_run: float | None = None  # only enforceable when ``pricing`` is set
    pricing: Pricing | None = None

    def __post_init__(self) -> None:
        for name in ("max_input_tokens_per_call", "max_output_tokens_per_call", "max_calls_per_visit", "max_calls_per_run",
                     "max_input_tokens_per_run", "max_output_tokens_per_run"):  # fmt: skip
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.max_estimated_usd_per_run is not None and self.pricing is None:
            raise ValueError("a dollar ceiling needs configured pricing: without it, cost cannot be estimated")


#: Tokens a provider adds around the text Agent Factory sends: the tool-use system prompt (286 tokens on
#: claude-sonnet-5-5, per its documentation), message framing, and the request's fixed wording. Generous on purpose.
REQUEST_OVERHEAD_TOKENS = 1024


def estimate_tokens(text: str) -> int:
    """An upper bound on the input tokens a request will be billed for, computed before sending.

    One token per UTF-8 byte: a byte-level tokenizer's token always covers at
    least one byte of text, so no tokenizer can produce more tokens than this
    (current Claude models produce about 30% more tokens than earlier ones,
    still far below it). Plus ``REQUEST_OVERHEAD_TOKENS`` for what the provider
    adds. Being pessimistic only reserves more than is spent; the actual count
    replaces the reservation once the provider reports it. An optimistic
    estimate could let a call exceed a budget, which must never happen.
    """
    return len(text.encode("utf-8")) + REQUEST_OVERHEAD_TOKENS


@dataclass
class UsageTotals:
    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    unfinished: int = 0  # calls started but never finished: counted at their reservation
    estimated_usd: float | None = None
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls, "failed_calls": self.failed_calls, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "cached_tokens": self.cached_tokens, "unfinished": self.unfinished,
            "estimated_usd": None if self.estimated_usd is None else round(self.estimated_usd, 6),
            "by_model": [dict(v) for _, v in sorted(self.by_model.items())],
        }  # fmt: skip


class UsageLedger:
    """The run's model-usage log. Every write is fsync'd before the call it describes proceeds."""

    def __init__(self, run_dir: Path, *, pricing: Pricing | None = None) -> None:
        from ..engine.log import ExecutionLog  # the same append-only, hash-chained, fsync'd format (bound late: no cycle)

        self.log = ExecutionLog(Path(run_dir) / USAGE_LOG)
        self.pricing = pricing

    def started(self, *, request_id: str, run_id: str, node: str, visit: int, call: int, agent: str, provider: str,
                model: str, prompt_version: str, reserved_input: int, reserved_output: int) -> None:  # fmt: skip
        self.log.append(
            "call_started", request_id=request_id, run_id=run_id, node=node, visit=visit, call=call, agent=agent,
            provider=provider, model=model, prompt_version=prompt_version, reserved_input=reserved_input,
            reserved_output=reserved_output,
        )  # fmt: skip

    def finished(self, *, request_id: str, status: str, input_tokens: int, output_tokens: int, cached_tokens: int,
                 latency_ms: int, category: str | None = None) -> None:  # fmt: skip
        self.log.append(
            "call_finished", request_id=request_id, status=status, input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens, latency_ms=latency_ms, category=category,
        )  # fmt: skip

    def calls_in_visit(self, node: str, visit: int) -> int:
        return sum(1 for e in self.log.entries() if e["event"] == "call_started" and e["node"] == node and e["visit"] == visit)

    def totals(self) -> UsageTotals:
        """Usage so far: each call once; an unfinished call at its reservation (it may have been billed)."""
        started: dict[str, dict[str, Any]] = {}
        finished: dict[str, dict[str, Any]] = {}
        for entry in self.log.entries():
            if entry["event"] == "call_started":
                started[entry["request_id"]] = entry
            elif entry["event"] == "call_finished":
                finished[entry["request_id"]] = entry
        totals = UsageTotals()
        cost = 0.0
        for request_id, start in started.items():
            end = finished.get(request_id)
            if end is None:
                inp, out, cached, failed = start["reserved_input"], start["reserved_output"], 0, True
                totals.unfinished += 1
            else:
                inp, out, cached = end["input_tokens"], end["output_tokens"], end["cached_tokens"]
                failed = end["status"] != "ok"
                if failed and end.get("category") in ("timeout", "provider_unavailable") and not (inp or out):
                    # No answer arrived: the provider may still have processed it. Count the reservation.
                    inp, out = start["reserved_input"], start["reserved_output"]
            totals.calls += 1
            totals.failed_calls += int(failed)
            totals.input_tokens += inp
            totals.output_tokens += out
            totals.cached_tokens += cached
            key = f"{start['provider']}/{start['model']}"
            row = totals.by_model.setdefault(key, {"provider": start["provider"], "model": start["model"], "calls": 0,
                                                   "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})  # fmt: skip
            row["calls"] += 1
            row["input_tokens"] += inp
            row["output_tokens"] += out
            row["cached_tokens"] += cached
            if self.pricing is not None:
                cost += self.pricing.cost(inp, out, cached)
        totals.estimated_usd = cost if self.pricing is not None else None
        return totals

    def check(self, budget: ModelBudget, *, node: str, visit: int, reserve_input: int, reserve_output: int) -> None:
        """Raise ``ModelBudgetExhausted`` unless one more call reserving this much fits every limit."""
        if reserve_input > budget.max_input_tokens_per_call:
            raise ModelBudgetExhausted(
                f"the request needs about {reserve_input} input tokens; the per-call limit is {budget.max_input_tokens_per_call}"
            )
        if reserve_output > budget.max_output_tokens_per_call:
            raise ModelBudgetExhausted(f"the output cap {reserve_output} exceeds the per-call limit {budget.max_output_tokens_per_call}")
        if self.calls_in_visit(node, visit) >= budget.max_calls_per_visit:
            raise ModelBudgetExhausted(f"{node} visit {visit} has used its {budget.max_calls_per_visit} model calls")
        totals = self.totals()
        if totals.calls >= budget.max_calls_per_run:
            raise ModelBudgetExhausted(f"the run has used its {budget.max_calls_per_run} model calls")
        if totals.input_tokens + reserve_input > budget.max_input_tokens_per_run:
            raise ModelBudgetExhausted(f"the run's input-token budget ({budget.max_input_tokens_per_run}) would be exceeded")
        if totals.output_tokens + reserve_output > budget.max_output_tokens_per_run:
            raise ModelBudgetExhausted(f"the run's output-token budget ({budget.max_output_tokens_per_run}) would be exceeded")
        if budget.max_estimated_usd_per_run is not None and budget.pricing is not None:
            worst = (totals.estimated_usd or 0.0) + budget.pricing.cost(reserve_input, reserve_output)
            if worst > budget.max_estimated_usd_per_run:
                raise ModelBudgetExhausted(f"the run's estimated-cost ceiling (${budget.max_estimated_usd_per_run:.2f}) would be exceeded")


__all__ = ["REQUEST_OVERHEAD_TOKENS", "USAGE_LOG", "ModelBudget", "ModelBudgetExhausted", "Pricing", "UsageLedger", "UsageTotals", "estimate_tokens"]

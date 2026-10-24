"""The Model Gateway: the only path from an agent to a model provider (docs/model-providers.md).

Like the Tool Gateway for tools, every model call of a node visit goes through
one gateway, which:

1. builds the request from a versioned prompt template, the agent's
   capability manifest (trusted) and the assembled project context (untrusted);
2. refuses it if it would carry a secret (``RequestGuard``): nothing is sent;
3. checks the budgets (per call, per visit, per run) and **records the call
   in the usage ledger before sending it**;
4. calls the provider under a deadline, retrying only transient failures,
   a bounded number of times, with backoff;
5. records the outcome and the usage, then validates the answer against the
   template's schema; an invalid answer gets **one** corrective re-ask;
6. writes an audit record (``model_call``) to the run's execution log:
   provider, model, prompt version, request ID, usage, latency, outcome, and
   which files left the machine. Never prompts in full, never hidden reasoning.

What it never does: execute anything the model says. A valid answer is data
for the agent, which acts only through the Tool Gateway.

Failures, and what the run does (``Executor``):

=============================================  ============================
budget exhausted, provider configuration,      BLOCKED (a person acts; no
transient failures still failing after retries  graph retry is consumed)
secret in request, context too large, request  FAILED (the node failed)
rejected, invalid answer after one re-ask
=============================================  ============================
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..agents.base import AgentOutputError
from ..agents.prompts import PromptTemplate
from ..agents.schema import validate
from ..util import canonical_json, sha256_hex
from .base import ModelProvider, ModelRequest, ProviderBadResponse, ProviderError
from .budget import ModelBudget, UsageLedger, estimate_tokens
from .capabilities import CapabilityManifest
from .guard import RequestGuard


class ModelUnavailable(ProviderError):
    """The provider kept failing transiently (timeouts, rate limits, overload) after the bounded retries."""

    category = "model_unavailable"


@dataclass(frozen=True)
class RetryPolicy:
    """Transport retries for one model call. Bounded: never more than ``max_retries`` per call."""

    max_retries: int = 2
    base_delay_s: float = 2.0
    max_delay_s: float = 30.0

    def delay(self, retry: int, retry_after: float | None) -> float:
        wanted = self.base_delay_s * (2 ** (retry - 1))
        if retry_after is not None:
            wanted = max(wanted, retry_after)
        return min(self.max_delay_s, wanted)


@dataclass
class ModelGateway:
    provider: ModelProvider
    ledger: UsageLedger
    budget: ModelBudget
    guard: RequestGuard
    manifest: CapabilityManifest
    run_id: str
    node: str
    agent: str
    visit: int
    record_action: Callable[[dict[str, Any]], None] = lambda entry: None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    timeout_s: float = 120.0

    def ask(self, template: PromptTemplate, inputs: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """A schema-valid answer for ``template``, or a classified error. See the module docstring."""
        problems: list[str] | None = None
        for corrective in (False, True):
            call_inputs = inputs if problems is None else {**inputs, "previous_answer_problems": problems[:10]}
            content, response_model, hint = self._call(template, call_inputs, context or {}, corrective=corrective)
            if content is None:
                problems = [hint or "the answer was not a single JSON object matching the schema"]
            else:
                problems = validate(content, template.output_schema)
                if not problems:
                    return content
            self._audit_invalid(template, problems, response_model)
        raise AgentOutputError(f"{self.agent}: model output failed validation twice: {problems}")

    # ---------------------------------------------------------------- one call (with transport retries)

    def _request(self, template: PromptTemplate, inputs: dict[str, Any], call: int) -> ModelRequest:
        request_id = "mreq-" + sha256_hex(canonical_json([self.run_id, self.node, self.visit, call]))[:20]
        return ModelRequest(
            run_id=self.run_id, agent=self.agent, purpose=template.purpose, instructions=template.text, inputs=inputs,
            output_schema=template.output_schema, attempt=self.visit, prompt_version=template.id, request_id=request_id,
            call=call, capabilities=self.manifest.to_dict(), max_output_tokens=self.budget.max_output_tokens_per_call,
            timeout_s=self.timeout_s,
        )  # fmt: skip

    def _call(self, template: PromptTemplate, inputs: dict[str, Any], context: dict[str, Any], *, corrective: bool):
        retries = 0
        while True:
            call = self.ledger.calls_in_visit(self.node, self.visit) + 1
            request = self._request(template, inputs, call)
            self.guard.check(request)  # fail closed: a secret never leaves; nothing recorded as spent
            reserve_input = estimate_tokens(canonical_json([request.instructions, request.inputs, request.capabilities,
                                                            request.output_schema]))  # fmt: skip
            self.ledger.check(self.budget, node=self.node, visit=self.visit, reserve_input=reserve_input,
                              reserve_output=request.max_output_tokens)  # fmt: skip
            model = getattr(self.provider, "model", "unknown")
            self.ledger.started(
                request_id=request.request_id, run_id=self.run_id, node=self.node, visit=self.visit, call=call,
                agent=self.agent, provider=self.provider.name, model=model, prompt_version=template.id,
                reserved_input=reserve_input, reserved_output=request.max_output_tokens,
            )  # fmt: skip
            started = self.monotonic()
            try:
                response = self.provider.complete(request)
            except ProviderError as exc:
                latency = int((self.monotonic() - started) * 1000)
                spent = {k: int(exc.usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cached_tokens")}
                self.ledger.finished(request_id=request.request_id, status="error", latency_ms=latency,
                                     category=exc.category, **spent)  # fmt: skip
                self._audit(request, template, model, "error", spent, latency, context, category=exc.category,
                            corrective=corrective, shape=exc.shape)  # fmt: skip
                if isinstance(exc, ProviderBadResponse):
                    return None, model, exc.hint  # the model answered, but unusably: one corrective re-ask
                if exc.transient and retries < self.retry.max_retries:
                    retries += 1
                    self.sleep(self.retry.delay(retries, exc.retry_after))
                    continue
                if exc.transient:
                    raise ModelUnavailable(f"{self.provider.name}: {exc.category} after {retries + 1} attempts") from None
                raise
            latency = int((self.monotonic() - started) * 1000)
            usage = {k: int(response.usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cached_tokens")}
            valid_shape = isinstance(response.content, dict)
            self.ledger.finished(request_id=request.request_id, status="ok" if valid_shape else "malformed",
                                 latency_ms=latency, **usage)  # fmt: skip
            self._audit(request, template, response.model, "ok" if valid_shape else "malformed", usage, latency, context,
                        corrective=corrective, shape=response.shape)  # fmt: skip
            return (response.content if valid_shape else None), response.model, None

    # ---------------------------------------------------------------- audit

    def _audit(self, request: ModelRequest, template: PromptTemplate, model: str, status: str, usage: dict[str, int],
               latency_ms: int, context: dict[str, Any], *, category: str | None = None, corrective: bool = False,
               shape: dict[str, Any] | None = None) -> None:  # fmt: skip
        entry = {
            "kind": "model_call", "provider": self.provider.name, "model": model, "purpose": template.purpose,
            "prompt_version": template.id, "request_id": request.request_id, "attempt": self.visit, "call": request.call,
            "corrective": corrective, "result": status, "category": category, "usage": usage, "latency_ms": latency_ms,
            "context": context,
        }  # fmt: skip
        if shape is not None:
            entry["response_shape"] = shape  # structure and counts only (the adapter's answer_shape), never content
        self.record_action(entry)

    def _audit_invalid(self, template: PromptTemplate, problems: list[str], model: str) -> None:
        self.record_action({
            "kind": "model_output_rejected", "provider": self.provider.name, "model": model, "prompt_version": template.id,
            "attempt": self.visit, "problems": [p[:200] for p in problems[:10]],
        })  # fmt: skip


__all__ = ["ModelGateway", "ModelUnavailable", "RetryPolicy"]

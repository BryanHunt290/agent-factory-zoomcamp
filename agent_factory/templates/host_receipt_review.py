"""U2A receipt gate around U1. Receipt consumption occurs outside the engine."""

from dataclasses import replace

from ..agents.base import Agent, AgentResult
from ..integrations.opportunities.host_receipts import ReceiptError
from ..util import utc_now
from .opportunity_review import opportunity_review_template


class ReceiptReviewAgent(Agent):
    def __init__(self, inner, session, accepted, clock):
        self.name = inner.name
        self.writes = inner.writes
        self._inner = inner
        self._session = session
        self._accepted = accepted
        self._clock = clock

    def run(self, ctx):
        try:
            if self._accepted is None:
                raise ReceiptError()
            audit = self._session.check_accepted(self._accepted, run_id=ctx.run_id, now=self._clock())
            receipt_digest = audit["receipt_digest"]
            inner_ctx = ctx
            if self.name != "validate":
                if type(ctx.state.plan) is not dict or ctx.state.plan.get("host_receipt_digest") != receipt_digest:
                    raise ReceiptError()
                state = ctx.state.snapshot()
                state.plan.pop("host_receipt_digest")
                inner_ctx = replace(ctx, state=state)
            result = self._inner.run(inner_ctx)
            if self.name == "validate" and result.outcome == "ok":
                result.updates["plan"]["host_receipt_digest"] = receipt_digest
            if "review" in result.updates:
                result.updates["review"]["host_receipt"] = audit
            return result
        except ReceiptError:
            return AgentResult("blocked", {"review": {"schema_version": "opportunity-review-v1",
                               "status": "BLOCKED", "reason": "HOST_RECEIPT_REJECTED",
                               "application_ready": False, "execution_authorized": False}},
                               "Host receipt review blocked")


def host_receipt_review_template(receipt, *, request, session, registry, assessment_policy,
                                 mappings=(), clock=utc_now):
    """Authenticate once in trusted coordinator code, then reuse U1 intake/assessment.

    A rejected receipt creates a blocked-review template containing no raw input.
    Keep the returned template/session in memory for resume; re-consuming a receipt
    is replay, and losing the session requires a fresh acquisition. No live I/O.
    """
    try:
        accepted = session.accept(receipt, request=request, now=clock())
    except ReceiptError:
        accepted = None
    payload = accepted._payload if accepted is not None else "{}"
    inner = opportunity_review_template(payload, mappings=mappings, registry=registry,
                                        assessment_policy=assessment_policy, clock=clock)
    return replace(inner, name="host-receipt-review-u2a",
                   description="Offline authenticated host receipt and capability review.",
                   agent_factory=lambda: {name: ReceiptReviewAgent(agent, session, accepted, clock)
                                          for name, agent in inner.agent_factory().items()})

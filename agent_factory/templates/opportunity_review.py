"""Offline validate -> assess -> review-report through the existing executor.

Not globally registered: callers explicitly supply observations, mappings and
the target registry/policy. No models, tools, source code or network are invoked.
The generic executor alone persists projected outputs. Resume requires the same
in-memory inputs and rechecks their binding and freshness.
"""

from ..agents.base import Agent, AgentResult
from ..graph import Edge, GraphDefinition, Node
from ..integrations.opportunities.contracts import ObservationError, digest, parse_observations, require
from ..integrations.opportunities.assessment import RequirementMapping, review_batch
from ..safety.permissions import ActionCategory, Decision, PermissionPolicy
from ..state import TaskSpec
from ..util import utc_now
from .base import ProjectTemplate


def review_graph():
    return GraphDefinition(name="opportunity-review-v1", start="validate", max_steps=4,
                           nodes=(Node("validate", "validate", max_visits=1),
                                  Node("assess", "assess", max_visits=1),
                                  Node("review_report", "review_report", max_visits=1),
                                  Node("done", terminal=True)),
                           edges=(Edge("validate", "ok", "assess"), Edge("assess", "ok", "review_report"),
                                  Edge("review_report", "ok", "done"),
                                  Edge("validate", "blocked", "done"), Edge("assess", "blocked", "done"),
                                  Edge("review_report", "blocked", "done")))


class ReviewAgent(Agent):
    def __init__(self, name, field, payload, mappings, registry, policy, clock):
        self.name = name
        self.writes = frozenset((field, "review"))
        self._field = field
        self._payload = payload
        self._mappings = mappings
        self._registry = registry
        self._policy = policy
        self._clock = clock

    def run(self, ctx):
        try:
            batch = parse_observations(self._payload, now=self._clock())
            signature = digest([batch.fingerprint, sorted(m.signature() for m in self._mappings)])
            if self.name != "validate":
                require(ctx.state.plan == {"input_digest": signature})
            if self.name == "validate":
                return AgentResult("ok", {"plan": {"input_digest": signature}}, "Observation validated")
            report = review_batch(batch, self._mappings, self._registry, self._policy)
            if self.name == "assess":
                return AgentResult("ok", {"implementation": {"report_digest": digest(report)}}, "Capabilities assessed")
            # Recompute instead of trusting cached assessment/availability after a restart.
            require(ctx.state.implementation == {"report_digest": digest(report)})
            return AgentResult("ok", {"review": report}, "Offline review prepared; no action authorized")
        except ObservationError:
            return AgentResult("blocked", {"review": {"schema_version": "opportunity-review-v1",
                               "status": "BLOCKED", "reason": "INVALID_OR_CHANGED_INPUT",
                               "application_ready": False, "execution_authorized": False}},
                               "Opportunity review blocked")


def opportunity_review_template(payload, *, mappings=(), registry, assessment_policy, clock=utc_now):
    """Inputs remain caller-owned in memory; task/scaffold never carry source text."""
    require(type(payload) is str and len(payload) <= 100_000)
    require(type(mappings) is tuple and len(mappings) <= 3)
    require(all(type(m) is RequirementMapping for m in mappings))
    # No fallback to default tools: even reads and sandbox execution are denied.
    policy = PermissionPolicy(category_rules={c: Decision.DENY for c in ActionCategory},
                              agent_tools={name: frozenset() for name in ("validate", "assess", "review_report")})
    return ProjectTemplate(name="opportunity-review-u1", description="Offline opportunity capability review only.",
                           task=TaskSpec(title="Opportunity review", goal="Prepare a read-only capability report."),
                           graph_factory=review_graph, policy=policy,
                           agent_factory=lambda: {name: ReviewAgent(name, field, payload, mappings, registry,
                                                                   assessment_policy, clock)
                                                  for name, field in (("validate", "plan"), ("assess", "implementation"),
                                                                      ("review_report", "review"))})

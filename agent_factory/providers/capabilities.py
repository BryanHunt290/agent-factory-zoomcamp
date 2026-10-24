"""Capability manifests: what an agent may do, described safely (docs/model-providers.md).

A manifest is derived from two trusted sources only: the permission policy
(which tools an agent may use, and which categories need a human first) and
the tool registry (what each tool is). Project content, model output, and the
environment never contribute to it. A model therefore cannot gain a capability
by asking for one, or because a file says it has one.

A manifest *describes*; it authorizes nothing. Every action is still decided by
the Tool Gateway at call time. The manifest is the foundation of a future
Capability Registry: tool packs (Git/GitHub, AWS Agent Toolkit, project
tools) register tools with categories and descriptions, and appear here
without ever bypassing the gateway, approvals, ActionExecution, or audit.

Descriptions are static strings written by maintainers. They never contain
credentials, endpoints, account IDs, or host paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..safety.permissions import ActionCategory, Decision, PermissionPolicy
from ..tools.base import ToolRegistry

if TYPE_CHECKING:
    from ..capabilities.registry import CapabilityRegistry

MAX_DESCRIPTION = 200


@dataclass(frozen=True)
class Capability:
    tool: str
    category: str
    decision: str  # "allow" or "require_approval"
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"tool": self.tool, "category": self.category, "decision": self.decision, "description": self.description}


@dataclass(frozen=True)
class CapabilityManifest:
    agent: str
    available: tuple[Capability, ...]
    unavailable: tuple[str, ...]  # registered tools this agent may not use (names only)
    #: How the model's answer turns into actions: always by the orchestrator, never by the model.
    acts_through: str = "orchestrator"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "acts_through": self.acts_through,
            "available": [c.to_dict() for c in self.available],
            "requires_human_approval": sorted(c.tool for c in self.available if c.decision == Decision.REQUIRE_APPROVAL.value),
            "unavailable": list(self.unavailable),
            "note": "The model cannot call tools. The orchestrator turns a schema-valid answer into actions and checks each one against its policy.",
        }

    def allows(self, tool: str) -> bool:
        return any(c.tool == tool for c in self.available)


def manifest_for(agent: str, policy: PermissionPolicy, registry: ToolRegistry,
                 capabilities: CapabilityRegistry | None = None) -> CapabilityManifest:  # fmt: skip
    """The capabilities ``agent`` has under ``policy``, for the tools in ``registry``.

    With a Capability Registry, a tool counts only while its capability is registered and available,
    and the capability's own rules may make the decision stricter (never looser). The shape of the
    manifest is unchanged: it is description, and the model's only callable tool stays submit_result.
    """
    available, unavailable = [], []
    for name in registry.names():
        tool = registry.get(name)
        assert tool is not None
        decision = policy.decide(agent, name, ActionCategory(tool.category))
        if capabilities is not None:
            cap = capabilities.for_tool(name)
            if cap is None or not capabilities.availability(cap.capability_id).available:
                decision = Decision.DENY
            else:
                decision = capabilities.effective_decision(decision, cap)
        if decision is Decision.DENY:
            unavailable.append(name)
            continue
        description = " ".join((tool.description or name).split())[:MAX_DESCRIPTION]
        available.append(Capability(tool=name, category=tool.category.value, decision=decision.value, description=description))
    return CapabilityManifest(agent=agent, available=tuple(available), unavailable=tuple(unavailable))


__all__ = ["Capability", "CapabilityManifest", "manifest_for"]

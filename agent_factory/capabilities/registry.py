"""The Capability Registry: what this installation can actually do, and what proves it (docs/capabilities.md).

The registry is built from tool packs, in code, and from nothing else: not project files, not model
output, not the environment. It answers "what exists, is it available now, and why do we believe it?".
It never authorizes anything. The Tool Gateway still decides every call, and the registry can only
make a decision stricter (``effective_decision``).

Registration fails closed on anything malformed, duplicated, or contradictory, including a
capability that claims weaker semantics than the tool that implements it.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from ..safety.execution import semantics_of
from ..safety.permissions import Decision, PermissionPolicy
from ..tools.base import Tool, ToolRegistry
from ..util import Clock, utc_now
from .model import (
    ApprovalRule,
    Availability,
    AvailabilityStatus,
    Capability,
    CapabilityError,
    Evidence,
    EvidenceKind,
    effective_decision,
    thaw,
)
from .live_evidence import ENVIRONMENTS, EVIDENCE_PATH, LiveEvidenceError, LiveVerification, load_live_evidence
from .packs import ToolPack

#: Where TESTED claims are checked. Outside a source checkout there are no tests, so nothing is TESTED.
DEFAULT_TESTS_ROOT = Path(__file__).resolve().parents[2]


@cache
def _test_functions(path: Path) -> frozenset[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return frozenset()
    return frozenset(n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))


def named_test_exists(test_id: str, root: Path = DEFAULT_TESTS_ROOT) -> bool:
    """Whether ``tests/<file>.py::test_<name>`` names a real test function (parsed, never imported or run)."""
    file, _, name = test_id.partition("::")
    return name in _test_functions((root / file).resolve())


def _tool_parameters(tool: Tool) -> tuple[set[str], set[str]] | None:
    """The tool's named keyword parameters and the required ones; None if it takes arbitrary keywords."""
    params = list(inspect.signature(tool.run).parameters.values())[1:]  # skip the workspace
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return None
    named = {p.name for p in params if p.name != "context"}
    required = {p.name for p in params if p.name != "context" and p.default is p.empty}
    return named, required


@dataclass(frozen=True)
class CapabilityView:
    """What a model may read about one capability: a description, never internals or credentials."""

    capability_id: str
    version: int
    description: str
    decision: str  # "allow" or "require_approval"
    effect: str
    mutates: bool
    cost: str
    available: bool
    unavailable_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability_id, "version": self.version, "description": self.description,
                "decision": self.decision, "effect": self.effect, "mutates": self.mutates, "cost": self.cost,
                "available": self.available, "unavailable_reason": self.unavailable_reason}  # fmt: skip


@dataclass(frozen=True)
class AgentCapabilityView:
    agent: str
    capabilities: tuple[CapabilityView, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "capabilities": [c.to_dict() for c in self.capabilities],
                "note": "Descriptions only. None of these is a callable tool; Agent Factory performs them "
                        "through its Tool Gateway after validating a structured answer."}  # fmt: skip


class CapabilityRegistry:
    def __init__(self, packs: Iterable[ToolPack], *, clock: Clock = utc_now, tests_root: Path = DEFAULT_TESTS_ROOT,
                 environment: str = "dev", live_evidence_path: Path = EVIDENCE_PATH) -> None:  # fmt: skip
        """``environment`` is where this installation runs (``dev``, ``staging``, ``prod``): LIVE_TESTED counts only
        for records from the same environment. Live evidence comes only from ``live_evidence_path`` (the
        repository's reviewed file by default); a file that does not verify grants no LIVE_TESTED at all."""
        if environment not in ENVIRONMENTS:
            raise CapabilityError("environment must be dev, staging, or prod")
        self._clock = clock
        self._tests_root = tests_root
        self.environment = environment
        self._packs: dict[str, ToolPack] = {}
        self._caps: dict[str, Capability] = {}
        self._by_tool: dict[str, Capability] = {}
        self._tools: dict[str, tuple[Tool, ToolPack]] = {}
        self._probes: dict[str, Availability] = {}
        self._cap_probes: dict[str, Availability] = {}
        try:
            self._live: tuple[LiveVerification, ...] = load_live_evidence(live_evidence_path)
            self.live_evidence_valid = True
        except (LiveEvidenceError, OSError):
            self._live = ()
            self.live_evidence_valid = False
        for pack in packs:
            self._register(pack)
        self.refresh()

    # ------------------------------------------------------------------ registration

    def _register(self, pack: ToolPack) -> None:
        name = getattr(pack, "name", None)
        if not isinstance(name, str) or name in self._packs:
            raise CapabilityError(f"pack {name!r} is unnamed or already registered")
        tools = {}
        for tool in pack.tools():
            if tool.name in tools or tool.name in self._tools:
                raise CapabilityError(f"tool {tool.name!r} is registered by more than one pack")
            tools[tool.name] = tool
        caps = pack.capabilities()
        claimed = [c.implemented_by for c in caps if isinstance(c, Capability) and c.executable]
        if len(claimed) != len(set(claimed)):
            raise CapabilityError(f"pack {name!r}: one tool implements more than one capability")
        for cap in caps:
            if not isinstance(cap, Capability):
                raise CapabilityError(f"pack {name!r} registered something that is not a Capability")
            if cap.pack != name:
                raise CapabilityError(f"{cap.capability_id}: pack {name!r} may only register its own namespace")
            if cap.capability_id in self._caps or cap.capability_id in {c.capability_id for c in caps if c is not cap}:
                raise CapabilityError(f"duplicate capability {cap.capability_id!r}")
            if cap.executable:
                tool = tools.get(cap.implemented_by)  # type: ignore[arg-type]
                if tool is None:
                    raise CapabilityError(f"{cap.capability_id}: implementing tool {cap.implemented_by!r} is not in pack {name!r}")
                self._check_against_tool(cap, tool)
                if cap.implemented_by in self._by_tool:
                    raise CapabilityError(f"tool {cap.implemented_by!r} implements more than one capability")
        unclaimed = set(tools) - {c.implemented_by for c in caps}
        if unclaimed:
            raise CapabilityError(f"pack {name!r} has tools no capability names: {sorted(unclaimed)}")
        self._packs[name] = pack
        for cap in caps:
            self._caps[cap.capability_id] = cap
            if cap.executable:
                self._by_tool[cap.implemented_by] = cap  # type: ignore[index]
        for tool in tools.values():
            self._tools[tool.name] = (tool, pack)

    @staticmethod
    def _check_against_tool(cap: Capability, tool: Tool) -> None:
        """A capability must describe its tool exactly: never a weaker category, effect, or parameter set."""
        if tool.category != cap.category:
            raise CapabilityError(f"{cap.capability_id}: category {cap.category.value!r} does not match tool "
                                  f"{tool.name!r} ({tool.category.value!r})")  # fmt: skip
        semantics = semantics_of(tool)  # an undeclared tool counts as the most conservative class
        if semantics.effect != cap.effect:
            raise CapabilityError(f"{cap.capability_id}: effect {cap.effect.value!r} does not match tool {tool.name!r} "
                                  f"({semantics.effect.value!r}); a capability cannot weaken its tool's semantics")  # fmt: skip
        params = _tool_parameters(tool)
        if params is not None:
            named, required = params
            declared = set(cap.input_schema.get("properties") or {})
            if declared != named or set(cap.input_schema.get("required") or ()) != required:
                raise CapabilityError(f"{cap.capability_id}: the input schema does not match tool {tool.name!r}'s "
                                      f"parameters {sorted(named)} (required {sorted(required)})")  # fmt: skip

    # ------------------------------------------------------------------ lookups

    def capabilities(self) -> tuple[Capability, ...]:
        """Every registered capability, in a stable order (by id)."""
        return tuple(self._caps[k] for k in sorted(self._caps))

    def get(self, capability_id: str) -> Capability | None:
        return self._caps.get(capability_id) if isinstance(capability_id, str) else None

    def for_tool(self, tool_name: str) -> Capability | None:
        return self._by_tool.get(tool_name)

    def pack_enabled(self, name: str) -> bool:
        pack = self._packs.get(name)
        return bool(pack is not None and pack.enabled is True)

    def tool_registry(self) -> ToolRegistry:
        """A ToolRegistry holding only the tools of enabled packs (existing semantics checks apply)."""
        return ToolRegistry([tool for tool, pack in self._tools.values() if pack.enabled is True])

    # ------------------------------------------------------------------ availability and evidence

    def refresh(self) -> None:
        """Re-run every enabled pack's probe. Probes are read-only and free; a probe that fails is UNAVAILABLE."""
        now = self._clock().isoformat().replace("+00:00", "Z")
        for name, pack in self._packs.items():
            if pack.enabled is not True:
                self._probes[name] = Availability(AvailabilityStatus.NOT_CONFIGURED, checked_at=now, detail="pack not enabled")
                continue
            try:
                result = pack.probe(now)
            except Exception:  # noqa: BLE001 - a broken probe must make the pack unavailable, never available
                result = Availability(AvailabilityStatus.PROBE_FAILED, checked_at=now, detail="the probe raised an error")
            if not isinstance(result, Availability):
                result = Availability(AvailabilityStatus.PROBE_FAILED, checked_at=now, detail="the probe returned no result")
            self._probes[name] = result
            for cap in self._caps.values():
                if cap.pack != name or not result.available or not cap.executable:
                    self._cap_probes.pop(cap.capability_id, None)
                    continue
                try:
                    narrow = pack.probe_capability(cap, now)
                except Exception:  # noqa: BLE001 - as above: fail closed
                    narrow = Availability(AvailabilityStatus.PROBE_FAILED, checked_at=now, detail="the probe raised an error")
                if narrow is None:
                    self._cap_probes.pop(cap.capability_id, None)
                else:
                    self._cap_probes[cap.capability_id] = narrow if isinstance(narrow, Availability) else Availability(
                        AvailabilityStatus.PROBE_FAILED, checked_at=now, detail="the probe returned no result")

    def availability(self, capability_id: str) -> Availability:
        cap = self.get(capability_id)
        if cap is None:
            return Availability(AvailabilityStatus.NOT_IMPLEMENTED, detail="not registered")
        probe = self._probes[cap.pack]
        if not probe.available:
            return probe
        if not cap.executable:
            return Availability(AvailabilityStatus.NOT_IMPLEMENTED, checked_at=probe.checked_at,
                                detail="defined, with no implementation")  # fmt: skip
        return self._cap_probes.get(capability_id, probe)

    def requires_approval(self, cap: Capability, policy: PermissionPolicy) -> bool:
        """Agent-independent: does the category's rule or the capability's own rule require a person?"""
        rule = policy.category_rules.get(cap.category, Decision.DENY)
        return effective_decision(rule, cap) is Decision.REQUIRE_APPROVAL

    def evidence(self, capability_id: str, policy: PermissionPolicy | None = None) -> tuple[Evidence, ...]:
        """Why Agent Factory believes what it does about one capability. Documentation is never evidence."""
        cap = self.get(capability_id)
        if cap is None:
            return ()
        out: list[Evidence] = []
        if cap.executable and cap.implemented_by in self._tools:
            out.append(Evidence(EvidenceKind.IMPLEMENTED, f"tool:{cap.implemented_by}", f"pack {cap.pack}"))
        for test in cap.tests:
            if named_test_exists(test, self._tests_root):
                out.append(Evidence(EvidenceKind.TESTED, test))
        live_backends = getattr(self._packs[cap.pack], "live_backends", frozenset())
        for record in self._live:
            # Only this exact version, from this environment, by a backend this pack declares as real.
            if (record.capability_id, record.version, record.environment) == (cap.capability_id, cap.version, self.environment) \
                    and record.backend in live_backends:  # fmt: skip
                out.append(Evidence(EvidenceKind.LIVE_TESTED, f"verification:{record.verification_id}",
                                    f"{record.backend} backend, suite {record.suite}, commit {record.commit}",
                                    record.recorded_at))  # fmt: skip
        availability = self.availability(capability_id)
        if availability.available:
            out.append(Evidence(EvidenceKind.AVAILABLE_NOW, f"probe:{cap.pack}", availability.detail, availability.checked_at))
        else:
            out.append(Evidence(EvidenceKind.UNAVAILABLE, f"probe:{cap.pack}", availability.status.value,
                                availability.checked_at))  # fmt: skip
        if cap.approval is ApprovalRule.ALWAYS or (policy is not None and self.requires_approval(cap, policy)):
            out.append(Evidence(EvidenceKind.REQUIRES_APPROVAL, f"policy:{cap.category.value}", cap.approval.value))
        return tuple(out)

    # ------------------------------------------------------------------ decisions and views

    def effective_decision(self, policy_decision: Decision, cap: Capability) -> Decision:
        return effective_decision(policy_decision, cap)

    def decision_for(self, agent: str, policy: PermissionPolicy, cap: Capability) -> Decision:
        if not cap.executable:
            return Decision.DENY
        return effective_decision(policy.decide(agent, cap.implemented_by, cap.category), cap)  # type: ignore[arg-type]

    def view_for(self, agent: str, policy: PermissionPolicy) -> AgentCapabilityView:
        """The capabilities ``agent`` may use, as safe descriptions. Denied ones are left out."""
        views = []
        for cap in self.capabilities():
            decision = self.decision_for(agent, policy, cap)
            if decision is Decision.DENY:
                continue
            availability = self.availability(cap.capability_id)
            views.append(CapabilityView(
                capability_id=cap.capability_id, version=cap.version, description=cap.description,
                decision=decision.value, effect=cap.effect.value, mutates=cap.mutates, cost=cap.cost.value,
                available=availability.available,
                unavailable_reason=None if availability.available else availability.status.value,
            ))  # fmt: skip
        return AgentCapabilityView(agent=agent, capabilities=tuple(views))

    def describe(self, policy: PermissionPolicy | None = None) -> list[dict[str, Any]]:
        """A machine-readable listing for people and tools (CLI, docs). Never contains a credential."""
        out = []
        for cap in self.capabilities():
            out.append({
                "capability": cap.capability_id, "version": cap.version, "pack": cap.pack,
                "description": cap.description, "implemented_by": cap.implemented_by,
                "category": cap.category.value, "effect": cap.effect.value, "mutates": cap.mutates,
                "approval": cap.approval.value, "cost": cap.cost.value, "risk": cap.risk.value,
                "network": cap.network.value, "credential_slot": cap.credential,
                "sandbox_compatible": cap.sandbox_compatible, "iam_actions": list(cap.iam_actions),
                "input_schema": thaw(cap.input_schema), "output_schema": thaw(cap.output_schema),
                "availability": self.availability(cap.capability_id).to_dict(),
                "evidence": [e.to_dict() for e in self.evidence(cap.capability_id, policy)],
            })  # fmt: skip
        return out


__all__ = ["AgentCapabilityView", "CapabilityRegistry", "CapabilityView", "named_test_exists"]

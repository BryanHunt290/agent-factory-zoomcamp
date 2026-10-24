"""Controlled fixtures for the action-execution tests. Nothing here touches anything outside tmp_path.

Two "external systems" are simulated by files in a directory of the test's
choosing (the *oracle*), so a test can count real effects independently of
everything Agent Factory records:

* ``SendNoticeTool``: class C (non-idempotent), like an email or SMS
  gateway. Every call appends a line to ``notices.jsonl``.
  ``UnknownOnceNotice`` (kind ``unknown-notice``) is the same gateway timing
  out after accepting the first notice: sent, but its outcome unknown.
* ``ChargeTool``: class B, keyed, like a payment API with idempotency keys.
  Every call appends to ``charge_calls.jsonl``; an effect is recorded in
  ``charges.jsonl`` only for a key the fake provider has not seen within its
  retention window, which is how real providers deduplicate retries.

``OperatorAgent`` runs one node that calls one approval-gated tool (or the
same call twice, to model a repeated identical request) and records what it
got back.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_factory.agents.base import Agent, AgentContext, AgentResult
from agent_factory.engine import Executor
from agent_factory.graph.definition import Edge, GraphDefinition, Node
from agent_factory.providers import FakeProvider
from agent_factory.safety import (
    ActionCategory,
    EffectClass,
    ExecutionSemantics,
    Idempotency,
    PermissionDenied,
    PermissionPolicy,
)
from agent_factory.tools import Tool, ToolRegistry, ToolResult
from agent_factory.tools.base import OutcomeUnknown, ToolContext
from agent_factory.workspace import Workspace

KEY_RETENTION = timedelta(hours=24)


def _append(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()


def read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class SendNoticeTool(Tool):
    """Class C: an external message that cannot be unsent or deduplicated."""

    name = "send_notice"
    category = ActionCategory.EXTERNAL_COMMUNICATION
    description = "Send a notice to someone outside this machine (test fixture)."
    execution = ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT, rationale="a sent message cannot be unsent")

    def __init__(self, oracle: Path) -> None:
        self.oracle = Path(oracle)

    def run(self, workspace: Workspace, *, to: str, text: str) -> ToolResult:
        _append(self.oracle / "notices.jsonl", {"to": to, "text": text})
        return ToolResult(data={"delivered_to": to}, summary=f"notice sent to {to}")


class UnknownOnceNotice(SendNoticeTool):
    """The notice gateway timing out after accepting the first notice: it was sent, but nobody can tell."""

    def run(self, workspace: Workspace, *, to: str, text: str) -> ToolResult:
        first = not read_lines(self.oracle / "notices.jsonl")
        result = super().run(workspace, to=to, text=text)
        if first:
            raise OutcomeUnknown("the gateway timed out after accepting the message")
        return result


class ChargeTool(Tool):
    """Class B, keyed: the provider deduplicates by the idempotency key Agent Factory sends."""

    name = "charge"
    category = ActionCategory.SPEND_MONEY
    description = "Charge an amount through a payment provider (test fixture)."
    wants_context = True
    execution = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT,
        idempotency=Idempotency.KEYED,
        key_retention=KEY_RETENTION,
        max_attempts=3,
        rationale="the provider deduplicates by idempotency key for 24 hours",
    )

    def __init__(self, oracle: Path) -> None:
        self.oracle = Path(oracle)

    def run(self, workspace: Workspace, *, context: ToolContext, amount: int) -> ToolResult:
        key = context.idempotency_key
        _append(self.oracle / "charge_calls.jsonl", {"key": key, "amount": amount, "attempt": context.attempt})
        if key is None:
            raise AssertionError("a keyed tool must always receive an idempotency key")
        seen = {line["key"] for line in read_lines(self.oracle / "charges.jsonl")}
        if key not in seen:
            _append(self.oracle / "charges.jsonl", {"key": key, "amount": amount})
        return ToolResult(data={"charged": amount, "key": key}, summary=f"charged {amount}")


class OperatorAgent(Agent):
    """One node: call ``tool`` with ``params`` (``repeat`` times), then finish."""

    name = "operator"
    writes = frozenset({"implementation"})

    def __init__(self, tool: str, params: dict[str, Any], *, repeat: int = 1, ask: bool = True) -> None:
        self.tool = tool
        self.params = params
        self.repeat = repeat
        self.ask = ask  # False models a re-run in which the agent no longer asks for the action

    def run(self, ctx: AgentContext) -> AgentResult:
        results, refused = [], []
        if self.ask:
            for _ in range(self.repeat):
                try:
                    result = ctx.tools.call(self.tool, **self.params)
                    results.append({"summary": result.summary, "data": result.data, "replayed": bool(result.details.get("replayed"))})
                except PermissionDenied as exc:
                    refused.append(str(exc))
        return AgentResult("done", {"implementation": {"results": results, "refused": refused}}, summary="operated")


def graph() -> GraphDefinition:
    return GraphDefinition(
        name="operate-v1",
        start="operate",
        nodes=(Node("operate", agent="operator"), Node("done", terminal=True)),
        edges=(Edge("operate", "done", "done"),),
    )


def workspace(root: Path) -> Workspace:
    root = Path(root)
    if (root / ".agent_factory").exists():
        return Workspace.open(root)
    return Workspace.create(root)


def tool_for(kind: str, oracle: Path) -> Tool:
    return {"notice": SendNoticeTool, "unknown-notice": UnknownOnceNotice, "charge": ChargeTool}[kind](oracle)


def params_for(kind: str) -> dict[str, Any]:
    notice = {"to": "billing@example.test", "text": "invoice ready"}
    return {"notice": notice, "unknown-notice": notice, "charge": {"amount": 42}}[kind]


def executor(ws: Workspace, kind: str, oracle: Path, *, recorder=None, ledger=None, clock=None, agent=None) -> Executor:
    tool = tool_for(kind, oracle)
    policy = PermissionPolicy().with_agent_tools("operator", {tool.name})
    extra: dict[str, Any] = {}
    if clock is not None:
        extra["clock"] = clock
    return Executor(
        graph=graph(),
        agents={"operator": agent or OperatorAgent(tool.name, params_for(kind))},
        provider=FakeProvider({}),
        workspace=ws,
        tools=ToolRegistry([tool]),
        policy=policy,
        recorder=recorder,
        ledger=ledger,
        **extra,
    )


def recorder_for(store, ws: Workspace, clock=None):
    """A product-store recorder for the fixture project (milestones, approvals, executions)."""
    from agent_factory.domain.milestones import standard_milestone_policy
    from agent_factory.domain.recorder import ProgressRecorder

    extra = {"clock": clock} if clock is not None else {}
    return ProgressRecorder.for_workspace(
        store, ws, name="Action fixture", policy=standard_milestone_policy(), node_agents={"operate": "operator"}, **extra
    )


def approve_pending(ex: Executor, run_id: str = "run-1"):
    state = ex.store.load(run_id)
    return ex.decide_approval(run_id, state.pending_approval["request_id"], approved=True, approver="tester")

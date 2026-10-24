import os
from typing import Any

import pytest

from agent_factory.engine.actions import workspace_guard
from agent_factory.safety import (
    DANGEROUS_CATEGORIES,
    ActionCategory,
    ApprovalRequired,
    Decision,
    EffectClass,
    ExecutionSemantics,
    ExecutionSemanticsError,
    PermissionDenied,
    PermissionPolicy,
    PolicyError,
)
from agent_factory.tools import Tool, ToolGateway, ToolRegistry, ToolResult, default_registry
from agent_factory.tools.testing import scrubbed_env
from agent_factory.workspace import Workspace


class _DangerousTool(Tool):
    """Stand-in for a future spend/deploy/credential/email tool."""

    execution = ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT, rationale="test stand-in: worst case")

    def __init__(self, category: ActionCategory) -> None:
        self.name = f"do_{category.value}"
        self.category = category
        self.calls = 0

    def run(self, workspace, **params: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(summary="did the dangerous thing")


def _gateway(ws, registry, policy, approvals=None, agent="developer", actions=None, guarded=True):
    return ToolGateway(
        registry=registry,
        policy=policy,
        workspace=ws,
        run_id="run1",
        node=agent,
        agent=agent,
        visit=1,
        approvals=approvals or {},
        on_action=(actions if actions is not None else []).append,
        executions=workspace_guard(ws, "run1", registry=registry) if guarded else None,
    )


def test_default_policy_is_conservative():
    p = PermissionPolicy()
    assert p.decide("developer", "write_file", ActionCategory.WRITE_WORKSPACE) is Decision.ALLOW
    assert p.decide("developer", "delete_file", ActionCategory.DESTRUCTIVE_DELETE) is Decision.REQUIRE_APPROVAL
    # Tools outside an agent's allowlist are denied, whatever their category.
    assert p.decide("reviewer", "write_file", ActionCategory.WRITE_WORKSPACE) is Decision.DENY
    assert p.decide("planner", "run_tests", ActionCategory.EXECUTE_LOCAL) is Decision.DENY
    assert p.decide("unknown-agent", "read_file", ActionCategory.READ_WORKSPACE) is Decision.DENY
    for cat in DANGEROUS_CATEGORIES:
        assert p.category_rules[cat] in (Decision.REQUIRE_APPROVAL, Decision.DENY)
    assert p.category_rules[ActionCategory.NETWORK_ACCESS] is Decision.DENY


@pytest.mark.parametrize("category", sorted(DANGEROUS_CATEGORIES))
def test_dangerous_categories_cannot_be_auto_allowed(category):
    with pytest.raises(PolicyError):
        PermissionPolicy().with_rules(**{category.value: Decision.ALLOW})


def test_unlisted_category_is_denied():
    p = PermissionPolicy(category_rules={}, agent_tools={"a": {"t"}})
    assert p.decide("a", "t", ActionCategory.READ_WORKSPACE) is Decision.DENY


@pytest.mark.parametrize(
    "category",
    [
        ActionCategory.SPEND_MONEY,
        ActionCategory.PRODUCTION_DEPLOY,
        ActionCategory.DESTRUCTIVE_DELETE,
        ActionCategory.CREDENTIAL_CHANGE,
        ActionCategory.EXTERNAL_COMMUNICATION,
    ],
)
def test_approval_gate_blocks_until_human_decides(tmp_path, category):
    ws = Workspace.create(tmp_path / "ws")
    tool = _DangerousTool(category)
    registry = ToolRegistry([tool])
    policy = PermissionPolicy().with_agent_tools("developer", {tool.name})

    with pytest.raises(ApprovalRequired) as exc:
        _gateway(ws, registry, policy).call(tool.name, target="prod")
    assert tool.calls == 0
    req = exc.value.request
    assert req.category == category.value and req.tool == tool.name

    # Same action, same visit -> same deterministic id.
    with pytest.raises(ApprovalRequired) as again:
        _gateway(ws, registry, policy).call(tool.name, target="prod")
    assert again.value.request.request_id == req.request_id

    actions = []
    with pytest.raises(PermissionDenied):
        _gateway(ws, registry, policy, {req.request_id: {"approved": False}}, actions=actions).call(
            tool.name, target="prod"
        )
    assert tool.calls == 0 and actions[-1]["result"] == "denied"

    actions = []
    _gateway(ws, registry, policy, {req.request_id: {"approved": True}}, actions=actions).call(tool.name, target="prod")
    assert tool.calls == 1
    assert actions[-1]["result"] == "ok" and actions[-1]["approval_id"] == req.request_id

    # One approval, one effect: the same approved call again replays the recorded result.
    replayed = _gateway(ws, registry, policy, {req.request_id: {"approved": True}}).call(tool.name, target="prod")
    assert tool.calls == 1 and replayed.details["replayed"] is True

    # Approval is bound to the exact params: a different target needs a new approval.
    with pytest.raises(ApprovalRequired):
        _gateway(ws, registry, policy, {req.request_id: {"approved": True}}).call(tool.name, target="other")


@pytest.mark.parametrize("category", sorted(DANGEROUS_CATEGORIES))
def test_an_approved_dangerous_action_never_runs_without_an_execution_ledger(tmp_path, category):
    ws = Workspace.create(tmp_path / "ws")
    tool = _DangerousTool(category)
    registry = ToolRegistry([tool])
    policy = PermissionPolicy().with_agent_tools("developer", {tool.name})
    with pytest.raises((ApprovalRequired, PermissionDenied)) as exc:
        _gateway(ws, registry, policy, guarded=False).call(tool.name, target="prod")
    if isinstance(exc.value, ApprovalRequired):  # network_access is denied before approval
        approvals = {exc.value.request.request_id: {"approved": True}}
        with pytest.raises(PermissionDenied, match="execution ledger"):
            _gateway(ws, registry, policy, approvals, guarded=False).call(tool.name, target="prod")
    assert tool.calls == 0


@pytest.mark.parametrize("category", sorted(DANGEROUS_CATEGORIES))
def test_a_dangerous_tool_without_declared_semantics_is_refused_at_registration(category):
    class Undeclared(Tool):
        name = "undeclared"

        def run(self, workspace, **params):  # pragma: no cover - never registered
            return ToolResult()

    Undeclared.category = category
    with pytest.raises(ExecutionSemanticsError, match="must declare"):
        ToolRegistry([Undeclared()])


def test_network_access_is_denied_even_if_allowlisted(tmp_path):
    ws = Workspace.create(tmp_path / "ws")
    tool = _DangerousTool(ActionCategory.NETWORK_ACCESS)
    policy = PermissionPolicy().with_agent_tools("developer", {tool.name})
    with pytest.raises(PermissionDenied):
        _gateway(ws, ToolRegistry([tool]), policy).call(tool.name)
    assert tool.calls == 0


def test_gateway_denies_unknown_and_unlisted_tools_and_logs_them(tmp_path):
    ws = Workspace.create(tmp_path / "ws")
    actions = []
    gw = _gateway(ws, default_registry(), PermissionPolicy(), agent="reviewer", actions=actions)
    with pytest.raises(PermissionDenied):
        gw.call("write_file", path="x.py", content="")
    with pytest.raises(PermissionDenied):
        gw.call("rm_rf")
    assert [a["result"] for a in actions] == ["denied", "denied"]
    assert not (ws.project_dir / "x.py").exists()


def test_gateway_logs_hash_of_large_content(tmp_path):
    ws = Workspace.create(tmp_path / "ws")
    actions = []
    gw = _gateway(ws, default_registry(), PermissionPolicy(), actions=actions)
    gw.call("write_file", path="big.py", content="x" * 1000)
    assert actions[0]["params"]["content"]["chars"] == 1000


def test_test_subprocess_env_has_no_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    env = scrubbed_env()
    assert "OPENAI_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env.get("PATH") == os.environ.get("PATH")

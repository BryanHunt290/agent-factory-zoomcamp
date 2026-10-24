"""Real-executor coverage of receipt and assessment resume boundaries."""

from datetime import timedelta
import json

import pytest

from test_opportunity_review import NOW, batch, fixture, mapping, registry
from test_upwork_host_receipts import setup

from agent_factory.capabilities.assessment import Requirement
from agent_factory.factory import build_executor
from agent_factory.providers import FakeProvider
from agent_factory.safety.permissions import Decision, PermissionPolicy
from agent_factory.state import RunStatus
from agent_factory.templates.host_receipt_review import host_receipt_review_template
from agent_factory.templates.opportunity_review import opportunity_review_template
from agent_factory.tools.gateway import ToolGateway


class Interrupted(BaseException):
    """Simulate process loss without turning the checkpoint into a failed run."""


def interrupt_before_node(ctx):
    raise Interrupted()


@pytest.fixture
def tool_calls(monkeypatch):
    calls = []

    def refuse_call(self, *args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("An offline review must not call tools")

    monkeypatch.setattr(ToolGateway, "call", refuse_call)
    monkeypatch.setattr(ToolGateway, "call_capability", refuse_call)
    return calls


def test_executor_rejects_receipt_bound_to_another_run(tmp_path, tool_calls):
    session, _, request, receipt = setup()
    template = host_receipt_review_template(
        receipt, request=request, session=session, registry=registry(),
        assessment_policy=PermissionPolicy(), clock=lambda: NOW,
    )
    provider = FakeProvider({})
    executor = build_executor(
        template, provider, template.create_workspace(tmp_path / "wrong-run"),
        clock=lambda: NOW,
    )

    result = executor.start(template.task, run_id="other-run")

    assert result.status is RunStatus.COMPLETED
    assert result.review["status"] == "BLOCKED"
    assert result.review["reason"] == "HOST_RECEIPT_REJECTED"
    assert result.review["application_ready"] is False
    assert result.review["execution_authorized"] is False
    assert result.plan is result.implementation is None
    assert [entry["node"] for entry in result.history] == ["validate"]
    assert executor.store.load("other-run") == result
    assert provider.calls == tool_calls == []


@pytest.mark.parametrize("expired", [False, True], ids=("before-expiry", "at-expiry"))
def test_executor_rechecks_receipt_expiry_before_resumed_assessment(tmp_path, tool_calls, expired):
    session, _, request, receipt = setup()
    current_time = [NOW]
    clock = lambda: current_time[0]
    template = host_receipt_review_template(
        receipt, request=request, session=session, registry=registry(),
        assessment_policy=PermissionPolicy(), clock=clock,
    )
    workspace = template.create_workspace(tmp_path / "receipt-expiry")
    provider = FakeProvider({})
    first = build_executor(template, provider, workspace, clock=clock)
    first.agents["assess"].run = interrupt_before_node
    with pytest.raises(Interrupted):
        first.start(template.task, run_id="review-1")
    checkpoint = first.store.load("review-1")
    assert checkpoint.current_node == "assess"
    assert checkpoint.plan["host_receipt_digest"]
    assert checkpoint.implementation is checkpoint.review is None

    current_time[0] = NOW + timedelta(minutes=5)
    if not expired:
        current_time[0] -= timedelta(microseconds=1)
    second = build_executor(template, provider, workspace, clock=clock)
    result = second.resume("review-1")

    assert result.status is RunStatus.COMPLETED
    assert result.review["status"] == ("BLOCKED" if expired else "REVIEW_READY")
    assert result.review["application_ready"] is False
    assert result.review["execution_authorized"] is False
    if expired:
        assert result.review["reason"] == "HOST_RECEIPT_REJECTED"
        assert result.implementation is None
        assert [entry["node"] for entry in result.history] == ["validate", "assess"]
    else:
        assert result.review["host_receipt"]["run_id"] == "review-1"
        assert [entry["node"] for entry in result.history] == ["validate", "assess", "review_report"]
    assert second.store.load("review-1") == result
    assert provider.calls == tool_calls == []


@pytest.mark.parametrize("changed", [False, True], ids=("same-policy", "changed-policy"))
def test_executor_rechecks_assessment_policy_before_resumed_report(tmp_path, tool_calls, changed):
    payload = json.dumps(fixture())
    mappings = (mapping(batch(), Requirement("r1", "core.files.read")),)
    target_registry = registry()
    original_policy = PermissionPolicy()

    def template(policy):
        return opportunity_review_template(
            payload, mappings=mappings, registry=target_registry,
            assessment_policy=policy, clock=lambda: NOW,
        )

    initial = template(original_policy)
    workspace = initial.create_workspace(tmp_path / "assessment-policy")
    provider = FakeProvider({})
    first = build_executor(initial, provider, workspace, clock=lambda: NOW)
    first.agents["review_report"].run = interrupt_before_node
    with pytest.raises(Interrupted):
        first.start(initial.task, run_id="policy-review")
    checkpoint = first.store.load("policy-review")
    assert checkpoint.current_node == "review_report"
    assert checkpoint.implementation["report_digest"]
    assert checkpoint.review is None

    resumed_policy = original_policy.with_rules(read_workspace=Decision.DENY) if changed else original_policy
    second = build_executor(template(resumed_policy), provider, workspace, clock=lambda: NOW)
    result = second.resume("policy-review")

    assert result.status is RunStatus.COMPLETED
    assert result.review["status"] == ("BLOCKED" if changed else "REVIEW_READY")
    assert result.review["application_ready"] is False
    assert result.review["execution_authorized"] is False
    assert result.implementation == checkpoint.implementation
    if changed:
        assert result.review["reason"] == "INVALID_OR_CHANGED_INPUT"
    else:
        assert result.review["jobs"][0]["capability_assessment"]["supported"]
    assert second.store.load("policy-review") == result
    assert provider.calls == tool_calls == []

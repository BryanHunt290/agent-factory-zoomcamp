"""Offline supervision: real trusted fixture tests, simulated worker failures.

Tampered worker output is rejected before any of it can run. These tests do
not execute hostile code or establish safe execution of real model output.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent_factory.agents import Agent, AgentResult
from agent_factory.engine import ApprovalPending, ExecutorError, RunNotResumable
from agent_factory.engine.executor import ApprovalExpired
from agent_factory.providers.base import ModelResponse
from agent_factory.providers.guard import RequestGuard
from agent_factory.sandbox.manager import SandboxManager
from agent_factory.sandbox.types import CodeTrust, SandboxBlocked
from agent_factory.state import RunStatus
from agent_factory.supervised_demo import (
    ALLOWED,
    PROTECTED,
    SCAFFOLD,
    EvidenceError,
    FakeCodexAdapter,
    SimulatedInterruption,
    SupervisorRecords,
    check_proposal,
    completion_report,
    create_demo,
    demo_template,
    digest,
    open_demo,
    snapshot,
)
from agent_factory.tools.base import ToolResult
from agent_factory.tools.testing import RunTestsTool, UNITTEST_COMMAND


def start(tmp_path, scenario="success", **kwargs):
    executor = create_demo(tmp_path / "fixture", scenario, **kwargs)
    return executor, executor.start(demo_template().task, run_id="demo")


def journal(executor):
    return SupervisorRecords(executor.workspace).load("demo")


class InterruptBefore(Agent):
    """A simulated supervisor process interruption, not a worker fixture."""

    def __init__(self, inner, visit=1):
        self.inner = inner
        self.name = inner.name
        self.writes = inner.writes
        self.visit = visit

    def run(self, ctx):
        if ctx.visit == self.visit:
            raise KeyboardInterrupt("simulated node interruption")
        return self.inner.run(ctx)


def test_success_requires_real_tests_complete_diff_and_bound_review(tmp_path, monkeypatch):
    requests = []
    actual_run = SandboxManager.run

    def observed_run(manager, request):
        requests.append(request)
        return actual_run(manager, request)

    monkeypatch.setattr(SandboxManager, "run", observed_run)
    executor, state = start(tmp_path)
    assert state.status is RunStatus.COMPLETED
    report = completion_report(executor)
    assert report["attempts"] == 1 and report["repairs"] == 0
    assert len(requests) == 1
    assert requests[0].trust is CodeTrust.TRUSTED_FIXTURE
    assert requests[0].command == UNITTEST_COMMAND
    evidence = report["attempt_evidence"][0]["evidence"]
    assert evidence["test_result"]["tests_run"] == 5
    assert evidence["test_result"]["passed"] is True
    assert evidence["test_result"]["returncode"] == 0
    assert "Ran 5 tests" in evidence["test_result"]["output"]
    assert evidence["execution"] == "actual trusted fixture tests"
    assert evidence["candidate_digest"] == digest(snapshot(executor.workspace))
    assert evidence["diff_digest"] == digest(evidence["diff"])
    assert {change["path"] for change in evidence["diff"]} == {"calculator.py", "notes.txt"}
    assert all(change["kind"] == "added" for change in evidence["diff"])
    review = report["attempt_evidence"][0]["review"]
    assert review["evidence_digest"] == digest(evidence)
    assert review["candidate_digest"] == evidence["candidate_digest"]
    assert review["package_digest"] == evidence["package_digest"]
    assert review["attempt_id"] == evidence["attempt_id"]
    assert not executor.workspace.is_git
    assert not (executor.workspace.project_dir / ".git").exists()
    tools = [entry.get("tool") for entry in executor.log_for("demo").entries()]
    assert "git_commit" not in tools
    assert executor.log_for("demo").verify() > 0
    assert set(report["runtime"].values()) == {"NOT VERIFIED"}


def test_failed_test_is_repaired_and_both_attempts_remain(tmp_path):
    executor, state = start(tmp_path, "repair")
    assert state.status is RunStatus.COMPLETED
    attempts = journal(executor)["attempts"]
    assert set(attempts) == {"1", "2"}
    first, second = attempts["1"], attempts["2"]
    assert first["evidence"]["test_result"]["passed"] is False
    assert first["evidence"]["test_result"]["tests_run"] == 5
    assert "test_subtract" in first["evidence"]["test_result"]["failures"][0]
    assert second["evidence"]["test_result"]["passed"] is True
    assert first["id"] != second["id"]
    assert first["evidence"]["candidate_digest"] != second["evidence"]["candidate_digest"]
    assert "review" not in first
    assert second["review"]["verdict"] == "approved"
    assert executor.provider.calls == [("demo", 1), ("demo", 2)]
    assert completion_report(executor)["repairs"] == 1


@pytest.mark.parametrize("scenario", ["test_exhaust", "review_exhaust", "mixed_exhaust"])
def test_one_shared_two_repair_limit_never_dispatches_fourth_attempt(tmp_path, scenario):
    executor, state = start(tmp_path, scenario)
    assert state.status is RunStatus.EXHAUSTED
    assert state.node_visits["developer"] == 3
    assert executor.provider.calls == [("demo", 1), ("demo", 2), ("demo", 3)]
    assert set(journal(executor)["attempts"]) == {"1", "2", "3"}
    assert completion_report(executor)["repairs"] == 2
    resumed = open_demo(executor.workspace.root)
    with pytest.raises(RunNotResumable):
        resumed.resume("demo")
    assert resumed.provider.calls == []


def test_resume_keeps_repair_budget_and_prior_dispatches(tmp_path):
    executor = create_demo(tmp_path / "fixture", "mixed_exhaust")
    executor.agents["tester"] = InterruptBefore(executor.agents["tester"], visit=2)
    with pytest.raises(KeyboardInterrupt):
        executor.start(demo_template().task, run_id="demo")
    before = journal(executor)
    assert executor.provider.calls == [("demo", 1), ("demo", 2)]
    resumed = open_demo(executor.workspace.root)
    state = resumed.resume("demo")
    assert state.status is RunStatus.EXHAUSTED
    assert resumed.provider.calls == [("demo", 3)]
    after = journal(resumed)
    assert after["attempts"]["1"] == before["attempts"]["1"]
    assert after["attempts"]["2"]["id"] == before["attempts"]["2"]["id"]
    assert len(after["attempts"]) == 3


def test_saved_proposal_replays_without_redispatch_after_node_interruption(tmp_path, monkeypatch):
    executor = create_demo(tmp_path / "fixture", "success")
    original = SupervisorRecords.operation

    def interrupt_after_proposal(records, context, operation, params):
        result = original(records, context, operation, params)
        if operation == "proposal":
            raise KeyboardInterrupt("proposal durable; developer checkpoint not written")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(SupervisorRecords, "operation", interrupt_after_proposal)
        with pytest.raises(KeyboardInterrupt):
            executor.start(demo_template().task, run_id="demo")
    assert journal(executor)["attempts"]["1"]["status"] == "proposed"
    identity = journal(executor)["attempts"]["1"]["id"]
    resumed = open_demo(executor.workspace.root)
    state = resumed.resume("demo")
    assert state.status is RunStatus.COMPLETED
    assert executor.provider.calls == [("demo", 1)]
    assert resumed.provider.calls == []
    assert journal(resumed)["attempts"]["1"]["id"] == identity


def test_approval_pause_and_fresh_executor_resume_do_not_repeat_worker(tmp_path):
    executor, state = start(tmp_path, "approval")
    assert state.status is RunStatus.AWAITING_APPROVAL
    assert snapshot(executor.workspace) == SCAFFOLD
    pending = state.pending_approval
    assert pending["tool"] == "delete_file"
    assert pending["params"] == {"path": "legacy_calc.py"}
    assert state.approvals == {}
    identity = journal(executor)["attempts"]["1"]["id"]
    resumed = open_demo(executor.workspace.root)
    assert resumed.advance("demo").status is RunStatus.AWAITING_APPROVAL
    with pytest.raises(ApprovalPending):
        resumed.resume("demo")
    assert resumed.provider.calls == []
    assert snapshot(resumed.workspace) == SCAFFOLD
    resumed.decide_approval("demo", pending["request_id"], approved=True, approver="fixture-owner")
    state = resumed.resume("demo")
    assert state.status is RunStatus.COMPLETED
    assert resumed.provider.calls == []
    assert executor.provider.calls == [("demo", 1)]
    assert journal(resumed)["attempts"]["1"]["id"] == identity
    report = completion_report(resumed)
    diff = report["attempt_evidence"][0]["evidence"]["diff"]
    assert {item["path"]: item["kind"] for item in diff} == {
        "calculator.py": "added", "notes.txt": "added", "legacy_calc.py": "deleted",
    }
    actions = [entry for entry in resumed.log_for("demo").entries()
               if entry.get("event") == "action" and entry.get("tool") == "delete_file"]
    assert len(actions) == 1
    assert actions[0]["approval_id"] == pending["request_id"]


def test_rejected_approval_stops_before_change_application(tmp_path):
    executor, state = start(tmp_path, "approval")
    executor.decide_approval("demo", state.pending_approval["request_id"], approved=False)
    resumed = open_demo(executor.workspace.root)
    state = resumed.resume("demo")
    assert state.status is RunStatus.FAILED
    assert snapshot(executor.workspace) == SCAFFOLD
    assert resumed.provider.calls == []


def test_expired_approval_cannot_dispatch_at_expiry(tmp_path):
    current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    executor, state = start(tmp_path, "approval", clock=lambda: current[0])
    current[0] += timedelta(hours=12)
    resumed = open_demo(executor.workspace.root, clock=lambda: current[0])
    with pytest.raises(ApprovalExpired):
        resumed.decide_approval("demo", state.pending_approval["request_id"], approved=True)
    state = resumed.resume("demo")
    assert state.status is RunStatus.FAILED
    assert snapshot(executor.workspace) == SCAFFOLD
    assert resumed.provider.calls == []


def test_mismatched_approval_identity_remains_paused(tmp_path):
    executor, _ = start(tmp_path, "approval")
    with pytest.raises(ExecutorError):
        executor.decide_approval("demo", "apr-different", approved=True)
    assert executor.store.load("demo").status is RunStatus.AWAITING_APPROVAL
    assert snapshot(executor.workspace) == SCAFFOLD


@pytest.mark.parametrize("scenario", ["worker_failure", "timeout", "malformed"])
def test_simulated_worker_failures_never_apply_changes(tmp_path, scenario):
    executor, state = start(tmp_path, scenario)
    assert state.status in (RunStatus.FAILED, RunStatus.BLOCKED)
    assert snapshot(executor.workspace) == SCAFFOLD
    assert journal(executor)["attempts"]["1"]["status"] == "started"
    assert executor.provider.calls == [("demo", 1)]
    assert not state.test_report
    assert not state.review
    if state.status is RunStatus.BLOCKED:
        resumed = open_demo(executor.workspace.root)
        assert resumed.resume("demo").status is RunStatus.BLOCKED
        assert resumed.provider.calls == []


def test_uncertain_dispatch_is_durable_and_resume_never_retries(tmp_path):
    executor = create_demo(tmp_path / "fixture", "uncertain")
    with pytest.raises(SimulatedInterruption):
        executor.start(demo_template().task, run_id="demo")
    attempt = journal(executor)["attempts"]["1"]
    assert attempt["status"] == "started"
    assert executor.provider.calls == [("demo", 1)]
    resumed = open_demo(executor.workspace.root)
    state = resumed.resume("demo")
    assert state.status is RunStatus.BLOCKED
    assert "unknown" in state.blocked["message"]
    assert resumed.provider.calls == []
    assert journal(resumed)["attempts"]["1"] == attempt
    assert snapshot(resumed.workspace) == SCAFFOLD


@pytest.mark.parametrize("damage", ["missing_evidence", "missing_tests", "missing_diff", "missing_review",
                                    "stale_candidate", "wrong_package", "wrong_attempt"])
def test_completion_rejects_missing_stale_or_mismatched_evidence(tmp_path, damage):
    executor, state = start(tmp_path)
    assert state.status is RunStatus.COMPLETED
    records = SupervisorRecords(executor.workspace)
    data = records.load("demo")
    attempt = data["attempts"]["1"]
    if damage == "missing_evidence":
        del attempt["evidence"]
    elif damage == "missing_tests":
        del attempt["evidence"]["test_result"]
    elif damage == "missing_diff":
        del attempt["evidence"]["diff"]
    elif damage == "missing_review":
        del attempt["review"]
    elif damage == "stale_candidate":
        executor.workspace.write_text("notes.txt", "Changed since acceptance execution.\n")
    elif damage == "wrong_package":
        attempt["evidence"]["package_digest"] = "another-task"
    else:
        attempt["evidence"]["attempt_id"] = "another-dispatch"
    if "evidence" in attempt:
        attempt["evidence_digest"] = digest(attempt["evidence"])
    records.save("demo", data)
    with pytest.raises(EvidenceError):
        completion_report(executor)


@pytest.mark.parametrize("damage", ["execution_id", "status", "cleanup", "exit_code", "zero_tests",
                                    "review_candidate", "review_package", "review_attempt"])
def test_semantically_invalid_evidence_is_refused_even_with_updated_digests(tmp_path, damage):
    executor, state = start(tmp_path)
    assert state.status is RunStatus.COMPLETED
    records = SupervisorRecords(executor.workspace)
    data = records.load("demo")
    attempt = data["attempts"]["1"]
    evidence = attempt["evidence"]
    report = evidence["test_result"]
    review = attempt["review"]
    if damage == "execution_id":
        del report["sandbox"]["execution_id"]
    elif damage == "status":
        report["sandbox"]["status"] = "timed_out"
    elif damage == "cleanup":
        report["sandbox"]["cleanup_verified"] = False
    elif damage == "exit_code":
        report["sandbox"]["exit_code"] = 1
    elif damage == "zero_tests":
        report["tests_run"] = 0
    else:
        key = {"review_candidate": "candidate_digest", "review_package": "package_digest",
               "review_attempt": "attempt_id"}[damage]
        review[key] = "mismatched-binding"
    # Recompute integrity hashes so the semantic check, not a stale hash, must refuse.
    evidence["test_digest"] = digest(report)
    attempt["evidence_digest"] = digest(evidence)
    review["evidence_digest"] = digest(evidence)
    records.save("demo", data)
    state.test_report = {**report, "evidence_digest": digest(evidence),
                         "candidate_digest": evidence["candidate_digest"], "number": 1}
    state.review["completion"]["evidence_digest"] = digest(evidence)
    state.review["completion"]["review_digest"] = digest(review)
    executor.store.save(state)
    with pytest.raises(EvidenceError):
        completion_report(executor)


@pytest.mark.parametrize("path", ["legacy_calc.py", "calculator.py", "notes.txt"])
def test_unknown_bytes_between_nodes_are_refused_before_test_execution(tmp_path, monkeypatch, path):
    executor = create_demo(tmp_path / "fixture", "success")
    executor.agents["tester"] = InterruptBefore(executor.agents["tester"])
    with pytest.raises(KeyboardInterrupt):
        executor.start(demo_template().task, run_id="demo")
    executor.workspace.write_text(path, "# inert non-fixture bytes, never executed\n")
    calls = []

    def refused_execution(*args):
        calls.append(args)
        raise AssertionError("unknown bytes reached sandbox dispatch")

    monkeypatch.setattr(SandboxManager, "run", refused_execution)
    resumed = open_demo(executor.workspace.root)
    state = resumed.resume("demo")
    assert state.status is RunStatus.FAILED
    assert state.error["type"] == "EvidenceError"
    assert calls == []
    assert resumed.provider.calls == []


@pytest.mark.parametrize("missing", ["zero", "report", "returncode", "sandbox"])
def test_missing_or_zero_test_execution_cannot_complete(tmp_path, monkeypatch, missing):
    # A synthetic tool result exercises evidence rejection; it is not an actual test run.
    result = {"passed": True, "returncode": 0, "tests_run": 5, "failures": [],
              "output": "simulated missing evidence", "sandbox": {"status": "completed"}}
    if missing == "zero":
        result["tests_run"] = 0
    elif missing == "report":
        result = {}
    else:
        del result[missing]
    monkeypatch.setattr(RunTestsTool, "run", lambda *args, **kwargs: ToolResult(data=result))
    executor, state = start(tmp_path)
    assert state.status is RunStatus.FAILED
    assert state.error["type"] == "EvidenceError"
    assert "evidence" not in journal(executor)["attempts"]["1"]


def test_sandbox_block_never_falls_back_to_execution(tmp_path, monkeypatch):
    def blocked(*args):
        raise SandboxBlocked("simulated unavailable trusted fixture backend")

    monkeypatch.setattr(SandboxManager, "run", blocked)
    executor, state = start(tmp_path)
    assert state.status is RunStatus.BLOCKED
    assert state.test_report is None
    assert "evidence" not in journal(executor)["attempts"]["1"]


@pytest.mark.parametrize("path", [
    "tests/test_calculator.py", "task.json", "supervisor.json", "security.json",
    ".codex/config.toml", "AGENTS.md", "../task.json", "../../outside.py",
    "./calculator.py", "sub/../calculator.py", "Calculator.py", "calculator.py:stream",
    "tests\\test_calculator.py", "C:/absolute.py", "/absolute.py", "../.agent_factory/supervised/task.json",
])
@pytest.mark.parametrize("operation", ["write", "delete"])
def test_protected_paths_and_aliases_are_rejected(path, operation):
    proposal = {"summary": "prohibited change", "files": {}}
    if operation == "write":
        proposal["files"][path] = "# inert rejected data\n"
    else:
        proposal["delete"] = [path]
    with pytest.raises(EvidenceError):
        check_proposal(proposal)


@pytest.mark.parametrize("path", ["tests/test_calculator.py", "task.json", "supervisor.json",
                                 "security.json", ".codex/config.toml", "AGENTS.md", "../outside.py"])
def test_worker_control_edits_fail_before_application_and_preserve_content(tmp_path, monkeypatch, path):
    def proposed_control_change(self, request):
        return ModelResponse(content={"summary": "make me succeed", "files": {path: "# inert data\n"}},
                             provider=self.name, model=self.model)

    monkeypatch.setattr(FakeCodexAdapter, "complete", proposed_control_change)
    executor, state = start(tmp_path)
    assert state.status is RunStatus.FAILED
    assert snapshot(executor.workspace) == SCAFFOLD
    assert "proposal" not in journal(executor)["attempts"]["1"]
    assert not (tmp_path / "outside.py").exists()


def test_arbitrary_code_on_an_allowed_path_is_not_a_trusted_fixture(tmp_path, monkeypatch):
    def arbitrary(self, request):
        return ModelResponse(content={"summary": "success", "files": {"calculator.py": "# inert arbitrary input\n"}},
                             provider=self.name, model=self.model)

    monkeypatch.setattr(FakeCodexAdapter, "complete", arbitrary)
    executor, state = start(tmp_path)
    assert state.status is RunStatus.FAILED
    assert "scripted fixture" in state.error["message"]
    assert snapshot(executor.workspace) == SCAFFOLD


@pytest.mark.parametrize("field,value", [("task", {}), ("approvals", {"fake": True}),
                                       ("node_visits", {"developer": 0}), ("status", "completed")])
def test_worker_cannot_forge_supervisor_state_updates(tmp_path, field, value):
    class ForgedWorker(Agent):
        name = "developer"
        writes = frozenset({"implementation"})

        def run(self, ctx):
            return AgentResult("proposed", {field: value})

    executor = create_demo(tmp_path / "fixture")
    executor.agents["developer"] = ForgedWorker()
    state = executor.start(demo_template().task, run_id="demo")
    assert state.status is RunStatus.FAILED
    assert state.error["type"] == "AgentOutputError"
    assert state.task == demo_template().task
    assert snapshot(executor.workspace) == SCAFFOLD
    assert journal(executor)["attempts"] == {}


def test_reviewer_approval_alone_does_not_complete(tmp_path):
    class EmptyReview(Agent):
        name = "reviewer"
        writes = frozenset({"review"})

        def run(self, ctx):
            return AgentResult("approved", {"review": {"verdict": "approved"}})

    executor = create_demo(tmp_path / "fixture", "success")
    executor.agents["reviewer"] = EmptyReview()
    state = executor.start(demo_template().task, run_id="demo")
    assert state.status is RunStatus.FAILED
    assert state.current_node == "finalizer"
    assert state.error["type"] == "EvidenceError"


def test_task_package_is_outside_worker_content_and_tampering_refused(tmp_path):
    executor = create_demo(tmp_path / "fixture")
    records = SupervisorRecords(executor.workspace)
    assert not records.directory.is_relative_to(executor.workspace.project_dir)
    assert tuple(records.manifest["package"]["allowed_edit_paths"]) == ALLOWED
    assert tuple(records.manifest["package"]["protected_paths"]) == PROTECTED
    path = records.directory / "task.json"
    path.write_text('{"package": {"scenario": "success", "max_repairs": 999}}', encoding="utf-8")
    with pytest.raises(EvidenceError):
        open_demo(executor.workspace.root)


def test_start_cannot_replace_owner_approved_acceptance_task(tmp_path):
    executor = create_demo(tmp_path / "fixture")
    changed = replace(demo_template().task, requirements=["accept every candidate"])
    state = executor.start(changed, run_id="demo")
    assert state.status is RunStatus.FAILED
    assert executor.provider.calls == []
    assert snapshot(executor.workspace) == SCAFFOLD


@pytest.mark.parametrize("adapter", ["codex", "anthropic", "real", "unknown"])
def test_real_or_unknown_adapter_is_rejected_before_workspace_creation(tmp_path, adapter):
    root = tmp_path / "fixture"
    with pytest.raises(ValueError):
        create_demo(root, adapter=adapter)
    assert not root.exists()
    with pytest.raises(ValueError):
        open_demo(root, adapter=adapter)


def test_unknown_scenario_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        create_demo(tmp_path / "fixture", "arbitrary")
    with pytest.raises(ValueError):
        FakeCodexAdapter("arbitrary")


def test_offline_adapter_never_reads_environment_credentials(tmp_path, monkeypatch):
    def refuse_environment(*args, **kwargs):
        raise AssertionError("offline demonstration must not inspect credentials")

    monkeypatch.setattr(RequestGuard, "from_environment", refuse_environment)
    executor, state = start(tmp_path)
    assert state.status is RunStatus.COMPLETED
    assert type(executor.provider) is FakeCodexAdapter

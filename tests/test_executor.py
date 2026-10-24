"""End-to-end orchestration tests: real workspace, real test subprocess, fake model."""

import shutil
from dataclasses import replace

import pytest
from conftest import Harness, events, path_of

from agent_factory.agents import Agent, AgentResult, TesterAgent, default_agents
from agent_factory.demo import APPROVE, DEV_BUGGY, DEV_POLISH, PLAN, REQUEST_CHANGES, scenario_provider
from agent_factory.engine import ApprovalPending, Executor, ExecutorError, RunNotResumable
from agent_factory.graph import GraphValidationError, build_software_dev_graph
from agent_factory.providers import FakeProvider
from agent_factory.state import RunStatus
from agent_factory.templates import CALCULATOR_TEMPLATE

HAPPY = [("planner", "planned"), ("developer", "implemented"), ("tester", "pass"), ("reviewer", "approved")]


# --------------------------------------------------------------- success


def test_successful_graph_execution(harness):
    provider = FakeProvider({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    ex, state = harness.run(provider)

    assert state.status is RunStatus.COMPLETED
    assert state.current_node == "done"
    assert path_of(state) == HAPPY
    assert state.test_report["passed"] and state.test_report["tests_run"] == 5
    assert state.review["verdict"] == "approved"
    assert harness.workspace.read_text("calculator.py").startswith('"""Four-function calculator."""')
    assert state.error is None

    # Persisted checkpoint matches the returned state.
    assert ex.store.load("run-1") == state


def test_execution_log_records_required_fields(harness):
    ex, state, _ = harness.run_scenario("repair")
    log = ex.log_for("run-1")
    assert log.verify() == len(log.entries())
    entries = log.entries()
    kinds = {e["event"] for e in entries}
    assert {"run_started", "node_entered", "action", "node_result", "transition", "run_completed"} <= kinds
    assert all(e["timestamp"].endswith("Z") for e in entries)

    entered = [e["node"] for e in entries if e["event"] == "node_entered"]
    assert entered == ["planner", "developer", "tester", "developer", "tester", "reviewer"]
    transitions = [(e["source"], e["outcome"], e["target"]) for e in entries if e["event"] == "transition"]
    assert transitions[2] == ("tester", "fail", "developer")
    tool_actions = [e for e in entries if e["event"] == "action" and e["kind"] == "tool"]
    assert {"run_tests", "write_file", "read_file", "list_files"} <= {a["tool"] for a in tool_actions}
    assert all(a["decision"] == "allow" and a["result"] in ("ok", "failed") for a in tool_actions)
    model_calls = [e for e in entries if e["event"] == "action" and e["kind"] == "model_call"]
    assert [m["node"] for m in model_calls] == ["planner", "developer", "developer", "reviewer"]


# ---------------------------------------------------------------- repair


def test_test_failure_routes_back_to_developer(harness):
    ex, state, provider = harness.run_scenario("repair")
    assert state.status is RunStatus.COMPLETED
    assert path_of(state) == [
        ("planner", "planned"),
        ("developer", "implemented"),
        ("tester", "fail"),
        ("developer", "implemented"),
        ("tester", "pass"),
        ("reviewer", "approved"),
    ]
    # The repair visit received structured test feedback, not a transcript.
    repair_call = provider.calls_for("developer")[1]
    feedback = repair_call.inputs["feedback"]
    assert feedback["source"] == "tester"
    assert feedback["failures"] == ["tests.test_calculator.CalculatorTests.test_subtract"]
    assert repair_call.attempt == 2
    assert "calculator.py" in repair_call.inputs["current_files"]
    assert state.implementation["responding_to"] == "tester"
    assert state.edge_traversals == {
        "planner:planned->developer": 1,
        "developer:implemented->tester": 2,
        "tester:fail->developer": 1,
        "tester:pass->reviewer": 1,
        "reviewer:approved->done": 1,
    }


def test_reviewer_rejection_routes_back_to_developer(harness):
    ex, state, provider = harness.run_scenario("review")
    assert state.status is RunStatus.COMPLETED
    assert path_of(state) == [
        ("planner", "planned"),
        ("developer", "implemented"),
        ("tester", "pass"),
        ("reviewer", "changes_required"),
        ("developer", "implemented"),
        ("tester", "pass"),
        ("reviewer", "approved"),
    ]
    feedback = provider.calls_for("developer")[1].inputs["feedback"]
    assert feedback == {"source": "reviewer", "comments": REQUEST_CHANGES["comments"]}
    assert state.review["iteration"] == 2


def test_both_repair_loops_in_one_run(harness):
    _, state, _ = harness.run_scenario("full")
    assert state.status is RunStatus.COMPLETED
    outcomes = [o for _, o in path_of(state)]
    assert outcomes == [
        "planned", "implemented", "fail", "implemented", "pass",
        "changes_required", "implemented", "pass", "approved",
    ]


# --------------------------------------------------------------- budgets


def test_retry_exhaustion_on_test_failures(harness):
    ex, state, provider = harness.run_scenario("exhaust")
    assert state.status is RunStatus.EXHAUSTED
    assert state.error["type"] == "RetryBudgetExhausted"
    assert "tester:fail->developer" in state.error["message"]
    # 1 initial attempt + 3 repairs, then the 4th failure is refused.
    assert state.node_visits["developer"] == 4
    assert state.node_visits["tester"] == 4
    assert state.edge_traversals["tester:fail->developer"] == 3
    assert state.current_node == "tester"
    assert state.test_report["passed"] is False  # last report kept
    assert "reviewer" not in state.node_visits

    entries = events(ex)
    assert entries[-1]["event"] == "run_exhausted"
    rejected = [e for e in entries if e["event"] == "transition" and not e["accepted"]]
    assert len(rejected) == 1 and rejected[0]["target"] == "developer"
    with pytest.raises(RunNotResumable):
        ex.resume("run-1")


def test_retry_exhaustion_on_review_rejections(harness):
    provider = FakeProvider(
        {"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [REQUEST_CHANGES]}, repeat_last=True
    )
    _, state = harness.run(provider)
    assert state.status is RunStatus.EXHAUSTED
    assert "reviewer:changes_required->developer" in state.error["message"]
    assert state.edge_traversals["reviewer:changes_required->developer"] == 2
    assert state.node_visits["reviewer"] == 3


def test_max_steps_backstop(tmp_path):
    h = Harness(tmp_path / "ws")
    template = replace(
        CALCULATOR_TEMPLATE,
        graph_factory=lambda: replace(build_software_dev_graph(), max_steps=3),
    )
    provider = FakeProvider({"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]})
    state = h.executor(provider, template).start(template.task, run_id="r")
    assert state.status is RunStatus.EXHAUSTED
    assert "max_steps (3)" in state.error["message"]
    assert state.step == 3 and state.current_node == "reviewer"


# ------------------------------------------------------ checkpoint/resume


class _CrashOnceTester(TesterAgent):
    """Simulates the process dying (e.g. Ctrl-C / power loss) inside the tester."""

    crashed = False

    def run(self, ctx):
        if not _CrashOnceTester.crashed:
            _CrashOnceTester.crashed = True
            raise KeyboardInterrupt("simulated interruption")
        return super().run(ctx)


def test_checkpoint_and_resume_after_interruption(harness):
    _CrashOnceTester.crashed = False
    agents = {**default_agents(), "tester": _CrashOnceTester()}
    template = replace(CALCULATOR_TEMPLATE, agent_factory=lambda: agents)

    first_provider = FakeProvider({"planner": [PLAN], "developer": [DEV_BUGGY, DEV_POLISH], "reviewer": [APPROVE]})
    ex1 = harness.executor(first_provider, template)
    with pytest.raises(KeyboardInterrupt):
        ex1.start(template.task, run_id="run-1")

    # Last checkpoint is from just before the tester ran.
    saved = ex1.store.load("run-1")
    assert saved.status is RunStatus.RUNNING
    assert saved.current_node == "tester" and saved.step == 2
    assert [h["node"] for h in saved.history] == ["planner", "developer"]

    # A brand-new executor and provider (as after a process restart) resumes.
    second_provider = FakeProvider({"planner": [PLAN], "developer": [DEV_BUGGY, DEV_POLISH], "reviewer": [APPROVE]})
    ex2 = harness.executor(second_provider, template)
    state = ex2.resume("run-1")

    assert state.status is RunStatus.COMPLETED
    assert [n for n, _ in path_of(state)] == ["planner", "developer", "tester", "developer", "tester", "reviewer"]
    # Planner and first developer visit were not repeated after resume.
    assert second_provider.calls_for("planner") == []
    assert [c.attempt for c in second_provider.calls_for("developer")] == [2]

    entries = events(ex2)
    assert ex2.log_for("run-1").verify() == len(entries)
    resumed = [e for e in entries if e["event"] == "run_resumed"]
    assert len(resumed) == 1 and resumed[0]["node"] == "tester"
    assert [e["node"] for e in entries if e["event"] == "node_entered"].count("tester") == 3  # crash + 2 real

    with pytest.raises(RunNotResumable):
        ex2.resume("run-1")


def test_resume_is_independent_of_executor_instance_for_every_checkpoint(tmp_path):
    """Stop after each node in turn; a fresh executor always finishes identically."""
    reference = Harness(tmp_path / "ref").run_scenario("full")[1]
    for stop_after in range(1, len(reference.history)):
        h = Harness(tmp_path / f"ws{stop_after}")

        calls = {"n": 0}
        base = default_agents()

        class _Interrupting(Agent):
            def __init__(self, inner):
                self.inner = inner
                self.name = inner.name
                self.writes = inner.writes

            def run(self, ctx):
                if calls["n"] == stop_after:
                    raise KeyboardInterrupt
                calls["n"] += 1
                return self.inner.run(ctx)

        wrapped = {k: _Interrupting(v) for k, v in base.items()}
        template = replace(CALCULATOR_TEMPLATE, agent_factory=lambda: wrapped)
        with pytest.raises(KeyboardInterrupt):
            h.executor(scenario_provider("full"), template).start(template.task, run_id="r")
        state = h.executor(scenario_provider("full")).resume("r")
        assert state.status is RunStatus.COMPLETED
        assert path_of(state) == path_of(reference), f"stop_after={stop_after}"


@pytest.mark.stress
def test_resume_from_every_checkpoint_under_repetition(tmp_path):
    """Opt-in (--run-stress). The test above failed once during E1-E2, unexplained and never reproduced.

    Its traceback was not kept. Suspected, not shown: Windows briefly denying a
    replace of checkpoint.json (docs/testing.md, "Stress tests").
    """
    for round_ in range(25):
        test_resume_is_independent_of_executor_instance_for_every_checkpoint(tmp_path / f"round-{round_}")


# --------------------------------------------------------- approval gate


def test_approval_gate_pauses_then_resumes_when_approved(harness):
    ex, state, _ = harness.run_scenario("approval")
    ws = harness.workspace

    assert state.status is RunStatus.AWAITING_APPROVAL
    req = state.pending_approval
    assert req["tool"] == "delete_file" and req["category"] == "destructive_delete"
    assert req["params"] == {"path": "legacy_calc.py"}
    assert (ws.project_dir / "legacy_calc.py").exists()
    assert not (ws.project_dir / "calculator.py").exists()  # paused before any writes
    assert ex.store.load("run-1").status is RunStatus.AWAITING_APPROVAL

    with pytest.raises(ApprovalPending):
        ex.resume("run-1")

    # Decision recorded by a different executor instance (e.g. the CLI).
    ex2 = harness.executor(scenario_provider("approval"))
    ex2.decide_approval("run-1", req["request_id"], approved=True, approver="alice", note="ok to remove")
    state = ex2.resume("run-1")

    assert state.status is RunStatus.COMPLETED
    assert not (ws.project_dir / "legacy_calc.py").exists()
    assert state.implementation["files_deleted"] == ["legacy_calc.py"]
    assert state.approvals[req["request_id"]]["approver"] == "alice"
    assert state.pending_approval is None

    entries = events(ex2)
    names = [e["event"] for e in entries]
    assert names.index("approval_requested") < names.index("run_paused") < names.index("approval_decision")
    delete_actions = [e for e in entries if e["event"] == "action" and e.get("tool") == "delete_file"]
    assert len(delete_actions) == 1
    assert delete_actions[0]["decision"] == "require_approval" and delete_actions[0]["result"] == "ok"
    assert delete_actions[0]["approval_id"] == req["request_id"]


def test_approval_gate_rejection_skips_the_action(harness):
    ex, state, _ = harness.run_scenario("approval")
    ex.decide_approval("run-1", state.pending_approval["request_id"], approved=False, note="keep it")
    state = ex.resume("run-1")
    assert state.status is RunStatus.COMPLETED
    assert (harness.workspace.project_dir / "legacy_calc.py").exists()
    assert state.implementation["deletions_rejected"] == ["legacy_calc.py"]
    assert state.implementation["files_deleted"] == []


def test_wrong_approval_id_is_refused(harness):
    ex, state, _ = harness.run_scenario("approval")
    with pytest.raises(ExecutorError):
        ex.decide_approval("run-1", "apr-bogus", approved=True)
    assert ex.store.load("run-1").status is RunStatus.AWAITING_APPROVAL


# ---------------------------------------------------- invalid transitions


class _BadOutcomeReviewer(Agent):
    name = "reviewer"
    writes = frozenset({"review"})

    def run(self, ctx):
        return AgentResult("ship_it_anyway", {"review": {"verdict": "??"}}, summary="bogus")


def test_invalid_transition_fails_the_run(harness):
    agents = {**default_agents(), "reviewer": _BadOutcomeReviewer()}
    template = replace(CALCULATOR_TEMPLATE, agent_factory=lambda: agents)
    provider = FakeProvider({"planner": [PLAN], "developer": [DEV_POLISH]})
    ex = harness.executor(provider, template)
    state = ex.start(template.task, run_id="run-1")

    assert state.status is RunStatus.FAILED
    assert state.error["type"] == "InvalidTransitionError"
    assert "ship_it_anyway" in state.error["message"]
    assert state.current_node == "reviewer"
    assert state.review is None  # outputs of a rejected node are not committed
    last_transition = [e for e in events(ex) if e["event"] == "transition"][-1]
    assert last_transition["accepted"] is False and last_transition["target"] is None
    with pytest.raises(RunNotResumable):
        ex.resume("run-1")


def test_graph_without_agent_for_a_node_is_rejected(harness):
    agents = default_agents()
    del agents["tester"]
    with pytest.raises(GraphValidationError, match="tester"):
        Executor(
            graph=build_software_dev_graph(),
            agents=agents,
            provider=FakeProvider({}),
            workspace=harness.workspace,
        )


# ------------------------------------------------------ contract failures


class _OverreachingTester(TesterAgent):
    def run(self, ctx):
        result = super().run(ctx)
        result.updates["review"] = {"verdict": "approved"}  # not the tester's field
        return result


def test_agent_cannot_write_fields_it_does_not_own(harness):
    agents = {**default_agents(), "tester": _OverreachingTester()}
    template = replace(CALCULATOR_TEMPLATE, agent_factory=lambda: agents)
    state = harness.executor(FakeProvider({"planner": [PLAN], "developer": [DEV_POLISH]}), template).start(
        template.task, run_id="run-1"
    )
    assert state.status is RunStatus.FAILED
    assert state.error["type"] == "AgentOutputError"
    assert state.review is None


def test_malformed_model_output_fails_the_run(harness):
    provider = FakeProvider({"planner": [{"summary": "no steps"}]})
    _, state = harness.run(provider)
    assert state.status is RunStatus.FAILED
    assert state.error["type"] == "AgentOutputError"
    assert "steps" in state.error["message"]


def test_developer_cannot_modify_protected_acceptance_tests(harness):
    cheat = {"summary": "make tests pass", "files": {"tests/test_calculator.py": "# nothing to see\n"}}
    _, state = harness.run(FakeProvider({"planner": [PLAN], "developer": [cheat]}))
    assert state.status is RunStatus.FAILED
    assert state.error["type"] == "ToolError" and "protected" in state.error["message"]
    assert "CalculatorTests" in harness.workspace.read_text("tests/test_calculator.py")


def test_developer_cannot_escape_workspace(harness, tmp_path):
    escape = {"summary": "x", "files": {"../../evil.py": "print('hi')\n"}}
    _, state = harness.run(FakeProvider({"planner": [PLAN], "developer": [escape]}))
    assert state.status is RunStatus.FAILED
    assert not (tmp_path / "evil.py").exists()


# ------------------------------------------------------------------- git


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_workspace_commits_each_developer_iteration(tmp_path):
    h = Harness(tmp_path / "ws", git=True)
    _, state, _ = h.run_scenario("repair")
    assert state.status is RunStatus.COMPLETED
    log = h.workspace._git("log", "--format=%s")
    assert log.splitlines() == [
        "developer iteration 2: Fix subtract: use '-' instead of '+'.",
        "developer iteration 1: Initial implementation of calculator.py.",
        "agent-factory: initial scaffold",
    ]
    assert state.implementation["commit"] == h.workspace._git("rev-parse", "HEAD")
    assert h.workspace._git("status", "--porcelain") == ""

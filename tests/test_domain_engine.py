"""The engine emits a small set of meaningful milestones into the product domain."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent_factory.agents import Agent, AgentResult, TesterAgent, default_agents
from agent_factory.demo import APPROVE, DEV_POLISH, PLAN, scenario_provider
from agent_factory.domain import (
    ApprovalOutcome,
    ApprovalService,
    ApprovalStatus,
    ConflictError,
    MilestoneCategory,
    MilestoneDefinition,
    MilestoneQuery,
    MilestoneStatus,
    NodeMilestoneRule,
    Project,
    Run,
    in_memory_store,
    milestone_policy,
)
from agent_factory.domain.identity import Actor, ActorKind
from agent_factory.domain.ids import new_principal_id
from agent_factory.domain.principals import Principal, PrincipalStatus
from agent_factory.domain.repositories import InMemoryMilestoneRepository
from agent_factory.engine.log import ExecutionLog
from agent_factory.factory import build_executor
from agent_factory.graph import Edge, GraphDefinition, Node
from agent_factory.providers import FakeProvider
from agent_factory.sandbox import FakeOutcome, FakeSandboxBackend, IsolationLevel, SandboxManager
from agent_factory.state import RunStatus, TaskSpec
from agent_factory.templates import CALCULATOR_TEMPLATE
from agent_factory.workspace import MARKER, Workspace

T = CALCULATOR_TEMPLATE


def _run(tmp_path, scenario, *, store=None, run_id="run-1", ws=None, **kw):
    ws = ws or T.create_workspace(tmp_path / f"ws-{scenario}")
    ex = build_executor(T, scenario_provider(scenario), ws, product_store=store, **kw)
    return ws, ex, ex.start(T.task, run_id=run_id)


def _milestones(store, ws, run_id=None):
    return store.milestones.query(MilestoneQuery(project_id=ws.project_id, run_id=run_id))


def _shape(milestones):
    return [(m.type, m.status.value) for m in milestones]


# ------------------------------------------------------------ identity


def test_project_identity_survives_multiple_runs(tmp_path):
    store = in_memory_store()
    ws, _, s1 = _run(tmp_path, "repair", store=store, run_id="run-1")
    _run(tmp_path, "review", store=store, run_id="run-2", ws=ws)
    project_id = ws.project_id
    assert project_id.startswith("prj-") and not any(c in project_id for c in "/\\:")
    assert [p.id for p in store.projects.list()] == [project_id]
    runs = store.runs.list_for_project(project_id)
    assert [(r.run_id, r.number, r.status) for r in runs] == [("run-1", 1, RunStatus.COMPLETED), ("run-2", 2, RunStatus.COMPLETED)]
    assert Workspace.open(ws.root).project_id == project_id  # persisted with the workspace, not derived from its path


def test_existing_workspaces_get_a_stable_project_id_on_first_use(tmp_path):
    ws = T.create_workspace(tmp_path / "legacy")
    marker = ws.meta_dir / MARKER
    data = json.loads(marker.read_text(encoding="utf-8"))
    del data["project_id"]  # a v0.2 workspace has no project id
    marker.write_text(json.dumps(data), encoding="utf-8")
    assert ws.project_id is None
    store = in_memory_store()
    build_executor(T, scenario_provider("repair"), ws, product_store=store)
    first = Workspace.open(ws.root).project_id
    build_executor(T, scenario_provider("repair"), ws, product_store=store)
    assert first is not None and Workspace.open(ws.root).project_id == first


# ------------------------------------------------------------ milestones


def test_repair_run_emits_meaningful_milestones(tmp_path):
    store = in_memory_store()
    ws, ex, state = _run(tmp_path, "repair", store=store)
    ms = _milestones(store, ws, "run-1")
    assert _shape(ms) == [
        ("plan_ready", "completed"),
        ("implementation_ready", "completed"),
        ("tests_passing", "failed"),
        ("implementation_ready", "completed"),
        ("tests_passing", "completed"),
        ("review_approved", "completed"),
        ("run_finished", "completed"),
    ]
    assert [m.sequence for m in ms] == [1, 2, 3, 4, 5, 6, 7]
    failed, passed = ms[2], ms[4]
    assert passed.resolves_id == failed.id  # "what was repaired?"
    assert ms[3].metadata["after"] == {"node": "tester", "outcome": "fail"}  # the repair responded to the failure
    assert ms[0].title == "Plan ready" and ms[0].category is MilestoneCategory.WORKFLOW
    assert ms[0].source.node == "planner" and ms[0].source.agent == "planner"
    assert ms[-1].category is MilestoneCategory.LIFECYCLE
    assert store.runs.get(ws.project_id, "run-1").status is RunStatus.COMPLETED


def test_review_rejection_is_failed_then_resolved(tmp_path):
    store = in_memory_store()
    ws, _, _ = _run(tmp_path, "review", store=store)
    reviews = [m for m in _milestones(store, ws) if m.type == "review_approved"]
    assert [m.status for m in reviews] == [MilestoneStatus.FAILED, MilestoneStatus.COMPLETED]
    assert reviews[1].resolves_id == reviews[0].id


def test_exhausted_run_ends_with_a_failed_milestone_and_no_false_repair(tmp_path):
    store = in_memory_store()
    ws, _, state = _run(tmp_path, "exhaust", store=store)
    ms = _milestones(store, ws)
    assert [m.status for m in ms if m.type == "tests_passing"] == [MilestoneStatus.FAILED] * 4
    assert all(m.resolves_id is None for m in ms)
    assert ms[-1].type == "run_finished" and ms[-1].status is MilestoneStatus.FAILED and "exhausted" in ms[-1].summary
    assert store.runs.get(ws.project_id, "run-1").status is RunStatus.EXHAUSTED


def test_milestones_and_execution_log_stay_distinct(tmp_path):
    store = in_memory_store()
    ws, ex, _ = _run(tmp_path, "repair", store=store)
    log = ex.log_for("run-1")
    assert log.verify() == len(log.entries())
    plain_ws, plain_ex, _ = _run(tmp_path / "plain", "repair")  # same run, no product store
    events = [(e["event"], e.get("node"), e.get("tool")) for e in log.entries()]
    plain_events = [(e["event"], e.get("node"), e.get("tool")) for e in plain_ex.log_for("run-1").entries()]
    assert events == plain_events  # the execution log is unchanged by milestone recording
    assert len(_milestones(store, ws)) == 7 and len(events) > 4 * 7  # milestones are a curated view, not a copy


def test_milestone_ordering_and_ids_are_deterministic(tmp_path):
    fixed = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
    shapes = []
    for name in ("a", "b"):
        store = in_memory_store()
        ws, _, _ = _run(tmp_path / name, "full", store=store, clock=lambda: fixed)
        shapes.append([(m.sequence, m.key, m.type, m.status.value) for m in _milestones(store, ws)])
        assert len({m.occurred_at for m in _milestones(store, ws)}) == 1  # identical timestamps: order from sequence
    assert shapes[0] == shapes[1]


def test_all_recorded_records_are_json_safe_and_round_trip(tmp_path):
    store = in_memory_store()
    ws, ex, _ = _run(tmp_path, "approval", store=store)
    request = store.approvals.list_requests(ws.project_id)[0]
    ex.decide_approval("run-1", request.id, approved=True, approver="alice")
    ex.resume("run-1")
    records = [store.projects.get(ws.project_id), *store.runs.list_for_project(ws.project_id), *_milestones(store, ws),
               *store.approvals.list_requests(ws.project_id), store.approvals.get_decision(ws.project_id, request.id)]  # fmt: skip
    for record in records:
        assert type(record).from_json(record.to_json()) == record


# ------------------------------------------------------------- approvals


def test_approval_request_decision_and_milestones_preserve_v02_semantics(tmp_path):
    store = in_memory_store()
    ws, ex, state = _run(tmp_path, "approval", store=store)
    assert state.status is RunStatus.AWAITING_APPROVAL
    [request] = store.approvals.list_requests(ws.project_id, status=ApprovalStatus.PENDING)
    assert request.id == state.pending_approval["request_id"]  # the engine's exact-action id
    assert (request.action.tool, request.action.params) == ("delete_file", {"path": "legacy_calc.py"})
    assert request.node == "developer" and request.agent == "developer"
    assert "deletes" in request.risk.lower()  # plain-language risk for the category
    decision_ms = [m for m in _milestones(store, ws) if m.type == "human_decision"]
    assert _shape(decision_ms) == [("human_decision", "blocked")]
    assert store.runs.get(ws.project_id, "run-1").status is RunStatus.AWAITING_APPROVAL

    ex.decide_approval("run-1", request.id, approved=True, approver="alice", note="dead code")
    decision = store.approvals.get_decision(ws.project_id, request.id)
    assert decision.outcome is ApprovalOutcome.APPROVED and decision.comment == "dead code"
    assert decision.actor.kind is ActorKind.UNAUTHENTICATED and decision.actor.display_name == "alice"
    assert store.approvals.get_request(ws.project_id, request.id).status is ApprovalStatus.APPROVED

    final = ex.resume("run-1")
    assert final.status is RunStatus.COMPLETED
    assert not (ws.project_dir / "legacy_calc.py").exists()  # same outcome as v0.2
    [done] = [m for m in _milestones(store, ws) if m.type == "human_decision"]
    assert done.status is MilestoneStatus.COMPLETED and done.id == decision_ms[0].id
    assert done.metadata["decision"] == "approved"


def test_rejection_keeps_v02_behaviour(tmp_path):
    store = in_memory_store()
    ws, ex, state = _run(tmp_path, "approval", store=store)
    ex.decide_approval("run-1", state.pending_approval["request_id"], approved=False, approver="alice")
    final = ex.resume("run-1")
    assert final.status is RunStatus.COMPLETED and (ws.project_dir / "legacy_calc.py").exists()
    assert store.approvals.list_requests(ws.project_id)[0].status is ApprovalStatus.REJECTED
    [m] = [m for m in _milestones(store, ws) if m.type == "human_decision"]
    assert m.metadata["decision"] == "rejected"


def _phone_user(store):
    """An enrolled principal, as the API would decide with it."""
    at = "2026-10-01T00:00:00.000000Z"
    principal = Principal(id=new_principal_id(), issuer="https://issuer.example.test", subject="bob",
                          status=PrincipalStatus.ACTIVE, created_at=at, updated_at=at, display_name="Bob")  # fmt: skip
    store.principals.add(principal)
    return principal.actor()


def test_conflicting_decision_is_refused_before_the_engine_changes(tmp_path):
    store = in_memory_store()
    ws, ex, state = _run(tmp_path, "approval", store=store)
    request = store.approvals.list_requests(ws.project_id)[0]
    # An authenticated phone user already rejected it through the domain.
    ApprovalService(store.approvals).decide(
        project_id=ws.project_id, request_id=request.id, action_fingerprint=request.action_fingerprint,
        outcome=ApprovalOutcome.REJECTED, actor=_phone_user(store),
    )  # fmt: skip
    with pytest.raises(ConflictError):
        ex.decide_approval("run-1", request.id, approved=True, approver="alice")
    saved = ex.store.load("run-1")
    assert saved.status is RunStatus.AWAITING_APPROVAL and saved.approvals == {}  # engine state untouched


def test_decision_binds_to_the_action_the_engine_will_execute(tmp_path):
    """Even if the product store holds a different action under the same id, approval cannot transfer."""
    from agent_factory.domain import ApprovalBindingError, ApprovalRequest, ExactAction, risk_for
    from agent_factory.domain.approvals import approval_expiry

    ws, ex, state = _run(tmp_path, "approval")  # paused, no product store yet
    pending = state.pending_approval
    store = in_memory_store()
    recorder_ex = build_executor(T, scenario_provider("approval"), ws, product_store=store)
    store.runs.add(Run(project_id=ws.project_id, run_id="run-1", number=1, graph_name=state.graph_name,
                       status=RunStatus.AWAITING_APPROVAL, created_at=state.created_at, updated_at=state.updated_at))  # fmt: skip
    forged_action = ExactAction(tool="delete_file", category="destructive_delete", params={"path": "tests/test_calculator.py"})
    store.approvals.add_request(ApprovalRequest(
        id=pending["request_id"], project_id=ws.project_id, run_id="run-1", node="developer", agent="developer",
        action=forged_action, action_fingerprint=forged_action.fingerprint, reason="forged",
        risk=risk_for("destructive_delete"), requested_at=pending["requested_at"],
        expires_at=approval_expiry(pending["requested_at"]),
    ))  # fmt: skip
    with pytest.raises(ApprovalBindingError):
        recorder_ex.decide_approval("run-1", pending["request_id"], approved=True, approver="alice")
    assert recorder_ex.store.load("run-1").status is RunStatus.AWAITING_APPROVAL
    assert store.approvals.get_decision(ws.project_id, pending["request_id"]) is None


# ------------------------------------------------- blocked and interrupted


class SimulatedRealModel(FakeProvider):
    name = "simulated-real-model"
    produces_trusted_code = False


SCRIPT = {"planner": [PLAN], "developer": [DEV_POLISH], "reviewer": [APPROVE]}


def test_blocked_milestone_is_unblocked_in_place_on_resume(tmp_path):
    store = in_memory_store()
    ws = T.create_workspace(tmp_path / "blocked")
    provider = SimulatedRealModel(SCRIPT)
    state = build_executor(T, provider, ws, product_store=store).start(T.task, run_id="run-1")  # no Docker in Tier 0
    assert state.status is RunStatus.BLOCKED
    blocked = [m for m in _milestones(store, ws) if m.status is MilestoneStatus.BLOCKED]
    assert _shape(blocked) == [("tests_passing", "blocked")]
    assert store.runs.get(ws.project_id, "run-1").status is RunStatus.BLOCKED

    container = SandboxManager(
        [FakeSandboxBackend(level=IsolationLevel.CONTAINER, outcome=FakeOutcome(stderr="Ran 5 tests in 0.01s\n\nOK\n"))],
        allow_test_doubles=True,
    )  # fmt: skip
    final = build_executor(T, provider, ws, product_store=store, sandbox=container).resume("run-1")
    assert final.status is RunStatus.COMPLETED
    tests = [m for m in _milestones(store, ws) if m.type == "tests_passing"]
    assert len(tests) == 1 and tests[0].id == blocked[0].id and tests[0].status is MilestoneStatus.COMPLETED
    assert _milestones(store, ws)[-1].type == "run_finished"


class _CrashOnceTester(TesterAgent):
    crashed = False

    def run(self, ctx):
        if not _CrashOnceTester.crashed:
            _CrashOnceTester.crashed = True
            raise KeyboardInterrupt("simulated interruption")
        return super().run(ctx)


def test_interrupted_and_resumed_run_records_each_milestone_once(tmp_path):
    _CrashOnceTester.crashed = False
    template = replace(T, agent_factory=lambda: {**default_agents(), "tester": _CrashOnceTester()})
    store = in_memory_store()
    ws = template.create_workspace(tmp_path / "crash")
    with pytest.raises(KeyboardInterrupt):
        build_executor(template, scenario_provider("repair"), ws, product_store=store).start(template.task, run_id="run-1")
    build_executor(template, scenario_provider("repair"), ws, product_store=store).resume("run-1")
    ms = _milestones(store, ws)
    reference_store = in_memory_store()
    ref_ws, _, _ = _run(tmp_path / "ref", "repair", store=reference_store)
    assert _shape(ms) == _shape(_milestones(reference_store, ref_ws))
    assert [m.sequence for m in ms] == list(range(1, len(ms) + 1))
    assert len({m.key for m in ms}) == len(ms)


class _MilestonesFailingOnAdd(InMemoryMilestoneRepository):
    """Simulates a storage outage on the n-th milestone write."""

    def __init__(self, db, fail_on: int) -> None:
        super().__init__(db)
        self._adds, self._fail_on = 0, fail_on

    def add(self, milestone):
        self._adds += 1
        if self._adds == self._fail_on:
            raise OSError("simulated storage outage")
        super().add(milestone)


def _flaky_store(fail_on):
    store = in_memory_store()
    return replace(store, milestones=_MilestonesFailingOnAdd(store.unit_of_work, fail_on))


def _reference_shape(tmp_path):
    reference = in_memory_store()
    ref_ws, _, _ = _run(tmp_path / "ref", "repair", store=reference)
    return _shape(_milestones(reference, ref_ws))


def test_a_recording_failure_is_surfaced_and_the_next_sync_fills_the_gap(tmp_path):
    store = _flaky_store(fail_on=1)  # the first milestone (plan ready) cannot be written
    ws = T.create_workspace(tmp_path / "outage")
    with pytest.raises(OSError, match="simulated storage outage"):
        build_executor(T, scenario_provider("repair"), ws, product_store=store).start(T.task, run_id="run-1")
    ex = build_executor(T, scenario_provider("repair"), ws, product_store=store)
    saved = ex.store.load("run-1")
    assert saved.status is RunStatus.RUNNING and saved.step == 1  # the planner's checkpoint was committed
    assert _milestones(store, ws) == []
    assert ex.resume("run-1").status is RunStatus.COMPLETED
    ms = _milestones(store, ws)
    assert _shape(ms) == _reference_shape(tmp_path)
    assert [m.sequence for m in ms] == list(range(1, len(ms) + 1))


def test_a_finished_run_is_reconciled_from_its_checkpoint(tmp_path):
    store = _flaky_store(fail_on=7)  # the last milestone (run finished) cannot be written
    ws = T.create_workspace(tmp_path / "outage")
    with pytest.raises(OSError):
        build_executor(T, scenario_provider("repair"), ws, product_store=store).start(T.task, run_id="run-1")
    ex = build_executor(T, scenario_provider("repair"), ws, product_store=store)
    assert ex.store.load("run-1").status is RunStatus.COMPLETED  # nothing left to resume
    assert [m.type for m in _milestones(store, ws)][-1] != "run_finished"
    # The failed sync was one transaction: its run-status update was rolled back with the failed write.
    assert store.runs.get(ws.project_id, "run-1").status is RunStatus.RUNNING
    ex.reconcile("run-1")  # re-derive records from the committed checkpoint
    ex.reconcile("run-1")  # idempotent
    assert _shape(_milestones(store, ws)) == _reference_shape(tmp_path)
    assert store.runs.get(ws.project_id, "run-1").status is RunStatus.COMPLETED


def _crash_on_next_checkpoint(monkeypatch, executor):
    """Simulates the process dying after the product store committed but before the checkpoint did."""
    real_save = executor.store.save

    def save(state):
        monkeypatch.setattr(executor.store, "save", real_save)
        raise OSError("simulated crash before the checkpoint was written")

    monkeypatch.setattr(executor.store, "save", save)


def test_a_decision_stored_before_a_crash_is_applied_by_reconcile(tmp_path, monkeypatch):
    from agent_factory.engine import ApprovalPending

    store = in_memory_store()
    ws, ex, state = _run(tmp_path, "approval", store=store)
    request_id = state.pending_approval["request_id"]
    _crash_on_next_checkpoint(monkeypatch, ex)
    with pytest.raises(OSError, match="simulated crash"):
        ex.decide_approval("run-1", request_id, approved=True, approver="alice", note="dead code")
    # The store says APPROVED; the engine never applied it, so the action has NOT run.
    assert store.approvals.get_decision(ws.project_id, request_id).outcome is ApprovalOutcome.APPROVED
    restarted = build_executor(T, scenario_provider("approval"), ws, product_store=store)
    paused = restarted.store.load("run-1")
    assert paused.status is RunStatus.AWAITING_APPROVAL and paused.approvals == {}
    assert (ws.project_dir / "legacy_calc.py").exists()
    with pytest.raises(ApprovalPending):
        restarted.resume("run-1")
    with pytest.raises(ConflictError):  # nobody can now decide differently
        restarted.decide_approval("run-1", request_id, approved=False, approver="mallory")

    applied = restarted.reconcile("run-1")
    assert applied.status is RunStatus.RUNNING and applied.approvals[request_id]["approved"] is True
    assert applied.approvals[request_id]["approver"] == "alice"
    entry = [e for e in restarted.log_for("run-1").entries() if e["event"] == "approval_decision"][-1]
    assert entry["source"] == "product_store" and entry["approved"] is True
    final = restarted.resume("run-1")
    assert final.status is RunStatus.COMPLETED and not (ws.project_dir / "legacy_calc.py").exists()
    [decision_ms] = [m for m in _milestones(store, ws) if m.type == "human_decision"]
    assert decision_ms.status is MilestoneStatus.COMPLETED and decision_ms.metadata["decision"] == "approved"


def test_reconcile_never_applies_a_stored_decision_for_a_different_action(tmp_path):
    """A forged request + decision under the engine's request id cannot authorize the engine's action."""
    from agent_factory.domain import ApprovalBindingError, ApprovalRequest, ExactAction, risk_for
    from agent_factory.domain.approvals import approval_expiry

    ws, _, state = _run(tmp_path, "approval")  # paused; no store yet
    pending = state.pending_approval
    store = in_memory_store()
    ex = build_executor(T, scenario_provider("approval"), ws, product_store=store)
    store.runs.add(Run(project_id=ws.project_id, run_id="run-1", number=1, graph_name=state.graph_name,
                       status=RunStatus.AWAITING_APPROVAL, created_at=state.created_at, updated_at=state.updated_at))  # fmt: skip
    forged = ExactAction(tool="delete_file", category="destructive_delete", params={"path": "tests/test_calculator.py"})
    service = ApprovalService(store.approvals)
    service.open_request(ApprovalRequest(
        id=pending["request_id"], project_id=ws.project_id, run_id="run-1", node="developer", agent="developer",
        action=forged, action_fingerprint=forged.fingerprint, reason="forged", risk=risk_for("destructive_delete"),
        requested_at=pending["requested_at"], expires_at=approval_expiry(pending["requested_at"]),
    ))  # fmt: skip
    service.decide(project_id=ws.project_id, request_id=pending["request_id"], action_fingerprint=forged.fingerprint,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("mallory"))  # fmt: skip
    with pytest.raises(ApprovalBindingError):
        ex.reconcile("run-1")
    still = ex.store.load("run-1")
    assert still.status is RunStatus.AWAITING_APPROVAL and still.approvals == {}
    assert (ws.project_dir / "legacy_calc.py").exists()


def test_a_decision_made_without_the_store_is_recorded_at_the_next_sync(tmp_path):
    store = in_memory_store()
    ws, _, state = _run(tmp_path, "approval", store=store)  # request opened in the store
    request_id = state.pending_approval["request_id"]
    engine_only = build_executor(T, scenario_provider("approval"), ws)  # e.g. `approve` without --database
    engine_only.decide_approval("run-1", request_id, approved=True, approver="alice")
    assert store.approvals.get_decision(ws.project_id, request_id) is None
    build_executor(T, scenario_provider("approval"), ws, product_store=store).resume("run-1")
    decision = store.approvals.get_decision(ws.project_id, request_id)
    assert decision.outcome is ApprovalOutcome.APPROVED and decision.actor.id == "local:alice"
    assert store.approvals.get_request(ws.project_id, request_id).status is ApprovalStatus.APPROVED


def test_a_contradicting_stored_decision_is_surfaced_not_overwritten(tmp_path):
    store = in_memory_store()
    ws, _, state = _run(tmp_path, "approval", store=store)
    request = store.approvals.list_requests(ws.project_id)[0]
    ApprovalService(store.approvals).decide(
        project_id=ws.project_id, request_id=request.id, action_fingerprint=request.action_fingerprint,
        outcome=ApprovalOutcome.REJECTED, actor=_phone_user(store),
    )  # fmt: skip
    build_executor(T, scenario_provider("approval"), ws).decide_approval("run-1", request.id, approved=True)  # engine only
    with pytest.raises(ConflictError, match="records rejected"):
        build_executor(T, scenario_provider("approval"), ws, product_store=store).reconcile("run-1")
    assert store.approvals.get_decision(ws.project_id, request.id).outcome is ApprovalOutcome.REJECTED


# --------------------------------------------------- project-defined types


class StageAgent(Agent):
    writes = frozenset()

    def __init__(self, name):
        self.name = name

    def run(self, ctx):
        return AgentResult("ok", {}, summary=f"{self.name} finished")


def _pipeline_graph():
    return GraphDefinition(
        name="data-pipeline-v1",
        start="ingest",
        nodes=(Node("ingest", agent="ingester"), Node("transform", agent="transformer"),
               Node("load", agent="loader"), Node("done", terminal=True)),
        edges=(Edge("ingest", "ok", "transform"), Edge("transform", "ok", "load"), Edge("load", "ok", "done")),
    )  # fmt: skip


def test_project_specific_milestones_need_no_core_changes(tmp_path):
    """A data-engineering project defines its own milestones purely through its template."""
    deliverables = [
        MilestoneDefinition("ingestion_working", "Ingestion working", MilestoneCategory.DELIVERABLE, order=1),
        MilestoneDefinition("transformation_complete", "Transformation complete", MilestoneCategory.DELIVERABLE, order=2),
        MilestoneDefinition("database_load_verified", "Database load verified", MilestoneCategory.DELIVERABLE, order=3),
    ]
    rules = [
        NodeMilestoneRule("ingest", "ingestion_working", {"ok": MilestoneStatus.COMPLETED}),
        NodeMilestoneRule("transform", "transformation_complete", {"ok": MilestoneStatus.COMPLETED}),
        NodeMilestoneRule("load", "database_load_verified", {"ok": MilestoneStatus.COMPLETED}),
    ]
    template = replace(
        T,
        name="data-pipeline",
        task=TaskSpec(title="Daily sales pipeline", goal="Load sales data", requirements=[]),
        scaffold={},
        graph_factory=_pipeline_graph,
        agent_factory=lambda: {n: StageAgent(n) for n in ("ingester", "transformer", "loader")},
        milestone_policy=milestone_policy(deliverables, rules),
    )
    store = in_memory_store()
    ws = template.create_workspace(tmp_path / "pipeline")
    state = build_executor(template, FakeProvider({}), ws, product_store=store).start(template.task, run_id="run-1")
    assert state.status is RunStatus.COMPLETED
    ms = _milestones(store, ws)
    assert [(m.title, m.category.value) for m in ms] == [
        ("Ingestion working", "deliverable"),
        ("Transformation complete", "deliverable"),
        ("Database load verified", "deliverable"),
        ("Run finished", "lifecycle"),
    ]
    project: Project = store.projects.get(ws.project_id)
    assert project.template == "data-pipeline" and project.milestone_policy == template.milestone_policy


def test_executor_without_a_product_store_is_unchanged(tmp_path):
    _, _, with_store = _run(tmp_path / "a", "full", store=in_memory_store())
    _, _, without = _run(tmp_path / "b", "full")
    assert [(h["node"], h["outcome"]) for h in with_store.history] == [(h["node"], h["outcome"]) for h in without.history]
    assert with_store.status is without.status is RunStatus.COMPLETED

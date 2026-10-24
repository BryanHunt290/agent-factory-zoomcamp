"""Opt-in offline supervision of closed, maintainer-authored coding fixtures.

This module wires the existing Executor; it is not a second scheduler or a
real Codex client. Supervisor records live outside project/, which is the
only directory exposed by workspace tools. Never use its fixture trust for
user-supplied or model-generated code.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path

from .agents.base import Agent, AgentContext, AgentResult
from .agents.developer import feedback_for
from .agents.prompts import DEVELOPER_V2
from .agents.schema import validate
from .capabilities import CapabilityRegistry
from .capabilities.model import ApprovalRule, Capability, CostImpact, Risk
from .capabilities.packs import ToolPack
from .demo import CALC_BUGGY, CALC_CORRECT, CALC_POLISHED
from .engine import Executor
from .engine.checkpoint import validate_run_id
from .graph.definition import Edge, GraphDefinition, Node
from .providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError, ProviderTimeout
from .providers.context import ContextBuilder
from .providers.gateway import RetryPolicy
from .providers.guard import RequestGuard
from .safety.execution import EffectClass, ExecutionSemantics, Idempotency
from .safety.permissions import ActionCategory, PermissionPolicy
from .sandbox.manager import SandboxManager
from .sandbox.types import CodeTrust, ResourceLimits, SandboxBlocked
from .state import RunStatus, TaskSpec
from .templates.base import ProjectTemplate
from .templates.calculator import ACCEPTANCE_TESTS, LEGACY_MODULE
from .tools.base import Tool, ToolContext, ToolError, ToolResult
from .tools.packs import CoreToolPack
from .tools.testing import UNITTEST_COMMAND
from .util import canonical_json, sha256_hex, utc_now, write_json_atomic
from .workspace import Workspace


SCENARIOS = (
    "success", "repair", "test_exhaust", "review_exhaust", "mixed_exhaust",
    "approval", "worker_failure", "timeout", "uncertain", "malformed",
)
PROTECTED = ("tests", ".codex", "AGENTS.md", "task.json", "supervisor.json", "security.json")
ALLOWED = ("calculator.py", "legacy_calc.py", "notes.txt")
SCAFFOLD = {
    "tests/__init__.py": "",
    "tests/test_calculator.py": ACCEPTANCE_TESTS,
    "legacy_calc.py": LEGACY_MODULE,
    "AGENTS.md": "Fixture controls are supervisor-owned.\n",
    ".codex/config.toml": "# Protected fixture, not host configuration.\n",
    "task.json": '{"acceptance": "five fixed calculator checks"}\n',
    "supervisor.json": '{"max_repairs": 2}\n',
    "security.json": '{"network": false, "human_approvals": true}\n',
}
LIMITS = ResourceLimits(timeout_s=10, output_bytes=32_000)
MAX_FILES = 32
MAX_BYTES = 128_000


class EvidenceError(ToolError):
    """Evidence or candidate content is incomplete, inconsistent, or unapproved."""


class SimulatedInterruption(BaseException):
    """A deliberate crash between the durable dispatch intent and its result."""


def digest(value) -> str:
    return sha256_hex(canonical_json(value))


def proposal_for(scenario: str, attempt: int) -> dict:
    """Closed scripts only: no caller-provided code, callbacks, or prompts."""
    if scenario not in SCENARIOS or not 1 <= attempt <= 3:
        raise ValueError("unknown scenario or exhausted coding attempts")
    buggy = scenario == "test_exhaust" or (scenario in ("repair", "mixed_exhaust") and attempt == 1)
    code = CALC_BUGGY if buggy else CALC_POLISHED
    if scenario in ("review_exhaust", "mixed_exhaust") and not buggy:
        code = CALC_CORRECT
    result = {"summary": "Scripted candidate; success requires independent evidence.",
              "files": {"calculator.py": code, "notes.txt": "Offline fixture candidate.\n"}}
    if scenario == "approval":
        result["delete"] = ["legacy_calc.py"]
    return result


class FakeCodexAdapter(ModelProvider):
    """Pure closed fixture selection. No filesystem, tools, credentials, or network."""

    name = "fake-codex"
    model = "maintainer-fixture-v1"
    produces_trusted_code = True

    def __init__(self, scenario: str):
        if scenario not in SCENARIOS:
            raise ValueError("unknown offline scenario")
        self.scenario = scenario
        self.calls: list[tuple[str, int]] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.agent != "developer" or request.call != 1 or not 1 <= request.attempt <= 3:
            raise ProviderError("offline worker refuses redispatch or unknown role")
        self.calls.append((request.run_id, request.attempt))
        if self.scenario == "worker_failure":
            raise ProviderError("simulated worker failure; no candidate produced")
        if self.scenario == "timeout":
            raise ProviderTimeout("simulated worker timeout; outcome unavailable")
        if self.scenario == "uncertain":
            raise SimulatedInterruption("simulated interruption after dispatch intent")
        if self.scenario == "malformed":
            # Deliberately exercise schema rejection. A corrective call is refused above.
            return ModelResponse(content={"success": True}, provider=self.name, model=self.model)
        return ModelResponse(content=proposal_for(self.scenario, request.attempt),
                             provider=self.name, model=self.model,
                             usage={"input_tokens": 0, "output_tokens": 0})


def task_package(scenario: str) -> dict:
    return {
        "version": 1, "scenario": scenario,
        "task": {"title": "Offline supervised calculator", "goal": "Implement the fixed calculator contract.",
                 "requirements": ["Correct add, subtract, multiply and divide.", "Zero divisor raises ValueError."]},
        "acceptance_checks": ["test_add", "test_subtract", "test_multiply", "test_divide", "test_divide_by_zero"],
        "acceptance_sha256": digest(ACCEPTANCE_TESTS),
        "allowed_edit_paths": list(ALLOWED), "protected_paths": list(PROTECTED),
        "test_command": list(UNITTEST_COMMAND), "limits": asdict(LIMITS),
        "max_coding_attempts": 3, "max_repairs": 2,
        "snapshot_limits": {"files": MAX_FILES, "bytes": MAX_BYTES},
    }


def snapshot(workspace: Workspace) -> dict[str, str]:
    """Complete bounded UTF-8 file snapshot; no git/list_files exclusions."""
    root = workspace.project_dir
    if root.is_symlink() or root.is_junction():
        raise EvidenceError("linked fixture root refused")
    result: dict[str, str] = {}
    size = 0
    pending = [root]
    entries = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as listing:
            for entry in listing:
                entries += 1
                if entries > MAX_FILES * 2:
                    raise EvidenceError("candidate exceeds entry bounds")
                path = Path(entry.path)
                # Windows DirEntry.stat may report st_nlink=0; Path.stat asks
                # the filesystem for the actual link count used by this guard.
                info = path.stat(follow_symlinks=False)
                if entry.is_symlink() or path.is_junction():
                    raise EvidenceError("linked candidate path refused")
                if stat.S_ISDIR(info.st_mode):
                    if path.relative_to(root).as_posix() not in ("tests", ".codex"):
                        raise EvidenceError("unknown fixture directory refused")
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise EvidenceError("non-regular or hard-linked candidate refused")
                size += info.st_size
                if size > MAX_BYTES or len(result) >= MAX_FILES:
                    raise EvidenceError("candidate exceeds snapshot bounds")
                try:
                    result[path.relative_to(root).as_posix()] = path.read_bytes().decode("utf-8")
                except UnicodeError as exc:
                    raise EvidenceError("candidate is not UTF-8") from exc
    return result


def candidate_diff(before: dict, after: dict) -> list[dict]:
    changes = []
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) == after.get(path):
            continue
        changes.append({"path": path, "kind": "added" if path not in before else
                        "deleted" if path not in after else "modified",
                        "before": before.get(path), "after": after.get(path),
                        "unified": "".join(difflib.unified_diff(
                            before.get(path, "").splitlines(keepends=True),
                            after.get(path, "").splitlines(keepends=True),
                            fromfile="before/" + path, tofile="after/" + path))})
    if len(canonical_json(changes).encode()) > MAX_BYTES * 4:
        raise EvidenceError("diff exceeds bounds")
    return changes


def check_proposal(proposal: dict) -> None:
    if validate(proposal, DEVELOPER_V2.output_schema):
        raise EvidenceError("malformed proposed changes")
    paths = [*proposal["files"], *proposal.get("delete", [])]
    # Exact canonical spelling rejects Windows case/ADS/backslash/.. aliases too.
    if any(path not in ALLOWED for path in paths) or len(paths) != len(set(paths)):
        raise EvidenceError("proposed path is outside the fixed canonical edit allowlist")


class SupervisorRecords:
    """Data/evidence storage, not orchestration. Every mutation is a gateway tool call."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.directory = workspace.meta_dir / "supervised"
        self.manifest = self._read(self.directory / "task.json")
        scenario = self.manifest.get("package", {}).get("scenario")
        if scenario not in SCENARIOS:
            raise EvidenceError("unknown saved scenario")
        package = task_package(scenario)
        if self.manifest != {"package": package, "package_digest": digest(package), "baseline": SCAFFOLD}:
            raise EvidenceError("fixed task package or baseline changed")
        if workspace.protected != PROTECTED or workspace.trust is not CodeTrust.TRUSTED_FIXTURE:
            raise EvidenceError("fixture protection or trust changed")
        if workspace.is_git or workspace.expects_git:
            raise EvidenceError("demo workspaces must not contain a git repository")
        self.scenario = scenario
        self.package_digest = digest(package)

    @staticmethod
    def _read(path: Path) -> dict:
        if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size > 2_000_000:
            raise EvidenceError("missing or invalid supervisor record")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise EvidenceError("invalid supervisor record") from exc
        if not isinstance(value, dict):
            raise EvidenceError("invalid supervisor record shape")
        return value

    def path(self, run_id: str) -> Path:
        return self.directory / (validate_run_id(run_id) + ".json")

    def load(self, run_id: str) -> dict:
        path = self.path(run_id)
        if not path.exists():
            return {"run_id": run_id, "package_digest": self.package_digest, "attempts": {}}
        envelope = self._read(path)
        data = envelope.get("data")
        if not isinstance(data, dict) or envelope.get("digest") != digest(data):
            raise EvidenceError("supervisor record digest mismatch")
        if data.get("run_id") != run_id or data.get("package_digest") != self.package_digest:
            raise EvidenceError("supervisor record identity mismatch")
        return data

    def save(self, run_id: str, data: dict) -> None:
        write_json_atomic(self.path(run_id), {"data": data, "digest": digest(data)})

    def current(self) -> dict:
        # Re-read the sealed configuration every time, including after resume.
        if self._read(self.directory / "task.json") != self.manifest:
            raise EvidenceError("task package changed during run")
        content = snapshot(self.workspace)
        for path, text in SCAFFOLD.items():
            if path not in ALLOWED and content.get(path) != text:
                raise EvidenceError("protected content changed")
        if set(content) - (set(SCAFFOLD) | set(ALLOWED)):
            raise EvidenceError("unknown fixture content; local execution refused")
        known = {"calculator.py": (CALC_BUGGY, CALC_CORRECT, CALC_POLISHED),
                 "notes.txt": ("Offline fixture candidate.\n",), "legacy_calc.py": (LEGACY_MODULE,)}
        if any(path in content and content[path] not in values for path, values in known.items()):
            raise EvidenceError("unknown fixture bytes; local execution refused")
        if self.workspace.protected != PROTECTED or self.workspace.trust is not CodeTrust.TRUSTED_FIXTURE:
            raise EvidenceError("fixture protection or trust changed")
        return content

    def attempt(self, data: dict, number: int) -> dict:
        record = data["attempts"].get(str(number))
        if not record or record.get("id") != digest([data["run_id"], self.package_digest, number]):
            raise EvidenceError("missing or mismatched dispatch identity")
        if record.get("number") != number or record.get("before") != self.before_attempt(number):
            raise EvidenceError("dispatch baseline does not match known fixture history")
        return record

    def before_attempt(self, number: int) -> dict:
        if type(number) is not int or not 1 <= number <= 3:
            raise EvidenceError("invalid coding attempt number")
        content = dict(SCAFFOLD)
        for previous in range(1, number):
            content = self.expected(proposal_for(self.scenario, previous), content)
        return content

    def expected(self, proposal: dict, base: dict) -> dict:
        check_proposal(proposal)
        result = dict(base)
        for path in proposal.get("delete", []):
            result.pop(path, None)
        result.update(proposal["files"])
        return result

    def verified(self, run_id: str, number: int) -> tuple[dict, dict]:
        data = self.load(run_id)
        record = self.attempt(data, number)
        evidence = record.get("evidence")
        if not evidence or record.get("evidence_digest") != digest(evidence):
            raise EvidenceError("missing or mismatched test evidence")
        candidate = self.current()
        expected = self.expected(proposal_for(self.scenario, number), record["before"])
        if candidate != expected or evidence.get("candidate_digest") != digest(candidate):
            raise EvidenceError("stale candidate evidence")
        if evidence.get("package_digest") != self.package_digest or evidence.get("attempt_id") != record["id"]:
            raise EvidenceError("evidence belongs to another task or dispatch")
        diff = candidate_diff(SCAFFOLD, candidate)
        if evidence.get("diff") != diff or evidence.get("diff_digest") != digest(diff):
            raise EvidenceError("missing or mismatched diff")
        report = evidence.get("test_result")
        if not isinstance(report, dict) or evidence.get("test_digest") != digest(report):
            raise EvidenceError("missing or mismatched test result")
        if evidence.get("command") != list(UNITTEST_COMMAND):
            raise EvidenceError("test command changed")
        self.check_test_result(report)
        return record, evidence

    @staticmethod
    def check_test_result(report: dict) -> None:
        if not isinstance(report, dict) or type(report.get("tests_run")) is not int or report["tests_run"] != 5:
            raise EvidenceError("missing acceptance test results or wrong test count")
        execution = report.get("sandbox", {})
        if (not isinstance(execution, dict) or not execution.get("execution_id") or execution.get("status") != "completed"
                or execution.get("cleanup_verified") is not True or execution.get("timed_out")
                or execution.get("stdout_truncated") or execution.get("stderr_truncated")
                or type(report.get("returncode")) is not int
                or execution.get("exit_code") != report["returncode"]):
            raise EvidenceError("missing or incomplete execution evidence")
        if (type(report.get("passed")) is not bool or report["passed"] != (report["returncode"] == 0)
                or (report["passed"] and report.get("failures") != [])):
            raise EvidenceError("inconsistent test verdict")

    def checked_review(self, record: dict, evidence: dict) -> dict:
        review = record.get("review")
        if not review or review != self.review_for(record, evidence):
            raise EvidenceError("missing or mismatched review")
        if review["verdict"] != "approved":
            raise EvidenceError("approved review required")
        return review

    def review_for(self, record: dict, evidence: dict) -> dict:
        verdict = "changes_required" if self.scenario in ("review_exhaust", "mixed_exhaust") else "approved"
        return {"verdict": verdict, "summary": "Scripted independent review of bound evidence.",
                "comments": ["Add documentation."] if verdict != "approved" else [],
                "evidence_digest": digest(evidence), "candidate_digest": evidence["candidate_digest"],
                "package_digest": self.package_digest, "attempt_id": record["id"]}

    def operation(self, context: ToolContext, operation: str, params: dict) -> dict:
        roles = {"plan": "planner", "begin": "developer", "proposal": "developer",
                 "apply": "apply", "prepare_tests": "tester", "tests": "tester",
                 "review": "reviewer", "finish": "finalizer"}
        if roles.get(operation) != context.agent:
            raise EvidenceError("supervisor operation refused for this role")
        run_id = context.run_id
        data = self.load(run_id)
        if operation == "plan":
            if self.current() != SCAFFOLD:
                raise EvidenceError("initial fixture does not match baseline")
            return {"summary": "Implement the fixed task; independently test and review.",
                    "package_digest": self.package_digest,
                    "acceptance_criteria": self.manifest["package"]["acceptance_checks"]}
        if operation == "begin":
            number = context.visit
            if not 1 <= number <= 3 or len(data["attempts"]) > 3:
                raise EvidenceError("coding budget exhausted")
            key = str(number)
            if key in data["attempts"]:
                record = self.attempt(data, number)
                if record.get("status") != "proposed":
                    raise SandboxBlocked("worker dispatch outcome unknown; no automatic redispatch")
                return copy.deepcopy(record)
            if len(data["attempts"]) != number - 1:
                raise EvidenceError("dispatch count disagrees with checkpoint")
            before = self.current()
            if before != self.before_attempt(number):
                raise EvidenceError("candidate does not match prior scripted attempt")
            record = {"id": digest([run_id, self.package_digest, number]), "number": number,
                      "status": "started", "before": before}
            data["attempts"][key] = record
            self.save(run_id, data)  # write ahead of even the fake provider invocation
            return copy.deepcopy(record)
        number = params.pop("number", context.visit)
        record = self.attempt(data, number)
        if operation == "proposal":
            proposal = params["proposal"]
            check_proposal(proposal)
            if proposal != proposal_for(self.scenario, number):
                raise EvidenceError("worker result is not the approved scripted fixture")
            if record["status"] != "started":
                raise EvidenceError("dispatch already has an outcome")
            record.update(status="proposed", proposal=proposal)
            self.save(run_id, data)
            return copy.deepcopy(record)
        if record.get("status") != "proposed" or record.get("proposal") != proposal_for(self.scenario, number):
            raise EvidenceError("missing or altered saved proposal")
        if operation == "apply":
            current = self.current()
            expected = self.expected(record["proposal"], record["before"])
            # Known idempotent writes can resume after partial application; nothing else can.
            for path in current.keys() | record["before"].keys() | expected.keys():
                if current.get(path) not in (record["before"].get(path), expected.get(path)):
                    raise EvidenceError("candidate changed outside saved proposal")
            return copy.deepcopy(record)
        if operation == "prepare_tests":
            current = self.current()
            if current != self.expected(proposal_for(self.scenario, number), record["before"]):
                raise EvidenceError("unrecognized fixture; refusing trusted local execution")
            record["testing_candidate"] = digest(current)
            self.save(run_id, data)
            return {"candidate_digest": digest(current)}
        if operation == "tests":
            candidate = self.current()
            if digest(candidate) != record.get("testing_candidate") or params["candidate_digest"] != digest(candidate):
                raise EvidenceError("candidate changed during tests")
            report = params["report"]
            self.check_test_result(report)
            diff = candidate_diff(SCAFFOLD, candidate)
            evidence = {"attempt_id": record["id"], "package_digest": self.package_digest,
                        "candidate_digest": digest(candidate), "diff": diff, "diff_digest": digest(diff),
                        "command": list(UNITTEST_COMMAND), "test_result": report, "test_digest": digest(report),
                        "execution": "actual trusted fixture tests"}
            record.update(evidence=evidence, evidence_digest=digest(evidence))
            self.save(run_id, data)
            return {**report, "evidence_digest": digest(evidence), "candidate_digest": digest(candidate), "number": number}
        record, evidence = self.verified(run_id, number)
        report = evidence["test_result"]
        if not report.get("passed") or report.get("returncode") != 0 or report.get("tests_run") != 5:
            raise EvidenceError("passing acceptance evidence required")
        if operation == "review":
            review = self.review_for(record, evidence)
            # verified() rereads records; mutate that freshly loaded record's containing journal.
            data = self.load(run_id)
            data["attempts"][str(number)]["review"] = review
            self.save(run_id, data)
            return review
        review = self.checked_review(record, evidence)
        if params.get("review") != review or params.get("test_report") != {
            **report, "evidence_digest": digest(evidence), "candidate_digest": evidence["candidate_digest"], "number": number
        }:
            raise EvidenceError("checkpoint evidence differs from supervisor evidence")
        return {"verdict": "approved", "summary": "Fixed acceptance checks, candidate diff and review verified.",
                "completion": {"package_digest": self.package_digest, "candidate_digest": evidence["candidate_digest"],
                               "evidence_digest": digest(evidence), "review_digest": digest(review),
                               "attempts": number, "repairs": number - 1}}


class SupervisorTool(Tool):
    name = "demo_control"
    category = ActionCategory.WRITE_WORKSPACE
    wants_context = True
    execution = ExecutionSemantics(effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.NATURAL,
                                   rationale="Same dispatch identity replays a saved result or blocks; supervisor records only.")

    def __init__(self, records: SupervisorRecords):
        self.records = records

    def run(self, workspace: Workspace, *, context: ToolContext, operation: str, **params) -> ToolResult:
        if workspace.root != self.records.workspace.root:
            raise EvidenceError("wrong supervisor workspace")
        return ToolResult(data=self.records.operation(context, operation, params), summary=f"supervisor {operation}")


class SupervisorPack(ToolPack):
    name = "supervised"

    def __init__(self, records: SupervisorRecords):
        self.tool = SupervisorTool(records)

    @property
    def enabled(self):
        return True

    def tools(self):
        return (self.tool,)

    def capabilities(self):
        return (Capability(capability_id="supervised.control", version=1, pack=self.name,
                           description="Record and verify offline demonstration dispatch and evidence.",
                           implemented_by=self.tool.name, category=self.tool.category, effect=EffectClass.IDEMPOTENT,
                           approval=ApprovalRule.POLICY, cost=CostImpact.NONE, risk=Risk.LOW, mutates=True,
                           input_schema={"type": "object", "additionalProperties": False,
                                         "required": ["operation"], "properties": {
                                             "operation": {"type": "string", "enum": ["plan", "begin", "proposal", "apply",
                                                           "prepare_tests", "tests", "review", "finish"]},
                                             "number": {"type": "integer", "minimum": 1, "maximum": 3},
                                             "proposal": DEVELOPER_V2.output_schema,
                                             "candidate_digest": {"type": "string", "maxLength": 64},
                                             "report": {"type": "object"}, "review": {"type": "object"},
                                             "test_report": {"type": "object"}}}, output_schema={"type": "object"},
                           required_permissions=("tool:demo_control", "category:write_workspace")),)


class DemoAgent(Agent):
    def __init__(self, name: str, field: str):
        self.name = name
        self.writes = frozenset({field})

    def run(self, ctx: AgentContext) -> AgentResult:
        if asdict(ctx.state.task) != task_package("success")["task"]:
            raise EvidenceError("run task differs from fixed approved task")
        def control(operation, **params):
            return ctx.tools.call("demo_control", operation=operation, **params).data

        if self.name == "planner":
            return AgentResult("planned", {"plan": control("plan")})
        if self.name == "developer":
            record = control("begin")
            if record["status"] == "started":
                context = ContextBuilder(host_paths=ctx.host_paths)
                proposal = self.ask(ctx, DEVELOPER_V2,
                                    {"task": asdict(ctx.state.task), "plan": ctx.state.plan,
                                     "iteration": ctx.visit, "feedback": feedback_for(ctx.state, context)},
                                    context=context.manifest())
                record = control("proposal", proposal=proposal)
            return AgentResult("proposed", {"implementation": {"number": record["number"],
                               "dispatch_id": record["id"], "summary": record["proposal"]["summary"]}})
        number = ctx.state.implementation["number"]
        if self.name == "apply":
            record = control("apply", number=number)
            proposal = record["proposal"]
            # Deletions pause before writes; rejection/expiry fail this demo, never auto-approve.
            for path in proposal.get("delete", []):
                ctx.tools.call("delete_file", path=path)
            for path, content in sorted(proposal["files"].items()):
                ctx.tools.call("write_file", path=path, content=content)
            return AgentResult("implemented", {"implementation": dict(ctx.state.implementation)})
        if self.name == "tester":
            candidate = control("prepare_tests", number=number)
            report = ctx.tools.call("run_tests").data
            bound = control("tests", number=number, report=report, **candidate)
            return AgentResult("pass" if report["passed"] else "fail", {"test_report": bound})
        if self.name == "reviewer":
            review = control("review", number=number)
            return AgentResult(review["verdict"], {"review": review})
        review = control("finish", number=number, test_report=ctx.state.test_report, review=ctx.state.review)
        return AgentResult("verified", {"review": review})


def demo_graph() -> GraphDefinition:
    return GraphDefinition(name="offline-supervised-v1", start="planner", max_steps=14,
                           nodes=(Node("planner", "planner", max_visits=1), Node("developer", "developer", max_visits=3),
                                  Node("apply", "apply", max_visits=3), Node("tester", "tester", max_visits=3),
                                  Node("reviewer", "reviewer", max_visits=3), Node("finalizer", "finalizer", max_visits=1),
                                  Node("done", terminal=True)),
                           edges=(Edge("planner", "planned", "developer"), Edge("developer", "proposed", "apply"),
                                  Edge("apply", "implemented", "tester"), Edge("tester", "pass", "reviewer"),
                                  Edge("tester", "fail", "developer", max_traversals=2),
                                  Edge("reviewer", "changes_required", "developer", max_traversals=2),
                                  Edge("reviewer", "approved", "finalizer"), Edge("finalizer", "verified", "done")))


def demo_template() -> ProjectTemplate:
    policy = PermissionPolicy(agent_tools={
        "planner": {"demo_control"}, "developer": {"demo_control"},
        "apply": {"demo_control", "write_file", "delete_file"},
        "tester": {"demo_control", "run_tests"}, "reviewer": {"demo_control"}, "finalizer": {"demo_control"},
    })
    return ProjectTemplate(name="offline-supervised-demo", description="Closed offline supervision fixtures only.",
                           task=TaskSpec(**task_package("success")["task"]), scaffold=dict(SCAFFOLD),
                           protected_paths=PROTECTED, sandbox_limits=LIMITS, graph_factory=demo_graph,
                           agent_factory=lambda: {name: DemoAgent(name, field) for name, field in (
                               ("planner", "plan"), ("developer", "implementation"), ("apply", "implementation"),
                               ("tester", "test_report"), ("reviewer", "review"), ("finalizer", "review"))}, policy=policy)


def open_demo(root: Path | str, *, adapter: str = "fake-codex", clock=utc_now, ownership=None) -> Executor:
    if adapter != "fake-codex":
        raise ValueError("only fake-codex is supported by this offline demonstration")
    workspace = Workspace.open(root)
    if workspace.info.get("template") != "offline-supervised-demo":
        raise EvidenceError("not an offline demonstration workspace")
    records = SupervisorRecords(workspace)
    template = demo_template()
    registry = CapabilityRegistry((CoreToolPack(template.test_command, LIMITS, SandboxManager.default().run),
                                   SupervisorPack(records)), clock=clock)
    return Executor(graph=template.graph_factory(), agents=template.agent_factory(),
                    provider=FakeCodexAdapter(records.scenario), workspace=workspace,
                    tools=registry.tool_registry(), capabilities=registry, policy=template.policy,
                    clock=clock, ownership=ownership, model_retry=RetryPolicy(max_retries=0),
                    # Closed offline fixtures contain no environment-derived data. Do not read key files.
                    request_guard=RequestGuard(frozenset()))


def create_demo(root: Path | str, scenario: str = "repair", *, adapter: str = "fake-codex", clock=utc_now,
                ownership=None) -> Executor:
    if adapter != "fake-codex" or scenario not in SCENARIOS:
        raise ValueError("unknown adapter or scenario; only closed offline fixtures are supported")
    root = Path(root).resolve()
    repository = Path(__file__).resolve().parent.parent
    if root == repository or repository in root.parents or root.exists():
        raise ValueError("demo requires a new disposable directory outside the repository")
    template = demo_template()
    workspace = template.create_workspace(root, git=False)
    package = task_package(scenario)
    write_json_atomic(workspace.meta_dir / "supervised" / "task.json",
                      {"package": package, "package_digest": digest(package), "baseline": SCAFFOLD})
    return open_demo(root, adapter=adapter, clock=clock, ownership=ownership)


def completion_report(executor: Executor, run_id: str = "demo") -> dict:
    state = executor.store.load(run_id)
    records = SupervisorRecords(executor.workspace)
    data = records.load(run_id)
    attempts = list(data["attempts"].values())
    if state.status is RunStatus.COMPLETED:
        number = state.implementation["number"]
        record, evidence = records.verified(run_id, number)
        review = records.checked_review(record, evidence)
        if not evidence["test_result"]["passed"]:
            raise EvidenceError("completion requires passing test evidence")
        expected = {"package_digest": records.package_digest, "candidate_digest": evidence["candidate_digest"],
                    "evidence_digest": digest(evidence), "review_digest": digest(review),
                    "attempts": number, "repairs": number - 1}
        if not review or review.get("verdict") != "approved" or state.review.get("completion") != expected:
            raise EvidenceError("completion lacks matching review evidence")
    return {"status": state.status.value, "scenario": records.scenario, "workspace": str(executor.workspace.root),
            "attempts": len(attempts), "repairs": max(0, len(attempts) - 1),
            "history": state.history, "attempt_evidence": attempts, "review": state.review,
            "pending_approval": state.pending_approval, "blocked": state.blocked, "error": state.error,
            "limitations": "Trusted scripted fixtures only; adversarial verdict forgery unsolved. Real Codex not connected.",
            "runtime": {"MXC": "NOT VERIFIED", "effective_permissions": "NOT VERIFIED",
                        "complete_effective_approval_rules": "NOT VERIFIED"}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="repair")
    parser.add_argument("--resume", type=Path, help="Retained demo workspace; never auto-approves a pending action")
    args = parser.parse_args(argv)
    if args.resume:
        executor = open_demo(args.resume)
        state = executor.advance("demo")
    else:
        parent = Path(tempfile.mkdtemp(prefix="agent-factory-supervised-"))
        executor = create_demo(parent / "fixture", args.scenario)
        try:
            state = executor.start(demo_template().task, run_id="demo")
        except SimulatedInterruption:
            print(json.dumps({"status": "simulated_interruption", "workspace": str(executor.workspace.root),
                              "message": "Dispatch intent retained; resume will block, never redispatch."}))
            return 2
    print(json.dumps(completion_report(executor), indent=2))
    return 0 if state.status is RunStatus.COMPLETED else 2


if __name__ == "__main__":
    raise SystemExit(main())

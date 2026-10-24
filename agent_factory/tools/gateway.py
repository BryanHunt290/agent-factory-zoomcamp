"""The single choke point between agents and tools.

Each call is checked against the permission policy, gated on human approval
when required, executed, and reported to the execution log via ``on_action``.

An approved action never runs here directly. It is handed to the run's
execution guard (``engine/actions.py``), which records the dispatch durably
before the tool runs, replays an outcome already recorded, and stops the run
when an earlier attempt's outcome is uncertain. Approval is not execution
(docs/design/action-execution.md). Without a guard, an approved action is
refused: there is no path that runs it without a write-ahead record.

With a Capability Registry (docs/capabilities.md), every tool must also belong
to a registered capability that is available now; the capability's own rules
can only make the policy's decision stricter, never looser. ``call_capability``
resolves a capability ID to its tool and then takes exactly the same path.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..domain.approvals import ExactAction
from ..safety.approval import ApprovalRequest, ApprovalRequired, PermissionDenied, make_request_id
from ..safety.permissions import Decision, PermissionPolicy
from ..workspace import Workspace
from .base import Tool, ToolContext, ToolError, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from ..capabilities.registry import CapabilityRegistry
    from ..engine.actions import ActionGuard, ExecutionContext

ActionRecorder = Callable[[dict[str, Any]], None]


class ToolGateway:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        workspace: Workspace,
        run_id: str,
        node: str,
        agent: str,
        visit: int,
        approvals: Mapping[str, Mapping[str, Any]],
        on_action: ActionRecorder,
        executions: ActionGuard | None = None,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._registry = registry
        self._policy = policy
        self._workspace = workspace
        self._run_id = run_id
        self._node = node
        self._agent = agent
        self._visit = visit
        self._approvals = approvals
        self._on_action = on_action
        self._executions = executions

    def call_capability(self, capability_id: str, **params: Any) -> ToolResult:
        """Run a capability by ID: resolved through the registry, then exactly the ``call`` path."""
        cap = self._capabilities.get(capability_id) if self._capabilities is not None else None
        if cap is None or cap.implemented_by is None:
            shown = capability_id[:80] if isinstance(capability_id, str) else repr(capability_id)[:80]
            self._record(shown, None, "deny", {}, "denied", "unregistered or non-executable capability")
            raise PermissionDenied(f"capability {shown!r} is not registered or has no implementation")
        return self.call(cap.implemented_by, **params)

    def call(self, tool_name: str, **params: Any) -> ToolResult:
        tool = self._registry.get(tool_name)
        if tool is None:
            self._record(tool_name, None, "deny", {}, "denied", "unknown tool")
            raise PermissionDenied(f"unknown tool {tool_name!r}")

        summary_params = tool.summarize_params(params)
        decision = self._policy.decide(self._agent, tool_name, tool.category)
        if self._capabilities is not None:
            cap = self._capabilities.for_tool(tool_name)
            if cap is None:
                self._record(tool_name, tool.category, Decision.DENY, summary_params, "denied", "no registered capability")
                raise PermissionDenied(f"{tool_name} belongs to no registered capability")
            availability = self._capabilities.availability(cap.capability_id)
            if not availability.available:
                self._record(tool_name, tool.category, Decision.DENY, summary_params, "denied",
                             f"capability {cap.capability_id} unavailable: {availability.status.value}")  # fmt: skip
                raise PermissionDenied(f"capability {cap.capability_id} is unavailable ({availability.status.value})")
            decision = self._capabilities.effective_decision(decision, cap)  # only ever stricter
        approval_id = None

        if decision is Decision.DENY:
            self._record(tool_name, tool.category, decision, summary_params, "denied", "denied by policy")
            raise PermissionDenied(f"{self._agent} may not use {tool_name} ({tool.category.value})")
        if tool.wants_context and "context" in params:
            self._record(tool_name, tool.category, decision, summary_params, "denied", "reserved parameter")
            raise PermissionDenied(f"{tool_name}: 'context' is supplied by the gateway, not the caller")

        if decision is Decision.REQUIRE_APPROVAL:
            approval_id = make_request_id(self._run_id, self._node, self._visit, tool_name, summary_params)
            record = self._approvals.get(approval_id)
            if record is None:
                raise ApprovalRequired(
                    ApprovalRequest(
                        request_id=approval_id,
                        run_id=self._run_id,
                        node=self._node,
                        agent=self._agent,
                        tool=tool_name,
                        category=tool.category.value,
                        params=summary_params,
                        reason=f"{tool.category.value} requires human approval",
                    )
                )
            if not record.get("approved"):
                refused = record.get("refused")
                if refused == "expired":
                    summary = "the approval expired before it could be used"
                    message = f"the approval for {tool_name} expired before it could be used (request {approval_id}); a new request is needed"
                elif refused:
                    summary = f"the approval was not honored ({refused})"
                    message = f"the approval for {tool_name} was not honored: {refused} (request {approval_id})"
                else:
                    summary = f"rejected by {record.get('approver', 'human')}"
                    message = f"human rejected {tool_name} (request {approval_id})"
                self._record(tool_name, tool.category, decision, summary_params, "denied", summary, approval_id)
                raise PermissionDenied(message)
            return self._execute_approved(tool, params, summary_params, decision, approval_id, record)

        return self._run(tool, params, summary_params, decision, approval_id, None)

    def _execute_approved(
        self,
        tool: Tool,
        params: dict[str, Any],
        summary_params: dict[str, Any],
        decision: Decision,
        approval_id: str,
        record: Mapping[str, Any],
    ) -> ToolResult:
        """Hand an approved action to the execution guard: it decides run, replay, or stop."""
        action = ExactAction(tool=tool.name, category=tool.category.value, params=summary_params)
        approved = record.get("tool"), record.get("category"), record.get("params")
        if approved != (None, None, None) and approved != (action.tool, action.category, action.params):
            # The request ID matched but the recorded action differs: never run something other than
            # exactly what was approved.
            self._record(tool.name, tool.category, decision, summary_params, "denied", "approved action differs", approval_id)
            raise PermissionDenied(f"{tool.name}: the approval {approval_id} is for a different action")
        if self._executions is None:
            self._record(
                tool.name, tool.category, decision, summary_params, "denied",
                "no execution ledger: approved actions run only with a durable record", approval_id,
            )  # fmt: skip
            raise PermissionDenied(f"{tool.name}: approved actions run only through the execution ledger")
        try:
            # Bad parameters are refused before anything is recorded or sent. A TypeError raised
            # later, during the dispatch, is then an unknown outcome, never "definitely not sent".
            extra = {"context": None} if tool.wants_context else {}
            inspect.signature(tool.run).bind(self._workspace, **extra, **params)
        except TypeError as exc:
            self._record(tool.name, tool.category, decision, summary_params, "error", f"bad parameters: {exc}", approval_id)
            raise ToolError(f"bad parameters for {tool.name}: {exc}") from exc
        return self._executions.execute(
            request_id=approval_id,
            approval=record,
            tool=tool,
            action=action,
            node=self._node,
            visit=self._visit,
            agent=self._agent,
            invoke=lambda context: self._run(tool, params, summary_params, decision, approval_id, context),
        )

    def _run(
        self,
        tool: Tool,
        params: dict[str, Any],
        summary_params: dict[str, Any],
        decision: Decision,
        approval_id: str | None,
        execution: ExecutionContext | None,
    ) -> ToolResult:
        tool_name = tool.name
        extra = {}
        if tool.wants_context:
            extra["context"] = ToolContext(
                self._run_id, self._node, self._agent, self._visit,
                execution_id=execution.execution_id if execution else None,
                attempt=execution.attempt if execution else None,
                idempotency_key=execution.idempotency_key if execution else None,
            )  # fmt: skip
        try:
            result = tool.run(self._workspace, **extra, **params)
        except ToolError as exc:
            self._record(tool_name, tool.category, decision, summary_params, "error", str(exc), approval_id)
            raise
        except TypeError as exc:
            self._record(tool_name, tool.category, decision, summary_params, "error", str(exc), approval_id)
            if execution is not None:
                raise  # parameters were checked before the dispatch: this happened inside the tool
            raise ToolError(f"bad parameters for {tool_name}: {exc}") from exc
        except Exception as exc:  # e.g. SandboxBlocked: record it, then let the executor decide
            self._record(
                tool_name, tool.category, decision, summary_params, "error", f"{type(exc).__name__}: {exc}", approval_id
            )
            raise

        self._record(
            tool_name, tool.category, decision, summary_params,
            "ok" if result.ok else "failed", result.summary, approval_id,
        )
        return result

    def _record(self, tool, category, decision, params, result, summary, approval_id=None) -> None:
        entry = {
            "tool": tool,
            "category": category.value if category else None,
            "decision": str(decision),
            "params": params,
            "result": result,
            "summary": summary,
        }
        if approval_id:
            entry["approval_id"] = approval_id
        self._on_action(entry)

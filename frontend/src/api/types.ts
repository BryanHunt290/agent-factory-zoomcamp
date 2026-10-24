// Names for the contract's types. Every shape here comes from schema.ts, which is generated from
// openapi.yaml; nothing is declared by hand, so a contract change that breaks the dashboard fails typecheck.
import type { components, operations } from "./schema";

type Schemas = components["schemas"];

export type ProjectSummary = Schemas["ProjectSummary"];
export type ProjectSummaryPage = Schemas["ProjectSummaryPage"];
export type Project = Schemas["Project"];
export type ProjectStatus = Schemas["ProjectStatus"];
export type Run = Schemas["Run"];
export type ModelUsage = Schemas["ModelUsage"];
export type RunPage = Schemas["RunPage"];
export type RunStatus = Schemas["RunStatus"];
export type Milestone = Schemas["Milestone"];
export type MilestonePage = Schemas["MilestonePage"];
export type MilestoneStatus = Schemas["MilestoneStatus"];
export type ApprovalRequest = Schemas["ApprovalRequest"];
export type ApprovalStatus = Schemas["ApprovalStatus"];
export type ActionExecution = Schemas["ActionExecution"];
export type ExecutionState = Schemas["ExecutionState"];
export type TimelineEntry = Schemas["TimelineEntry"];
export type TimelineKind = Schemas["TimelineKind"];
export type AttentionItem = Schemas["AttentionItem"];
export type AttentionKind = Schemas["AttentionKind"];
export type AttentionPage = Schemas["AttentionPage"];
export type NotificationStatus = Schemas["NotificationStatus"];
export type NotificationState = Schemas["NotificationState"];
export type Actor = Schemas["Actor"];
export type Problem = Schemas["Problem"];
export type ErrorCode = Schemas["ErrorCode"];
export type DecisionOutcome = Schemas["DecisionOutcome"];
export type DecisionRequest = Schemas["ApprovalDecisionRequest"];
export type ReconciliationChoice = Schemas["ReconciliationChoice"];
export type ReconciliationRequest = Schemas["ReconciliationRequest"];

/** The JSON body an operation answers with for ``Status``. */
export type Json<Op extends keyof operations, Status extends number> = operations[Op]["responses"] extends {
  [S in Status]: { content: { "application/json": infer Body } };
}
  ? Body
  : never;

/** An operation's query parameters (all optional in this contract). */
export type Query<Op extends keyof operations> = NonNullable<operations[Op]["parameters"]["query"]>;

/** The request body an operation accepts. */
export type Body<Op extends keyof operations> = operations[Op] extends {
  requestBody: { content: { "application/json": infer B } };
}
  ? B
  : never;

/** The API's readiness answer (GET /readyz, infrastructure, outside openapi.yaml; docs/backend.md). */
export type Readiness =
  | { status: "ready" }
  | { status: "not_ready"; reason: "database_unavailable" | "schema_not_current" };

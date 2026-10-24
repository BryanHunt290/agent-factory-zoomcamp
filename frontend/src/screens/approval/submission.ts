// What happened to a decision or reconciliation answer the person sent: pure logic, no React.
//
// The dashboard never shows a decision as recorded until the API says so. When a submission gets no
// answer (a timeout, a dropped connection) or a server failure (5xx), the outcome is unknown: the API may
// or may not have recorded it. The dashboard then asks the API for the approval and decides from that
// authoritative state, never from what the person tapped:
//
//   recorded_as_sent     the API holds exactly what was sent         -> done; nothing to resend
//   different_on_record  the API holds a different decision/answer   -> show it; never send another
//   closed               the request can no longer be decided        -> show why; nothing to send
//   not_recorded         still waiting, nothing recorded             -> the person may send the same again
//
// Sending the identical decision again is safe: the API treats an identical repeat by the same person
// as a no-op (200). A different second decision would be refused (409).
import type { ApiError } from "../../api/errors";
import type { ApprovalRequest, DecisionOutcome, ReconciliationChoice } from "../../api/types";

export type Sent =
  | { kind: "decision"; outcome: DecisionOutcome; comment: string }
  | { kind: "reconciliation"; choice: ReconciliationChoice; attempt: number; executionId: string; comment: string };

export type Submission =
  | { phase: "idle" }
  | { phase: "sending"; sent: Sent }
  | { phase: "recorded"; sent: Sent; recordedNow: boolean; requestId: string | null }
  | { phase: "refused"; sent: Sent; error: ApiError }
  | { phase: "unknown"; sent: Sent; error: unknown; /** tick() at the failure */ failedAt: number };

export type Resolution = "recorded_as_sent" | "different_on_record" | "closed" | "not_recorded";

/** Decide an unknown outcome from an approval the API returned after the failure. */
export function resolveUnknown(sent: Sent, approval: ApprovalRequest): Resolution {
  if (sent.kind === "decision") {
    if (approval.decision) return approval.decision.outcome === sent.outcome ? "recorded_as_sent" : "different_on_record";
    return approval.status === "pending" && approval.attention === "decision_required" ? "not_recorded" : "closed";
  }
  const execution = approval.execution;
  const answer = execution?.reconciliation;
  if (answer && answer.attempt === sent.attempt) {
    return answer.choice === sent.choice ? "recorded_as_sent" : "different_on_record";
  }
  if (execution && execution.id === sent.executionId && execution.state === "uncertain" && execution.attempt === sent.attempt) {
    return "not_recorded";
  }
  return "closed";
}

function noun(sent: Sent): string {
  if (sent.kind === "reconciliation") return "answer";
  return sent.outcome === "approved" ? "approval" : "rejection";
}

export function sendingText(sent: Sent): string {
  return `Sending your ${noun(sent)} to Agent Factory…`;
}

export function recordedText(sent: Sent, recordedNow: boolean): string {
  const already = recordedNow ? "" : " It was already on record.";
  if (sent.kind === "reconciliation") {
    return `Agent Factory recorded your answer.${already} A worker applies it shortly.`;
  }
  return sent.outcome === "approved"
    ? `Agent Factory recorded your approval.${already} A worker continues the run shortly.`
    : `Agent Factory recorded your rejection.${already} The run continues without this action.`;
}

export function unknownText(sent: Sent, resolution: Resolution | null): string {
  const what = noun(sent);
  switch (resolution) {
    case "recorded_as_sent":
      return `Your ${what} reached Agent Factory and is on record.`;
    case "different_on_record":
      return sent.kind === "reconciliation"
        ? "A different answer is on record for this attempt. Nothing more was sent."
        : "A different decision is on record. Nothing more was sent.";
    case "closed":
      return `This can no longer be ${sent.kind === "reconciliation" ? "answered" : "decided"}. Your ${what} was not recorded.`;
    case "not_recorded":
      return `Your ${what} did not reach Agent Factory. Nothing was recorded. You can send it again.`;
    default:
      return `We couldn't confirm whether your ${what} reached Agent Factory. Checking with Agent Factory; nothing will be sent again without you.`;
  }
}

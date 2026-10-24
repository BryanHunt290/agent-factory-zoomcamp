// The dashboard's vocabulary: what each state means, in plain words. Status is always shown as text
// (with a symbol); color only reinforces it.
import type {
  AttentionKind,
  ExecutionState,
  MilestoneStatus,
  ModelUsage,
  NotificationStatus,
  ProjectStatus,
  ReconciliationChoice,
  RunStatus,
  TimelineEntry,
} from "../api/types";

export type Tone = "attention" | "active" | "ok" | "bad" | "neutral";

type NotificationFailure = NonNullable<NotificationStatus["failure"]>;

interface StatusInfo {
  label: string;
  tone: Tone;
  symbol: string;
}

const RUN_STATUS: Record<ProjectStatus, StatusInfo> = {
  not_started: { label: "Not started", tone: "neutral", symbol: "○" },
  running: { label: "Running", tone: "active", symbol: "▶" },
  awaiting_approval: { label: "Waiting for approval", tone: "attention", symbol: "!" },
  awaiting_reconciliation: { label: "Waiting for you to confirm an action", tone: "attention", symbol: "?" },
  blocked: { label: "Blocked", tone: "bad", symbol: "■" },
  completed: { label: "Completed", tone: "ok", symbol: "✓" },
  failed: { label: "Failed", tone: "bad", symbol: "✕" },
  exhausted: { label: "Stopped at a limit", tone: "bad", symbol: "✕" },
};

export function runStatus(status: ProjectStatus | RunStatus): StatusInfo {
  return RUN_STATUS[status] ?? { label: status, tone: "neutral", symbol: "•" };
}

/** Is the run still doing something, or waiting for someone? (Polled more often.) */
export function isActive(status: ProjectStatus | RunStatus | undefined | null): boolean {
  return status === "running" || status === "awaiting_approval" || status === "awaiting_reconciliation" || status === "blocked";
}

const MILESTONE_STATUS: Record<MilestoneStatus, StatusInfo> = {
  planned: { label: "Planned", tone: "neutral", symbol: "○" },
  in_progress: { label: "In progress", tone: "active", symbol: "▶" },
  completed: { label: "Completed", tone: "ok", symbol: "✓" },
  failed: { label: "Failed", tone: "bad", symbol: "✕" },
  blocked: { label: "Blocked", tone: "attention", symbol: "!" },
};

export function milestoneStatus(status: MilestoneStatus): StatusInfo {
  return MILESTONE_STATUS[status] ?? { label: status, tone: "neutral", symbol: "•" };
}

export const ATTENTION_LABEL: Record<AttentionKind, string> = {
  decision_required: "Approval required",
  reconciliation_required: "Reconciliation required",
};

const EXECUTION_STATE: Record<ExecutionState, string> = {
  authorized: "Authorized, not sent yet",
  dispatching: "Being sent",
  succeeded: "Succeeded",
  failed: "Did not take effect",
  uncertain: "Outcome unknown",
  lapsed: "Never sent (the authorization ended)",
};

export function executionState(state: ExecutionState): string {
  return EXECUTION_STATE[state] ?? state;
}

export const CHOICE_LABEL: Record<ReconciliationChoice, string> = {
  it_happened: "It happened",
  did_not_happen: "It didn't happen — don't retry",
  did_not_happen_retry: "It didn't happen — authorize one retry",
};

function who(entry: TimelineEntry): string {
  return entry.actor?.display_name || entry.actor?.id || "someone";
}

function attempt(entry: TimelineEntry): string {
  return entry.attempt ? ` (attempt ${entry.attempt})` : "";
}

/** One line for a timeline entry, built only from its structured fields. */
export function timelineText(entry: TimelineEntry): string {
  switch (entry.kind) {
    case "approval_requested":
      return "Approval requested";
    case "approved":
      return `Approved by ${who(entry)}`;
    case "rejected":
      return `Rejected by ${who(entry)}`;
    case "expired":
      return "Expired before anyone decided";
    case "action_dispatching":
      return `Action sent${attempt(entry)}`;
    case "retry_dispatched":
      return `Retry sent${attempt(entry)}`;
    case "action_succeeded":
      return `Action succeeded${attempt(entry)}`;
    case "action_failed":
      return `Action did not take effect${attempt(entry)}`;
    case "action_not_dispatched":
      return `Action was not sent${attempt(entry)}`;
    case "outcome_uncertain":
      return `Outcome unknown${attempt(entry)}`;
    case "reconciliation_required":
      return `A person must confirm whether it happened${attempt(entry)}`;
    case "reconciliation_decided": {
      const choice = entry.outcome as ReconciliationChoice | null;
      return `${who(entry)} answered: ${choice && CHOICE_LABEL[choice] ? CHOICE_LABEL[choice] : "an answer"}`;
    }
    case "retry_authorized":
      return `${who(entry)} authorized one retry`;
    case "resolved":
      return `Resolved: ${entry.outcome ? executionState(entry.outcome as ExecutionState).toLowerCase() : "finished"}`;
    default:
      return entry.kind;
  }
}

const NOTIFICATION_FAILURE: Record<NotificationFailure, string> = {
  timeout: "no answer from the provider",
  provider_unavailable: "the provider was unavailable",
  rate_limited: "too many notifications at once",
  outcome_unknown: "a try ended without an answer",
  invalid_destination: "the address or number was refused",
  provider_rejected_configuration: "the notification service is not set up correctly",
  unsupported_destination: "this kind of destination cannot be reached yet",
  max_attempts: "every try failed",
  attention_resolved: "already handled",
  destination_disabled: "the destination was turned off",
  recipient_not_permitted: "the person may no longer act on it",
};

/** What one notification status says, in plain words. Informational only: the approval itself is the authority. */
export function notificationText(status: NotificationStatus): { about: string; text: string; tone: Tone } {
  const about =
    status.about === "decision_required"
      ? "The decision"
      : `Whether attempt ${status.attempt ?? "?"} happened`;
  const why = status.failure ? NOTIFICATION_FAILURE[status.failure] ?? status.failure : null;
  const people = status.deliveries === 1 ? "" : ` (${status.sent} of ${status.deliveries} destinations)`;
  switch (status.state) {
    case "queued":
      return { about, text: "Notification queued", tone: "neutral" };
    case "sending":
      return { about, text: "Notification being sent", tone: "active" };
    case "retrying":
      return { about, text: `Notification retrying${why ? ` — ${why}` : ""}`, tone: "attention" };
    case "sent":
      return {
        about,
        text: `Notification sent${people}${status.outcome_unknown ? " — possibly more than once" : ""}`,
        tone: "ok",
      };
    case "failed":
      return {
        about,
        text: `Notification could not be delivered${why ? ` — ${why}` : ""}. This request still waits here.`,
        tone: "bad",
      };
    case "skipped":
      return { about, text: `No notification sent${why ? ` — ${why}` : ""}`, tone: "neutral" };
    default:
      return { about, text: status.state, tone: "neutral" };
  }
}

const tokens = (n: number) => (n >= 10_000 ? `${Math.round(n / 1000)}k` : n.toLocaleString("en-US"));

/** A run's model usage in one line: which model, how many calls and tokens, and a cost only when it is configured. */
export function modelUsageText(usage: ModelUsage): string {
  const models = usage.by_model.map((m) => `${m.provider} / ${m.model}`).join(", ") || "a model";
  const calls = `${usage.calls} ${usage.calls === 1 ? "call" : "calls"}`;
  const failed = usage.failed_calls ? ` (${usage.failed_calls} failed)` : "";
  const cost =
    usage.estimated_cost_usd === null ? "cost not estimated" : `about $${usage.estimated_cost_usd.toFixed(2)} (estimate)`;
  return `Model: ${models} · ${calls}${failed} · ${tokens(usage.input_tokens)} in / ${tokens(usage.output_tokens)} out tokens · ${cost}`;
}

/** Tone of a timeline entry: what a glance should notice. */
export function timelineTone(entry: TimelineEntry): Tone {
  switch (entry.kind) {
    case "approved":
    case "action_succeeded":
      return "ok";
    case "rejected":
    case "expired":
    case "action_failed":
      return "bad";
    case "outcome_uncertain":
    case "reconciliation_required":
    case "approval_requested":
      return "attention";
    case "action_dispatching":
    case "retry_dispatched":
    case "retry_authorized":
      return "active";
    default:
      return "neutral";
  }
}

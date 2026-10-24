// The small system-status indicator: is Agent Factory reachable, and can it serve requests?
// Built from GET /readyz (no credential), which never names hosts, versions, or errors, and from the
// shell's signed-in poll, which shows whether the API can check credentials. A refused or expired
// credential is not shown here: the dashboard returns to sign-in and says why.
import { isApiError, isUnreachable } from "../api/errors";
import type { Readiness } from "../api/types";
import type { PollState } from "../lib/polling";

export type Health =
  | "checking"
  | "operational"
  | "unreachable"
  | "database_unavailable"
  | "not_ready"
  | "authentication_unavailable";

export function health(readiness: PollState<Readiness>, signedIn?: PollState<unknown>): Health {
  if (readiness.error) return isUnreachable(readiness.error) ? "unreachable" : "not_ready";
  const data = readiness.data;
  if (!data) return "checking";
  if (data.status !== "ready") return data.reason === "database_unavailable" ? "database_unavailable" : "not_ready";
  // Ready, yet signed-in requests fail: the API cannot check credentials right now (its key source is down).
  if (isApiError(signedIn?.error) && signedIn.error.code === "authentication_unavailable") return "authentication_unavailable";
  return "operational";
}

const TEXT: Record<Health, { label: string; symbol: string; tone: string }> = {
  checking: { label: "Checking Agent Factory…", symbol: "…", tone: "neutral" },
  operational: { label: "Agent Factory is running", symbol: "✓", tone: "ok" },
  unreachable: { label: "API unavailable", symbol: "✕", tone: "bad" },
  database_unavailable: { label: "Database unavailable", symbol: "!", tone: "bad" },
  not_ready: { label: "API not ready", symbol: "!", tone: "attention" },
  authentication_unavailable: { label: "Sign-in checks unavailable", symbol: "!", tone: "bad" },
};

export function SystemStatus({ readiness, signedIn }: { readiness: PollState<Readiness>; signedIn?: PollState<unknown> }) {
  const state = health(readiness, signedIn);
  const text = TEXT[state];
  return (
    <p className={`system-status tone-${text.tone}`} role="status" aria-live="polite" data-health={state}>
      <span aria-hidden="true">{text.symbol}</span> {text.label}
    </p>
  );
}

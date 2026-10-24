import { describe, expect, it } from "vitest";
import type { NotificationStatus, TimelineEntry, TimelineKind } from "../api/types";
import { formatDuration, formatRelative, parameterText, shortId } from "./format";
import { ATTENTION_LABEL, isActive, milestoneStatus, modelUsageText, notificationText, runStatus, timelineText } from "./labels";
import { parseRoute, paths } from "./router";
import { ApiError } from "../api/errors";
import type { Readiness } from "../api/types";
import { health } from "../components/SystemStatus";
import type { PollState } from "./polling";

describe("format", () => {
  it("formats durations compactly", () => {
    expect(formatDuration(5_000)).toBe("5 s");
    expect(formatDuration(12 * 60_000)).toBe("12 min");
    expect(formatDuration(3 * 3_600_000 + 5 * 60_000)).toBe("3 h 5 min");
    expect(formatDuration(30 * 3_600_000)).toBe("30 h");
    expect(formatDuration(5 * 24 * 3_600_000)).toBe("5 days");
  });

  it("formats relative times in both directions", () => {
    const now = Date.UTC(2026, 9, 5, 12);
    expect(formatRelative(now - 10_000, now)).toBe("just now");
    expect(formatRelative(now - 5 * 60_000, now)).toBe("5 min ago");
    expect(formatRelative(now + 2 * 3_600_000, now)).toBe("in 2 h");
    expect(formatRelative(now + 10_000, now)).toBe("in under a minute");
  });

  it("shortens long identifiers and keeps short ones", () => {
    expect(shortId("wkr-5e5e5e5e5e5e5e5e5e5e")).toBe("wkr-5e5e5e…");
    expect(shortId("run-1")).toBe("run-1");
  });

  it("renders parameter values as plain text", () => {
    expect(parameterText("legacy_calc.py")).toBe("legacy_calc.py");
    expect(parameterText({ a: [1, 2] })).toBe('{"a":[1,2]}');
    expect(parameterText(42)).toBe("42");
  });
});

describe("router", () => {
  it("parses the five screens and refuses malformed IDs", () => {
    expect(parseRoute("/")).toEqual({ name: "dashboard" });
    expect(parseRoute("/attention")).toEqual({ name: "attention" });
    expect(parseRoute("/projects/prj-0123456789abcdef0123")).toEqual({ name: "project", projectId: "prj-0123456789abcdef0123" });
    expect(parseRoute("/projects/prj-0123456789abcdef0123/approvals/apr-0123456789abcdef")).toEqual({
      name: "approval",
      projectId: "prj-0123456789abcdef0123",
      approvalId: "apr-0123456789abcdef",
    });
    for (const bad of ["/projects/../etc", "/projects/prj-1", "/projects/prj-0123456789abcdef0123/approvals/x", "/admin"]) {
      expect(parseRoute(bad)).toEqual({ name: "not_found" });
    }
  });

  it("builds paths that parse back", () => {
    const route = parseRoute(paths.approval("prj-0123456789abcdef0123", "apr-0123456789abcdef"));
    expect(route.name).toBe("approval");
  });
});

describe("labels", () => {
  it("names every run status in words, with a symbol (never color alone)", () => {
    for (const status of ["running", "awaiting_approval", "awaiting_reconciliation", "blocked", "completed", "failed", "exhausted", "not_started"] as const) {
      const info = runStatus(status);
      expect(info.label.length).toBeGreaterThan(3);
      expect(info.symbol.length).toBeGreaterThan(0);
    }
    expect(isActive("awaiting_approval")).toBe(true);
    expect(isActive("completed")).toBe(false);
    expect(milestoneStatus("blocked").label).toBe("Blocked");
  });

  it("distinguishes the two kinds of attention", () => {
    expect(ATTENTION_LABEL.decision_required).toBe("Approval required");
    expect(ATTENTION_LABEL.reconciliation_required).toBe("Reconciliation required");
  });

  it("describes every timeline kind from structured fields", () => {
    const kinds: TimelineKind[] = [
      "approval_requested", "approved", "rejected", "expired", "action_dispatching", "retry_dispatched",
      "action_succeeded", "action_failed", "action_not_dispatched", "outcome_uncertain", "reconciliation_required",
      "reconciliation_decided", "retry_authorized", "resolved",
    ];
    const actor = { kind: "user" as const, id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true };
    for (const kind of kinds) {
      const entry: TimelineEntry = { kind, at: "2026-10-05T12:00:00.000000Z", attempt: 2, actor, worker: null, outcome: null, until: null };
      expect(timelineText(entry)).not.toBe(kind);
    }
    expect(timelineText({ kind: "approved", at: "x", attempt: null, actor, worker: null, outcome: "approved", until: null })).toBe("Approved by Dana");
    expect(
      timelineText({ kind: "reconciliation_decided", at: "x", attempt: 1, actor, worker: null, outcome: "did_not_happen_retry", until: null }),
    ).toBe("Dana answered: It didn't happen — authorize one retry");
    expect(timelineText({ kind: "resolved", at: "x", attempt: null, actor: null, worker: null, outcome: "succeeded", until: null })).toBe(
      "Resolved: succeeded",
    );
  });
});

describe("system status", () => {
  const poll = <T,>(data: T | undefined, error?: unknown): PollState<T> => ({
    data, dataTick: 1, error, lastSuccessAt: data ? 1 : null, loading: false, failures: error ? 1 : 0,
  });
  const ready = poll<Readiness>({ status: "ready" });

  it("tells API unavailable, not ready, database unavailable, and authentication unavailable apart", () => {
    expect(health(poll<Readiness>(undefined, new ApiError({ kind: "gateway", status: 500, message: "" })))).toBe("unreachable");
    expect(health(poll<Readiness>(undefined, new ApiError({ kind: "network", message: "" })))).toBe("unreachable");
    expect(health(poll<Readiness>({ status: "not_ready", reason: "database_unavailable" }))).toBe("database_unavailable");
    expect(health(poll<Readiness>({ status: "not_ready", reason: "schema_not_current" }))).toBe("not_ready");
    const keys = new ApiError({ kind: "problem", status: 503, code: "authentication_unavailable", message: "" });
    expect(health(ready, poll(undefined, keys))).toBe("authentication_unavailable");
    expect(health(ready, poll({ items: [] }))).toBe("operational");
    expect(health(poll<Readiness>(undefined))).toBe("checking");
  });
});

describe("notification status", () => {
  const base: NotificationStatus = {
    about: "decision_required", attempt: null, state: "sent", deliveries: 1, sent: 1, failed: 0,
    created_at: "2026-10-05T01:00:00Z", last_attempt_at: "2026-10-05T01:00:01Z", sent_at: "2026-10-05T01:00:01Z",
    outcome_unknown: false, failure: null,
  };

  it("says plainly what happened, and that a failed alert leaves the request waiting", () => {
    expect(notificationText(base)).toEqual({ about: "The decision", text: "Notification sent", tone: "ok" });
    expect(notificationText({ ...base, outcome_unknown: true }).text).toBe("Notification sent — possibly more than once");
    expect(notificationText({ ...base, state: "retrying", sent: 0, sent_at: null, failure: "timeout" }).text).toBe(
      "Notification retrying — no answer from the provider",
    );
    expect(notificationText({ ...base, state: "failed", sent: 0, failed: 1, sent_at: null, failure: "invalid_destination" }).text).toBe(
      "Notification could not be delivered — the address or number was refused. This request still waits here.",
    );
    expect(notificationText({ ...base, state: "skipped", sent: 0, sent_at: null, failure: "attention_resolved" }).text).toBe(
      "No notification sent — already handled",
    );
    expect(notificationText({ ...base, about: "reconciliation_required", attempt: 2 }).about).toBe("Whether attempt 2 happened");
  });
});
describe("model usage", () => {
  const usage = {
    calls: 6, failed_calls: 1, input_tokens: 18_420, output_tokens: 3_110, cached_tokens: 2_048, estimated_cost_usd: null,
    by_model: [{ provider: "anthropic", model: "example-model", calls: 6, input_tokens: 18_420, output_tokens: 3_110, cached_tokens: 2_048 }],
  };

  it("names the model and counts, and claims a cost only when one is configured", () => {
    expect(modelUsageText(usage)).toBe(
      "Model: anthropic / example-model · 6 calls (1 failed) · 18k in / 3,110 out tokens · cost not estimated",
    );
    expect(modelUsageText({ ...usage, estimated_cost_usd: 0.0412 })).toContain("about $0.04 (estimate)");
  });
});
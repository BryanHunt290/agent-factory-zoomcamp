// A scripted stand-in for the API, for unit and component tests only. The real ApiClient runs on top of
// it, so these tests also cover how the client turns responses into data and errors. Browser and E2E
// tests (tests/test_dashboard_*.py) use the real API instead.
import type { ApprovalRequest, AttentionItem, Problem, ProjectSummary } from "../api/types";

export type Handler = (request: { method: string; url: URL; body: unknown; headers: Headers }) => Response | Promise<Response>;

export interface Recorded {
  method: string;
  url: URL;
  body: unknown;
  headers: Headers;
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "X-Request-ID": "req_test0001", ...headers },
  });
}

export function problem(status: number, code: Problem["code"], extra: Partial<Problem> = {}, headers: Record<string, string> = {}): Response {
  const body: Problem = {
    type: `urn:agent-factory:problem:${code}`,
    title: code,
    status,
    code,
    request_id: "req_problem01",
    ...extra,
  };
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/problem+json", ...headers } });
}

/** The frontend server's proxy, when the API behind it is down: a plain 500 that is not the API's. */
export function proxyDown(): Response {
  return new Response("", { status: 500, headers: { "content-type": "text/plain" } });
}

export class FakeApi {
  readonly requests: Recorded[] = [];
  private routes: { method: string; pattern: RegExp; handler: Handler }[] = [];

  on(method: string, pattern: RegExp, handler: Handler): this {
    this.routes.unshift({ method, pattern, handler }); // later routes override earlier ones
    return this;
  }

  readonly fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(String(input), "http://127.0.0.1:4173");
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers);
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    this.requests.push({ method, url, body, headers });
    const signal = init.signal;
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    const route = this.routes.find((r) => r.method === method && r.pattern.test(url.pathname));
    if (!route) return problem(404, "route_not_found");
    const answer = Promise.resolve(route.handler({ method, url, body, headers }));
    if (!signal) return answer;
    // Like the real fetch: an abort (the client's timeout, or a view closing) rejects at once.
    return Promise.race([
      answer,
      new Promise<Response>((_, reject) =>
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }),
      ),
    ]);
  };

  count(method: string, pattern: RegExp): number {
    return this.requests.filter((r) => r.method === method && pattern.test(r.url.pathname)).length;
  }
}

const NOW = Date.now();
export const iso = (offsetMs: number) => new Date(NOW + offsetMs).toISOString();

export const PROJECT = "prj-0123456789abcdef0123";
export const APPROVAL = "apr-0123456789abcdef";
export const FINGERPRINT = `sha256:${"ab".repeat(32)}`;

export function approvalFixture(overrides: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    id: APPROVAL,
    project_id: PROJECT,
    project_name: "Calculator",
    run_id: "run-1",
    node: "developer",
    agent: "developer",
    action: { tool: "delete_file", category: "destructive_delete", params: { path: "legacy_calc.py" } },
    action_fingerprint: FINGERPRINT,
    action_summary: "delete_file (destructive delete)",
    reason: "The old module is replaced by calculator.py.",
    risk: "Permanently deletes data; Agent Factory cannot undo it.",
    status: "pending",
    requested_at: iso(-60_000),
    expires_at: iso(12 * 3_600_000),
    decision: null,
    attention: "decision_required",
    execution: null,
    notifications: [],
    timeline: [{ kind: "approval_requested", at: iso(-60_000), attempt: null, actor: null, worker: null, outcome: null, until: iso(12 * 3_600_000) }],
    ...overrides,
  };
}

export function uncertainFixture(overrides: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return approvalFixture({
    status: "approved",
    attention: "reconciliation_required",
    action: { tool: "send_notice", category: "external_communication", params: { to: "billing@example.test" } },
    action_summary: "send_notice (external communication)",
    decision: {
      id: "apd-0123456789abcdef0123",
      outcome: "approved",
      action_fingerprint: FINGERPRINT,
      decided_at: iso(-50_000),
      decided_by: { kind: "user", id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true },
      comment: "",
    },
    execution: { id: "axe-0123456789abcdef0123", state: "uncertain", attempt: 1, retry_authorized_until: null, reconciliation: null },
    ...overrides,
  });
}

export function attentionItem(overrides: Partial<AttentionItem> = {}): AttentionItem {
  return {
    kind: "decision_required",
    project_id: PROJECT,
    project_name: "Calculator",
    run_id: "run-1",
    approval_id: APPROVAL,
    execution_id: null,
    attempt: null,
    tool: "delete_file",
    category: "destructive_delete",
    summary: "Approval needed: delete_file (destructive delete).",
    since: iso(-60_000),
    expires_at: iso(12 * 3_600_000),
    ...overrides,
  };
}

export function projectFixture(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    id: PROJECT,
    name: "Calculator",
    template: "calculator",
    status: "awaiting_approval",
    latest_run: { run_id: "run-1", number: 1, status: "awaiting_approval", updated_at: iso(-60_000) },
    pending_approvals: 1,
    activity: { since: iso(-12 * 3_600_000), milestones: 3 },
    ...overrides,
  };
}

export const TOKEN = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJkYW5hIn0.c2lnbmF0dXJlLWJ5dGVz";

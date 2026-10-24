// The dashboard's only way to reach the API. Components never call fetch() themselves.
//
//   UI components -> this client -> (same-origin proxy) -> FastAPI -> PostgreSQL
//
// * Every product request carries the session's bearer token in the Authorization header, and only
//   there: never in a URL, a query string, a log line, or an error message.
// * Every request has a timeout. A request that times out, cannot connect, or is answered by the
//   frontend server's proxy instead of the API becomes an ApiError of kind network/timeout/gateway.
// * Decisions are sent once per call, and the client never retries them by itself: a caller that does
//   not know whether a decision was recorded must ask the API (getApproval), and may then resend the
//   identical decision, which the API treats as a safe repeat (200).
import { ApiError } from "./errors";
import type {
  ApprovalRequest,
  AttentionPage,
  Body,
  DecisionRequest,
  Json,
  MilestonePage,
  Problem,
  Project,
  ProjectSummaryPage,
  Query,
  Readiness,
  ReconciliationRequest,
  Run,
  RunPage,
} from "./types";

export const DEFAULT_TIMEOUT_MS = 10_000;

export interface ClientOptions {
  /** The session's token, read on every request (so signing out takes effect at once). */
  getToken: () => string | null;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  /** Prefix for every path; "" means this origin (the frontend server proxies to the API). */
  baseUrl?: string;
  /** Called when the API answers 401: the token expired or was refused, and the session is over. */
  onUnauthorized?: () => void;
}

export interface CallOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

export interface DecisionResult {
  approval: ApprovalRequest;
  /** True when this call recorded it (201); false for an identical repeat that was already recorded (200). */
  recordedNow: boolean;
  requestId: string | null;
}

type QueryValue = string | number | boolean | readonly (string | number)[] | null | undefined;

function queryString(query: Record<string, QueryValue> | undefined): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) params.append(name, String(item));
    } else {
      params.append(name, String(value));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

function retryAfter(response: Response): number | null {
  const value = response.headers.get("Retry-After");
  if (!value) return null;
  const seconds = Number.parseInt(value, 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

function isProblem(value: unknown): value is Problem {
  return typeof value === "object" && value !== null && typeof (value as { code?: unknown }).code === "string";
}

export class ApiClient {
  private readonly getToken: () => string | null;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;
  private readonly baseUrl: string;
  private readonly onUnauthorized: (() => void) | undefined;

  constructor(options: ClientOptions) {
    this.getToken = options.getToken;
    this.fetchImpl = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.baseUrl = options.baseUrl ?? "";
    this.onUnauthorized = options.onUnauthorized;
  }

  // ------------------------------------------------------------------ product operations

  listProjects(query: Query<"listProjects"> = {}, options?: CallOptions): Promise<ProjectSummaryPage> {
    return this.get<Json<"listProjects", 200>>("/api/v1/projects", query, options);
  }

  getProject(projectId: string, query: Query<"getProject"> = {}, options?: CallOptions): Promise<Project> {
    return this.get<Json<"getProject", 200>>(`/api/v1/projects/${segment(projectId)}`, query, options);
  }

  listRuns(projectId: string, query: Query<"listRuns"> = {}, options?: CallOptions): Promise<RunPage> {
    return this.get<Json<"listRuns", 200>>(`/api/v1/projects/${segment(projectId)}/runs`, query, options);
  }

  getRun(projectId: string, runId: string, options?: CallOptions): Promise<Run> {
    return this.get<Json<"getRun", 200>>(`/api/v1/projects/${segment(projectId)}/runs/${segment(runId)}`, {}, options);
  }

  listProjectMilestones(
    projectId: string,
    query: Query<"listProjectMilestones"> = {},
    options?: CallOptions,
  ): Promise<MilestonePage> {
    return this.get<Json<"listProjectMilestones", 200>>(
      `/api/v1/projects/${segment(projectId)}/milestones`,
      query,
      options,
    );
  }

  listAttention(query: Query<"listAttention"> = {}, options?: CallOptions): Promise<AttentionPage> {
    return this.get<Json<"listAttention", 200>>("/api/v1/attention", query, options);
  }

  getApproval(projectId: string, approvalId: string, options?: CallOptions): Promise<ApprovalRequest> {
    return this.get<Json<"getApproval", 200>>(
      `/api/v1/projects/${segment(projectId)}/approvals/${segment(approvalId)}`,
      {},
      options,
    );
  }

  /** Record one decision. Sent once; see the module notes for what to do when the outcome is unknown. */
  async decideApproval(
    projectId: string,
    approvalId: string,
    body: DecisionRequest & Body<"decideApproval">,
    options?: CallOptions,
  ): Promise<DecisionResult> {
    const path = `/api/v1/projects/${segment(projectId)}/approvals/${segment(approvalId)}/decision`;
    const { data, status, requestId } = await this.send<Json<"decideApproval", 201>>("POST", path, body, options);
    return { approval: data, recordedNow: status === 201, requestId };
  }

  /** Record one reconciliation answer. Sent once, like a decision. */
  async reconcileAction(
    projectId: string,
    approvalId: string,
    body: ReconciliationRequest & Body<"reconcileAction">,
    options?: CallOptions,
  ): Promise<DecisionResult> {
    const path = `/api/v1/projects/${segment(projectId)}/approvals/${segment(approvalId)}/reconciliation`;
    const { data, status, requestId } = await this.send<Json<"reconcileAction", 201>>("POST", path, body, options);
    return { approval: data, recordedNow: status === 201, requestId };
  }

  /** Ask the API whether `candidate` is accepted (sign-in), without using or ending the session. */
  async checkToken(candidate: string, options?: CallOptions): Promise<void> {
    await this.send<Json<"listProjects", 200>>("GET", "/api/v1/projects?limit=1", undefined, options, candidate);
  }

  // ------------------------------------------------------------------ infrastructure

  /** GET /readyz: no credential needed. A 503 still answers (not ready, and why). */
  async readiness(options?: CallOptions): Promise<Readiness> {
    const response = await this.fetchWithTimeout("/readyz", { method: "GET", headers: { Accept: "application/json" } }, options);
    const type = response.headers.get("content-type") ?? "";
    if ((response.ok || response.status === 503) && type.includes("application/json")) {
      const body = (await response.json()) as Readiness;
      if (body && (body.status === "ready" || body.status === "not_ready")) return body;
    }
    throw new ApiError({ kind: "gateway", status: response.status, message: "the API did not answer its readiness check" });
  }

  // ------------------------------------------------------------------ plumbing

  private get<T>(path: string, query: object, options?: CallOptions): Promise<T> {
    return this.send<T>("GET", path + queryString(query as Record<string, QueryValue>), undefined, options).then((r) => r.data);
  }

  private async send<T>(
    method: "GET" | "POST",
    path: string,
    body: unknown,
    options?: CallOptions,
    candidate?: string,
  ): Promise<{ data: T; status: number; requestId: string | null }> {
    const token = candidate ?? this.getToken();
    if (!token) {
      throw new ApiError({ kind: "signed_out", status: 401, code: "authentication_required", message: "not signed in" });
    }
    const headers: Record<string, string> = { Accept: "application/json", Authorization: `Bearer ${token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await this.fetchWithTimeout(
      path,
      { method, headers, body: body === undefined ? undefined : JSON.stringify(body) },
      options,
    );
    const requestId = response.headers.get("X-Request-ID");
    const type = response.headers.get("content-type") ?? "";
    if (response.ok) {
      if (!type.includes("application/json")) {
        throw new ApiError({ kind: "unexpected", status: response.status, requestId, message: "unexpected response type" });
      }
      return { data: (await response.json()) as T, status: response.status, requestId };
    }
    if (type.includes("application/problem+json")) {
      const problem: unknown = await response.json().catch(() => null);
      if (isProblem(problem)) {
        if (response.status === 401 && candidate === undefined) this.onUnauthorized?.();
        throw new ApiError({
          kind: "problem",
          status: response.status,
          code: problem.code,
          requestId: problem.request_id || requestId,
          problem,
          retryAfterSeconds: retryAfter(response),
          message: problem.title || `request failed with ${response.status}`,
        });
      }
    }
    if (response.status >= 500) {
      // Not the API's own answer: the frontend server's proxy could not reach it.
      throw new ApiError({ kind: "gateway", status: response.status, requestId, message: "Agent Factory is unreachable" });
    }
    throw new ApiError({ kind: "unexpected", status: response.status, requestId, message: `unexpected ${response.status}` });
  }

  private async fetchWithTimeout(path: string, init: RequestInit, options?: CallOptions): Promise<Response> {
    const controller = new AbortController();
    const outer = options?.signal;
    if (outer?.aborted) throw outer.reason ?? new DOMException("aborted", "AbortError");
    const onAbort = () => controller.abort(outer?.reason);
    outer?.addEventListener("abort", onAbort, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, options?.timeoutMs ?? this.timeoutMs);
    try {
      return await this.fetchImpl(this.baseUrl + path, {
        ...init,
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
    } catch (error) {
      if (outer?.aborted) throw error; // the caller gave up (e.g. the view closed): not a failure to report
      if (timedOut) throw new ApiError({ kind: "timeout", message: "Agent Factory did not answer in time" });
      throw new ApiError({ kind: "network", message: "Agent Factory is unreachable" });
    } finally {
      clearTimeout(timer);
      outer?.removeEventListener("abort", onAbort);
    }
  }
}

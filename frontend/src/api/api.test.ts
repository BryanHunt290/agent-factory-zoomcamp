import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, readSignOutReason, readToken, saveToken, TOKEN_STORAGE_KEY, TokenFormatError } from "../auth/session";
import { FakeApi, FINGERPRINT, json, problem, proxyDown, TOKEN, approvalFixture } from "../test/fakeApi";
import { ApiClient } from "./client";
import { ApiError, isConflict, isSignedOut, isUnreachable, mayHaveBeenRecorded, userMessage } from "./errors";

function client(api: FakeApi, options: Partial<ConstructorParameters<typeof ApiClient>[0]> = {}) {
  return new ApiClient({ getToken: () => TOKEN, fetchImpl: api.fetch, timeoutMs: 200, ...options });
}

describe("ApiClient", () => {
  it("sends the token in the Authorization header only, never in the URL", async () => {
    const api = new FakeApi().on("GET", /^\/api\/v1\/projects$/, () => json({ items: [], next_cursor: null }));
    await client(api).listProjects({ limit: 5, activity_since: "2026-10-05T00:00:00.000Z" });
    const [request] = api.requests;
    expect(request!.headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
    expect(request!.url.toString()).not.toContain(TOKEN);
    expect(request!.url.search).toBe("?limit=5&activity_since=2026-10-05T00%3A00%3A00.000Z");
  });

  it("encodes repeated query parameters and path segments", async () => {
    const api = new FakeApi().on("GET", /milestones$/, () => json({ items: [], next_cursor: null }));
    await client(api).listProjectMilestones("prj-0123456789abcdef0123", { status: ["failed", "blocked"] });
    expect(api.requests[0]!.url.search).toBe("?status=failed&status=blocked");
  });

  it("turns a problem document into an ApiError with its code and request ID", async () => {
    const api = new FakeApi().on("GET", /approvals/, () => problem(403, "forbidden", { required_permission: "approvals:read" }));
    const error = await client(api).getApproval("prj-0123456789abcdef0123", "apr-0123456789abcdef").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect([error.status, error.code, error.requestId, error.kind]).toEqual([403, "forbidden", "req_problem01", "problem"]);
  });

  it("tells an unreachable API apart from the API's own answers", async () => {
    const down = new FakeApi().on("GET", /projects/, () => proxyDown());
    const gateway = await client(down).listProjects().catch((e) => e);
    expect(gateway.kind).toBe("gateway");
    expect(isUnreachable(gateway)).toBe(true);

    const refused = new FakeApi().on("GET", /projects/, () => {
      throw new TypeError("Failed to fetch");
    });
    const network = await client(refused).listProjects().catch((e) => e);
    expect(network.kind).toBe("network");

    const hanging = new FakeApi().on("GET", /projects/, () => new Promise<Response>(() => undefined));
    const timeout = await client(hanging, { timeoutMs: 30 }).listProjects().catch((e) => e);
    expect(timeout.kind).toBe("timeout");
  });

  it("treats a failed write as possibly recorded unless the API refused it with a 4xx", () => {
    const answered = (status: number) => new ApiError({ kind: "problem", status, message: "" });
    for (const status of [400, 401, 403, 404, 409, 422, 429]) expect(mayHaveBeenRecorded(answered(status))).toBe(false);
    for (const status of [500, 503]) expect(mayHaveBeenRecorded(answered(status))).toBe(true);
    for (const kind of ["network", "timeout", "gateway", "unexpected"] as const) {
      expect(mayHaveBeenRecorded(new ApiError({ kind, message: "" }))).toBe(true);
    }
    expect(mayHaveBeenRecorded(new ApiError({ kind: "signed_out", status: 401, message: "" }))).toBe(false); // never sent
    expect(mayHaveBeenRecorded(new TypeError("Failed to fetch"))).toBe(true);
  });

  it("reports 201 and 200 apart for decisions, and never retries one by itself", async () => {
    let calls = 0;
    const api = new FakeApi().on("POST", /decision$/, () => {
      calls += 1;
      return calls === 1 ? json(approvalFixture(), 201) : json(approvalFixture(), 200);
    });
    const c = client(api);
    const body = { outcome: "approved" as const, action_fingerprint: FINGERPRINT, comment: "" };
    expect((await c.decideApproval("prj-0123456789abcdef0123", "apr-0123456789abcdef", body)).recordedNow).toBe(true);
    expect((await c.decideApproval("prj-0123456789abcdef0123", "apr-0123456789abcdef", body)).recordedNow).toBe(false);

    const failing = new FakeApi().on("POST", /decision$/, () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(client(failing).decideApproval("prj-0123456789abcdef0123", "apr-0123456789abcdef", body)).rejects.toThrow(ApiError);
    expect(failing.count("POST", /decision$/)).toBe(1);
  });

  it("ends the session on a 401, and does not call the API without a token", async () => {
    const onUnauthorized = vi.fn();
    const api = new FakeApi().on("GET", /attention/, () => problem(401, "invalid_token"));
    await client(api, { onUnauthorized }).listAttention().catch(() => undefined);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);

    const silent = new FakeApi();
    const error = await new ApiClient({ getToken: () => null, fetchImpl: silent.fetch }).listAttention().catch((e) => e);
    expect(isSignedOut(error)).toBe(true);
    expect(silent.requests).toHaveLength(0);
  });

  it("reads readiness without a credential, including a 503 with its reason", async () => {
    const api = new FakeApi().on("GET", /^\/readyz$/, () => json({ status: "not_ready", reason: "database_unavailable" }, 503));
    const c = new ApiClient({ getToken: () => null, fetchImpl: api.fetch });
    expect(await c.readiness()).toEqual({ status: "not_ready", reason: "database_unavailable" });
    expect(api.requests[0]!.headers.get("Authorization")).toBeNull();
  });
});

describe("error messages", () => {
  const make = (status: number, code: Parameters<typeof problem>[1], extra = {}) =>
    new ApiError({ kind: "problem", status, code, requestId: "req_x", message: "x", problem: { type: "t", title: "t", status, code, request_id: "req_x", ...extra } });

  it("maps every class of failure to plain words, keeping the request ID", () => {
    expect(userMessage(make(401, "invalid_token")).title).toBe("Your session is no longer valid.");
    expect(userMessage(make(403, "forbidden", { required_permission: "approvals:decide" })).detail).toContain("approvals:decide");
    expect(userMessage(make(409, "approval_already_decided")).title).toContain("already decided");
    expect(userMessage(make(409, "approval_expired")).title).toContain("expired");
    expect(userMessage(make(409, "reconciliation_stale")).title).toContain("newer attempt");
    expect(userMessage(make(429, "too_many_requests")).title).toBe("Too many attempts. Try again shortly.");
    expect(userMessage(make(503, "storage_unavailable")).title).toBe("Agent Factory is temporarily unavailable.");
    expect(userMessage(new ApiError({ kind: "network", message: "x" })).title).toBe("Agent Factory is unreachable right now.");
    expect(userMessage(make(409, "approval_cancelled")).requestId).toBe("req_x");
    expect(isConflict(make(409, "approval_expired"))).toBe(true);
  });

  it("never shows an exception's text or stack", () => {
    const message = userMessage(new Error("TypeError at secret.js:12 token=abc"));
    expect(JSON.stringify(message)).not.toContain("secret");
  });
});

describe("LOCAL DEVELOPMENT token session", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
  });
  afterEach(() => clearToken());

  it("keeps the token in sessionStorage only, and it survives a re-read (a refresh)", () => {
    saveToken(`  ${TOKEN}\n`);
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe(TOKEN);
    expect(localStorage.length).toBe(0);
    expect(readToken()).toBe(TOKEN);
  });

  it("refuses something that is not a token, without echoing it", () => {
    const secretish = "not a token but a secret-canary-981";
    expect(() => saveToken(secretish)).toThrow(TokenFormatError);
    try {
      saveToken(secretish);
    } catch (error) {
      expect(String(error)).not.toContain("secret-canary");
    }
    expect(readToken()).toBeNull();
  });

  it("clears the token and remembers why, in memory only", () => {
    saveToken(TOKEN);
    clearToken("Your session is no longer valid.");
    expect(readToken()).toBeNull();
    expect(readSignOutReason()).toBe("Your session is no longer valid.");
    expect(JSON.stringify({ ...sessionStorage })).not.toContain("session is no longer");
  });
});

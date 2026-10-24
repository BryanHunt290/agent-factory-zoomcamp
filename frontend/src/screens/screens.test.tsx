// Component tests: the whole app over a scripted API (FakeApi), as a person would use it.
// What they prove is the UI's behaviour; real reliability evidence comes from the browser and E2E
// suites against the real API (tests/test_dashboard_*.py).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";
import type { ApprovalRequest } from "../api/types";
import { clearToken, readToken, TOKEN_STORAGE_KEY } from "../auth/session";
import {
  APPROVAL,
  approvalFixture,
  attentionItem,
  FakeApi,
  FINGERPRINT,
  json,
  PROJECT,
  problem,
  projectFixture,
  TOKEN,
  uncertainFixture,
} from "../test/fakeApi";

const APPROVAL_PATH = `/projects/${PROJECT}/approvals/${APPROVAL}`;

function baseApi(): FakeApi {
  return new FakeApi()
    .on("GET", /^\/readyz$/, () => json({ status: "ready" }))
    .on("GET", /^\/api\/v1\/attention$/, () => json({ items: [attentionItem()], next_cursor: null }))
    .on("GET", /^\/api\/v1\/projects$/, () => json({ items: [projectFixture()], next_cursor: null }))
    .on("GET", /^\/api\/v1\/projects\/prj-[0-9a-f]+\/runs\/run-1$/, () =>
      json({
        project_id: PROJECT, run_id: "run-1", number: 1, workflow: "calculator-v1", status: "awaiting_approval",
        status_reason: null, pending_approval_id: APPROVAL, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        model_usage: { calls: 0, failed_calls: 0, input_tokens: 0, output_tokens: 0, cached_tokens: 0, estimated_cost_usd: null, by_model: [] },
      }),
    )
    .on("GET", /^\/api\/v1\/projects\/prj-[0-9a-f]+\/milestones$/, () => json({ items: [], next_cursor: null }));
}

function renderApp(api: FakeApi, path = "/") {
  sessionStorage.setItem(TOKEN_STORAGE_KEY, TOKEN);
  window.history.pushState(null, "", path);
  const client = new ApiClient({
    getToken: readToken,
    fetchImpl: api.fetch,
    timeoutMs: 1_000,
    onUnauthorized: () => clearToken("Your session is no longer valid. Sign in again with a new development token."),
  });
  return render(<App client={client} />);
}

/** The confirm button ignores taps for 400 ms after it appears (a double tap on the first button). */
const pastConfirmGuard = () => new Promise((resolve) => setTimeout(resolve, 450));

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  clearToken();
});

describe("the shell", () => {
  it("says LOCAL DEVELOPMENT, shows system status, and puts attention first", async () => {
    const api = baseApi().on("GET", /^\/api\/v1\/attention$/, () =>
      json({
        items: [attentionItem(), attentionItem({ kind: "reconciliation_required", approval_id: "apr-1111111111111111", attempt: 1, execution_id: "axe-0123456789abcdef0123", expires_at: null, summary: "Outcome unknown: send_notice (external communication) was started; say whether it happened." })],
        next_cursor: null,
      }),
    );
    renderApp(api);
    expect(screen.getAllByText("LOCAL DEVELOPMENT").length).toBeGreaterThan(0);
    await screen.findByText("Agent Factory is running");
    const attention = await screen.findByRole("region", { name: /Needs your attention/ });
    await within(attention).findByText("Approval required");
    expect(within(attention).getByText("Reconciliation required")).toBeTruthy();
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent ?? "");
    expect(headings[0]).toContain("Needs your attention");
  });

  it("returns to sign-in when the API says the session is over", async () => {
    const api = baseApi().on("GET", /^\/api\/v1\/attention$/, () => problem(401, "invalid_token"));
    renderApp(api);
    await screen.findByRole("heading", { name: "Sign in" });
    expect(screen.getByText(/Your session is no longer valid/)).toBeTruthy();
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });
});

describe("approval detail", () => {
  it("shows what, why, risk, project, agent, and expiry before any decision can be sent", async () => {
    renderApp(baseApi().on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(approvalFixture())), APPROVAL_PATH);
    await screen.findByText("delete_file (destructive delete)");
    for (const text of ["Why it is requested", "Risk", "Requested by", "Expires", "Calculator", "legacy_calc.py"]) {
      expect(screen.getAllByText(new RegExp(text)).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("Permanently deletes data; Agent Factory cannot undo it.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Yes, approve/ })).toBeNull(); // nothing sends on the first tap
  });

  it("approves only after a deliberate confirmation, never optimistically, then shows the server's state", async () => {
    let decided: ApprovalRequest | null = null;
    let release: () => void = () => undefined;
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(decided ?? approvalFixture()))
      .on("POST", /decision$/, ({ body }) => {
        return new Promise<Response>((resolve) => {
          release = () => {
            decided = approvalFixture({
              status: "approved",
              attention: null,
              decision: {
                id: "apd-0123456789abcdef0123", outcome: "approved", action_fingerprint: FINGERPRINT,
                decided_at: new Date().toISOString(), comment: (body as { comment: string }).comment,
                decided_by: { kind: "user", id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true },
              },
            });
            resolve(json(decided, 201));
          };
        });
      });
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    expect(await screen.findByRole("heading", { name: "Confirm approval" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Comment (optional)"), { target: { value: "Looks right." } });
    await act(pastConfirmGuard);
    const confirm = screen.getByRole("button", { name: "Yes, approve this action" }) as HTMLButtonElement;
    act(() => {
      confirm.click();
      confirm.click(); // a double tap, both before React re-renders: sends once
    });
    await screen.findByText("Sending your approval to Agent Factory…");
    expect((screen.getByRole("button", { name: "Yes, approve this action" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText(/Approved by/)).toBeNull(); // not before the API confirms
    expect(api.count("POST", /decision$/)).toBe(1);
    const sent = api.requests.find((r) => r.method === "POST")!;
    expect(sent.body).toEqual({ outcome: "approved", action_fingerprint: FINGERPRINT, comment: "Looks right." });
    await act(async () => release());
    await screen.findByText("Agent Factory recorded your approval. A worker continues the run shortly.");
    await screen.findByText(/Approved by/);
    expect(screen.queryByRole("button", { name: "Approve…" })).toBeNull();
  });

  it("ignores a confirming tap that comes right after the confirmation appeared (a double tap)", async () => {
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(approvalFixture()))
      .on("POST", /decision$/, () => json(approvalFixture({ status: "approved", attention: null }), 201));
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, approve this action" })); // within 400 ms
    await act(() => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(api.count("POST", /decision$/)).toBe(0);
    expect(screen.queryByText(/Sending your approval/)).toBeNull();
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, approve this action" })); // a deliberate tap
    await screen.findByText(/recorded your approval/);
    expect(api.count("POST", /decision$/)).toBe(1);
  });

  it("settles a lost answer only from data loaded after the loss, never from what was on screen", async () => {
    let gets = 0;
    let answer: (response: Response) => void = () => undefined;
    const approved = approvalFixture({
      status: "approved",
      attention: null,
      decision: {
        id: "apd-0123456789abcdef0123", outcome: "approved", action_fingerprint: FINGERPRINT, decided_at: new Date().toISOString(),
        comment: "", decided_by: { kind: "user", id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true },
      },
    });
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => {
        gets += 1;
        if (gets === 1) return json(approvalFixture()); // what the person saw: still waiting
        return new Promise<Response>((resolve) => (answer = resolve)); // after the loss: the API is slow to say
      })
      .on("POST", /decision$/, () => {
        throw new TypeError("Failed to fetch");
      });
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, approve this action" }));
    await screen.findByText(/We couldn't confirm whether your approval reached Agent Factory/);
    // The approval on screen says "waiting", but it was loaded before the loss: it proves nothing.
    expect(screen.queryByRole("button", { name: /Send the same/ })).toBeNull();
    expect(screen.queryByText(/did not reach Agent Factory/)).toBeNull();
    // Nor can the person change their mind and send a different decision while the first may be on record.
    for (const name of ["Yes, approve this action", "Cancel"]) {
      expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
    }
    await act(async () => answer(json(approved)));
    await screen.findByText("Your approval reached Agent Factory and is on record.");
    expect(api.count("POST", /decision$/)).toBe(1);
  });

  it("shows whether people were told, and a failed notification changes nothing about the decision", async () => {
    const failed = approvalFixture({
      notifications: [
        {
          about: "decision_required", attempt: null, state: "failed", deliveries: 1, sent: 0, failed: 1,
          created_at: new Date().toISOString(), last_attempt_at: new Date().toISOString(), sent_at: null,
          outcome_unknown: false, failure: "invalid_destination",
        },
      ],
    });
    renderApp(baseApi().on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(failed)), APPROVAL_PATH);
    const list = await screen.findByTestId("notifications");
    expect(list.textContent).toContain("Notification could not be delivered");
    expect(list.textContent).toContain("This request still waits here.");
    expect((screen.getByRole("button", { name: "Approve…" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("says when no notification was sent", async () => {
    renderApp(baseApi().on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(approvalFixture())), APPROVAL_PATH);
    expect(await screen.findByText(/None sent\. This page and the attention list always show what needs you\./)).toBeTruthy();
  });

  it("disables decisions while Agent Factory answers that it is temporarily unavailable", async () => {
    let unavailable = false;
    const api = baseApi().on("GET", /approvals\/apr-[0-9a-f]+$/, () =>
      unavailable ? problem(503, "storage_unavailable", {}, { "Retry-After": "5" }) : json(approvalFixture()),
    );
    renderApp(api, APPROVAL_PATH);
    await screen.findByRole("button", { name: "Approve…" });
    unavailable = true;
    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    const banner = await screen.findByTestId("stale-banner");
    expect(banner.textContent).toMatch(/Agent Factory is temporarily unavailable — showing data from \d/);
    expect((screen.getByRole("button", { name: "Approve…" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("explains a conflict, records nothing, and shows the decision on record instead", async () => {
    let calls = 0;
    const rejected = approvalFixture({
      status: "rejected",
      attention: null,
      decision: {
        id: "apd-0123456789abcdef0123", outcome: "rejected", action_fingerprint: FINGERPRINT, decided_at: new Date().toISOString(),
        comment: "", decided_by: { kind: "user", id: "prn-9999999999999999aaaa", display_name: "Sam", verified: true },
      },
    });
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(calls++ === 0 ? approvalFixture() : rejected))
      .on("POST", /decision$/, () => problem(409, "approval_already_decided", { approval: rejected }));
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, approve this action" }));
    await screen.findByText(/already decided/);
    expect(screen.getByText("Nothing was recorded by this attempt.")).toBeTruthy();
    await screen.findByText(/Rejected by/);
    expect(screen.queryByText(/recorded your approval/)).toBeNull();
  });

  it("after a lost answer, asks the API and reports a decision that did get through", async () => {
    let calls = 0;
    const approved = approvalFixture({
      status: "approved",
      attention: null,
      decision: {
        id: "apd-0123456789abcdef0123", outcome: "approved", action_fingerprint: FINGERPRINT, decided_at: new Date().toISOString(),
        comment: "", decided_by: { kind: "user", id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true },
      },
    });
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(calls++ === 0 ? approvalFixture() : approved))
      .on("POST", /decision$/, () => {
        throw new TypeError("Failed to fetch"); // the request reached the API, the answer was lost
      });
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, approve this action" }));
    await screen.findByText("Your approval reached Agent Factory and is on record.");
    expect(screen.queryByRole("button", { name: /Send the same/ })).toBeNull();
    expect(api.count("POST", /decision$/)).toBe(1);
  });

  it("after a lost request, offers only the same decision again, which the API accepts once", async () => {
    let posts = 0;
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(approvalFixture()))
      .on("POST", /decision$/, () => {
        posts += 1;
        if (posts === 1) throw new TypeError("Failed to fetch");
        return json(approvalFixture({ status: "approved", attention: null }), 201);
      });
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Reject…" }));
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, reject this action" }));
    await screen.findByText("Your rejection did not reach Agent Factory. Nothing was recorded. You can send it again.");
    fireEvent.click(screen.getByRole("button", { name: "Send the same decision again" }));
    await screen.findByText(/recorded your rejection/);
    const bodies = api.requests.filter((r) => r.method === "POST").map((r) => r.body);
    expect(bodies).toEqual([
      { outcome: "rejected", action_fingerprint: FINGERPRINT, comment: "" },
      { outcome: "rejected", action_fingerprint: FINGERPRINT, comment: "" },
    ]);
  });

  it("treats a server failure while deciding as an unknown outcome, settled by the API's state", async () => {
    // The database dropped during the commit: the API answered 503, yet the decision is on record.
    let calls = 0;
    const approved = approvalFixture({
      status: "approved",
      attention: null,
      decision: {
        id: "apd-0123456789abcdef0123", outcome: "approved", action_fingerprint: FINGERPRINT, decided_at: new Date().toISOString(),
        comment: "", decided_by: { kind: "user", id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true },
      },
    });
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(calls++ === 0 ? approvalFixture() : approved))
      .on("POST", /decision$/, () => problem(503, "storage_unavailable"));
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, approve this action" }));
    await screen.findByText("Your approval reached Agent Factory and is on record.");
    expect(screen.queryByText("Nothing was recorded by this attempt.")).toBeNull();
    expect(screen.queryByRole("button", { name: /Send the same/ })).toBeNull();
    expect(api.count("POST", /decision$/)).toBe(1);
  });

  it("after a server failure with nothing on record, offers only the same decision again", async () => {
    let posts = 0;
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(approvalFixture()))
      .on("POST", /decision$/, () => {
        posts += 1;
        return posts === 1 ? problem(500, "internal_error") : json(approvalFixture({ status: "approved", attention: null }), 201);
      });
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, approve this action" }));
    await screen.findByText("Your approval did not reach Agent Factory. Nothing was recorded. You can send it again.");
    expect(screen.getByText("req_problem01")).toBeTruthy(); // the failed request's ID, for support
    fireEvent.click(screen.getByRole("button", { name: "Send the same decision again" }));
    await screen.findByText(/recorded your approval/);
    expect(api.count("POST", /decision$/)).toBe(2);
  });

  it("marks data stale when the connection drops, and disables decisions until it returns", async () => {
    let healthy = true;
    const api = baseApi().on("GET", /approvals\/apr-[0-9a-f]+$/, () => {
      if (!healthy) throw new TypeError("Failed to fetch");
      return json(approvalFixture());
    });
    renderApp(api, APPROVAL_PATH);
    await screen.findByRole("button", { name: "Approve…" });
    healthy = false;
    act(() => {
      window.dispatchEvent(new Event("online")); // forces a reload now
    });
    const banner = await screen.findByTestId("stale-banner");
    expect(banner.textContent).toMatch(/Connection lost — showing data from \d/);
    expect((screen.getByRole("button", { name: "Approve…" }) as HTMLButtonElement).disabled).toBe(true);
    healthy = true;
    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    await waitFor(() => expect(screen.queryByTestId("stale-banner")).toBeNull());
    expect((screen.getByRole("button", { name: "Approve…" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("explains a missing permission", async () => {
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(approvalFixture()))
      .on("POST", /decision$/, () => problem(403, "forbidden", { required_permission: "approvals:decide" }));
    renderApp(api, APPROVAL_PATH);
    fireEvent.click(await screen.findByRole("button", { name: "Approve…" }));
    await act(pastConfirmGuard);
    fireEvent.click(screen.getByRole("button", { name: "Yes, approve this action" }));
    await screen.findByText("You don't have permission to perform this action.");
    expect(screen.getByText(/approvals:decide/)).toBeTruthy();
  });
});

describe("reconciliation", () => {
  it("has no default answer and needs an explicit acknowledgement to authorize a retry", async () => {
    const api = baseApi()
      .on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(uncertainFixture()))
      .on("POST", /reconciliation$/, () => json(uncertainFixture({ attention: null }), 201));
    renderApp(api, APPROVAL_PATH);
    await screen.findByText(/cannot prove whether it completed/);
    const radios = screen.getAllByRole("radio") as HTMLInputElement[];
    expect(radios.map((r) => r.checked)).toEqual([false, false, false]);
    expect(screen.getByText("It happened")).toBeTruthy();
    expect(screen.getByText("It didn't happen — don't retry")).toBeTruthy();
    expect(screen.getByText("It didn't happen — authorize one retry")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Review answer…" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("It didn't happen — authorize one retry"));
    fireEvent.click(screen.getByRole("button", { name: "Review answer…" }));
    await screen.findByText(/This creates a new authorization. Agent Factory may perform the action one more time./);
    const authorize = screen.getByRole("button", { name: "Authorize one retry" }) as HTMLButtonElement;
    expect(authorize.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/I authorize one new attempt for 15 minutes/));
    await act(pastConfirmGuard);
    fireEvent.click(authorize);
    await screen.findByText(/recorded your answer/);
    expect(api.requests.find((r) => r.method === "POST")!.body).toEqual({
      execution_id: "axe-0123456789abcdef0123",
      attempt: 1,
      action_fingerprint: FINGERPRINT,
      choice: "did_not_happen_retry",
      comment: "",
    });
  });
});

describe("untrusted text", () => {
  it("renders HTML-like content from the API as inert text", async () => {
    const payload = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';
    const evil = approvalFixture({
      project_name: `Evil ${payload}`,
      reason: payload,
      risk: `javascript:alert(1) ${payload}`,
      action: { tool: "delete_file", category: "destructive_delete", params: { path: payload } },
      timeline: [{ kind: "approved", at: new Date().toISOString(), attempt: null, outcome: "approved", until: null, worker: null,
        actor: { kind: "user", id: "prn-0123456789abcdef0123", display_name: payload, verified: true } }],
    });
    const { container } = renderApp(baseApi().on("GET", /approvals\/apr-[0-9a-f]+$/, () => json(evil)), APPROVAL_PATH);
    await screen.findAllByText(payload);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
    expect(container.innerHTML).toContain("&lt;img src=x");
  });
});

describe("sign-in", () => {
  it("keeps a valid token in sessionStorage, never in localStorage, and never on screen", async () => {
    const api = baseApi();
    window.history.pushState(null, "", "/");
    render(<App client={new ApiClient({ getToken: readToken, fetchImpl: api.fetch })} />);
    fireEvent.change(screen.getByLabelText("Development access token"), { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("region", { name: /Needs your attention/ });
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe(TOKEN);
    expect(localStorage.length).toBe(0);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("refuses a token the API does not accept, and forgets it", async () => {
    const api = baseApi().on("GET", /^\/api\/v1\/projects$/, () => problem(401, "invalid_token"));
    window.history.pushState(null, "", "/");
    render(<App client={new ApiClient({ getToken: readToken, fetchImpl: api.fetch })} />);
    fireEvent.change(screen.getByLabelText("Development access token"), { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText(/That token was not accepted/);
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });
});

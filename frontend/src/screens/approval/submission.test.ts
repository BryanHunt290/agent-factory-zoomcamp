import { describe, expect, it } from "vitest";
import { approvalFixture, FINGERPRINT, uncertainFixture } from "../../test/fakeApi";
import { recordedText, resolveUnknown, unknownText, type Sent } from "./submission";

const approve: Sent = { kind: "decision", outcome: "approved", comment: "" };
const answer: Sent = { kind: "reconciliation", choice: "it_happened", attempt: 1, executionId: "axe-0123456789abcdef0123", comment: "" };
const decided = (outcome: "approved" | "rejected") =>
  approvalFixture({
    status: outcome,
    attention: null,
    decision: {
      id: "apd-0123456789abcdef0123",
      outcome,
      action_fingerprint: FINGERPRINT,
      decided_at: "2026-10-05T12:00:00.000000Z",
      decided_by: { kind: "user", id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true },
      comment: "",
    },
  });

describe("resolving an unknown outcome from authoritative state", () => {
  it("recognizes a decision that reached the API", () => {
    expect(resolveUnknown(approve, decided("approved"))).toBe("recorded_as_sent");
  });

  it("never treats a different decision as ours", () => {
    expect(resolveUnknown(approve, decided("rejected"))).toBe("different_on_record");
  });

  it("allows sending again only when nothing was recorded and the request still waits", () => {
    expect(resolveUnknown(approve, approvalFixture())).toBe("not_recorded");
    expect(resolveUnknown(approve, approvalFixture({ status: "expired", attention: null }))).toBe("closed");
  });

  it("does the same for reconciliation answers, attempt by attempt", () => {
    const recorded = uncertainFixture({
      attention: null,
      execution: {
        id: "axe-0123456789abcdef0123",
        state: "uncertain",
        attempt: 1,
        retry_authorized_until: null,
        reconciliation: {
          attempt: 1,
          choice: "it_happened",
          decided_at: "2026-10-05T12:00:00.000000Z",
          decided_by: { kind: "user", id: "prn-0123456789abcdef0123", display_name: "Dana", verified: true },
          comment: "",
          applied: false,
        },
      },
    });
    expect(resolveUnknown(answer, recorded)).toBe("recorded_as_sent");
    expect(resolveUnknown({ ...answer, choice: "did_not_happen" }, recorded)).toBe("different_on_record");
    expect(resolveUnknown(answer, uncertainFixture())).toBe("not_recorded");
    expect(
      resolveUnknown(answer, uncertainFixture({ execution: { id: "axe-0123456789abcdef0123", state: "uncertain", attempt: 2, retry_authorized_until: null, reconciliation: null } })),
    ).toBe("closed");
  });

  it("explains each outcome honestly", () => {
    expect(recordedText(approve, true)).toBe("Agent Factory recorded your approval. A worker continues the run shortly.");
    expect(recordedText(approve, false)).toContain("already on record");
    expect(unknownText(approve, null)).toContain("couldn't confirm");
    expect(unknownText(approve, "not_recorded")).toContain("did not reach");
    expect(unknownText(approve, "different_on_record")).toContain("Nothing more was sent");
  });
});

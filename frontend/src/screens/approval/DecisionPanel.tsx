// APPROVE / REJECT, deliberately: a first tap opens a confirmation that restates the exact request;
// only the second, separate button sends. Nothing is shown as decided until the API confirms it.
import { useEffect, useRef, useState } from "react";
import type { ApprovalRequest, DecisionOutcome } from "../../api/types";
import { formatDateTime, formatRelative } from "../../lib/format";
import type { Sent } from "./submission";

/** Taps on the confirm button this soon after it appeared are ignored (a double tap on the first button). */
export const CONFIRM_GUARD_MS = 400;

export function DecisionPanel({
  approval,
  disabled,
  now,
  onSubmit,
}: {
  approval: ApprovalRequest;
  disabled: boolean;
  now: number;
  onSubmit: (sent: Sent) => void;
}) {
  const [outcome, setOutcome] = useState<DecisionOutcome | null>(null);
  const [comment, setComment] = useState("");
  const openedAt = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (outcome) heading.current?.focus();
  }, [outcome]);

  const choose = (value: DecisionOutcome) => {
    openedAt.current = Date.now();
    setOutcome(value);
  };
  const confirm = () => {
    if (!outcome || disabled || Date.now() - openedAt.current < CONFIRM_GUARD_MS) return;
    onSubmit({ kind: "decision", outcome, comment: comment.trim() });
  };

  if (!outcome) {
    return (
      <section aria-labelledby="decide-heading" className="panel decision-panel">
        <h2 id="decide-heading">Your decision</h2>
        <p>Review what will happen, why, and the risk above. You will be asked to confirm.</p>
        <div className="actions">
          <button type="button" className="button primary" disabled={disabled} onClick={() => choose("approved")}>
            Approve…
          </button>
          <button type="button" className="button danger" disabled={disabled} onClick={() => choose("rejected")}>
            Reject…
          </button>
        </div>
      </section>
    );
  }

  const approving = outcome === "approved";
  return (
    <section aria-labelledby="confirm-heading" className={`panel decision-panel confirming ${approving ? "approving" : "rejecting"}`}>
      <h2 id="confirm-heading" tabIndex={-1} ref={heading}>
        {approving ? "Confirm approval" : "Confirm rejection"}
      </h2>
      <p className="confirm-what">
        {approving ? "Agent Factory may then perform:" : "Agent Factory will not perform:"} <strong>{approval.action_summary}</strong>
      </p>
      <p>
        Project <strong>{approval.project_name}</strong>, requested by <strong>{approval.agent}</strong>.
        {approval.expires_at ? (
          <>
            {" "}
            This request expires {formatRelative(approval.expires_at, now)} ({formatDateTime(approval.expires_at)}).
          </>
        ) : null}
      </p>
      <label htmlFor="decision-comment">Comment (optional)</label>
      <textarea
        id="decision-comment"
        maxLength={1000}
        rows={3}
        value={comment}
        onChange={(event) => setComment(event.target.value)}
      />
      <div className="actions">
        <button type="button" className={`button ${approving ? "primary" : "danger"}`} disabled={disabled} onClick={confirm}>
          {approving ? "Yes, approve this action" : "Yes, reject this action"}
        </button>
        <button type="button" className="button secondary" disabled={disabled} onClick={() => setOutcome(null)}>
          Cancel
        </button>
      </div>
    </section>
  );
}

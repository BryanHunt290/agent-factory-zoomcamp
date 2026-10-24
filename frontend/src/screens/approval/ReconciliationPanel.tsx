// An approved action whose outcome is unknown: a person who checked says whether it happened.
// No answer is preselected. "Authorize one retry" needs an explicit acknowledgement, because it is a new
// authorization: Agent Factory may perform the action one more time.
import { useEffect, useRef, useState } from "react";
import type { ApprovalRequest, ReconciliationChoice } from "../../api/types";
import { formatDateTime } from "../../lib/format";
import { CHOICE_LABEL } from "../../lib/labels";
import { CONFIRM_GUARD_MS } from "./DecisionPanel";
import type { Sent } from "./submission";

const CHOICES: { value: ReconciliationChoice; help: string }[] = [
  {
    value: "it_happened",
    help: "The action took effect. Agent Factory records it as done and will not send it again.",
  },
  {
    value: "did_not_happen",
    help: "The action did not take effect, and must not be tried again. The run continues without it.",
  },
  {
    value: "did_not_happen_retry",
    help:
      "This creates a new authorization. Agent Factory may perform the action one more time, within 15 minutes " +
      "of your answer. Choose this only if you are sure the action did not happen.",
  },
];

export function ReconciliationPanel({
  approval,
  disabled,
  onSubmit,
}: {
  approval: ApprovalRequest;
  disabled: boolean;
  onSubmit: (sent: Sent) => void;
}) {
  const execution = approval.execution;
  const [choice, setChoice] = useState<ReconciliationChoice | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [understood, setUnderstood] = useState(false);
  const [comment, setComment] = useState("");
  const openedAt = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (reviewing) heading.current?.focus();
  }, [reviewing]);

  if (!execution) return null;
  const sentAt = [...approval.timeline]
    .reverse()
    .find((entry) => (entry.kind === "action_dispatching" || entry.kind === "retry_dispatched") && entry.attempt === execution.attempt);
  const retry = choice === "did_not_happen_retry";

  const review = () => {
    if (!choice) return;
    openedAt.current = Date.now();
    setReviewing(true);
  };
  const confirm = () => {
    if (!choice || disabled || (retry && !understood) || Date.now() - openedAt.current < CONFIRM_GUARD_MS) return;
    onSubmit({ kind: "reconciliation", choice, attempt: execution.attempt, executionId: execution.id, comment: comment.trim() });
  };

  return (
    <section aria-labelledby="reconcile-heading" className="panel reconciliation-panel">
      <h2 id="reconcile-heading">Did it happen?</h2>
      <p className="explain">
        <strong>Agent Factory attempted this action but cannot prove whether it completed.</strong> It will not guess:
        the run waits until someone who checked says what happened.
      </p>
      <p>
        Attempt {execution.attempt}
        {sentAt ? <> was sent {formatDateTime(sentAt.at)}</> : null}. Check the system it acts on (for example, whether
        the message arrived), then answer for this attempt.
      </p>
      {!reviewing ? (
        <>
          <fieldset className="choices" disabled={disabled}>
            <legend>What happened?</legend>
            {CHOICES.map((option) => (
              <div key={option.value} className={`choice${option.value === "did_not_happen_retry" ? " choice-retry" : ""}`}>
                <input
                  type="radio"
                  id={`choice-${option.value}`}
                  name="reconciliation-choice"
                  value={option.value}
                  checked={choice === option.value}
                  onChange={() => {
                    setChoice(option.value);
                    setUnderstood(false);
                  }}
                  aria-describedby={`choice-${option.value}-help`}
                />
                <label htmlFor={`choice-${option.value}`}>{CHOICE_LABEL[option.value]}</label>
                <p id={`choice-${option.value}-help`} className="help">
                  {option.help}
                </p>
              </div>
            ))}
          </fieldset>
          <label htmlFor="reconcile-comment">Comment (optional)</label>
          <textarea
            id="reconcile-comment"
            maxLength={1000}
            rows={3}
            value={comment}
            disabled={disabled}
            onChange={(event) => setComment(event.target.value)}
          />
          <div className="actions">
            <button type="button" className="button primary" disabled={disabled || !choice} onClick={review}>
              Review answer…
            </button>
          </div>
        </>
      ) : (
        <div className={`review${retry ? " review-retry" : ""}`}>
          <h3 tabIndex={-1} ref={heading}>
            Confirm your answer
          </h3>
          <p>
            Your answer for attempt {execution.attempt}: <strong>{choice ? CHOICE_LABEL[choice] : ""}</strong>
          </p>
          {retry ? (
            <div className="retry-warning">
              <p>
                <strong>This creates a new authorization. Agent Factory may perform the action one more time.</strong> The
                retry must begin within 15 minutes of your answer, or it lapses and nothing is sent.
              </p>
              <div className="ack">
                <input
                  type="checkbox"
                  id="retry-understood"
                  checked={understood}
                  onChange={(event) => setUnderstood(event.target.checked)}
                />
                <label htmlFor="retry-understood">
                  I checked that the action did not happen, and I authorize one new attempt for 15 minutes.
                </label>
              </div>
            </div>
          ) : null}
          <div className="actions">
            <button
              type="button"
              className={`button ${retry ? "danger" : "primary"}`}
              disabled={disabled || (retry && !understood)}
              onClick={confirm}
            >
              {retry ? "Authorize one retry" : "Confirm answer"}
            </button>
            <button type="button" className="button secondary" disabled={disabled} onClick={() => setReviewing(false)}>
              Change answer
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

// One approval, readable on a phone: what will happen, why, the risk, who asked, until when; then the
// decision (or, for an uncertain action, the reconciliation), and what happened to the action since.
//
// Server-authoritative: after every submission, whatever its outcome, the approval is reloaded from the
// API, and the screen shows that state. A decision is never displayed before the API confirms it.
import { useCallback, useRef, useState } from "react";
import { ApiError, isConflict, mayHaveBeenRecorded, userMessage } from "../../api/errors";
import type { ApprovalRequest, NotificationStatus, Run } from "../../api/types";
import { useClient } from "../../app/context";
import { ErrorNotice, Freshness, isStale, LoadFailure, Loading, StatusBadge, When } from "../../components/common";
import { Timeline } from "../../components/lists";
import { formatDateTime, formatRelative, parameterText } from "../../lib/format";
import { CHOICE_LABEL, executionState, isActive, notificationText, runStatus } from "../../lib/labels";
import { tick, useNow, usePolling } from "../../lib/polling";
import { Link, paths } from "../../lib/router";
import { DecisionPanel } from "./DecisionPanel";
import { ReconciliationPanel } from "./ReconciliationPanel";
import { recordedText, resolveUnknown, sendingText, unknownText, type Sent, type Submission } from "./submission";

const WATCH_MS = 5_000;
const SETTLED_MS = 30_000;

/** Is anything about this approval still moving (a person or a worker still has to act)? */
function moving(approval: ApprovalRequest | undefined): boolean {
  if (!approval) return true;
  if (approval.attention) return true;
  const state = approval.execution?.state;
  if (state === "authorized" || state === "dispatching" || state === "uncertain") return true;
  return approval.status === "approved" && !approval.execution; // approved, a worker has yet to use it
}

const HEADLINE: Record<string, string> = {
  decision_required: "Approval required",
  reconciliation_required: "Reconciliation required",
  pending: "Waiting for a decision",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired",
  cancelled: "No longer applies",
};

export function ApprovalDetail({ projectId, approvalId }: { projectId: string; approvalId: string }) {
  const client = useClient();
  const now = useNow(5_000);
  const approval = usePolling<ApprovalRequest>(
    `approval:${projectId}:${approvalId}`,
    useCallback((signal) => client.getApproval(projectId, approvalId, { signal }), [client, projectId, approvalId]),
    { intervalMs: (a) => (moving(a) ? WATCH_MS : SETTLED_MS) },
  );
  const runId = approval.data?.run_id ?? null;
  const run = usePolling<Run>(
    runId ? `run:${projectId}:${runId}` : null,
    useCallback((signal) => client.getRun(projectId, runId as string, { signal }), [client, projectId, runId]),
    { intervalMs: (r) => (isActive(r?.status) ? WATCH_MS : SETTLED_MS) },
  );
  const [submission, setSubmission] = useState<Submission>({ phase: "idle" });
  const inFlight = useRef(false); // a second tap before React re-renders must not send twice

  const refreshApproval = approval.refresh;
  const refreshRun = run.refresh;
  const submit = useCallback(
    async (sent: Sent) => {
      const current = approval.data;
      if (inFlight.current || !current) return;
      inFlight.current = true;
      setSubmission({ phase: "sending", sent });
      try {
        const result =
          sent.kind === "decision"
            ? await client.decideApproval(projectId, approvalId, {
                outcome: sent.outcome,
                action_fingerprint: current.action_fingerprint, // exactly as the API gave it
                comment: sent.comment,
              })
            : await client.reconcileAction(projectId, approvalId, {
                execution_id: sent.executionId,
                attempt: sent.attempt,
                action_fingerprint: current.action_fingerprint,
                choice: sent.choice,
                comment: sent.comment,
              });
        setSubmission({ phase: "recorded", sent, recordedNow: result.recordedNow, requestId: result.requestId });
      } catch (error) {
        // A 4xx answer is a refusal: nothing was recorded. Anything else (no answer, or a 5xx) may have
        // been recorded before it failed; the approval reloaded below settles which.
        if (error instanceof ApiError && !mayHaveBeenRecorded(error)) {
          setSubmission({ phase: "refused", sent, error });
        } else {
          setSubmission({ phase: "unknown", sent, error, failedAt: tick() });
        }
      } finally {
        inFlight.current = false;
        await refreshApproval(); // the authoritative state, whatever happened
        void refreshRun();
      }
    },
    [approval.data, client, projectId, approvalId, refreshApproval, refreshRun],
  );

  if (approval.loading) return <Loading what="the approval" />;
  const a = approval.data;
  if (!a) return <LoadFailure poll={approval} what="the approval" />;

  const stale = isStale([approval]);
  const sending = submission.phase === "sending";
  // An unknown outcome is settled only by data from a load that started after the failure.
  const resolution =
    submission.phase === "unknown" && approval.dataTick > submission.failedAt ? resolveUnknown(submission.sent, a) : null;
  const unsettled = submission.phase === "unknown" && resolution !== "not_recorded";
  const decidedHere = submission.phase === "recorded" && submission.sent.kind === "decision";
  const answeredHere = submission.phase === "recorded" && submission.sent.kind === "reconciliation";
  const controlsDisabled = stale || sending || unsettled;
  const headline = HEADLINE[a.attention ?? a.status] ?? "Approval";

  return (
    <>
      <p className="breadcrumb">
        <Link to={paths.dashboard()}>Dashboard</Link> / <Link to={paths.project(a.project_id)}>{a.project_name}</Link> / Approval
      </p>
      <div className="title-row">
        <h1>{headline}</h1>
        {a.attention ? (
          <span className={`badge tone-attention kind-${a.attention}`}>
            <span className="badge-symbol" aria-hidden="true">
              {a.attention === "reconciliation_required" ? "?" : "!"}
            </span>
            Needs you
          </span>
        ) : null}
      </div>
      <Freshness polls={[approval, run]} />

      <SubmissionNotice submission={submission} resolution={resolution} />

      <section aria-labelledby="what-heading" className="panel what-panel">
        <h2 id="what-heading">What will happen</h2>
        <p className="action-summary">{a.action_summary}</p>
        <h3>Exact action</h3>
        <dl className="params">
          <div>
            <dt>Tool</dt>
            <dd>
              <code>{a.action.tool}</code>
            </dd>
          </div>
          {Object.entries(a.action.params).map(([name, value]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd>
                <code>{parameterText(value)}</code>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section aria-labelledby="why-heading" className="panel">
        <h2 id="why-heading">Why it is requested</h2>
        <p>{a.reason}</p>
        <h2>Risk</h2>
        <p className="risk">{a.risk}</p>
      </section>

      <section aria-labelledby="context-heading" className="panel">
        <h2 id="context-heading">Context</h2>
        <dl className="facts">
          <div>
            <dt>Project</dt>
            <dd>
              <Link to={paths.project(a.project_id)}>{a.project_name}</Link>
            </dd>
          </div>
          <div>
            <dt>Run</dt>
            <dd>
              <code>{a.run_id}</code> {run.data ? <StatusBadge {...runStatus(run.data.status)} /> : null}
            </dd>
          </div>
          <div>
            <dt>Requested by</dt>
            <dd>
              Agent <strong>{a.agent}</strong>, at workflow step <strong>{a.node}</strong>
            </dd>
          </div>
          <div>
            <dt>Requested</dt>
            <dd>
              <When at={a.requested_at} now={now} />
            </dd>
          </div>
          <div>
            <dt>Expires</dt>
            <dd>
              {a.expires_at ? (
                <>
                  {Date.parse(a.expires_at) > now ? "in " : "expired "}
                  <time dateTime={a.expires_at}>
                    {formatRelative(a.expires_at, now).replace(/^in /, "")}
                  </time>{" "}
                  ({formatDateTime(a.expires_at)})
                </>
              ) : (
                "—"
              )}
            </dd>
          </div>
          <div>
            <dt>Decision</dt>
            <dd>
              <DecisionStatus approval={a} />
            </dd>
          </div>
          <div>
            <dt>Notifications</dt>
            <dd>
              <NotificationSummary statuses={a.notifications} now={now} />
            </dd>
          </div>
        </dl>
        <details className="technical">
          <summary>Technical details</summary>
          <dl className="facts">
            <div>
              <dt>Action fingerprint</dt>
              <dd>
                <code className="wrap">{a.action_fingerprint}</code>
              </dd>
            </div>
            <div>
              <dt>Approval</dt>
              <dd>
                <code>{a.id}</code>
              </dd>
            </div>
            {a.execution ? (
              <div>
                <dt>Execution</dt>
                <dd>
                  <code>{a.execution.id}</code>
                </dd>
              </div>
            ) : null}
          </dl>
        </details>
      </section>

      {a.attention === "decision_required" && !decidedHere ? (
        <DecisionPanel approval={a} disabled={controlsDisabled} now={now} onSubmit={submit} />
      ) : null}
      {a.attention === "reconciliation_required" && !answeredHere ? (
        <ReconciliationPanel approval={a} disabled={controlsDisabled} onSubmit={submit} />
      ) : null}
      {resolution === "not_recorded" && submission.phase === "unknown" ? (
        <div className="actions resend">
          <button type="button" className="button primary" disabled={stale || sending} onClick={() => void submit(submission.sent)}>
            Send the same {submission.sent.kind === "decision" ? "decision" : "answer"} again
          </button>
          <button type="button" className="button secondary" onClick={() => setSubmission({ phase: "idle" })}>
            Choose again
          </button>
        </div>
      ) : null}

      <section aria-labelledby="execution-heading" className="panel">
        <h2 id="execution-heading">What happened to the action</h2>
        <ExecutionSummary approval={a} />
        <h3>Timeline</h3>
        <Timeline entries={a.timeline} />
      </section>
    </>
  );
}

function DecisionStatus({ approval }: { approval: ApprovalRequest }) {
  const decision = approval.decision;
  if (decision) {
    const verified = decision.decided_by.verified ? "signed in" : "local operator, unverified";
    return (
      <>
        {decision.outcome === "approved" ? "Approved" : "Rejected"} by <strong>{decision.decided_by.display_name}</strong> (
        {verified}), {formatDateTime(decision.decided_at)}
        {decision.comment ? <span className="comment">: “{decision.comment}”</span> : null}
      </>
    );
  }
  if (approval.status === "expired") return <>Expired before anyone decided. The workflow continues without this action.</>;
  if (approval.status === "cancelled") return <>This request no longer applies.</>;
  return <>Waiting for a decision</>;
}

function ExecutionSummary({ approval }: { approval: ApprovalRequest }) {
  const execution = approval.execution;
  if (!execution) {
    return (
      <p>
        {approval.status === "approved"
          ? "Approved. A worker has not used this approval yet."
          : "Nothing has been attempted."}
      </p>
    );
  }
  const answer = execution.reconciliation;
  return (
    <dl className="facts">
      <div>
        <dt>State</dt>
        <dd>{executionState(execution.state)}</dd>
      </div>
      <div>
        <dt>Attempts</dt>
        <dd>{execution.attempt}</dd>
      </div>
      {execution.retry_authorized_until ? (
        <div>
          <dt>Retry</dt>
          <dd>One retry may begin until {formatDateTime(execution.retry_authorized_until)}</dd>
        </div>
      ) : null}
      {answer ? (
        <div>
          <dt>Answer</dt>
          <dd>
            {CHOICE_LABEL[answer.choice]}, by {answer.decided_by.display_name}, {formatDateTime(answer.decided_at)} —{" "}
            {answer.applied ? "applied" : "waiting for a worker to apply it"}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

// Whether people were told: informational only. It never changes what can be decided here, and it never
// names who was told or where.
function NotificationSummary({ statuses, now }: { statuses: NotificationStatus[]; now: number }) {
  if (statuses.length === 0) {
    return <>None sent. This page and the attention list always show what needs you.</>;
  }
  return (
    <ul className="notifications" data-testid="notifications">
      {statuses.map((status) => {
        const { about, text, tone } = notificationText(status);
        return (
          <li key={`${status.about}:${status.attempt ?? 0}`} className={`tone-${tone}`}>
            <span className="notification-about">{about}:</span> {text}
            {status.sent_at ? (
              <>
                {" "}
                <When at={status.sent_at} now={now} />
              </>
            ) : status.last_attempt_at ? (
              <>
                {" "}
                <When at={status.last_attempt_at} now={now} prefix="last try" />
              </>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function SubmissionNotice({ submission, resolution }: { submission: Submission; resolution: ReturnType<typeof resolveUnknown> | null }) {
  switch (submission.phase) {
    case "idle":
      return null;
    case "sending":
      return (
        <div className="notice notice-progress" role="status" aria-live="polite" data-testid="submission">
          <p className="notice-title">{sendingText(submission.sent)}</p>
        </div>
      );
    case "recorded":
      return (
        <div className="notice notice-ok" role="status" aria-live="polite" data-testid="submission">
          <p className="notice-title">{recordedText(submission.sent, submission.recordedNow)}</p>
        </div>
      );
    case "refused":
      return (
        <div data-testid="submission">
          <ErrorNotice error={submission.error} action={isConflict(submission.error) ? <p>Nothing was recorded by this attempt.</p> : null} />
        </div>
      );
    case "unknown": {
      const cause = userMessage(submission.error);
      return (
        <div
          className={`notice ${resolution === "recorded_as_sent" ? "notice-ok" : "notice-warning"}`}
          role="status"
          aria-live="polite"
          data-testid="submission"
        >
          <p className="notice-title">{unknownText(submission.sent, resolution)}</p>
          {resolution === null ? <p>{[cause.title, cause.detail].filter(Boolean).join(" ")}</p> : null}
          {cause.requestId ? (
            <details className="support">
              <summary>Support details</summary>
              <p>
                Request ID: <code>{cause.requestId}</code>
              </p>
            </details>
          ) : null}
        </div>
      );
    }
  }
}

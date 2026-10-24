// Small presentational pieces shared by every screen. Every API-provided string is rendered as text
// (React escapes it); nothing here ever renders HTML from data.
import type { ReactNode } from "react";
import { isApiError, isSignedOut, isTemporarilyUnavailable, isUnreachable, userMessage } from "../api/errors";
import { formatClock, formatDateTime, formatRelative } from "../lib/format";
import type { Tone } from "../lib/labels";
import type { PollState } from "../lib/polling";

export function StatusBadge({ label, tone, symbol }: { label: string; tone: Tone; symbol: string }) {
  return (
    <span className={`badge tone-${tone}`}>
      <span className="badge-symbol" aria-hidden="true">
        {symbol}
      </span>
      {label}
    </span>
  );
}

/** A time as "5 min ago", with the exact time next to it (no hover needed). */
export function When({ at, now, prefix }: { at: string; now: number; prefix?: string }) {
  return (
    <span className="when">
      {prefix ? `${prefix} ` : ""}
      <time dateTime={at}>{formatRelative(at, now)}</time>
      <span className="when-exact"> ({formatDateTime(at)})</span>
    </span>
  );
}

/** A failed request explained in plain words, with the request ID for support in a details area. */
export function ErrorNotice({ error, action }: { error: unknown; action?: ReactNode }) {
  const message = userMessage(error);
  return (
    <div className="notice notice-error" role="alert">
      <p className="notice-title">{message.title}</p>
      {message.detail ? <p>{message.detail}</p> : null}
      {action}
      {message.requestId ? (
        <details className="support">
          <summary>Support details</summary>
          <p>
            Request ID: <code>{message.requestId}</code>
          </p>
        </details>
      ) : null}
    </div>
  );
}

/** The stale-data warning: data the API has not confirmed recently is never shown as if it were current. */
export function Freshness({ polls }: { polls: PollState<unknown>[] }) {
  const failing = polls.filter((poll) => poll.error && !isSignedOut(poll.error));
  if (!failing.length) return null;
  const connection = failing.some((poll) => isUnreachable(poll.error) || isTemporarilyUnavailable(poll.error));
  const confirmed = failing.map((poll) => poll.lastSuccessAt).filter((at): at is number => at !== null);
  if (!connection) return null; // other failures are explained where they happen
  const unavailable = failing.some((poll) => isTemporarilyUnavailable(poll.error));
  const cause = unavailable ? "Agent Factory is temporarily unavailable" : "Connection lost";
  return (
    <div className="notice notice-stale" role="status" aria-live="polite" data-testid="stale-banner">
      <p className="notice-title">
        {confirmed.length
          ? `${cause} — showing data from ${formatClock(Math.min(...confirmed))}.`
          : `${cause} — no data to show yet.`}
      </p>
      <p>The dashboard keeps trying and will update by itself. Decisions are disabled until it reconnects.</p>
    </div>
  );
}

/** True when what's on screen may be out of date: decisions must wait for fresh data. */
export function isStale(polls: PollState<unknown>[]): boolean {
  return polls.some((poll) => poll.error && (isUnreachable(poll.error) || isTemporarilyUnavailable(poll.error)));
}

export function Loading({ what }: { what: string }) {
  return (
    <p className="loading" role="status" aria-live="polite">
      Loading {what}…
    </p>
  );
}

/** First load failed, or a non-connection failure (403, 404, …) that the screen cannot work around. */
export function LoadFailure({ poll, what }: { poll: PollState<unknown>; what: string }) {
  if (!poll.error || (poll.data !== undefined && (isUnreachable(poll.error) || isTemporarilyUnavailable(poll.error)))) {
    return null;
  }
  if (isApiError(poll.error) && isSignedOut(poll.error)) return null; // the app returns to sign-in
  return <ErrorNotice error={poll.error} action={<p>Could not load {what}.</p>} />;
}

// How the dashboard understands failed requests, and what it tells a person about them.
//
// The API answers errors as RFC 9457 problem documents (application/problem+json) with a stable `code`
// and a `request_id`. Anything else that fails (no answer at all, a timeout, the frontend server's
// proxy reporting that the API is down) means Agent Factory is unreachable from this browser: the
// data on screen may be stale, but nothing about it is known to be wrong.
import type { ErrorCode, Problem } from "./types";

export type FailureKind = "network" | "timeout" | "gateway" | "problem" | "unexpected" | "signed_out";

export class ApiError extends Error {
  readonly kind: FailureKind;
  readonly status: number | null;
  readonly code: ErrorCode | null;
  readonly requestId: string | null;
  readonly problem: Problem | null;
  readonly retryAfterSeconds: number | null;

  constructor(init: {
    kind: FailureKind;
    message: string;
    status?: number | null;
    code?: ErrorCode | null;
    requestId?: string | null;
    problem?: Problem | null;
    retryAfterSeconds?: number | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.kind = init.kind;
    this.status = init.status ?? null;
    this.code = init.code ?? null;
    this.requestId = init.requestId ?? null;
    this.problem = init.problem ?? null;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/** No answer from Agent Factory: the network, a timeout, or the API down behind the frontend server. */
export function isUnreachable(error: unknown): boolean {
  return isApiError(error) && (error.kind === "network" || error.kind === "timeout" || error.kind === "gateway");
}

/** Agent Factory answered that it cannot serve requests right now (its database or key source is down). */
export function isTemporarilyUnavailable(error: unknown): boolean {
  return isApiError(error) && error.status === 503;
}

/** The credential is missing, refused, or expired: the person must sign in again. */
export function isSignedOut(error: unknown): boolean {
  return isApiError(error) && (error.kind === "signed_out" || error.status === 401);
}

/**
 * After a write failed: might the API have recorded it anyway? Yes when no answer arrived, and when the
 * API (or its database) failed while handling it (5xx): a commit can succeed and its answer still be
 * lost. Only a 4xx answer means the API checked the request and recorded nothing.
 */
export function mayHaveBeenRecorded(error: unknown): boolean {
  if (!isApiError(error)) return true;
  if (error.kind === "signed_out") return false; // never sent: there was no token
  if (error.kind === "problem") return error.status === null || error.status >= 500;
  return true; // network, timeout, gateway, unexpected
}

/** The item changed under the person's feet: refresh and show the new state. */
export function isConflict(error: unknown): boolean {
  return isApiError(error) && error.status === 409;
}

export interface UserMessage {
  title: string;
  detail?: string;
  requestId?: string;
}

const CONFLICTS: Partial<Record<ErrorCode, string>> = {
  approval_already_decided: "This approval was already decided. Refreshing to show the decision on record.",
  approval_expired: "This approval expired before your decision reached Agent Factory. Refreshing.",
  approval_cancelled: "This approval no longer applies. Refreshing.",
  action_fingerprint_mismatch: "The requested action changed before your decision. Refreshing.",
  reconciliation_stale: "This action changed before your answer: a newer attempt is waiting. Refreshing.",
  reconciliation_already_decided: "Someone already answered for this attempt. Refreshing.",
  reconciliation_not_required: "This action no longer needs an answer. Refreshing.",
};

/** A short, honest explanation of ``error`` for the person using the dashboard. Never a stack trace. */
export function userMessage(error: unknown): UserMessage {
  if (!isApiError(error)) {
    return { title: "Something went wrong in the dashboard." };
  }
  const requestId = error.requestId ?? undefined;
  if (error.kind === "network" || error.kind === "gateway") {
    return { title: "Agent Factory is unreachable right now.", detail: "The dashboard will keep trying.", requestId };
  }
  if (error.kind === "timeout") {
    return { title: "Agent Factory did not answer in time.", detail: "The dashboard will keep trying.", requestId };
  }
  if (error.kind === "signed_out" || error.status === 401) {
    return { title: "Your session is no longer valid.", detail: "Sign in again with a new development token.", requestId };
  }
  if (error.status === 403) {
    const permission = error.problem?.required_permission;
    return {
      title: "You don't have permission to perform this action.",
      detail: permission ? `It requires the permission ${permission}.` : undefined,
      requestId,
    };
  }
  if (error.status === 409) {
    return {
      title: (error.code && CONFLICTS[error.code]) || "The item changed before your decision. Refreshing.",
      requestId,
    };
  }
  if (error.status === 429) {
    const wait = error.retryAfterSeconds;
    return {
      title: "Too many attempts. Try again shortly.",
      detail: wait ? `You can try again in about ${wait} seconds.` : undefined,
      requestId,
    };
  }
  if (error.status === 503) {
    return { title: "Agent Factory is temporarily unavailable.", detail: "The dashboard will keep trying.", requestId };
  }
  if (error.status === 404) {
    return { title: "This item does not exist, or no longer exists.", requestId };
  }
  if (error.status === 422 || error.status === 400) {
    return { title: "Agent Factory could not accept this request.", detail: error.problem?.detail, requestId };
  }
  return { title: "Agent Factory could not complete this request.", requestId };
}

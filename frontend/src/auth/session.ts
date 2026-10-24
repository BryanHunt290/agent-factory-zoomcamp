// LOCAL DEVELOPMENT sign-in: the development issuer's bearer token, kept for this browser tab only.
//
// * sessionStorage, never localStorage: the token survives a refresh in the same tab and is gone when
//   the tab closes. It is never written to a URL, a log, an error message, or the bundle.
// * It is only a credential, never workflow state: deleting it loses nothing but the sign-in.
// * This is not the production sign-in. Production browser sessions wait for a configured OpenID
//   Connect provider (docs/authentication.md).
const KEY = "agent-factory.local-development-token";
const TOKEN_SHAPE = /^[A-Za-z0-9_-]{2,4096}\.[A-Za-z0-9_-]{2,8192}\.[A-Za-z0-9_-]{2,4096}$/;

export class TokenFormatError extends Error {
  constructor() {
    super("This does not look like an access token (three base64url parts separated by dots).");
    this.name = "TokenFormatError";
  }
}

type Listener = () => void;
const listeners = new Set<Listener>();

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null; // storage disabled: the person signs in again after each refresh
  }
}

let memoryFallback: string | null = null;
let signOutReason: string | null = null; // memory only: why the last session ended, for the sign-in screen

export function readToken(): string | null {
  const store = storage();
  if (!store) return memoryFallback;
  try {
    return store.getItem(KEY);
  } catch {
    return memoryFallback;
  }
}

/** The trimmed token, if ``raw`` looks like one. Throws TokenFormatError, which never contains the input. */
export function checkTokenFormat(raw: string): string {
  const token = raw.trim();
  if (!TOKEN_SHAPE.test(token)) throw new TokenFormatError();
  return token;
}

/** Keep ``raw`` (trimmed) as this tab's token. Throws TokenFormatError, which never contains the input. */
export function saveToken(raw: string): void {
  const token = checkTokenFormat(raw);
  const store = storage();
  memoryFallback = store ? null : token;
  signOutReason = null;
  try {
    store?.setItem(KEY, token);
  } catch {
    memoryFallback = token;
  }
  listeners.forEach((listener) => listener());
}

/** End this tab's session; ``reason`` (never the token) is shown on the sign-in screen. */
export function clearToken(reason?: string): void {
  memoryFallback = null;
  signOutReason = reason ?? null;
  try {
    storage()?.removeItem(KEY);
  } catch {
    // nothing stored
  }
  listeners.forEach((listener) => listener());
}

export function readSignOutReason(): string | null {
  return signOutReason;
}

export function subscribeToken(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export const TOKEN_STORAGE_KEY = KEY;

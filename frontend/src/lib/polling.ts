// Bounded polling: how the dashboard stays current without WebSockets.
//
// * One load at a time per resource; the next is scheduled only after the previous one finished, so a
//   slow API is never flooded.
// * After a failure the last good data stays on screen (marked stale by the caller), and the delay
//   grows: interval, x2, x4, ... up to maxBackoffMs. The first success resets it.
// * While the tab is hidden nothing is scheduled; when it becomes visible again, or the browser reports
//   that it is back online, the resource is loaded at once.
// * refresh() loads now (e.g. right after a decision) and resolves when that load has finished.
// The server is the source of truth: nothing here is kept beyond the page's memory.
import { useCallback, useEffect, useRef, useState } from "react";

export interface PollOptions<T> {
  /** Milliseconds between loads after a success, or a function of the latest data. */
  intervalMs: number | ((data: T | undefined) => number);
  /** The longest delay between attempts while loads keep failing. */
  maxBackoffMs?: number;
  /** When false nothing is loaded and the state is reset (e.g. no key yet). */
  enabled?: boolean;
}

export interface PollState<T> {
  data: T | undefined;
  /** tick() when the load that produced `data` started: data with a later tick than an event is newer than it. */
  dataTick: number;
  /** The latest failure, or undefined once a load has succeeded again. */
  error: unknown;
  /** When the data on screen was last confirmed by the API (ms since the epoch). */
  lastSuccessAt: number | null;
  /** No data yet, and a load is in flight. */
  loading: boolean;
  /** Consecutive failures. */
  failures: number;
}

export interface Poll<T> extends PollState<T> {
  refresh: () => Promise<void>;
}

export const DEFAULT_MAX_BACKOFF_MS = 15_000;

const INITIAL: PollState<never> = { data: undefined, dataTick: 0, error: undefined, lastSuccessAt: null, loading: true, failures: 0 };

let ticks = 0;
/** A strictly increasing counter: orders events and loads exactly (timestamps can tie within a millisecond). */
export function tick(): number {
  ticks += 1;
  return ticks;
}

/** The delay before the next attempt: the interval after a success, then doubling per failure, capped. */
export function nextDelay(intervalMs: number, failures: number, maxBackoffMs = DEFAULT_MAX_BACKOFF_MS): number {
  if (failures <= 0) return intervalMs;
  return Math.min(maxBackoffMs, intervalMs * 2 ** (failures - 1));
}

function hidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function usePolling<T>(
  key: string | null,
  load: (signal: AbortSignal) => Promise<T>,
  options: PollOptions<T>,
): Poll<T> {
  const [state, setState] = useState<PollState<T>>(INITIAL);
  const loadRef = useRef(load);
  const optionsRef = useRef(options);
  const refreshRef = useRef<() => Promise<void>>(() => Promise.resolve());
  loadRef.current = load;
  optionsRef.current = options;
  const enabled = options.enabled !== false && key !== null;

  useEffect(() => {
    setState(INITIAL);
    if (!enabled) {
      refreshRef.current = () => Promise.resolve();
      return;
    }
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let inFlight: Promise<void> | null = null;
    let again = false;
    let failures = 0;
    let latest: T | undefined;
    let afterAgain: (() => void)[] = [];

    const schedule = () => {
      clearTimeout(timer);
      if (disposed || hidden()) return;
      const { intervalMs, maxBackoffMs } = optionsRef.current;
      const base = typeof intervalMs === "function" ? intervalMs(latest) : intervalMs;
      timer = setTimeout(() => void run(), nextDelay(base, failures, maxBackoffMs));
    };

    const run = (): Promise<void> => {
      if (disposed) return Promise.resolve();
      if (inFlight) {
        again = true;
        return inFlight;
      }
      clearTimeout(timer);
      const current = new AbortController();
      controller = current;
      const startedTick = tick();
      inFlight = (async () => {
        try {
          const data = await loadRef.current(current.signal);
          if (disposed || current.signal.aborted) return;
          failures = 0;
          latest = data;
          setState({ data, dataTick: startedTick, error: undefined, lastSuccessAt: Date.now(), loading: false, failures: 0 });
        } catch (error) {
          if (disposed || current.signal.aborted) return;
          failures += 1;
          const count = failures;
          setState((previous) => ({ ...previous, error, loading: false, failures: count }));
        }
      })().finally(() => {
        inFlight = null;
        if (disposed) return;
        if (again) {
          again = false;
          const waiting = afterAgain;
          afterAgain = [];
          void run().then(() => waiting.forEach((resolve) => resolve()));
          return;
        }
        schedule();
      });
      return inFlight;
    };

    refreshRef.current = () =>
      new Promise<void>((resolve) => {
        if (inFlight) {
          again = true;
          afterAgain.push(resolve);
        } else {
          void run().then(resolve);
        }
      });

    const wake = () => {
      if (!hidden()) void run();
    };
    const onVisibility = () => {
      if (hidden()) clearTimeout(timer);
      else void run();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", wake);
    void run();

    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", wake);
      for (const resolve of afterAgain) resolve();
    };
  }, [key, enabled]);

  const refresh = useCallback(() => refreshRef.current(), []);
  return { ...state, refresh };
}

/** The current time, re-read every ``intervalMs``: for "5 min ago" labels and expiry countdowns. */
export function useNow(intervalMs = 15_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

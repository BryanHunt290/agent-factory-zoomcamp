import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { nextDelay, usePolling } from "./polling";

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => {
  vi.useRealTimers();
  setVisibility("visible");
});

describe("nextDelay", () => {
  it("waits the interval after a success and backs off after failures, capped", () => {
    expect(nextDelay(5_000, 0)).toBe(5_000);
    expect(nextDelay(5_000, 1)).toBe(5_000);
    expect(nextDelay(5_000, 2)).toBe(10_000);
    expect(nextDelay(5_000, 3)).toBe(15_000);
    expect(nextDelay(5_000, 10)).toBe(15_000);
  });
});

describe("usePolling", () => {
  it("loads at once, then again after the interval, one load at a time", async () => {
    vi.useFakeTimers();
    let n = 0;
    const load = vi.fn(async () => ++n);
    const { result } = renderHook(() => usePolling("k", load, { intervalMs: 1_000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.data).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(result.current.data).toBe(2);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("keeps the last data on a failure, records when it was confirmed, and recovers", async () => {
    vi.useFakeTimers();
    const outcomes: (number | Error)[] = [1, new Error("down"), new Error("down"), 2];
    const load = vi.fn(async () => {
      const next = outcomes.shift()!;
      if (next instanceof Error) throw next;
      return next;
    });
    const { result } = renderHook(() => usePolling("k", load, { intervalMs: 1_000, maxBackoffMs: 4_000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const confirmed = result.current.lastSuccessAt;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(result.current.data).toBe(1); // still shown
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.lastSuccessAt).toBe(confirmed);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000 + 2_000); // backoff: 1 s, then 2 s
    });
    expect(result.current.data).toBe(2);
    expect(result.current.error).toBeUndefined();
    expect(result.current.failures).toBe(0);
  });

  it("refresh() loads now and resolves when that load has finished", async () => {
    let n = 0;
    const { result } = renderHook(() => usePolling("k", async () => ++n, { intervalMs: 60_000 }));
    await waitFor(() => expect(result.current.data).toBe(1));
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.data).toBe(2);
  });

  it("pauses while the tab is hidden and loads at once when it is visible again", async () => {
    vi.useFakeTimers();
    let n = 0;
    const load = vi.fn(async () => ++n);
    renderHook(() => usePolling("k", load, { intervalMs: 1_000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    act(() => setVisibility("hidden"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(load).toHaveBeenCalledTimes(1);
    act(() => setVisibility("visible"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("schedules nothing after a load that finishes while the tab is hidden", async () => {
    vi.useFakeTimers();
    let finish: (value: number) => void = () => undefined;
    const load = vi.fn(() => new Promise<number>((resolve) => (finish = resolve)));
    renderHook(() => usePolling("k", load, { intervalMs: 1_000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    act(() => setVisibility("hidden")); // hidden while the first load is still in flight
    await act(async () => {
      finish(1);
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("forgets a previous resource's data when the key changes", async () => {
    const { result, rerender } = renderHook(({ key }) => usePolling(key, async () => key, { intervalMs: 60_000 }), {
      initialProps: { key: "a" },
    });
    await waitFor(() => expect(result.current.data).toBe("a"));
    rerender({ key: "b" });
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(result.current.data).toBe("b"));
  });
});

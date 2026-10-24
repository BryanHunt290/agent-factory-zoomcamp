// What every screen shares: the API client, and the polls the shell keeps running for all of them
// (the attention inbox and the API's readiness), so screens never poll the same thing twice.
import { createContext, useContext } from "react";
import type { ApiClient } from "../api/client";
import type { AttentionPage, Readiness } from "../api/types";
import type { Poll } from "../lib/polling";

export const ClientContext = createContext<ApiClient | null>(null);

export function useClient(): ApiClient {
  const client = useContext(ClientContext);
  if (!client) throw new Error("useClient outside ClientContext");
  return client;
}

export interface Shared {
  attention: Poll<AttentionPage>;
  readiness: Poll<Readiness>;
  /** How far back "recent activity" reaches, in hours (presentation only). */
  windowHours: number;
  setWindowHours: (hours: number) => void;
}

export const SharedContext = createContext<Shared | null>(null);

export function useShared(): Shared {
  const shared = useContext(SharedContext);
  if (!shared) throw new Error("useShared outside SharedContext");
  return shared;
}

export const WINDOW_CHOICES = [
  { hours: 12, label: "Last 12 hours" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 24 * 7, label: "Last 7 days" },
] as const;

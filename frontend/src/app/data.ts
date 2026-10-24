// Loaders that combine contract operations. Still only through the API client.
import type { ApiClient } from "../api/client";
import type { Milestone } from "../api/types";

/** A project's milestones since ``since``, oldest first, following cursors up to ``maxPages`` pages. */
export async function loadMilestones(
  client: ApiClient,
  projectId: string,
  since: string,
  signal: AbortSignal,
  maxPages = 5,
): Promise<{ items: Milestone[]; truncated: boolean }> {
  const items: Milestone[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < maxPages; page += 1) {
    const result = await client.listProjectMilestones(projectId, { since, limit: 100, cursor }, { signal });
    items.push(...result.items);
    if (!result.next_cursor) return { items, truncated: false };
    cursor = result.next_cursor;
  }
  return { items, truncated: true };
}

// The first screen: what needs you, what is running, and what happened recently (the overnight view).
import { useCallback, useEffect, useMemo, useRef } from "react";
import type { Milestone, ProjectSummaryPage } from "../api/types";
import { useClient, useShared, WINDOW_CHOICES } from "../app/context";
import { loadMilestones } from "../app/data";
import { Freshness, LoadFailure, Loading } from "../components/common";
import { AttentionList, MilestoneList, ProjectCard } from "../components/lists";
import { isActive } from "../lib/labels";
import { useNow, usePolling } from "../lib/polling";
import { Link, paths } from "../lib/router";

const ACTIVE_MS = 5_000;
const IDLE_MS = 15_000;
const ACTIVITY_MS = 30_000;
const RECENT_SHOWN = 10;

function windowStart(hours: number): string {
  // Rounded to the minute, so the polled key stays stable while the window slides.
  const start = Date.now() - hours * 3_600_000;
  return new Date(Math.floor(start / 60_000) * 60_000).toISOString();
}

export function Dashboard() {
  const client = useClient();
  const { attention, windowHours, setWindowHours } = useShared();
  const now = useNow();

  const projects = usePolling<ProjectSummaryPage>(
    `projects:${windowHours}`,
    useCallback((signal) => client.listProjects({ limit: 100, activity_since: windowStart(windowHours) }, { signal }), [client, windowHours]),
    { intervalMs: (page) => (page?.items.some((p) => isActive(p.status)) ? ACTIVE_MS : IDLE_MS) },
  );

  const withActivity = useMemo(
    () => (projects.data?.items ?? []).filter((p) => (p.activity?.milestones ?? 0) > 0).slice(0, 20),
    [projects.data],
  );
  // The milestones behind the counts: loaded every 30 s, and at once whenever the counts change.
  const withActivityRef = useRef(withActivity);
  withActivityRef.current = withActivity;
  const recent = usePolling<Milestone[]>(
    projects.data ? `recent:${windowHours}` : null,
    useCallback(
      async (signal) => {
        const from = windowStart(windowHours);
        const pages = await Promise.all(withActivityRef.current.map((p) => loadMilestones(client, p.id, from, signal, 2)));
        return pages.flatMap((page) => page.items).sort((a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at));
      },
      [client, windowHours],
    ),
    { intervalMs: ACTIVITY_MS },
  );
  const signature = withActivity.map((p) => `${p.id}:${p.activity?.milestones}`).join(",");
  const refreshRecent = recent.refresh;
  useEffect(() => {
    if (signature) void refreshRecent();
  }, [signature, refreshRecent]);

  const items = attention.data?.items ?? [];
  const all = projects.data?.items ?? [];
  const active = all.filter((p) => isActive(p.status));
  const others = all.filter((p) => !isActive(p.status));
  const names = Object.fromEntries(all.map((p) => [p.id, p.name]));
  const milestones = recent.data ?? [];
  const counts = {
    completed: milestones.filter((m) => m.status === "completed").length,
    failed: milestones.filter((m) => m.status === "failed").length,
    repaired: milestones.filter((m) => m.resolves_milestone_id).length,
    blocked: milestones.filter((m) => m.status === "blocked").length,
    uncertain: items.filter((i) => i.kind === "reconciliation_required").length,
    running: active.length,
  };

  return (
    <>
      <h1 className="visually-hidden">Dashboard</h1>
      <Freshness polls={[attention, projects, recent]} />

      <section aria-labelledby="attention-heading" className={`panel attention-panel${items.length ? " has-items" : ""}`}>
        <h2 id="attention-heading">
          Needs your attention <span className="count">({items.length})</span>
        </h2>
        {attention.loading ? <Loading what="what needs you" /> : <AttentionList items={items.slice(0, 5)} now={now} />}
        <LoadFailure poll={attention} what="the attention inbox" />
        {items.length > 5 ? <Link to={paths.attention()}>See all {items.length} items</Link> : null}
      </section>

      <section aria-labelledby="active-heading" className="panel">
        <h2 id="active-heading">Active projects</h2>
        {projects.loading ? (
          <Loading what="projects" />
        ) : active.length ? (
          <ul className="project-list">
            {active.map((p) => (
              <ProjectCard key={p.id} project={p} now={now} />
            ))}
          </ul>
        ) : (
          <p className="empty">Nothing is running.</p>
        )}
        <LoadFailure poll={projects} what="projects" />
      </section>

      <section aria-labelledby="activity-heading" className="panel">
        <div className="panel-head">
          <h2 id="activity-heading">Recent activity</h2>
          <label className="period">
            Period{" "}
            <select value={windowHours} onChange={(event) => setWindowHours(Number(event.target.value))}>
              {WINDOW_CHOICES.map((choice) => (
                <option key={choice.hours} value={choice.hours}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <dl className="summary" aria-label="Summary of this period">
          <div><dt>Completed</dt><dd>{counts.completed}</dd></div>
          <div><dt>Failed</dt><dd>{counts.failed}</dd></div>
          <div><dt>Repaired</dt><dd>{counts.repaired}</dd></div>
          <div><dt>Blocked</dt><dd>{counts.blocked}</dd></div>
          <div><dt>Uncertain</dt><dd>{counts.uncertain}</dd></div>
          <div><dt>Running</dt><dd>{counts.running}</dd></div>
        </dl>
        {withActivity.length && recent.loading ? (
          <Loading what="recent milestones" />
        ) : (
          <MilestoneList milestones={milestones.slice(0, RECENT_SHOWN)} now={now} projectNames={names} />
        )}
        <LoadFailure poll={recent} what="recent milestones" />
      </section>

      <section aria-labelledby="other-heading" className="panel">
        <h2 id="other-heading">Other projects</h2>
        {others.length ? (
          <ul className="project-list">
            {others.map((p) => (
              <ProjectCard key={p.id} project={p} now={now} />
            ))}
          </ul>
        ) : (
          <p className="empty">No other projects.</p>
        )}
      </section>
    </>
  );
}

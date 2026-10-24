// One project: its status, its runs, what needs you there, and its progress (milestones, never raw logs).
import { useCallback } from "react";
import type { Milestone, Project, RunPage } from "../api/types";
import { loadMilestones } from "../app/data";
import { useClient, useShared, WINDOW_CHOICES } from "../app/context";
import { Freshness, LoadFailure, Loading, StatusBadge, When } from "../components/common";
import { AttentionList, MilestoneList } from "../components/lists";
import { isActive, modelUsageText, runStatus } from "../lib/labels";
import { useNow, usePolling } from "../lib/polling";
import { Link, paths } from "../lib/router";

const ACTIVE_MS = 5_000;
const IDLE_MS = 30_000;

function windowStart(hours: number): string {
  return new Date(Date.now() - hours * 3_600_000).toISOString();
}

export function ProjectDetail({ projectId }: { projectId: string }) {
  const client = useClient();
  const { attention, windowHours, setWindowHours } = useShared();
  const now = useNow();

  const project = usePolling<Project>(
    `project:${projectId}:${windowHours}`,
    useCallback((signal) => client.getProject(projectId, { activity_since: windowStart(windowHours) }, { signal }), [client, projectId, windowHours]),
    { intervalMs: (p) => (isActive(p?.status) ? ACTIVE_MS : IDLE_MS) },
  );
  const active = isActive(project.data?.status);
  const runs = usePolling<RunPage>(
    `runs:${projectId}`,
    useCallback((signal) => client.listRuns(projectId, { limit: 20 }, { signal }), [client, projectId]),
    { intervalMs: () => (active ? ACTIVE_MS : IDLE_MS) },
  );
  const milestones = usePolling<{ items: Milestone[]; truncated: boolean }>(
    `milestones:${projectId}:${windowHours}`,
    useCallback(
      (signal) => loadMilestones(client, projectId, windowStart(windowHours), signal),
      [client, projectId, windowHours],
    ),
    { intervalMs: () => (active ? ACTIVE_MS : IDLE_MS) },
  );

  const items = (attention.data?.items ?? []).filter((item) => item.project_id === projectId);
  const newestFirst = [...(milestones.data?.items ?? [])].reverse();

  if (project.loading) return <Loading what="the project" />;
  if (!project.data) return <LoadFailure poll={project} what="the project" />;
  const p = project.data;
  return (
    <>
      <p className="breadcrumb">
        <Link to={paths.dashboard()}>Dashboard</Link> / Project
      </p>
      <div className="title-row">
        <h1>{p.name}</h1>
        <StatusBadge {...runStatus(p.status)} />
      </div>
      {p.description ? <p className="lede">{p.description}</p> : null}
      <Freshness polls={[project, runs, milestones, attention]} />

      <section aria-labelledby="project-attention" className={`panel attention-panel${items.length ? " has-items" : ""}`}>
        <h2 id="project-attention">
          Needs your attention <span className="count">({items.length})</span>
        </h2>
        <AttentionList items={items} now={now} showProject={false} />
      </section>

      <section aria-labelledby="runs-heading" className="panel">
        <h2 id="runs-heading">Runs</h2>
        {runs.loading ? (
          <Loading what="runs" />
        ) : runs.data?.items.length ? (
          <ol className="run-list">
            {runs.data.items.map((run) => (
              <li key={run.run_id} className="run">
                <div className="run-head">
                  <span className="run-name">
                    Run {run.number} <code>{run.run_id}</code>
                  </span>
                  <StatusBadge {...runStatus(run.status)} />
                </div>
                {run.status_reason ? <p className="run-reason">{run.status_reason}</p> : null}
                <p className="run-meta">
                  Updated <When at={run.updated_at} now={now} />
                  {run.pending_approval_id ? (
                    <>
                      {" · "}
                      <Link to={paths.approval(run.project_id, run.pending_approval_id)}>Open the approval</Link>
                    </>
                  ) : null}
                </p>
                {run.model_usage.calls > 0 ? (
                  <p className="run-meta" data-testid="model-usage">
                    {modelUsageText(run.model_usage)}
                  </p>
                ) : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="empty">No runs yet.</p>
        )}
        <LoadFailure poll={runs} what="runs" />
      </section>

      <section aria-labelledby="progress-heading" className="panel">
        <div className="panel-head">
          <h2 id="progress-heading">Progress</h2>
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
        {milestones.loading ? <Loading what="milestones" /> : <MilestoneList milestones={newestFirst} now={now} />}
        {milestones.data?.truncated ? <p className="help">Showing the earliest 500 milestones of this period.</p> : null}
        <LoadFailure poll={milestones} what="milestones" />
      </section>
    </>
  );
}

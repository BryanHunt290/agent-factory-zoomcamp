// Lists the screens share: attention items, milestones, the execution timeline, and project cards.
import type { AttentionItem, Milestone, ProjectSummary, TimelineEntry } from "../api/types";
import { formatDateTime, formatRelative, shortId } from "../lib/format";
import { ATTENTION_LABEL, isActive, milestoneStatus, runStatus, timelineText, timelineTone } from "../lib/labels";
import { Link, paths } from "../lib/router";
import { StatusBadge, When } from "./common";

export function AttentionList({ items, now, showProject = true }: { items: AttentionItem[]; now: number; showProject?: boolean }) {
  if (!items.length) {
    return <p className="empty">Nothing needs you right now.</p>;
  }
  return (
    <ol className="attention-list">
      {items.map((item) => {
        const reconciliation = item.kind === "reconciliation_required";
        return (
          <li key={`${item.kind}:${item.approval_id}:${item.attempt ?? 0}`} className={`attention-item kind-${item.kind}`}>
            <Link to={paths.approval(item.project_id, item.approval_id)} className="attention-link">
              <span className="attention-kind">
                <span aria-hidden="true">{reconciliation ? "?" : "!"}</span> {ATTENTION_LABEL[item.kind]}
              </span>
              {showProject ? <span className="attention-project">{item.project_name}</span> : null}
              <span className="attention-summary">{item.summary}</span>
              <span className="attention-meta">
                <When at={item.since} now={now} prefix="Waiting since" />
                {item.expires_at ? (
                  <span className="attention-expiry">
                    {" · "}
                    {Date.parse(item.expires_at) > now ? "Expires " : "Expired "}
                    <time dateTime={item.expires_at}>{formatRelative(item.expires_at, now)}</time>
                  </span>
                ) : null}
              </span>
            </Link>
          </li>
        );
      })}
    </ol>
  );
}

export function MilestoneList({ milestones, now, projectNames }: { milestones: Milestone[]; now: number; projectNames?: Record<string, string> }) {
  if (!milestones.length) {
    return <p className="empty">No milestones in this period.</p>;
  }
  return (
    <ol className="milestone-list">
      {milestones.map((milestone) => {
        const status = milestoneStatus(milestone.status);
        return (
          <li key={milestone.id} className="milestone">
            <div className="milestone-head">
              <StatusBadge {...status} />
              <span className="milestone-title">{milestone.title}</span>
              {milestone.resolves_milestone_id ? <span className="repaired">Repaired an earlier failure</span> : null}
            </div>
            {milestone.summary ? <p className="milestone-summary">{milestone.summary}</p> : null}
            <p className="milestone-meta">
              {projectNames ? (
                <>
                  <Link to={paths.project(milestone.project_id)}>{projectNames[milestone.project_id] ?? milestone.project_id}</Link>
                  {" · "}
                </>
              ) : null}
              Run {milestone.run_id} · <When at={milestone.occurred_at} now={now} />
            </p>
          </li>
        );
      })}
    </ol>
  );
}

export function Timeline({ entries }: { entries: TimelineEntry[] }) {
  if (!entries.length) return <p className="empty">Nothing has happened yet.</p>;
  return (
    <ol className="timeline">
      {entries.map((entry, index) => (
        <li key={`${entry.kind}:${entry.at}:${index}`} className={`timeline-entry tone-${timelineTone(entry)}`}>
          <p className="timeline-text">{timelineText(entry)}</p>
          <p className="timeline-meta">
            <time dateTime={entry.at}>{formatDateTime(entry.at)}</time>
            {entry.worker ? (
              <>
                {" · "}by worker <code title={entry.worker.worker_id}>{shortId(entry.worker.worker_id)}</code> (generation{" "}
                {entry.worker.fencing_token})
              </>
            ) : null}
            {entry.until ? (
              <>
                {" · "}
                {entry.kind === "retry_authorized" ? "valid until " : "expires "}
                <time dateTime={entry.until}>{formatDateTime(entry.until)}</time>
              </>
            ) : null}
          </p>
        </li>
      ))}
    </ol>
  );
}

export function ProjectCard({ project, now }: { project: ProjectSummary; now: number }) {
  const status = runStatus(project.status);
  return (
    <li className={`project-card${project.pending_approvals ? " needs-attention" : ""}`}>
      <Link to={paths.project(project.id)} className="project-link">
        <span className="project-name">{project.name}</span>
        <StatusBadge {...status} />
      </Link>
      <p className="project-meta">
        {project.latest_run ? (
          <>
            Run {project.latest_run.number} · updated <When at={project.latest_run.updated_at} now={now} />
          </>
        ) : (
          "No runs yet"
        )}
      </p>
      <p className="project-meta">
        {project.activity
          ? `${project.activity.milestones} milestone${project.activity.milestones === 1 ? "" : "s"} in this period`
          : null}
        {project.pending_approvals ? (
          <span className="project-attention">
            {project.activity ? " · " : ""}
            <span aria-hidden="true">!</span> {project.pending_approvals} waiting for approval
          </span>
        ) : null}
        {isActive(project.status) && !project.pending_approvals ? (project.activity ? " · in progress" : "In progress") : null}
      </p>
    </li>
  );
}

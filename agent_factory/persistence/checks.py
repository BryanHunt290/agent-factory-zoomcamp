"""Developer checks for the PostgreSQL product store (``python -m agent_factory db ...``)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from psycopg import sql

from ..domain import (
    Actor,
    ApprovalOutcome,
    ApprovalService,
    ExactAction,
    Milestone,
    MilestoneCategory,
    MilestoneQuery,
    MilestoneSource,
    MilestoneStatus,
    Project,
    Run,
    milestone_id,
    new_project_id,
    risk_for,
    standard_milestone_policy,
)
from ..domain.approvals import ApprovalRequest, approval_expiry
from ..domain.errors import StorageError
from ..state import RunStatus
from ..util import timestamp, utc_now
from .config import DatabaseConfig
from .migrator import MigrationStatus, migrate, migration_status
from .postgres import connect, open_postgres_store


@dataclass(frozen=True)
class ConnectionReport:
    server_version: str
    user: str
    superuser: bool
    migrations: MigrationStatus


def check_connection(config: DatabaseConfig) -> ConnectionReport:
    """Connect and describe the server, the role, and the schema's migration status. Changes nothing."""
    conn = connect(config)
    try:
        version = conn.execute("SHOW server_version").fetchone()[0]
        user, superuser = conn.execute(
            "SELECT current_user, rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
        ).fetchone()
        return ConnectionReport(version, user, bool(superuser), migration_status(conn, config.schema))
    finally:
        conn.close()


def verify_round_trip(config: DatabaseConfig) -> list[str]:
    """Prove the store persists records across connections, without touching real data.

    Uses a throwaway schema: migrate it, write one of each record through the
    repositories, close the connection, reconnect, read everything back,
    compare, and drop the schema.
    """
    scratch = config.with_schema(f"af_verify_{secrets.token_hex(6)}")
    admin = connect(scratch)
    steps: list[str] = []
    try:
        migrate(admin, scratch.schema)
        steps.append(f"migrated scratch schema {scratch.schema}")
        now = timestamp(utc_now)
        project = Project(
            id=new_project_id(), name="persistence check", created_at=now, milestone_policy=standard_milestone_policy()
        )
        run = Run(project_id=project.id, run_id="verify", number=1, graph_name="software-dev-v1",
                  status=RunStatus.RUNNING, created_at=now, updated_at=now)  # fmt: skip
        milestone = Milestone(
            id=milestone_id(project.id, run.run_id, "step:0"), project_id=project.id, run_id=run.run_id, sequence=1,
            key="step:0", type="plan_ready", category=MilestoneCategory.WORKFLOW, title="Plan ready",
            summary="written by db verify", status=MilestoneStatus.COMPLETED, created_at=now, occurred_at=now,
            source=MilestoneSource(node="planner", agent="planner", step=0), metadata={"check": True},
        )  # fmt: skip
        action = ExactAction(tool="delete_file", category="destructive_delete", params={"path": "example.txt"})
        request = ApprovalRequest(
            id="apr-" + secrets.token_hex(8), project_id=project.id, run_id=run.run_id, node="developer",
            agent="developer", action=action, action_fingerprint=action.fingerprint, reason="db verify",
            risk=risk_for(action.category), requested_at=now, expires_at=approval_expiry(now),
        )  # fmt: skip
        with open_postgres_store(scratch) as first:
            with first.transaction():
                first.projects.add(project)
                first.runs.add(run)
                first.milestones.add(milestone)
            ApprovalService(first.approvals).open_request(request)
            decision = ApprovalService(first.approvals).decide(
                project_id=project.id, request_id=request.id, action_fingerprint=action.fingerprint,
                outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("db verify"), at=now,
            )  # fmt: skip
        steps.append("wrote a project, run, milestone, approval request, and decision; closed the connection")
        with open_postgres_store(scratch) as second:
            found = {
                "project": second.projects.get(project.id) == project,
                "run": second.runs.get(project.id, run.run_id) == run,
                "milestone": second.milestones.query(MilestoneQuery(project_id=project.id)) == [milestone],
                "decision": second.approvals.get_decision(project.id, request.id) == decision,
                "request approved": second.approvals.get_request(project.id, request.id).status.value == "approved",
            }
        missing = [name for name, ok in found.items() if not ok]
        if missing:
            raise StorageError(f"records read back after reconnecting differ from what was written: {missing}")
        steps.append("reconnected and read every record back unchanged")
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(scratch.schema)))
        admin.close()
        steps.append(f"dropped scratch schema {scratch.schema}")
    return steps

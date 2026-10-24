"""Schema migrations, constraints, and indexes against a real PostgreSQL database.

    .venv\\Scripts\\python -m pytest -m postgres --run-postgres    (docs/database.md)

psycopg is imported inside the tests, so the default suite collects this file
without the driver installed.
"""

import re
import threading

import pytest

pytestmark = pytest.mark.postgres

TABLES = {
    "projects", "runs", "milestones", "approval_requests", "approval_decisions", "idempotency_keys", "schema_migrations",
    "action_executions", "action_execution_attempts", "action_reconciliations", "principals", "principal_permissions",
    "run_claims", "reconciliation_decisions", "notification_destinations", "notifications", "notification_attempts", "run_model_usage",
}
#: Every migration in agent_factory/persistence/migrations, in order.
MIGRATIONS = ["initial", "idempotency_keys", "action_executions", "principals", "run_claims", "human_decisions", "notifications", "model_usage",
              "cloud_read_category"]
VERSIONS = list(range(1, len(MIGRATIONS) + 1))
NEXT_VERSION = len(MIGRATIONS) + 1


def _connect(config):
    from agent_factory.persistence.postgres import connect

    return connect(config)


def test_migrations_build_a_fresh_schema(pg_empty_schema):
    from agent_factory.persistence.migrator import available_migrations, migrate, migration_status

    schema = pg_empty_schema.schema
    conn = _connect(pg_empty_schema)
    try:
        before = migration_status(conn, schema)
        assert before.current_version == 0 and [m.name for m in before.pending] == MIGRATIONS
        assert not before.up_to_date
        assert [m.version for m in migrate(conn, schema)] == VERSIONS
        after = migration_status(conn, schema)
        assert after.up_to_date and after.current_version == after.latest_version == len(available_migrations())
        assert after.applied[0].checksum == available_migrations()[0].checksum
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,))}
        assert tables == TABLES
        assert migrate(conn, schema) == []  # repeatable: running it again changes nothing
    finally:
        conn.close()


def test_the_store_refuses_an_unmigrated_schema_and_never_migrates_itself(pg_empty_schema):
    from agent_factory.persistence.migrator import SchemaNotReadyError
    from agent_factory.persistence.postgres import open_postgres_store

    with pytest.raises(SchemaNotReadyError, match="db migrate"):
        open_postgres_store(pg_empty_schema)
    conn = _connect(pg_empty_schema)
    try:
        assert conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = %s", (pg_empty_schema.schema,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_an_edited_migration_is_refused(pg_schema):
    from agent_factory.persistence.migrator import MigrationError, SchemaNotReadyError, migrate
    from agent_factory.persistence.postgres import open_postgres_store

    conn = _connect(pg_schema)
    try:
        conn.execute("UPDATE schema_migrations SET checksum = 'sha256:' || repeat('0', 64) WHERE version = 1")
        with pytest.raises(MigrationError, match="edited"):
            migrate(conn, pg_schema.schema)
        with pytest.raises(SchemaNotReadyError, match="edited"):
            open_postgres_store(pg_schema)
    finally:
        conn.close()


def test_a_newer_database_is_refused(pg_schema):
    from agent_factory.persistence.migrator import MigrationError, SchemaNotReadyError, migrate
    from agent_factory.persistence.postgres import open_postgres_store

    conn = _connect(pg_schema)
    try:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, 'from_the_future', %s)",
            (NEXT_VERSION, "sha256:" + "1" * 64),
        )
        with pytest.raises(MigrationError, match="newer"):
            migrate(conn, pg_schema.schema)
        with pytest.raises(SchemaNotReadyError, match="newer"):
            open_postgres_store(pg_schema)
    finally:
        conn.close()


def test_a_failed_migration_leaves_nothing_behind(pg_empty_schema, monkeypatch):
    from agent_factory.persistence import migrator

    real = migrator.available_migrations()
    broken = migrator.Migration(
        NEXT_VERSION, "broken", "CREATE TABLE half_done (x integer);\nSELECT 1 / 0;\n", "sha256:" + "2" * 64
    )
    monkeypatch.setattr(migrator, "available_migrations", lambda: (*real, broken))
    conn = _connect(pg_empty_schema)
    try:
        with pytest.raises(migrator.MigrationError, match=f"{NEXT_VERSION} \\(broken\\) failed and was rolled back"):
            migrator.migrate(conn, pg_empty_schema.schema)
        status = migrator.migration_status(conn, pg_empty_schema.schema)
        assert status.current_version == VERSIONS[-1] and [m.version for m in status.pending] == [NEXT_VERSION]
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s", (pg_empty_schema.schema,))}
        assert "half_done" not in tables  # transactional DDL: the failed migration's table is gone too
    finally:
        conn.close()


def test_0006_gives_old_pending_requests_the_default_expiry_and_then_requires_one(pg_empty_schema, monkeypatch):
    """A database from before E5 may hold pending requests without an expiry; none may stay decidable for ever."""
    from datetime import timedelta

    import psycopg

    from agent_factory.domain import ApprovalRequest, Project, Run, standard_milestone_policy
    from agent_factory.domain.approvals import ExactAction, approval_expiry, risk_for
    from agent_factory.persistence import migrator
    from agent_factory.persistence.postgres import open_postgres_store
    from agent_factory.state import RunStatus

    real = migrator.available_migrations()
    assert real[5].name == "human_decisions"
    monkeypatch.setattr(migrator, "available_migrations", lambda: real[:5])  # the schema as E4 left it
    conn = _connect(pg_empty_schema)
    try:
        migrator.migrate(conn, pg_empty_schema.schema)
        store = open_postgres_store(pg_empty_schema)
        at = "2026-10-01T08:00:00.000000Z"
        store.projects.add(Project(id="prj-" + "6" * 20, name="old", created_at=at, milestone_policy=standard_milestone_policy()))
        store.runs.add(Run(project_id="prj-" + "6" * 20, run_id="run-1", number=1, graph_name="g",
                           status=RunStatus.AWAITING_APPROVAL, created_at=at, updated_at=at))  # fmt: skip
        ids = []
        for path in ("a.py", "b.py"):
            action = ExactAction(tool="delete_file", category="destructive_delete", params={"path": path})
            request = ApprovalRequest(
                id="apr-" + ("a" if path == "a.py" else "b") * 16, project_id="prj-" + "6" * 20, run_id="run-1",
                node="developer", agent="developer", action=action, action_fingerprint=action.fingerprint, reason="r",
                risk=risk_for(action.category), requested_at=at, expires_at=approval_expiry(at),
            )  # fmt: skip
            store.approvals.add_request(request)
            ids.append(request.id)
        store.close()
        conn.execute("UPDATE approval_requests SET expires_at = NULL")  # stored before requests expired
        conn.execute("UPDATE approval_requests SET status = 'cancelled', updated_at = requested_at WHERE id = %s", (ids[1],))
        monkeypatch.setattr(migrator, "available_migrations", lambda: real[:6])
        assert [m.name for m in migrator.migrate(conn, pg_empty_schema.schema)] == ["human_decisions"]
        rows = dict(conn.execute("SELECT id, expires_at - requested_at FROM approval_requests").fetchall())
        assert rows == {ids[0]: timedelta(hours=12), ids[1]: None}  # pending: 12 hours; closed: as it was
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE approval_requests SET expires_at = NULL WHERE id = %s", (ids[0],))
    finally:
        conn.close()


def test_concurrent_migrators_do_not_interleave(pg_empty_schema):
    from agent_factory.persistence.migrator import migrate, migration_status

    results, errors = [], []

    def run():
        conn = _connect(pg_empty_schema)
        try:
            results.append([m.version for m in migrate(conn, pg_empty_schema.schema)])
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert sorted(results) == [[], [], VERSIONS]  # exactly one migrator applied it; the others waited, then found nothing
    conn = _connect(pg_empty_schema)
    try:
        assert migration_status(conn, pg_empty_schema.schema).up_to_date
    finally:
        conn.close()


def test_schema_vocabularies_match_the_domain_model(pg_schema):
    """Every CHECK (... IN (...)) list equals its Python enum: adding a value needs a migration."""
    from agent_factory.domain import ApprovalOutcome, ApprovalStatus, MilestoneCategory, MilestoneStatus
    from agent_factory.domain.executions import AttemptResult, ExecutionState, ReconciliationChoice
    from agent_factory.domain.identity import ActorKind
    from agent_factory.domain.principals import Permission, PrincipalStatus
    from agent_factory.safety.execution import EffectClass
    from agent_factory.safety.permissions import ActionCategory
    from agent_factory.state import RunStatus

    expected = {
        "runs_status_valid": RunStatus,
        "milestones_category_valid": MilestoneCategory,
        "milestones_status_valid": MilestoneStatus,
        "approval_requests_status_valid": ApprovalStatus,
        "approval_requests_category_valid": ActionCategory,
        "approval_decisions_outcome_valid": ApprovalOutcome,
        "approval_decisions_actor_kind_valid": ActorKind,
        "action_executions_state_valid": ExecutionState,
        "action_executions_effect_valid": EffectClass,
        "action_executions_category_valid": ActionCategory,
        "action_execution_attempts_result_valid": AttemptResult,
        "action_reconciliations_choice_valid": ReconciliationChoice,
        "principals_status_valid": PrincipalStatus,
        "principal_permissions_permission_valid": Permission,
        "reconciliation_decisions_choice_valid": ReconciliationChoice,
    }
    conn = _connect(pg_schema)
    try:
        rows = conn.execute(
            "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = %s AND c.conname = ANY(%s)",
            (pg_schema.schema, list(expected)),
        ).fetchall()
    finally:
        conn.close()
    assert {name for name, _ in rows} == set(expected)
    for name, definition in rows:
        assert set(re.findall(r"'([a-z_:]+)'", definition)) == {e.value for e in expected[name]}, name


def test_constraints_and_indexes_exist(pg_schema):
    conn = _connect(pg_schema)
    try:
        constraints = {
            r[0]
            for r in conn.execute(
                "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = %s",
                (pg_schema.schema,),
            )
        }
        indexes = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = %s", (pg_schema.schema,))}
    finally:
        conn.close()
    assert {
        "runs_project_fkey", "milestones_run_fkey", "milestones_resolves_fkey", "approval_requests_run_fkey",
        "approval_decisions_binding_fkey", "approval_decisions_request_key", "runs_number_key",
        "milestones_sequence_key", "milestones_key_key", "approval_requests_updated_iff_final",
        "action_executions_one_per_approval", "action_executions_approved_binding_fkey",
        "action_executions_request_fkey", "action_executions_run_fkey", "action_executions_non_idempotent_single",
        "action_executions_dispatch_owned", "action_execution_attempts_execution_fkey",
        "action_reconciliations_attempt_fkey", "action_reconciliations_binding_fkey",
        "action_reconciliations_actor_kind_valid", "action_reconciliations_retry_window",
        "principals_external_identity_key", "principals_id_format", "principals_status_valid",
        "principal_permissions_principal_fkey", "principal_permissions_permission_valid",
        "approval_decisions_principal_fkey", "approval_decisions_user_is_principal",
        "run_claims_pkey", "run_claims_run_fkey", "run_claims_worker_id_format", "run_claims_fencing_token_range",
        "run_claims_claimed_iff_generation", "run_claims_owner_has_lease", "run_claims_owner_has_heartbeat",
        "run_claims_lease_window", "run_claims_heartbeat_after_claim", "run_claims_time_order",
        "approval_requests_pending_expires", "reconciliation_decisions_pkey", "reconciliation_decisions_attempt_fkey",
        "reconciliation_decisions_run_fkey", "reconciliation_decisions_binding_fkey",
        "reconciliation_decisions_principal_fkey", "reconciliation_decisions_user_is_principal",
        "action_reconciliations_principal_fkey", "action_reconciliations_user_is_principal",
        "run_claims_wake_seq_valid",
        "notification_destinations_principal_fkey", "notification_destinations_address_key",
        "notification_destinations_policy_valid", "notifications_dedup_key", "notifications_approval_fkey",
        "notifications_attempt_fkey", "notifications_link_path_format", "notifications_sending_has_owner",
        "notifications_lease_window", "notifications_skip_reason", "notification_attempts_ended_iff_outcome",
        "run_model_usage_pkey", "run_model_usage_run_fkey", "run_model_usage_counts_valid", "run_model_usage_cost_valid",
    } <= constraints  # fmt: skip
    assert {
        "milestones_project_occurred_idx", "approval_requests_pending_idx", "action_executions_run_idx",
        "action_execution_attempts_one_open", "run_claims_claimable_idx", "approval_requests_attention_idx",
        "action_executions_uncertain_idx", "notifications_due_idx", "notifications_sending_idx",
        "notifications_approval_idx", "notification_attempts_one_open",
    } <= indexes  # fmt: skip


# Each query Agent Factory makes, and the index(es) documented to serve it.
INDEXED_QUERIES = {
    "milestones in a time range": (
        "SELECT * FROM milestones WHERE project_id = %s AND occurred_at >= now() - interval '10 hours' "
        "AND occurred_at < now() ORDER BY occurred_at, run_id, sequence, id",
        {"milestones_project_occurred_idx"},
    ),
    "pending approvals": (
        "SELECT * FROM approval_requests WHERE project_id = %s AND status = 'pending' ORDER BY requested_at, id",
        # Both partial indexes hold only pending rows; the attention index (0006) orders them globally.
        {"approval_requests_pending_idx", "approval_requests_attention_idx"},
    ),
    "runs of a project": ("SELECT * FROM runs WHERE project_id = %s ORDER BY number", {"runs_number_key"}),
    "milestones of a run": (
        "SELECT * FROM milestones WHERE project_id = %s AND run_id = 'run-1' ORDER BY sequence",
        {"milestones_sequence_key"},
    ),
    "next milestone sequence": (
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM milestones WHERE project_id = %s AND run_id = 'run-1'",
        {"milestones_sequence_key"},
    ),
    # All share the leading column project_id and contain id; the others exist only as targets
    # of composite foreign keys (the decision's binding, the execution's request), and the planner
    # may pick any of them.
    "approval request lookup": (
        "SELECT * FROM approval_requests WHERE project_id = %s AND id = 'apr-0000000000000000'",
        {"approval_requests_pkey", "approval_requests_action_key", "approval_requests_run_member_key"},
    ),
    # approval_decisions_outcome_key (project_id, request_id, ...) is the execution binding's target.
    "decision for a request": (
        "SELECT * FROM approval_decisions WHERE project_id = %s AND request_id = 'apr-0000000000000000'",
        {"approval_decisions_request_key", "approval_decisions_outcome_key"},
    ),
    "project by id": ("SELECT * FROM projects WHERE id = %s", {"projects_pkey"}),
    "executions of a run": (
        "SELECT * FROM action_executions WHERE project_id = %s AND run_id = 'run-1' ORDER BY created_at, id",
        # action_executions_run_member_key (0006) has the same leading columns; it exists as a foreign key target.
        {"action_executions_run_idx", "action_executions_run_member_key"},
    ),
    "execution by id": (
        "SELECT * FROM action_executions WHERE id = 'axe-' || substr(%s, 5)",
        {"action_executions_pkey", "action_executions_fingerprint_key"},
    ),
    "attempts of an execution": (
        "SELECT * FROM action_execution_attempts WHERE execution_id = 'axe-' || substr(%s, 5) ORDER BY attempt",
        {"action_execution_attempts_pkey", "action_execution_attempts_one_open"},
    ),
    "reconciliation of an attempt": (
        "SELECT * FROM action_reconciliations WHERE execution_id = 'axe-' || substr(%s, 5) AND attempt = 1",
        {"action_reconciliations_pkey"},
    ),
    "milestone by id": ("SELECT * FROM milestones WHERE id = 'ms-' || substr(%s, 5)", {"milestones_pkey"}),
    # The attention inbox (0006), one partial index per branch, each already in inbox order.
    "attention inbox: decisions to make": (
        "SELECT * FROM approval_requests WHERE status = 'pending' AND expires_at > now() AND project_id >= %s "
        "ORDER BY requested_at, project_id, id",
        {"approval_requests_attention_idx"},
    ),
    "attention inbox: uncertain actions to answer": (
        "SELECT * FROM action_executions WHERE state = 'uncertain' AND request_id IS NOT NULL AND project_id >= %s "
        "ORDER BY updated_at, project_id, request_id",
        {"action_executions_uncertain_idx"},
    ),
    "decidable approvals across projects (listPendingApprovals without project_id)": (
        "SELECT * FROM approval_requests WHERE status = 'pending' AND expires_at > now() AND %s::text IS NOT NULL "
        "ORDER BY requested_at, project_id, id",
        {"approval_requests_attention_idx"},
    ),
    "the answer recorded for an attempt": (
        "SELECT * FROM reconciliation_decisions WHERE execution_id = 'axe-' || substr(%s, 5) AND attempt = 1",
        {"reconciliation_decisions_pkey"},
    ),
    # The two lookups every authenticated request makes (api/auth/authenticator.py).
    "principal for a token's issuer and subject": (
        "SELECT * FROM principals WHERE issuer = %s AND subject = 'owner-7f3c'",
        {"principals_external_identity_key"},
    ),
    "permissions of a principal": (
        "SELECT permission FROM principal_permissions WHERE principal_id = 'prn-' || substr(%s, 5)",
        {"principal_permissions_pkey"},
    ),
    "principal by id": ("SELECT * FROM principals WHERE id = 'prn-' || substr(%s, 5)", {"principals_pkey"}),
    # The notification outbox (0007).
    "notifications about an approval (the approval view)": (
        "SELECT * FROM notifications WHERE project_id = %s AND approval_id = 'apr-0000000000000000' ORDER BY created_at, id",
        {"notifications_approval_idx"},
    ),
    "notifications whose dispatcher's lease expired": (
        "SELECT * FROM notifications WHERE status = 'sending' AND lease_expires_at <= now() AND id >= %s "
        "ORDER BY lease_expires_at, id",
        {"notifications_sending_idx"},
    ),
    "attempts of a notification": (
        "SELECT * FROM notification_attempts WHERE notification_id = 'ntf-' || substr(%s, 5) ORDER BY attempt",
        {"notification_attempts_pkey", "notification_attempts_one_open"},
    ),
}


@pytest.mark.parametrize("name", sorted(INDEXED_QUERIES))
def test_each_documented_query_is_served_by_its_index(pg_schema, name):
    """With sequential scans priced out, the planner answers the query from a documented index."""
    query, indexes = INDEXED_QUERIES[name]
    conn = _connect(pg_schema)
    try:
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + query, ("prj-" + "a" * 20,)))
    finally:
        conn.close()
    # "Index Scan using <name>", "Index Only Scan using <name>", or "Bitmap Index Scan on <name>".
    assert any(re.search(rf"\b(using|on) {re.escape(index)}\b", plan) for index in indexes), plan


@pytest.mark.parametrize("name", ["claim", "fence check", "renew", "release", "verify"])
def test_the_worker_claim_statements_are_served_by_their_indexes(pg_schema, name):
    """The statements workers run most often, exactly as the code sends them (persistence/claims.py)."""
    from datetime import timedelta

    from agent_factory.persistence import claims

    fence = {"project_id": "prj-" + "a" * 20, "run_id": "run-1", "worker_id": "wkr-" + "b" * 20, "fencing_token": 3}
    statement, params, index = {
        "claim": (claims.CLAIM_NEXT, {"worker_id": fence["worker_id"], "lease": timedelta(seconds=60),
                                      "finished": claims.FINISHED_STATUSES}, "run_claims_claimable_idx"),
        "fence check": (claims.FENCE_CHECK, fence, "run_claims_pkey"),
        "renew": (claims.RENEW, {**fence, "lease": timedelta(seconds=60)}, "run_claims_pkey"),
        "release": (claims.RELEASE, {**fence, "keep": False, "requeue": True, "delay": timedelta(0), "at": None},
                    "run_claims_pkey"),
        "verify": (claims.FENCE_CHECK, fence, "run_claims_pkey"),
    }[name]  # fmt: skip
    conn = _connect(pg_schema)
    try:
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + statement, params))
    finally:
        conn.close()
    assert re.search(rf"\b(using|on) {index}\b", plan), plan

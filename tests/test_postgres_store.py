"""Durable product records in a real PostgreSQL database.

    .venv\\Scripts\\python -m pytest -m postgres --run-postgres    (docs/database.md)

Persistence across reconnects, JSON and UTC round trips, and integrity enforced
by the database itself (raw SQL that bypasses the Python checks entirely).
"""

import threading

import pytest

from agent_factory.domain import (
    Actor,
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalService,
    ApprovalStatus,
    ConflictError,
    ExactAction,
    Milestone,
    MilestoneCategory,
    MilestoneDefinition,
    MilestoneQuery,
    MilestoneSource,
    MilestoneStatus,
    NodeMilestoneRule,
    Project,
    Run,
    decision_id,
    milestone_id,
    milestone_policy,
    new_project_id,
    risk_for,
)
from agent_factory.domain.approvals import ApprovalRequest, approval_expiry
from agent_factory.util import timestamp, utc_now
from agent_factory.domain.errors import StorageUnavailableError
from agent_factory.safety.approval import make_request_id
from agent_factory.state import RunStatus

pytestmark = pytest.mark.postgres

T0 = "2026-10-02T22:00:00.000000Z"


def ts(hour, minute=0, micro=0):
    day, hour = (2, hour) if hour < 24 else (3, hour - 24)
    return f"2026-10-0{day}T{hour:02d}:{minute:02d}:00.{micro:06d}Z"


PIPELINE_POLICY = milestone_policy(
    [MilestoneDefinition("ingestion_working", "Ingestion working", MilestoneCategory.DELIVERABLE, order=1),
     MilestoneDefinition("database_load_verified", "Database load verified", MilestoneCategory.DELIVERABLE, order=2)],
    [NodeMilestoneRule("ingest", "ingestion_working", {"ok": MilestoneStatus.COMPLETED, "error": MilestoneStatus.FAILED}),
     NodeMilestoneRule("load", "database_load_verified", {"ok": MilestoneStatus.COMPLETED})],
)  # fmt: skip


def _project(**kw):
    base = dict(id=new_project_id(), name="Daily sales pipeline", created_at=T0, milestone_policy=PIPELINE_POLICY,
                template="data-pipeline", description="Load sales data", metadata={"owner": "data team"})  # fmt: skip
    base.update(kw)
    return Project(**base)


def _run(project, run_id="run-1", number=1, status=RunStatus.RUNNING, at=T0):
    return Run(project_id=project.id, run_id=run_id, number=number, graph_name="data-pipeline-v1", status=status,
               created_at=at, updated_at=at)  # fmt: skip


def _ms(project, run_id, key, seq, mtype="ingestion_working", status=MilestoneStatus.COMPLETED, at=T0, **kw):
    return Milestone(
        id=milestone_id(project.id, run_id, key), project_id=project.id, run_id=run_id, sequence=seq, key=key,
        type=mtype, category=MilestoneCategory.DELIVERABLE, title=mtype.replace("_", " ").capitalize(), summary="",
        status=status, created_at=at, occurred_at=at, source=MilestoneSource(node="ingest", agent="ingester", step=seq),
        **kw,
    )  # fmt: skip


def _request(project, run_id="run-1", path="legacy.csv", requested_at=T0):
    action = ExactAction(tool="delete_file", category="destructive_delete", params={"path": path})
    return ApprovalRequest(
        id=make_request_id(run_id, "load", 1, "delete_file", {"path": path}), project_id=project.id, run_id=run_id,
        node="load", agent="loader", action=action, action_fingerprint=action.fingerprint,
        reason="destructive_delete requires human approval", risk=risk_for("destructive_delete"),
        requested_at=requested_at, context={"step": 3},
        # Decisions in these tests use the real clock: the request must not have expired by then.
        expires_at=approval_expiry(timestamp(utc_now)),
    )  # fmt: skip


def _principal(subject):
    from agent_factory.domain.ids import new_principal_id
    from agent_factory.domain.principals import Principal, PrincipalStatus

    at = "2026-10-01T00:00:00.000000Z"
    return Principal(id=new_principal_id(), issuer="https://issuer.example.test", subject=subject,
                     status=PrincipalStatus.ACTIVE, created_at=at, updated_at=at)  # fmt: skip


def _seed(store, project, *runs):
    store.projects.add(project)
    for n, run_id in enumerate(runs, start=1):
        store.runs.add(_run(project, run_id, number=n))


# --------------------------------------------------------------- persistence


def test_every_record_survives_closing_and_reconnecting(pg_open):
    project = _project()
    failed = _ms(project, "run-1", "step:0", 1, status=MilestoneStatus.FAILED, metadata={"outcome": "error"})
    repaired = _ms(project, "run-1", "step:2", 2, at=ts(23), resolves_id=failed.id, metadata={"outcome": "ok"})
    other_run = _ms(project, "run-2", "step:0", 1, mtype="database_load_verified", at=ts(25))
    request = _request(project)
    first = pg_open()
    _seed(first, project, "run-1", "run-2")
    for m in (failed, repaired, other_run):
        first.milestones.add(m)
    svc = ApprovalService(first.approvals)
    svc.open_request(request)
    decision = svc.decide(project_id=project.id, request_id=request.id, action_fingerprint=request.action_fingerprint,
                          outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), comment="old file",
                          at=ts(24))  # fmt: skip
    first.close()  # the connection - and with it any in-process state - is gone

    second = pg_open()
    assert second.projects.get(project.id) == project
    assert second.projects.get(project.id).milestone_policy == PIPELINE_POLICY  # project-specific meaning survives
    assert [(r.run_id, r.number) for r in second.runs.list_for_project(project.id)] == [("run-1", 1), ("run-2", 2)]
    assert second.milestones.query(MilestoneQuery(project_id=project.id)) == [failed, repaired, other_run]
    assert second.milestones.get(repaired.id).resolves_id == failed.id
    stored = second.approvals.get_request(project.id, request.id)
    assert stored.action == request.action and stored.action_fingerprint == request.action_fingerprint
    assert stored.status is ApprovalStatus.APPROVED and stored.updated_at == ts(24)
    assert second.approvals.get_decision(project.id, request.id) == decision
    assert second.milestones.next_sequence(project.id, "run-1") == 3


def test_json_metadata_round_trips(pg_open):
    metadata = {
        "text": "quotes \" and \\ backslashes, newline\n, tab\t, unicode: é 中文 🚀,  ",
        "numbers": [0, -7, 2**53 + 1, 10**30, 0.1, 1.5e-300, 2.0],
        "flags": [True, False, None],
        "empty": {"list": [], "object": {}},
        "nested": {"a": {"b": {"c": [1, {"d": "deep"}]}}},
        "big_float": 1e20,
    }
    project = _project(metadata=metadata)
    store = pg_open()
    store.projects.add(project)
    store.close()
    back = pg_open().projects.get(project.id)
    assert back.metadata == metadata
    assert isinstance(back.metadata["numbers"][2], int) and back.metadata["numbers"][3] == 10**30
    # JSON keeps values, not Python's int/float distinction: 1e20 comes back equal, as an integer.
    assert back.metadata["big_float"] == 1e20


def test_timestamps_are_utc_instants_whatever_the_session_time_zone(pg_open, pg_schema):
    from agent_factory.persistence.postgres import connect

    project = _project()
    store = pg_open()
    _seed(store, project, "run-1")
    m = _ms(project, "run-1", "step:0", 1, at="2026-03-08T09:30:00.123456Z")  # a US daylight-saving switch day
    store.milestones.add(m)
    store.unit_of_work.conn.execute("SET TIME ZONE 'Asia/Kolkata'")  # a session in another zone reads the same instant
    assert store.milestones.get(m.id).occurred_at == "2026-03-08T09:30:00.123456Z"
    raw = connect(pg_schema)
    try:
        raw.execute("SET TIME ZONE 'America/Los_Angeles'")
        local, epoch = raw.execute(
            "SELECT occurred_at::text, extract(epoch FROM occurred_at) FROM milestones WHERE id = %s", (m.id,)
        ).fetchone()
    finally:
        raw.close()
    assert local == "2026-03-08 01:30:00.123456-08"  # shown in the session's zone...
    assert float(epoch) == pytest.approx(1772962200.123456)  # ...but stored as one absolute instant


def test_overnight_time_range_queries(pg_store):
    project = _project()
    _seed(pg_store, project, "run-1")
    early = _ms(project, "run-1", "step:0", 1, at=ts(21))
    overnight = _ms(project, "run-1", "step:1", 2, mtype="database_load_verified", status=MilestoneStatus.BLOCKED, at=ts(23))
    for m in (early, overnight):
        pg_store.milestones.add(m)
    pg_store.milestones.update(overnight.transition(MilestoneStatus.COMPLETED, at=ts(27)))  # 03:00 next day
    night = MilestoneQuery(project_id=project.id, since=ts(22), until=ts(31))  # 22:00 - 07:00
    assert [m.key for m in pg_store.milestones.query(night)] == ["step:1"]
    assert pg_store.milestones.query(MilestoneQuery(project_id=project.id, since=ts(27, micro=1))) == []


# ------------------------------------------------------------------ approvals


def test_approval_for_action_a_never_authorizes_action_b(pg_open):
    project = _project()
    store = pg_open()
    _seed(store, project, "run-1")
    svc = ApprovalService(store.approvals)
    a, b = svc.open_request(_request(project, path="a.csv")), svc.open_request(_request(project, path="b.csv"))
    svc.decide(project_id=project.id, request_id=a.id, action_fingerprint=a.action_fingerprint,
               outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"))  # fmt: skip
    with pytest.raises(ApprovalBindingError):
        svc.decide(project_id=project.id, request_id=b.id, action_fingerprint=a.action_fingerprint,
                   outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"))  # fmt: skip
    store.close()
    again = pg_open()
    assert again.approvals.get_request(project.id, b.id).status is ApprovalStatus.PENDING
    assert again.approvals.get_decision(project.id, b.id) is None
    assert again.approvals.list_requests(project.id, status=ApprovalStatus.PENDING) == [again.approvals.get_request(project.id, b.id)]


def test_duplicate_and_conflicting_decisions(pg_open):
    project = _project()
    store = pg_open()
    _seed(store, project, "run-1")
    req = ApprovalService(store.approvals).open_request(_request(project))
    kw = dict(project_id=project.id, request_id=req.id, action_fingerprint=req.action_fingerprint)
    first = ApprovalService(store.approvals).decide(outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), **kw)
    assert ApprovalService(store.approvals).decide(outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"), **kw) == first
    with pytest.raises(ConflictError):
        ApprovalService(store.approvals).decide(outcome=ApprovalOutcome.REJECTED, actor=Actor.unauthenticated("bryan"), **kw)
    with pytest.raises(ConflictError):
        ApprovalService(store.approvals).decide(outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("eve"), **kw)
    assert pg_open().approvals.get_decision(project.id, req.id) == first


def test_concurrent_decisions_from_two_devices(pg_open):
    """Two connections decide the same request at once: exactly one decision is ever stored."""
    project = _project()
    setup = pg_open()
    _seed(setup, project, "run-1")
    req = ApprovalService(setup.approvals).open_request(_request(project))
    deciders = [_principal(f"user-{i}") for i in range(2)]
    for principal in deciders:
        setup.principals.add(principal)
    stores = [pg_open(), pg_open()]
    outcomes = [ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED]
    results, barrier = [None, None], threading.Barrier(2)

    def decide(i):
        barrier.wait()
        try:
            results[i] = ApprovalService(stores[i].approvals).decide(
                project_id=project.id, request_id=req.id, action_fingerprint=req.action_fingerprint,
                outcome=outcomes[i], actor=deciders[i].actor(),
            )  # fmt: skip
        except ConflictError as exc:
            results[i] = exc

    threads = [threading.Thread(target=decide, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    winners = [r for r in results if isinstance(r, ApprovalDecision)]
    assert len(winners) == 1 and sum(isinstance(r, ConflictError) for r in results) == 1
    stored = pg_open().approvals.get_decision(project.id, req.id)
    assert stored == winners[0]
    assert pg_open().approvals.get_request(project.id, req.id).status.value == stored.outcome.value


def test_a_decision_write_that_fails_halfway_leaves_nothing(pg_store, monkeypatch):
    """The decision row and the request's new status commit together, or not at all."""
    from agent_factory.persistence.postgres import PostgresApprovalRepository

    project = _project()
    _seed(pg_store, project, "run-1")
    req = ApprovalService(pg_store.approvals).open_request(_request(project))

    def fail_after_insert(self, request):
        raise StorageUnavailableError("connection lost after the decision row was written")

    monkeypatch.setattr(PostgresApprovalRepository, "_write_status", fail_after_insert)
    with pytest.raises(StorageUnavailableError):
        ApprovalService(pg_store.approvals).decide(
            project_id=project.id, request_id=req.id, action_fingerprint=req.action_fingerprint,
            outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("bryan"),
        )  # fmt: skip
    monkeypatch.undo()
    assert pg_store.approvals.get_decision(project.id, req.id) is None  # the inserted row was rolled back
    assert pg_store.approvals.get_request(project.id, req.id).status is ApprovalStatus.PENDING


# ------------------------------------------- integrity enforced by the database


def test_the_database_itself_enforces_integrity(pg_store, pg_schema):
    """Raw SQL, bypassing every Python check: the schema still refuses invalid states."""
    from psycopg import errors

    from agent_factory.persistence.postgres import connect

    project = _project()
    _seed(pg_store, project, "run-1")
    req = ApprovalService(pg_store.approvals).open_request(_request(project))
    pid, ghost = project.id, "prj-" + "0" * 20
    run_cols = "(project_id, run_id, number, graph_name, status, created_at, updated_at, status_reason)"
    decision_cols = ("(id, project_id, request_id, action_fingerprint, outcome, decided_at, actor_kind, actor_id, "
                     "actor_display_name, actor_verified, comment)")  # fmt: skip
    did = decision_id(pid, req.id)
    cases = [
        (errors.ForeignKeyViolation, f"INSERT INTO runs {run_cols} VALUES ('{ghost}', 'r', 1, 'g', 'running', now(), now(), '')"),
        (errors.ForeignKeyViolation,
         "INSERT INTO milestones (id, project_id, run_id, sequence, key, type, category, title, summary, status, "
         f"created_at, occurred_at, metadata) VALUES ('ms-{'1' * 20}', '{pid}', 'no-such-run', 1, 'k', 't', 'workflow', "
         "'T', '', 'completed', now(), now(), '{}')"),
        (errors.ForeignKeyViolation,
         f"INSERT INTO approval_decisions {decision_cols} VALUES ('apd-{'2' * 20}', '{pid}', 'apr-{'3' * 16}', "
         f"'sha256:{'4' * 64}', 'approved', now(), 'unauthenticated', 'local:x', 'x', false, '')"),
        # Exact-action binding: a decision may only carry the fingerprint its request carries.
        (errors.ForeignKeyViolation,
         f"INSERT INTO approval_decisions {decision_cols} VALUES ('{did}', '{pid}', '{req.id}', 'sha256:{'5' * 64}', "
         "'approved', now(), 'unauthenticated', 'local:x', 'x', false, '')"),
        (errors.UniqueViolation, f"INSERT INTO runs {run_cols} VALUES ('{pid}', 'run-2', 1, 'g', 'running', now(), now(), '')"),
        (errors.UniqueViolation,
         f"INSERT INTO projects (id, name, created_at, description, milestone_policy, metadata) "
         f"VALUES ('{pid}', 'dup', now(), '', '{{}}', '{{}}')"),
        (errors.CheckViolation, f"UPDATE runs SET status = 'paused' WHERE project_id = '{pid}'"),
        (errors.CheckViolation, f"UPDATE approval_requests SET status = 'approved' WHERE id = '{req.id}'"),  # no updated_at
        (errors.CheckViolation, f"INSERT INTO runs {run_cols} VALUES ('{pid}', '../etc', 2, 'g', 'running', now(), now(), '')"),
    ]
    raw = connect(pg_schema)
    try:
        for error, statement in cases:
            with pytest.raises(error):
                with raw.transaction():
                    raw.execute(statement)
        # A real decision, then: one per request, and the request's fingerprint is frozen.
        raw.execute(f"INSERT INTO approval_decisions {decision_cols} VALUES (%s, %s, %s, %s, 'approved', now(), "
                    "'unauthenticated', 'local:x', 'x', false, '')", (did, pid, req.id, req.action_fingerprint))  # fmt: skip
        with pytest.raises(errors.UniqueViolation):
            raw.execute(f"INSERT INTO approval_decisions {decision_cols} VALUES (%s, %s, %s, %s, 'rejected', now(), "
                        "'unauthenticated', 'local:y', 'y', false, '')", ("apd-" + "6" * 20, pid, req.id, req.action_fingerprint))  # fmt: skip
        with pytest.raises(errors.ForeignKeyViolation):
            raw.execute("UPDATE approval_requests SET action_fingerprint = %s WHERE id = %s", ("sha256:" + "7" * 64, req.id))
    finally:
        raw.close()


def test_a_tampered_row_fails_validation_when_read(pg_store, pg_schema):
    """Defence in depth: a row edited behind the domain's back no longer loads."""
    from agent_factory.domain import DomainValidationError
    from agent_factory.persistence.postgres import connect

    project = _project()
    _seed(pg_store, project, "run-1")
    req = ApprovalService(pg_store.approvals).open_request(_request(project))
    raw = connect(pg_schema)
    try:  # change the action but not its fingerprint (no decision references it yet)
        raw.execute("UPDATE approval_requests SET action_params = '{\"path\": \"other.csv\"}' WHERE id = %s", (req.id,))
    finally:
        raw.close()
    with pytest.raises(DomainValidationError, match="does not match the action"):
        pg_store.approvals.get_request(project.id, req.id)


# ------------------------------------------------------------- availability


def test_a_lost_connection_is_reported_as_storage_unavailable(pg_store, pg_schema):
    from agent_factory.persistence.postgres import connect

    project = _project()
    pg_store.projects.add(project)
    pid = pg_store.unit_of_work.conn.info.backend_pid
    admin = connect(pg_schema)
    try:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    finally:
        admin.close()
    with pytest.raises(StorageUnavailableError, match="unavailable"):
        pg_store.projects.get(project.id)


def test_connection_errors_never_reveal_the_password(pg_schema):
    from urllib.parse import urlsplit

    from agent_factory.persistence.postgres import open_postgres_store

    real = urlsplit(pg_schema.url)
    wrong = pg_schema.url.replace(f":{real.password}@", ":wrong-canary-password@")
    with pytest.raises(StorageUnavailableError) as info:
        open_postgres_store(pg_schema.__class__(url=wrong, schema=pg_schema.schema))
    text = str(info.value) + repr(info.value)
    assert "wrong-canary-password" not in text and real.password not in text
    assert info.value.__cause__ is None and info.value.__suppress_context__

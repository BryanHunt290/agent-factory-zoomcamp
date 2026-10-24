"""Principals and permissions in both stores: identity, uniqueness, vocabulary, durability.

The PostgreSQL variants also re-read through a brand-new connection (as an API
restart would) and check that the database itself refuses invalid rows.
"""

import pytest

from agent_factory.domain.errors import ConflictError, DomainValidationError, RecordNotFoundError
from agent_factory.domain.ids import new_principal_id
from agent_factory.domain.principals import ALL_PERMISSIONS, Permission, Principal, PrincipalStatus
from agent_factory.domain.repositories import in_memory_store

ISSUER = "https://issuer.example.test"
T0 = "2026-10-01T09:00:00.000000Z"
T1 = "2026-10-02T09:00:00.000000Z"


def principal(subject="owner-1", issuer=ISSUER, **kwargs):
    values = dict(id=new_principal_id(), issuer=issuer, subject=subject, status=PrincipalStatus.ACTIVE,
                  created_at=T0, updated_at=T0, display_name="Owner", email="owner@example.test")  # fmt: skip
    return Principal(**{**values, **kwargs})


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def stores(request):
    """(store, reopen): ``reopen()`` reads through a new connection where the store has one."""
    if request.param == "memory":
        store = in_memory_store()
        return store, lambda: store
    pg_open = request.getfixturevalue("pg_open")
    return pg_open(), pg_open


def test_issuer_and_subject_map_to_one_stable_principal(stores):
    store, _ = stores
    owner = principal()
    store.principals.add(owner)
    assert store.principals.find(ISSUER, "owner-1") == owner
    assert store.principals.find(ISSUER, "owner-2") is None
    assert store.principals.find("https://other.example.test", "owner-1") is None  # a subject means nothing alone
    with pytest.raises(ConflictError):
        store.principals.add(principal())  # the same issuer and subject, a new id
    with pytest.raises(ConflictError):
        store.principals.add(principal(subject="someone-else", id=owner.id))  # the same id
    namesake = principal(issuer="https://other.example.test")  # same subject, other issuer: another person
    store.principals.add(namesake)
    assert namesake.id != owner.id and store.principals.find(namesake.issuer, "owner-1") == namesake


def test_email_and_display_name_change_but_identity_never_does(stores):
    store, _ = stores
    owner = principal()
    store.principals.add(owner)
    renamed = owner.with_profile(display_name="Renamed", email="new@example.test", at=T1)
    store.principals.update(renamed)
    assert store.principals.find(ISSUER, "owner-1") == renamed and renamed.id == owner.id
    for changed in ({"subject": "owner-2"}, {"issuer": "https://other.example.test"}, {"created_at": T1}):
        moved = Principal(**{**vars(renamed), **changed})
        with pytest.raises(DomainValidationError, match="identity never changes"):
            store.principals.update(moved)
    with pytest.raises(DomainValidationError, match="backwards"):
        store.principals.update(renamed.with_profile(display_name="Older", email=None, at=T0))


def test_permissions_come_from_a_closed_vocabulary_and_need_an_enrolled_principal(stores):
    store, _ = stores
    owner = principal()
    store.principals.add(owner)
    store.principals.grant(owner.id, [Permission.APPROVALS_READ], at=T0)
    store.principals.grant(owner.id, [Permission.APPROVALS_READ, Permission.RUNS_READ], at=T1)  # idempotent
    assert store.principals.permissions(owner.id) == {Permission.APPROVALS_READ, Permission.RUNS_READ}
    store.principals.revoke(owner.id, [Permission.RUNS_READ, Permission.ACTIONS_RECONCILE])  # not held: ignored
    assert store.principals.permissions(owner.id) == {Permission.APPROVALS_READ}
    with pytest.raises(DomainValidationError):
        store.principals.grant(owner.id, ["admin:everything"], at=T0)
    with pytest.raises(RecordNotFoundError):
        store.principals.grant(new_principal_id(), [Permission.RUNS_READ], at=T0)
    assert store.principals.permissions(new_principal_id()) == frozenset()


def test_identity_permissions_and_disabled_state_survive_a_reconnect(stores):
    store, reopen = stores
    owner, reader = principal(), principal(subject="reader-1")
    for p in (owner, reader):
        store.principals.add(p)
    store.principals.grant(owner.id, ALL_PERMISSIONS, at=T0)
    store.principals.grant(reader.id, [Permission.PROJECTS_READ], at=T0)
    store.principals.update(reader.with_status(PrincipalStatus.DISABLED, T1))
    fresh = reopen()
    assert fresh.principals.find(ISSUER, "owner-1") == owner
    assert fresh.principals.permissions(owner.id) == ALL_PERMISSIONS
    assert fresh.principals.get(reader.id).status is PrincipalStatus.DISABLED
    assert fresh.principals.permissions(reader.id) == {Permission.PROJECTS_READ}
    assert [p.id for p in fresh.principals.list()] == sorted([owner.id, reader.id])  # same created_at: by id


@pytest.mark.parametrize(
    "invalid",
    [{"id": "user-1"}, {"issuer": "has space"}, {"subject": ""}, {"subject": "x" * 256}, {"status": "admin"},
     {"display_name": ""}, {"email": "x" * 321}, {"updated_at": "2026-09-01T00:00:00.000000Z"}],  # fmt: skip
)
def test_the_domain_refuses_invalid_principals(invalid):
    with pytest.raises(DomainValidationError):
        principal(**invalid)


# ------------------------------------------------------------- PostgreSQL only


def _raw(pg_schema):
    from agent_factory.persistence.postgres import connect

    return connect(pg_schema)


@pytest.mark.postgres
@pytest.mark.parametrize(
    "column, value",
    [("id", "user-1"), ("issuer", "has space"), ("subject", "x" * 256), ("status", "admin"), ("issuer", "i" * 513)],
)
def test_the_database_refuses_invalid_principal_rows(pg_schema, column, value):
    import psycopg

    row = {"id": new_principal_id(), "issuer": ISSUER, "subject": "s", "status": "active"}
    row[column] = value
    conn = _raw(pg_schema)
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO principals (id, issuer, subject, status, created_at, updated_at) "
                "VALUES (%(id)s, %(issuer)s, %(subject)s, %(status)s, now(), now())",
                row,
            )
    finally:
        conn.close()


@pytest.mark.postgres
def test_the_database_refuses_unknown_permissions_and_orphan_grants(pg_schema, pg_store):
    import psycopg

    owner = principal()
    pg_store.principals.add(owner)
    conn = _raw(pg_schema)
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO principal_permissions VALUES (%s, 'admin:everything', now())", (owner.id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("INSERT INTO principal_permissions VALUES (%s, 'runs:read', now())", (new_principal_id(),))
    finally:
        conn.close()


@pytest.mark.postgres
def test_a_user_decision_must_name_its_enrolled_principal(pg_schema, pg_store):
    """approval_decisions: a user actor carries principal_id = actor_id of an enrolled principal; nobody else does."""
    import psycopg
    from api_support import add_approval, add_project, add_run

    project = add_project(pg_store)
    add_run(pg_store, project)
    request = add_approval(pg_store, project)
    owner = principal()
    pg_store.principals.add(owner)
    base = dict(id="apd-" + "1" * 20, project_id=project.id, request_id=request.id, fp=request.action_fingerprint)

    def insert(conn, kind, actor_id, verified, principal_id):
        conn.execute(
            "INSERT INTO approval_decisions (id, project_id, request_id, action_fingerprint, outcome, decided_at, "
            "actor_kind, actor_id, actor_display_name, actor_verified, comment, principal_id) VALUES "
            "(%(id)s, %(project_id)s, %(request_id)s, %(fp)s, 'approved', now(), %(kind)s, %(actor_id)s, 'x', "
            "%(verified)s, '', %(principal_id)s)",
            {**base, "kind": kind, "actor_id": actor_id, "verified": verified, "principal_id": principal_id},
        )

    conn = _raw(pg_schema)
    try:
        for kind, actor_id, verified, principal_id, error in (
            ("user", owner.id, True, None, psycopg.errors.CheckViolation),  # a user without a principal
            ("user", owner.id, False, owner.id, psycopg.errors.CheckViolation),  # not verified
            ("user", owner.id, True, new_principal_id(), psycopg.errors.CheckViolation),  # another id
            ("unauthenticated", "local:x", False, owner.id, psycopg.errors.CheckViolation),  # only users have one
        ):
            with pytest.raises(error):
                insert(conn, kind, actor_id, verified, principal_id)
        ghost = new_principal_id()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            insert(conn, "user", ghost, True, ghost)  # not enrolled
        insert(conn, "user", owner.id, True, owner.id)  # the one valid shape
    finally:
        conn.close()

"""The repositories used by the HTTP API have one contract in both stores."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_factory.domain import (
    Actor, ApprovalOutcome, ApprovalService, ApprovalStatus, ConflictError,
    DomainValidationError, IdempotencyRecord, MilestoneCategory, MilestoneStatus,
    in_memory_store, request_hash,
)
from agent_factory.domain.repositories import IdempotencyRepository, MilestoneQuery
from test_domain_approvals import _request
from test_domain_repositories import P1, P2, _ms, _project, _run, seed, ts

P3 = "prj-" + "f6" * 10
KEY = "create-project-0001"


def idempotency_record(**changes):
    values = dict(
        scope="create_project", key=KEY, request_hash=request_hash("create_project", {"name": "Calc"}),
        response_status=201, response_body={"id": P1, "name": "Calc"}, created_at=ts(1), expires_at=ts(2),
    )
    values.update(changes)
    return IdempotencyRecord(**values)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request):
    return in_memory_store() if request.param == "memory" else request.getfixturevalue("pg_store")


def test_request_hash_covers_scope_and_canonical_body():
    first = request_hash("create_project", {"name": "Calc", "description": "Demo"})
    assert first == request_hash("create_project", {"description": "Demo", "name": "Calc"})
    assert first != request_hash("another_operation", {"name": "Calc", "description": "Demo"})
    assert first != request_hash("create_project", {"name": "Other", "description": "Demo"})


@pytest.mark.parametrize("changes", [
    {"request_hash": "sha256:" + "z" * 64}, {"request_hash": "sha256:" + "A" * 64},
    {"request_hash": "sha256:" + "0" * 64 + "\n"}, {"key": "too-short"},
    {"response_status": 199}, {"response_status": 300}, {"response_status": True},
    {"expires_at": ts(1)}, {"response_body": {"secret": "\x00"}},
])
def test_idempotency_records_reject_invalid_storage_values(changes):
    with pytest.raises(DomainValidationError):
        idempotency_record(**changes)


def test_idempotency_response_is_an_independent_copy():
    body = {"milestone_types": [{"name": "first"}]}
    record = idempotency_record(response_body=body)
    body["milestone_types"][0]["name"] = "changed input"
    response = record.response()
    response["milestone_types"][0]["name"] = "changed output"
    assert record.response() == {"milestone_types": [{"name": "first"}]}
    assert not record.is_expired(ts(1, 59)) and record.is_expired(ts(2))


def test_project_pages_are_stable_when_new_projects_arrive(store):
    for pid in (P1, P2):
        store.projects.add(_project(pid, at=ts(1)))
    first = store.projects.page(limit=1)
    assert [p.id for p in first] == [P2]
    store.projects.add(_project(P3, at=ts(2)))
    rest = store.projects.page(limit=2, after=(first[-1].created_at, first[-1].id))
    assert [p.id for p in rest] == [P1]
    assert store.projects.page(limit=2, after=(rest[-1].created_at, rest[-1].id)) == []
    assert store.projects.get_many([P1, P1, "prj-" + "0" * 20]) == {P1: _project(P1, at=ts(1))}
    assert store.projects.get_many([]) == {}


def test_run_pages_and_batch_latest_are_scoped_and_newest_first(store):
    seed(store, projects=[P1, P2, P3])
    for number in (1, 2, 3):
        store.runs.add(_run(P1, f"run-{number}", number=number))
    store.runs.add(_run(P2, "run-1"))
    assert [r.number for r in store.runs.page_for_project(P1, limit=2)] == [3, 2]
    assert [r.number for r in store.runs.page_for_project(P1, limit=2, before_number=2)] == [1]
    assert store.runs.page_for_project(P1, limit=1, before_number=1) == []
    latest = store.runs.latest_for_projects([P1, P2, P3, P1])
    assert {p: r.number for p, r in latest.items()} == {P1: 3, P2: 1}
    assert store.runs.latest_for_projects([]) == {}


def test_milestone_pages_use_complete_sort_key_and_filters(store):
    seed(store, (P1, "Run-B"), (P1, "run-a"), (P2, "Run-B"))
    milestones = [
        _ms(P1, "Run-B", "one", 1),
        replace(_ms(P1, "Run-B", "two", 2), category=MilestoneCategory.DELIVERABLE),
        _ms(P1, "run-a", "one", 1),
        _ms(P2, "Run-B", "one", 1),
    ]
    for milestone in reversed(milestones):
        store.milestones.add(milestone)
    query = MilestoneQuery(project_id=P1)
    first = store.milestones.page(query, limit=2)
    assert first == milestones[:2]
    last = first[-1]
    assert store.milestones.page(query, limit=2, after=(last.occurred_at, last.run_id, last.sequence)) == [milestones[2]]
    filtered = MilestoneQuery(
        project_id=P1, run_id="Run-B", categories=frozenset({MilestoneCategory.DELIVERABLE}),
        statuses=frozenset({MilestoneStatus.COMPLETED}), since=ts(1), until=ts(2),
    )
    assert store.milestones.page(filtered, limit=10) == [milestones[1]]
    assert store.milestones.page(replace(filtered, categories=frozenset()), limit=10) == []
    assert store.milestones.count_since([P1, P2, P3, P1], ts(1)) == {P1: 3, P2: 1}
    assert store.milestones.count_since([P1], ts(2)) == {}
    assert store.milestones.count_since([], ts(1)) == {}


def test_pending_inbox_pages_and_counts_span_projects_without_decided_requests(store):
    seed(store, (P1, "run-1"), (P2, "run-1"), (P3, "run-1"))
    requests = [_request(path, project_id=pid, requested_at=ts(1)) for pid, path in (
        (P2, "b.py"), (P1, "b.py"), (P1, "a.py"), (P3, "decided.py"),
    )]
    for request in requests:
        store.approvals.add_request(request)
    ApprovalService(store.approvals).decide(
        project_id=P3, request_id=requests[-1].id, action_fingerprint=requests[-1].action_fingerprint,
        outcome=ApprovalOutcome.REJECTED, actor=Actor.unauthenticated("owner"), at=ts(2),
    )
    expected = sorted(requests[:-1], key=lambda r: (r.requested_at, r.project_id, r.id))
    first = store.approvals.page_pending(limit=2)
    assert first == expected[:2]
    last = first[-1]
    assert store.approvals.page_pending(limit=2, after=(last.requested_at, last.project_id, last.id)) == expected[2:]
    assert store.approvals.page_pending(limit=10, project_id=P2) == [requests[0]]
    assert store.approvals.pending_counts([P1, P2, P3, P1]) == {P1: 2, P2: 1}
    assert store.approvals.pending_counts([]) == {}


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_repository_page_limits_are_validated(store, limit):
    queries = [
        lambda: store.projects.page(limit=limit),
        lambda: store.runs.page_for_project(P1, limit=limit),
        lambda: store.milestones.page(MilestoneQuery(project_id=P1), limit=limit),
        lambda: store.approvals.page_pending(limit=limit),
        lambda: store.idempotency.purge_expired(at=ts(2), limit=limit),
    ]
    for query in queries:
        with pytest.raises(DomainValidationError):
            query()


def test_idempotency_effect_and_response_commit_or_rollback_together(store):
    assert isinstance(store.idempotency, IdempotencyRepository)
    record = idempotency_record()
    with pytest.raises(RuntimeError, match="interrupted"):
        with store.transaction():
            store.projects.add(_project(P1))
            store.idempotency.add(record)
            raise RuntimeError("interrupted")
    assert store.projects.get(P1) is None and store.idempotency.get(record.scope, record.key) is None
    with store.transaction():
        store.projects.add(_project(P1))
        store.idempotency.add(record)
    with pytest.raises(ConflictError):
        with store.transaction():
            store.projects.add(_project(P2))
            store.idempotency.add(replace(record, response_body={"id": P2}))
    assert store.projects.get(P2) is None
    assert store.idempotency.get(record.scope, record.key) == record
    store.idempotency.add(replace(record, scope="another_operation"))
    assert store.idempotency.get("another_operation", record.key) is not None


def test_expired_idempotency_keys_can_be_reused_and_purging_is_bounded(store):
    first = idempotency_record()
    store.idempotency.add(first)
    assert not store.idempotency.remove_expired(first.scope, first.key, at=ts(1, 59))
    assert store.idempotency.remove_expired(first.scope, first.key, at=ts(2))
    assert not store.idempotency.remove_expired(first.scope, first.key, at=ts(2))
    second = replace(first, created_at=ts(2), expires_at=ts(4), response_body={"id": P2})
    store.idempotency.add(second)
    for n in range(3):
        store.idempotency.add(replace(first, key=f"expired-project-{n:04d}"))
    assert store.idempotency.purge_expired(at=ts(2), limit=2) == 2
    assert store.idempotency.purge_expired(at=ts(2), limit=2) == 1
    assert store.idempotency.get(first.scope, first.key) == second


def test_decision_result_distinguishes_recording_from_replay(store):
    seed(store, (P1, "run-1"))
    request = _request(project_id=P1)
    store.approvals.add_request(request)
    service = ApprovalService(store.approvals)
    values = dict(
        project_id=P1, request_id=request.id, action_fingerprint=request.action_fingerprint,
        outcome=ApprovalOutcome.APPROVED, actor=Actor.unauthenticated("owner"), at=ts(2),
    )
    first, second = service.record(**values), service.record(**values)
    assert first.recorded_now and not second.recorded_now
    assert first.decision == second.decision


def test_memory_readers_never_replay_a_response_from_a_rolled_back_transaction():
    store = in_memory_store()
    attempted = threading.Event()

    def read():
        attempted.set()
        return store.idempotency.get("create_project", KEY), store.projects.get(P1)

    with ThreadPoolExecutor(max_workers=1) as threads:
        with pytest.raises(RuntimeError, match="rollback"):
            with store.transaction():
                store.projects.add(_project(P1))
                store.idempotency.add(idempotency_record())
                result = threads.submit(read)
                assert attempted.wait(2)
                # The future must not return the uncommitted response while the writer owns the transaction.
                with pytest.raises(TimeoutError):
                    result.result(timeout=0.05)
                raise RuntimeError("rollback")
        assert result.result(timeout=2) == (None, None)

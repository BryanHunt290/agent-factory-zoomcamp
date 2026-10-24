"""The whole human-in-the-loop path, end to end: an authenticated person over HTTP, then a real worker.

    the run pauses -> the approval is stored -> a person decides over HTTP (approvals:decide)
    -> the decision is stored and the run woken (one transaction) -> a worker claims the run
    -> the worker re-checks the exact pending action -> ActionExecution -> the graph resumes

and, for an action whose outcome is unknown:

    awaiting_reconciliation -> the inbox lists it -> a person answers over HTTP (actions:reconcile)
    -> a worker applies the answer through the ledger -> the run continues

In memory (default suite) and on PostgreSQL through the production pool and
fenced worker sessions (``-m postgres --run-postgres``). Every HTTP response
is validated against openapi.yaml; the oracle counts real effects.
"""

import pytest

pytest.importorskip("fastapi", reason="API tests need the API packages: run with .venv (requirements-api.txt)")
pytest.importorskip("jsonschema", reason="API tests validate against openapi.yaml: run with .venv (requirements-dev.txt)")

from api_support import ContractClient, application  # noqa: E402
from auth_support import ensure_owner  # noqa: E402
from decision_support import Flow, claimed  # noqa: E402

from agent_factory.api.stores import InMemoryStoreProvider  # noqa: E402
from agent_factory.state import RunStatus  # noqa: E402


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request, tmp_path):
    """(flow factory, API client factory) over one store."""
    if request.param == "memory":
        def flow_for(kind):
            flow = Flow.memory(tmp_path, kind)
            ensure_owner(flow.store)
            return flow, lambda: ContractClient(application(InMemoryStoreProvider(flow.store), clock=flow.clock))

        yield flow_for
        return
    from agent_factory.persistence.pool import PostgresStorePool

    config = request.getfixturevalue("pg_schema")
    seed = request.getfixturevalue("pg_store")
    ensure_owner(seed)
    pools = []

    def flow_for(kind):
        flow = Flow.postgres(tmp_path, config, seed, kind)

        def client():
            pool = PostgresStorePool(config, min_size=1, max_size=4, timeout=5.0)
            pools.append(pool)
            return ContractClient(application(pool, clock=flow.clock))

        return flow, client

    yield flow_for
    for pool in pools:
        pool.close()


def approval_url(flow):
    return f"/api/v1/projects/{flow.project_id}/approvals/{flow.request().id}"


def view(client, flow):
    return client.get(approval_url(flow)).json()


def test_an_http_approval_is_consumed_by_a_worker_and_the_action_runs_once(backend):
    flow, client_for = backend("notice")
    flow.paused()
    with client_for() as client:
        inbox = client.get("/api/v1/attention").json()["items"]
        assert [i["kind"] for i in inbox] == ["decision_required"]
        before = view(client, flow)
        assert before["attention"] == "decision_required" and before["execution"] is None
        body = {"outcome": "approved", "action_fingerprint": before["action_fingerprint"], "comment": "Go ahead."}
        decided = client.post(approval_url(flow) + "/decision", json=body)
        assert decided.status_code == 201
        assert decided.json()["execution"] is None  # the API executed nothing
        assert flow.notices() == 0 and flow.checkpoint().status is RunStatus.AWAITING_APPROVAL
        events = flow.work()
        assert claimed(events) is not None
        assert flow.checkpoint().status is RunStatus.COMPLETED and flow.notices() == 1
        after = view(client, flow)
        assert client.get("/api/v1/attention").json()["items"] == []
    assert after["execution"]["state"] == "succeeded" and after["attention"] is None
    kinds = [s["kind"] for s in after["timeline"]]
    assert kinds == ["approval_requested", "approved", "action_dispatching", "action_succeeded", "resolved"]
    approved, dispatched = after["timeline"][1], after["timeline"][2]
    assert approved["actor"]["kind"] == "user" and approved["actor"]["verified"] is True
    assert dispatched["worker"] == {"worker_id": events[0]["worker_id"], "fencing_token": claimed(events)["fencing_token"]}
    assert flow.work() and flow.notices() == 1  # nothing consumes it twice


def test_an_http_rejection_reaches_the_engine_and_nothing_runs(backend):
    flow, client_for = backend("notice")
    flow.paused()
    with client_for() as client:
        fingerprint = view(client, flow)["action_fingerprint"]
        body = {"outcome": "rejected", "action_fingerprint": fingerprint}
        assert client.post(approval_url(flow) + "/decision", json=body).status_code == 201
        flow.work()
        after = view(client, flow)
    assert flow.checkpoint().status is RunStatus.COMPLETED and flow.notices() == 0
    assert [s["kind"] for s in after["timeline"]] == ["approval_requested", "rejected"] and after["execution"] is None


def test_an_uncertain_action_is_answered_over_http_and_the_retry_runs_once(backend):
    flow, client_for = backend("unknown-notice")
    flow.uncertain()
    with client_for() as client:
        [item] = client.get("/api/v1/attention").json()["items"]
        assert item["kind"] == "reconciliation_required" and item["attempt"] == 1
        execution = view(client, flow)["execution"]
        assert execution["state"] == "uncertain"
        body = {
            "execution_id": item["execution_id"], "attempt": item["attempt"],
            "action_fingerprint": view(client, flow)["action_fingerprint"], "choice": "did_not_happen_retry",
        }  # fmt: skip
        answered = client.post(approval_url(flow) + "/reconciliation", json=body)
        assert answered.status_code == 201 and answered.json()["execution"]["state"] == "uncertain"
        assert flow.notices() == 1  # recording the answer sent nothing
        flow.work()
        after = view(client, flow)
    assert flow.checkpoint().status is RunStatus.COMPLETED and flow.notices() == 2  # exactly one more attempt
    assert after["execution"]["state"] == "succeeded" and after["execution"]["attempt"] == 2
    assert [s["kind"] for s in after["timeline"]] == [
        "approval_requested", "approved", "action_dispatching", "outcome_uncertain", "reconciliation_required",
        "reconciliation_decided", "retry_authorized", "retry_dispatched", "action_succeeded", "resolved",
    ]  # fmt: skip
    assert after["execution"]["reconciliation"]["applied"] is True

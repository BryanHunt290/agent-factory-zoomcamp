"""Notification status in the API (openapi.yaml NotificationStatus; docs/notifications.md).

Read only: ``getApproval`` and ``listPendingApprovals`` report, per attention event, whether people were
told. Responses are validated against the checked-in openapi.yaml (``ContractClient``). They never contain
a recipient, an address, a provider, or a message, and no route sends anything.
"""

import random

from api_support import NOW, ContractClient, add_approval, add_project, add_run, application
from auth_support import ensure_owner
from notification_support import destination

from agent_factory.api.stores import InMemoryStoreProvider
from agent_factory.domain.attention import AttentionItem, AttentionKind
from agent_factory.domain.notification_repositories import InMemoryNotificationOutbox
from agent_factory.domain.notifications import NotificationIntent, new_dispatcher_id
from agent_factory.notifications import DispatcherSettings, FakeNotificationProvider, NotificationDispatcher

ADDRESS = "private.owner@example.test"


def setup(script=()):
    provider_store = InMemoryStoreProvider()
    store = provider_store.store
    owner = ensure_owner(store)
    destination(store, owner, address=ADDRESS)
    project = add_project(store)
    add_run(store, project)
    request = add_approval(store, project, at="2026-10-02T11:30:00.000000Z")
    item = AttentionItem(kind=AttentionKind.DECISION_REQUIRED, project_id=project.id, run_id="run-1", approval_id=request.id,
                         since=request.requested_at, tool=request.action.tool, category=request.action.category,
                         expires_at=request.expires_at)  # fmt: skip
    with store.transaction():
        store.notifications.enqueue(NotificationIntent.for_item(item, project.name, at="2026-10-02T11:30:00.000000Z"))
    outbox = InMemoryNotificationOutbox(store.unit_of_work, lambda: NOW)
    did = new_dispatcher_id()
    dispatcher = NotificationDispatcher(did, DispatcherSettings(dashboard_url="http://127.0.0.1:4173"), open_outbox=lambda: outbox,
                                        provider=FakeNotificationProvider(script), rng=random.Random(1))  # fmt: skip
    return provider_store, project, request, dispatcher


def test_the_approval_shows_whether_people_were_told_and_never_to_whom():
    stores, project, request, dispatcher = setup()
    with ContractClient(application(stores)) as client:
        queued = client.get(f"/api/v1/projects/{project.id}/approvals/{request.id}").json()
        assert [(n["about"], n["state"], n["deliveries"]) for n in queued["notifications"]] == [("decision_required", "queued", 1)]
        dispatcher.run_once()
        body = client.get(f"/api/v1/projects/{project.id}/approvals/{request.id}")
        listed = client.get("/api/v1/approvals?status=pending").json()["items"]
    [status] = body.json()["notifications"]
    assert status == {
        "about": "decision_required", "attempt": None, "state": "sent", "deliveries": 1, "sent": 1, "failed": 0,
        "created_at": "2026-10-02T11:30:00.000000Z", "last_attempt_at": "2026-10-02T12:00:00.000000Z",
        "sent_at": "2026-10-02T12:00:00.000000Z", "outcome_unknown": False, "failure": None,
    }  # fmt: skip
    assert listed[0]["notifications"] == [status]
    for text in (body.text, str(listed)):
        assert ADDRESS not in text and "private.owner" not in text and "fake" not in text and "nds-" not in text


def test_a_failed_notification_is_shown_and_the_approval_still_waits():
    stores, project, request, dispatcher = setup(["permanent"])
    dispatcher.run_once()
    with ContractClient(application(stores)) as client:
        approval = client.get(f"/api/v1/projects/{project.id}/approvals/{request.id}").json()
        inbox = client.get("/api/v1/attention").json()["items"]
    [status] = approval["notifications"]
    assert (status["state"], status["failure"]) == ("failed", "invalid_destination")
    assert approval["attention"] == "decision_required" and approval["status"] == "pending"
    assert [i["approval_id"] for i in inbox] == [request.id]  # notification failure never removes the attention item


def test_an_unknown_outcome_is_reported_honestly():
    stores, project, request, dispatcher = setup(["timeout"])
    dispatcher.run_once()
    with ContractClient(application(stores)) as client:
        [status] = client.get(f"/api/v1/projects/{project.id}/approvals/{request.id}").json()["notifications"]
    assert (status["state"], status["outcome_unknown"], status["failure"]) == ("retrying", True, "timeout")


def test_the_contract_vocabularies_equal_the_domain_enums():
    import typing

    from agent_factory.api.schemas.common import NotificationFailure, NotificationState
    from agent_factory.domain.notifications import FailureCategory
    from agent_factory.domain.notifications import NotificationState as DomainState

    assert set(typing.get_args(NotificationState)) == {s.value for s in DomainState}
    assert set(typing.get_args(NotificationFailure)) == {c.value for c in FailureCategory}


def test_no_notification_route_exists_and_none_accepts_a_recipient():
    from api_support import SPEC, deref

    def names(schema):
        schema = deref(schema)
        found = set(schema.get("properties", {}))
        for part in schema.get("allOf", []) + schema.get("oneOf", []):
            found |= names(part)
        return found

    for path, item in SPEC["paths"].items():
        assert "notif" not in path
        for method, operation in item.items():
            if not isinstance(operation, dict) or "requestBody" not in operation:
                continue
            for media in deref(operation["requestBody"])["content"].values():
                fields = names(media["schema"])
                assert not fields & {"recipient", "recipients", "to", "phone", "address", "email", "destination", "channel"}, (method, path)
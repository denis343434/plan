import json
import uuid

import httpx
import respx
from fastapi.testclient import TestClient

from app.adapters.base import ReplyCheckResult
from app.config import settings
from app.main import app

BASE = settings.DATA_SERVICE_URL


def _message_payload(message_id: str, lead_id: str, account_id: str, reply_status: str = "none") -> dict:
    return {
        "id": message_id,
        "lead_id": lead_id,
        "account_id": account_id,
        "template_variant": "A",
        "text_sent": "Hi Fitness Club",
        "sent_at": "2026-01-01T00:00:00",
        "delivery_status": "sent",
        "reply_status": reply_status,
        "error_reason": None,
        "reply_preview": None,
        "replied_at": None,
    }


def _lead_payload(lead_id: str) -> dict:
    return {
        "id": lead_id,
        "platform": "vk",
        "external_id": "123456",
        "group_url": "https://vk.com/fitness_club",
        "admin_contact": None,
        "title": "Fitness Club",
        "status": "contacted",
        "campaign_id": None,
        "found_at": "2026-01-01T00:00:00",
    }


@respx.mock
def test_check_inbox_with_no_reply_marks_message_no_reply():
    assert settings.VK_ADAPTER_MODE == "fake"  # DryRunInboxAdapter — never reports a reply
    account_id = str(uuid.uuid4())
    lead_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())

    respx.get(f"{BASE}/messages").mock(
        return_value=httpx.Response(200, json=[_message_payload(message_id, lead_id, account_id)])
    )
    respx.get(f"{BASE}/leads").mock(return_value=httpx.Response(200, json=[_lead_payload(lead_id)]))
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    reply_route = respx.patch(f"{BASE}/messages/{message_id}/reply").mock(
        return_value=httpx.Response(200, json=_message_payload(message_id, lead_id, account_id, "no_reply"))
    )

    with TestClient(app) as client:
        start = client.post("/inbox/check", params={"account_id": account_id})
        assert start.status_code == 202
        assert start.json()["status"] == "queued"

        status_resp = client.get("/inbox/check-status", params={"account_id": account_id})

    body = status_resp.json()
    assert body["status"] == "done"
    assert body["checked"] == 1
    assert body["total"] == 1
    assert body["replied"] == 0
    assert body["results"][0]["has_reply"] is False

    assert reply_route.called
    sent_body = json.loads(reply_route.calls.last.request.content)
    assert sent_body["reply_status"] == "no_reply"


@respx.mock
def test_check_inbox_with_reply_updates_message_and_lead(monkeypatch):
    account_id = str(uuid.uuid4())
    lead_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())

    respx.get(f"{BASE}/messages").mock(
        return_value=httpx.Response(200, json=[_message_payload(message_id, lead_id, account_id)])
    )
    respx.get(f"{BASE}/leads").mock(return_value=httpx.Response(200, json=[_lead_payload(lead_id)]))
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    reply_route = respx.patch(f"{BASE}/messages/{message_id}/reply").mock(
        return_value=httpx.Response(200, json=_message_payload(message_id, lead_id, account_id, "replied"))
    )
    status_route = respx.patch(f"{BASE}/leads/{lead_id}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead_id))
    )

    class _FakeInboxAdapter:
        def check_replies(self, leads, on_progress=None):
            total = len(leads)
            results = {}
            for checked, lead in enumerate(leads, start=1):
                results[lead["id"]] = ReplyCheckResult(has_reply=True, preview="Здравствуйте!")
                if on_progress is not None:
                    on_progress(checked, total)
            return results

    monkeypatch.setattr("app.inbox.get_inbox_adapter", lambda platform, storage_state=None: _FakeInboxAdapter())

    with TestClient(app) as client:
        client.post("/inbox/check", params={"account_id": account_id})
        status_resp = client.get("/inbox/check-status", params={"account_id": account_id})

    body = status_resp.json()
    assert body["status"] == "done"
    assert body["checked"] == 1
    assert body["replied"] == 1
    assert body["results"][0]["has_reply"] is True
    assert body["results"][0]["preview"] == "Здравствуйте!"

    assert reply_route.called
    assert status_route.called


@respx.mock
def test_check_inbox_persists_resolved_chat_href_on_lead(monkeypatch):
    account_id = str(uuid.uuid4())
    lead_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())

    respx.get(f"{BASE}/messages").mock(
        return_value=httpx.Response(200, json=[_message_payload(message_id, lead_id, account_id)])
    )
    respx.get(f"{BASE}/leads").mock(return_value=httpx.Response(200, json=[_lead_payload(lead_id)]))
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    respx.patch(f"{BASE}/messages/{message_id}/reply").mock(
        return_value=httpx.Response(200, json=_message_payload(message_id, lead_id, account_id, "no_reply"))
    )
    chat_href_route = respx.patch(f"{BASE}/leads/{lead_id}/chat-href").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead_id))
    )

    class _FakeInboxAdapter:
        def check_replies(self, leads, on_progress=None):
            total = len(leads)
            results = {}
            for checked, lead in enumerate(leads, start=1):
                results[lead["id"]] = ReplyCheckResult(
                    has_reply=False, resolved_chat_href="/im/convo/-777?sel=-777"
                )
                if on_progress is not None:
                    on_progress(checked, total)
            return results

    monkeypatch.setattr("app.inbox.get_inbox_adapter", lambda platform, storage_state=None: _FakeInboxAdapter())

    with TestClient(app) as client:
        client.post("/inbox/check", params={"account_id": account_id})
        client.get("/inbox/check-status", params={"account_id": account_id})

    assert chat_href_route.called
    sent_body = json.loads(chat_href_route.calls.last.request.content)
    assert sent_body["chat_href"] == "/im/convo/-777?sel=-777"


@respx.mock
def test_check_inbox_with_no_pending_messages_short_circuits():
    account_id = str(uuid.uuid4())
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))

    with TestClient(app) as client:
        client.post("/inbox/check", params={"account_id": account_id})
        status_resp = client.get("/inbox/check-status", params={"account_id": account_id})

    body = status_resp.json()
    assert body["status"] == "done"
    assert body["checked"] == 0
    assert body["replied"] == 0
    assert body["results"] == []


def test_check_status_for_unknown_account_is_404():
    with TestClient(app) as client:
        resp = client.get("/inbox/check-status", params={"account_id": str(uuid.uuid4())})
    assert resp.status_code == 404


@respx.mock
def test_post_check_twice_while_running_does_not_spawn_second_task():
    account_id = str(uuid.uuid4())

    from app.inbox import InboxCheckStatus, InboxCheckTask, TASKS

    TASKS[account_id] = InboxCheckTask(account_id=account_id, status=InboxCheckStatus.running, total=5, checked=2)

    with TestClient(app) as client:
        resp = client.post("/inbox/check", params={"account_id": account_id})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"
    assert body["checked"] == 2
    assert body["total"] == 5

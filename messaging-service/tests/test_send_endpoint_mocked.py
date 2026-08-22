import uuid

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

BASE = settings.DATA_SERVICE_URL


def _account_payload(account_id: str, status: str = "locked") -> dict:
    return {
        "id": account_id,
        "platform": "vk",
        "login": "acc",
        "purpose": "messaging",
        "proxy": None,
        "user_agent": None,
        "viewport": None,
        "hourly_limit": 100,
        "daily_limit": 1000,
        "status": status,
        "warmup_stage": 0,
        "cooldown_until": None,
        "locked_until": None,
        "locked_task_ref": None,
        "last_used_at": None,
        "hourly_used": 0,
        "daily_used": 0,
    }


def _lead_payload(lead_id: str) -> dict:
    return {
        "id": lead_id,
        "platform": "vk",
        "external_id": "123456",
        "group_url": "https://vk.com/fitness_club",
        "admin_contact": None,
        "title": "Fitness Club",
        "status": "new",
        "campaign_id": None,
        "found_at": "2026-01-01T00:00:00",
    }


@respx.mock
def test_post_send_runs_task_to_done_and_status_endpoint_reflects_it():
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": campaign_id,
                "name": "test",
                "platform": "vk",
                "keyword": "fitness",
                "template_id": template_id,
                "status": "running",
                "created_at": "2026-01-01T00:00:00",
            },
        )
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(
            200, json={"id": template_id, "campaign_id": campaign_id, "variant": "A", "body": "Hi {{org_name}}"}
        )
    )
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
            httpx.Response(200, json=[]),
        ]
    )
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200,
            json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"},
        )
    )
    respx.patch(f"{BASE}/leads/{lead1}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead1))
    )
    message_route = respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "lead_id": lead1,
                "account_id": account_id,
                "template_variant": "A",
                "text_sent": "Hi Fitness Club",
                "sent_at": "2026-01-01T00:00:00",
                "delivery_status": "sent",
                "reply_status": "none",
            },
        )
    )
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    with TestClient(app) as client:
        resp = client.post(f"/campaigns/{campaign_id}/send")
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"

        status_resp = client.get(f"/campaigns/{campaign_id}/send-status")
        body = status_resp.json()
        assert body["status"] == "done"
        assert body["sent"] == 1
        assert body["failed"] == 0

    assert message_route.called


def test_send_status_for_unknown_campaign_is_404():
    with TestClient(app) as client:
        resp = client.get(f"/campaigns/{uuid.uuid4()}/send-status")
        assert resp.status_code == 404


@respx.mock
def test_post_send_twice_while_running_does_not_spawn_second_task():
    campaign_id = str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": campaign_id,
                "name": "test",
                "platform": "vk",
                "keyword": "fitness",
                "template_id": None,
                "status": "running",
                "created_at": "2026-01-01T00:00:00",
            },
        )
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))

    from app.tasks import SendTask, SendTaskStatus, TASKS

    TASKS[uuid.UUID(campaign_id)] = SendTask(campaign_id=uuid.UUID(campaign_id), status=SendTaskStatus.running)

    with TestClient(app) as client:
        resp = client.post(f"/campaigns/{campaign_id}/send")
        assert resp.status_code == 202
        assert resp.json()["status"] == "running"

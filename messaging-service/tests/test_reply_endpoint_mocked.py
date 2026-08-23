import base64
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
        "status": "replied",
        "campaign_id": None,
        "found_at": "2026-01-01T00:00:00",
    }


@respx.mock
def test_reply_sends_via_same_account_and_persists_message():
    lead_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    respx.get(f"{BASE}/leads/{lead_id}").mock(return_value=httpx.Response(200, json=_lead_payload(lead_id)))
    lock_route = respx.post(f"{BASE}/accounts/{account_id}/lock").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    message_route = respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "lead_id": lead_id,
                "account_id": account_id,
                "template_variant": None,
                "text_sent": "Спасибо, уточню детали",
                "sent_at": "2026-01-01T00:00:00",
                "delivery_status": "sent",
                "reply_status": "none",
                "error_reason": None,
                "reply_preview": None,
                "replied_at": None,
            },
        )
    )
    release_route = respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    with TestClient(app) as client:
        resp = client.post(f"/leads/{lead_id}/reply", json={"account_id": account_id, "text": "Спасибо, уточню детали"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery_status"] == "sent"
    assert body["account_id"] == account_id
    assert body["lead_id"] == lead_id
    assert lock_route.called
    assert message_route.called
    assert release_route.called


@respx.mock
def test_reply_for_unknown_lead_is_404():
    lead_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    respx.get(f"{BASE}/leads/{lead_id}").mock(return_value=httpx.Response(404, json={"detail": "not found"}))

    with TestClient(app) as client:
        resp = client.post(f"/leads/{lead_id}/reply", json={"account_id": account_id, "text": "hi"})

    assert resp.status_code == 404


@respx.mock
def test_reply_with_busy_account_is_409():
    lead_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    respx.get(f"{BASE}/leads/{lead_id}").mock(return_value=httpx.Response(200, json=_lead_payload(lead_id)))
    respx.post(f"{BASE}/accounts/{account_id}/lock").mock(
        return_value=httpx.Response(409, json={"detail": "account is locked"})
    )

    with TestClient(app) as client:
        resp = client.post(f"/leads/{lead_id}/reply", json={"account_id": account_id, "text": "hi"})

    assert resp.status_code == 409


def test_reply_with_invalid_base64_image_is_400():
    lead_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    with TestClient(app) as client:
        resp = client.post(
            f"/leads/{lead_id}/reply",
            json={
                "account_id": account_id,
                "text": "hi",
                "image_base64": "not-valid-base64!!",
                "image_filename": "photo.jpg",
            },
        )

    assert resp.status_code == 400


def test_reply_with_non_image_attachment_is_400():
    lead_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    payload = base64.b64encode(b"fake pdf bytes").decode()

    with TestClient(app) as client:
        resp = client.post(
            f"/leads/{lead_id}/reply",
            json={
                "account_id": account_id,
                "text": "hi",
                "image_base64": payload,
                "image_filename": "document.pdf",
            },
        )

    assert resp.status_code == 400


def test_reply_without_text_or_image_is_422():
    lead_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())

    with TestClient(app) as client:
        resp = client.post(f"/leads/{lead_id}/reply", json={"account_id": account_id, "text": "  "})

    assert resp.status_code == 422


@respx.mock
def test_reply_with_valid_image_is_sent_via_dryrun_adapter():
    lead_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    image_bytes = base64.b64encode(b"\xff\xd8\xff fake jpeg bytes").decode()

    respx.get(f"{BASE}/leads/{lead_id}").mock(return_value=httpx.Response(200, json=_lead_payload(lead_id)))
    respx.post(f"{BASE}/accounts/{account_id}/lock").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    message_route = respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "lead_id": lead_id,
                "account_id": account_id,
                "template_variant": None,
                "text_sent": "",
                "sent_at": "2026-01-01T00:00:00",
                "delivery_status": "sent",
                "reply_status": "none",
                "error_reason": None,
                "reply_preview": None,
                "replied_at": None,
            },
        )
    )
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    with TestClient(app) as client:
        resp = client.post(
            f"/leads/{lead_id}/reply",
            json={
                "account_id": account_id,
                "text": "",
                "image_base64": image_bytes,
                "image_filename": "photo.jpg",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["delivery_status"] == "sent"
    assert message_route.called

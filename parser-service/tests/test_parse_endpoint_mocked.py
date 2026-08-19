import json
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
        "purpose": "parsing",
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


@respx.mock
def test_post_parse_sends_correct_bulk_payload_and_reaches_done():
    account_id = str(uuid.uuid4())
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200,
            json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"},
        )
    )
    bulk_route = respx.post(f"{BASE}/leads/bulk").mock(
        return_value=httpx.Response(200, json={"inserted": 3, "skipped": 0, "lead_ids": []})
    )
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    with TestClient(app) as client:
        resp = client.post("/parse", json={"platform": "vk", "keyword": "fitness"})
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        assert resp.json()["status"] == "queued"

        status_resp = client.get(f"/parse/{task_id}/status")
        body = status_resp.json()
        assert body["status"] == "done"
        assert body["found"] == 3
        assert body["inserted"] == 3

    sent_body = json.loads(bulk_route.calls.last.request.content)
    assert len(sent_body) == 3
    assert all(item["platform"] == "vk" for item in sent_body)
    assert all(item["external_id"].startswith("fake-fitness-") for item in sent_body)


def test_status_for_unknown_task_is_404():
    with TestClient(app) as client:
        resp = client.get(f"/parse/{uuid.uuid4()}/status")
        assert resp.status_code == 404

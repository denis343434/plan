import json
import uuid

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

DATA_BASE = settings.DATA_SERVICE_URL


@respx.mock
def test_pause_account_uses_large_default_cooldown():
    account_id = str(uuid.uuid4())
    route = respx.post(f"{DATA_BASE}/accounts/{account_id}/cooldown").mock(
        return_value=httpx.Response(200, json={"id": account_id, "status": "cooldown"})
    )

    with TestClient(app) as client:
        resp = client.post(f"/accounts/{account_id}/pause")

    assert resp.status_code == 200
    body = json.loads(route.calls.last.request.content)
    assert body["minutes"] == 10080
    assert body["permanent"] is False


@respx.mock
def test_pause_account_permanent_ban():
    account_id = str(uuid.uuid4())
    route = respx.post(f"{DATA_BASE}/accounts/{account_id}/cooldown").mock(
        return_value=httpx.Response(200, json={"id": account_id, "status": "banned"})
    )

    with TestClient(app) as client:
        resp = client.post(f"/accounts/{account_id}/pause", json={"permanent": True, "reason": "abuse"})

    assert resp.status_code == 200
    body = json.loads(route.calls.last.request.content)
    assert body["permanent"] is True
    assert body["reason"] == "abuse"

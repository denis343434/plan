import uuid

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

BASE = settings.DATA_SERVICE_URL


@respx.mock
def test_session_check_fake_mode_reflects_whether_storage_state_is_present():
    account_id = str(uuid.uuid4())
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200,
            json={"account_id": account_id, "storage_state": {"cookies": []}, "updated_at": "2026-01-01T00:00:00"},
        )
    )

    original_mode = settings.VK_ADAPTER_MODE
    settings.VK_ADAPTER_MODE = "fake"
    try:
        with TestClient(app) as client:
            resp = client.get(f"/accounts/{account_id}/session-check")
            assert resp.status_code == 200
            assert resp.json() == {"account_id": account_id, "valid": True}
    finally:
        settings.VK_ADAPTER_MODE = original_mode


@respx.mock
def test_session_check_fake_mode_no_storage_state_is_invalid():
    account_id = str(uuid.uuid4())
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200,
            json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"},
        )
    )

    original_mode = settings.VK_ADAPTER_MODE
    settings.VK_ADAPTER_MODE = "fake"
    try:
        with TestClient(app) as client:
            resp = client.get(f"/accounts/{account_id}/session-check")
            assert resp.status_code == 200
            assert resp.json() == {"account_id": account_id, "valid": False}
    finally:
        settings.VK_ADAPTER_MODE = original_mode


@respx.mock
def test_session_check_unknown_account_is_404():
    account_id = str(uuid.uuid4())
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(return_value=httpx.Response(404, json={"detail": "not found"}))

    with TestClient(app) as client:
        resp = client.get(f"/accounts/{account_id}/session-check")
        assert resp.status_code == 404

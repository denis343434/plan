from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_get_headless_reflects_current_setting():
    original = settings.VK_HEADLESS
    settings.VK_HEADLESS = True
    try:
        with TestClient(app) as client:
            resp = client.get("/config/headless")
            assert resp.status_code == 200
            assert resp.json() == {"headless": True}
    finally:
        settings.VK_HEADLESS = original


def test_put_headless_updates_setting_and_is_visible_via_get():
    original = settings.VK_HEADLESS
    try:
        with TestClient(app) as client:
            resp = client.put("/config/headless", json={"headless": False})
            assert resp.status_code == 200
            assert resp.json() == {"headless": False}
            assert settings.VK_HEADLESS is False

            resp = client.get("/config/headless")
            assert resp.json() == {"headless": False}
    finally:
        settings.VK_HEADLESS = original

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text


def _create_account(client: TestClient, login: str = "acc1") -> str:
    resp = client.post(
        "/accounts",
        json={
            "platform": "vk",
            "login": login,
            "purpose": "messaging",
            "hourly_limit": 100,
            "daily_limit": 1000,
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def test_next_available_release_cycle(client: TestClient) -> None:
    account_id = _create_account(client)

    first = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert first.status_code == 200
    assert first.json()["status"] == "locked"

    blocked = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert blocked.status_code == 409

    released = client.post(f"/accounts/{account_id}/release")
    assert released.status_code == 200
    assert released.json()["status"] == "active"

    second = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert second.status_code == 200
    assert second.json()["id"] == account_id


def test_cooldown_expires_and_account_becomes_available(client: TestClient) -> None:
    """Регрессия: next_available() раньше проверял автоистечение только для status='locked',
    а cooldown с прошедшим cooldown_until так и оставался недоступен навсегда, пока кто-то
    не позвал activate() руками (см. VK_PARSE_COOLDOWN_MIN в parser-service — с ним это стало
    обычным сценарием каждые 5 минут, а не редким edge case)."""
    account_id = _create_account(client, "acc-cooldown")

    resp = client.post(f"/accounts/{account_id}/cooldown", json={"minutes": 30})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cooldown"

    blocked = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert blocked.status_code == 409

    from app.database import engine

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE accounts SET cooldown_until=:t WHERE id=:id"),
            {"t": past, "id": account_id},
        )

    # Статус в БД по-прежнему 'cooldown' (никто его не менял) — только истекший cooldown_until.
    still_shows_cooldown = client.get(f"/accounts?platform=vk").json()
    assert next(a for a in still_shows_cooldown if a["id"] == account_id)["status"] == "active"

    available = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert available.status_code == 200
    assert available.json()["id"] == account_id


def test_release_after_cooldown_does_not_reactivate(client: TestClient) -> None:
    """Регрессия: caller (например Parser при обнаружении капчи) вызывает cooldown(),
    а затем безусловно release() в finally — release() не должен затирать cooldown обратно на active."""
    account_id = _create_account(client, "acc-cooldown-release")

    locked = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert locked.status_code == 200

    cooled = client.post(
        f"/accounts/{account_id}/cooldown", json={"minutes": 30, "reason": "captcha_detected"}
    )
    assert cooled.status_code == 200
    assert cooled.json()["status"] == "cooldown"

    released = client.post(f"/accounts/{account_id}/release")
    assert released.status_code == 200
    assert released.json()["status"] == "cooldown"

    blocked = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert blocked.status_code == 409


def test_permanent_cooldown_bans_account(client: TestClient) -> None:
    account_id = _create_account(client, "acc-ban")

    resp = client.post(
        f"/accounts/{account_id}/cooldown", json={"minutes": 0, "permanent": True, "reason": "banned by vk"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "banned"

    blocked = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert blocked.status_code == 409

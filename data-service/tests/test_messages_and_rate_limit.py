from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text


def _create_account(client: TestClient) -> str:
    resp = client.post(
        "/accounts",
        json={
            "platform": "vk",
            "login": "rl-acc",
            "purpose": "messaging",
            "hourly_limit": 2,
            "daily_limit": 100,
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _create_lead(client: TestClient, external_id: str) -> str:
    resp = client.post(
        "/leads/bulk",
        json=[{"platform": "vk", "external_id": external_id, "group_url": "https://vk.com/g"}],
    )
    return resp.json()["lead_ids"][0]


def _send_message(client: TestClient, lead_id: str, account_id: str) -> None:
    resp = client.post(
        "/messages",
        json={
            "lead_id": lead_id,
            "account_id": account_id,
            "text_sent": "hello",
            "delivery_status": "sent",
        },
    )
    assert resp.status_code == 200


def test_hourly_limit_blocks_next_available_until_window_expires(client: TestClient) -> None:
    account_id = _create_account(client)

    # Release it first — next_available already locked it via account creation? No, creation
    # leaves status=active, so lock it via next-available then release to have a normal flow.
    acquired = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert acquired.status_code == 200
    client.post(f"/accounts/{account_id}/release")

    lead1 = _create_lead(client, "m1")
    lead2 = _create_lead(client, "m2")
    _send_message(client, lead1, account_id)
    _send_message(client, lead2, account_id)

    blocked = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert blocked.status_code == 409

    from app.database import engine

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE messages SET sent_at=:t WHERE account_id=:id"),
            {"t": past, "id": account_id},
        )

    available = client.post("/accounts/next-available", json={"platform": "vk", "purpose": "messaging"})
    assert available.status_code == 200
    assert available.json()["id"] == account_id

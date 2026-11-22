from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text


def _setup(client: TestClient) -> tuple[str, str]:
    account = client.post(
        "/accounts",
        json={
            "platform": "vk",
            "login": "stats-acc",
            "purpose": "messaging",
            "hourly_limit": 100,
            "daily_limit": 1000,
        },
    ).json()
    campaign = client.post(
        "/campaigns", json={"name": "Кафе", "platform": "vk", "keyword": "кафе"}
    ).json()
    return account["id"], campaign["id"]


def _lead(client: TestClient, campaign_id: str, external_id: str) -> str:
    resp = client.post(
        "/leads/bulk",
        json=[
            {
                "platform": "vk",
                "external_id": external_id,
                "group_url": f"https://vk.com/{external_id}",
                "campaign_id": campaign_id,
            }
        ],
    )
    return resp.json()["lead_ids"][0]


def _message(
    client: TestClient,
    lead_id: str,
    account_id: str,
    variant: str | None,
    sent_at: datetime,
    status: str = "sent",
    replied_at: datetime | None = None,
) -> str:
    message_id = client.post(
        "/messages",
        json={
            "lead_id": lead_id,
            "account_id": account_id,
            "template_variant": variant,
            "text_sent": "hello",
            "delivery_status": status,
        },
    ).json()["id"]

    from app.database import engine

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE messages SET sent_at=:sent, replied_at=:replied,"
                " reply_status=CASE WHEN CAST(:replied AS timestamp) IS NULL THEN 'none' ELSE 'replied' END"
                " WHERE id=:id"
            ),
            {"sent": sent_at, "replied": replied_at, "id": message_id},
        )
    return message_id


def _rows(client: TestClient, **params) -> list[dict]:
    resp = client.get("/stats/messages", params={"tz": "UTC", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


def test_variant_stats_count_replies_within_window(client: TestClient) -> None:
    account_id, campaign_id = _setup(client)
    t0 = datetime(2026, 10, 1, 10, 0)

    # A: три отправлено, один ответ через 2 часа, один через 3 дня, одна ошибка доставки.
    _message(client, _lead(client, campaign_id, "a1"), account_id, "A", t0, replied_at=t0 + timedelta(hours=2))
    _message(client, _lead(client, campaign_id, "a2"), account_id, "A", t0, replied_at=t0 + timedelta(days=3))
    _message(client, _lead(client, campaign_id, "a3"), account_id, "A", t0)
    _message(client, _lead(client, campaign_id, "a4"), account_id, "A", t0, status="failed")
    # B: один отправлен, без ответа.
    _message(client, _lead(client, campaign_id, "b1"), account_id, "B", t0)

    rows = {r["template_variant"]: r for r in _rows(client, group_by="variant", campaign_id=campaign_id)}

    a = rows["A"]
    assert a["campaign_name"] == "Кафе"
    assert a["kind"] == "initial"
    assert (a["attempted"], a["sent"], a["failed"]) == (4, 3, 1)
    assert a["replied"] == 2
    assert a["replied_in_window"] == 1
    assert a["reply_rate"] == round(2 / 3, 4)
    assert a["reply_rate_in_window"] == round(1 / 3, 4)
    assert a["median_reply_hours"] == 37.0  # медиана из 2ч и 72ч

    b = rows["B"]
    assert (b["sent"], b["replied"], b["reply_rate"], b["median_reply_hours"]) == (1, 0, 0.0, None)


def test_reply_after_followup_is_attributed_only_to_followup(client: TestClient) -> None:
    account_id, campaign_id = _setup(client)
    t0 = datetime(2026, 10, 1, 10, 0)
    reply_at = t0 + timedelta(days=4)

    lead_id = _lead(client, campaign_id, "f1")
    # inbox.py помечает replied оба сообщения лида — ответ должен уйти только повтору.
    _message(client, lead_id, account_id, "A", t0, replied_at=reply_at)
    _message(client, lead_id, account_id, "F", t0 + timedelta(days=3), replied_at=reply_at)
    # Ручной ответ из окна "Кому написали" — в статистику не входит вовсе.
    _message(client, lead_id, account_id, None, reply_at + timedelta(hours=1))

    rows = {r["kind"]: r for r in _rows(client, group_by="variant", campaign_id=campaign_id)}
    assert set(rows) == {"initial", "followup"}
    assert (rows["initial"]["sent"], rows["initial"]["replied"]) == (1, 0)
    assert (rows["followup"]["sent"], rows["followup"]["replied"], rows["followup"]["replied_in_window"]) == (1, 1, 1)

    total = _rows(client)[0]
    assert (total["attempted"], total["sent"], total["leads"], total["replied"]) == (2, 2, 1, 1)


def test_day_grouping_and_period_filter_respect_timezone(client: TestClient) -> None:
    account_id, campaign_id = _setup(client)
    # 22:30 UTC 1 октября = 01:30 МСК 2 октября.
    late = datetime(2026, 10, 1, 22, 30)
    _message(client, _lead(client, campaign_id, "d1"), account_id, "A", late, replied_at=late + timedelta(hours=1))
    _message(client, _lead(client, campaign_id, "d2"), account_id, "A", datetime(2026, 10, 1, 9, 0))

    utc_days = {r["day"]: r for r in _rows(client, group_by="day")}
    assert set(utc_days) == {"2026-10-01"}
    assert (utc_days["2026-10-01"]["sent"], utc_days["2026-10-01"]["replied"]) == (2, 1)

    msk = client.get("/stats/messages", params={"group_by": "day", "tz": "Europe/Moscow"}).json()["rows"]
    assert [(r["day"], r["sent"]) for r in msk] == [("2026-10-02", 1), ("2026-10-01", 1)]

    only_second = client.get(
        "/stats/messages",
        params={"tz": "Europe/Moscow", "date_from": "2026-10-02", "date_to": "2026-10-02"},
    ).json()["rows"]
    assert (only_second[0]["sent"], only_second[0]["replied"]) == (1, 1)


def test_empty_total_and_bad_params(client: TestClient) -> None:
    total = _rows(client)
    assert len(total) == 1
    assert (total[0]["sent"], total[0]["replied"], total[0]["reply_rate"]) == (0, 0, None)

    assert _rows(client, group_by="campaign") == []

    bad_tz = client.get("/stats/messages", params={"tz": "Mars/Olympus"})
    assert bad_tz.status_code == 422

    bad_period = client.get("/stats/messages", params={"date_from": "2026-10-05", "date_to": "2026-10-01"})
    assert bad_period.status_code == 422

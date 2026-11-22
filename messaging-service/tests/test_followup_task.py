import uuid
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.config import settings
from app.followup import FollowupTaskStatus, create_task, run_followup_task

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


def _lead_payload(lead_id: str, platform: str = "vk") -> dict:
    return {
        "id": lead_id,
        "platform": platform,
        "external_id": "123456",
        "group_url": "https://vk.com/fitness_club",
        "admin_contact": None,
        "title": "Fitness Club",
        "status": "contacted",
        "campaign_id": None,
        "found_at": "2026-01-01T00:00:00",
    }


def _template_payload(template_id: str, campaign_id: str, variant: str = "F") -> dict:
    return {"id": template_id, "campaign_id": campaign_id, "variant": variant, "body": "Всё ещё интересно, {{org_name}}?"}


def _message_payload(lead_id: str, sent_at: str, delivery_status: str = "sent", variant: str = "A") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "lead_id": lead_id,
        "account_id": str(uuid.uuid4()),
        "template_variant": variant,
        "text_sent": "Hi",
        "sent_at": sent_at,
        "delivery_status": delivery_status,
        "reply_status": "none",
    }


def _old_iso(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat()


@respx.mock
def test_followup_sends_to_lead_overdue_by_three_days():
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/templates").mock(
        return_value=httpx.Response(200, json=[_template_payload(template_id, campaign_id)])
    )
    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json={"id": campaign_id, "platform": "vk"})
    )
    respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
            httpx.Response(200, json=[]),
        ]
    )
    respx.get(f"{BASE}/messages", params={"lead_id": lead1}).mock(
        return_value=httpx.Response(200, json=[_message_payload(lead1, _old_iso(4))])
    )
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    post_message_route = respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(200, json=_message_payload(lead1, _old_iso(0), variant="F"))
    )
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_followup_task(uuid.UUID(campaign_id))

    assert task.status == FollowupTaskStatus.done
    assert task.sent == 1
    assert task.failed == 0
    assert post_message_route.called
    import json

    posted_body = json.loads(post_message_route.calls.last.request.content)
    assert posted_body["template_variant"] == "F"


@respx.mock
def test_followup_skips_lead_not_yet_overdue():
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/templates").mock(
        return_value=httpx.Response(200, json=[_template_payload(template_id, campaign_id)])
    )
    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json={"id": campaign_id, "platform": "vk"})
    )
    respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
            httpx.Response(200, json=[]),
        ]
    )
    respx.get(f"{BASE}/messages", params={"lead_id": lead1}).mock(
        return_value=httpx.Response(200, json=[_message_payload(lead1, _old_iso(1))])
    )

    task = create_task(uuid.UUID(campaign_id))
    run_followup_task(uuid.UUID(campaign_id))

    assert task.status == FollowupTaskStatus.done
    assert task.sent == 0
    assert task.skipped == 1


@respx.mock
def test_followup_skips_lead_already_followed_up():
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/templates").mock(
        return_value=httpx.Response(200, json=[_template_payload(template_id, campaign_id)])
    )
    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json={"id": campaign_id, "platform": "vk"})
    )
    respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
            httpx.Response(200, json=[]),
        ]
    )
    respx.get(f"{BASE}/messages", params={"lead_id": lead1}).mock(
        return_value=httpx.Response(
            200,
            json=[
                _message_payload(lead1, _old_iso(5)),
                _message_payload(lead1, _old_iso(4), variant="F"),
            ],
        )
    )

    task = create_task(uuid.UUID(campaign_id))
    run_followup_task(uuid.UUID(campaign_id))

    assert task.status == FollowupTaskStatus.done
    assert task.sent == 0
    assert task.skipped == 1


@respx.mock
def test_followup_fails_when_no_followup_template_configured():
    campaign_id = str(uuid.uuid4())

    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))

    task = create_task(uuid.UUID(campaign_id))
    run_followup_task(uuid.UUID(campaign_id))

    assert task.status == FollowupTaskStatus.failed
    assert task.error == "no follow-up template configured for campaign"


@respx.mock
def test_followup_ignores_ab_templates_and_uses_only_variant_f():
    campaign_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/templates").mock(
        return_value=httpx.Response(
            200,
            json=[
                _template_payload(str(uuid.uuid4()), campaign_id, variant="A"),
                _template_payload(str(uuid.uuid4()), campaign_id, variant="B"),
            ],
        )
    )

    task = create_task(uuid.UUID(campaign_id))
    run_followup_task(uuid.UUID(campaign_id))

    assert task.status == FollowupTaskStatus.failed
    assert task.error == "no follow-up template configured for campaign"

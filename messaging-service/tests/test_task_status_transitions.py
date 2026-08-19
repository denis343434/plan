import uuid

import httpx
import respx

from app.config import settings
from app.tasks import SendTaskStatus, create_task, run_send_task

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
        "status": "new",
        "campaign_id": None,
        "found_at": "2026-01-01T00:00:00",
    }


def _campaign_payload(campaign_id: str, template_id: str | None) -> dict:
    return {
        "id": campaign_id,
        "name": "test campaign",
        "platform": "vk",
        "keyword": "fitness",
        "template_id": template_id,
        "status": "running",
        "created_at": "2026-01-01T00:00:00",
    }


def _template_payload(template_id: str, campaign_id: str | None, variant: str = "A") -> dict:
    return {"id": template_id, "campaign_id": campaign_id, "variant": variant, "body": "Hi {{org_name}}"}


@respx.mock
def test_send_task_goes_running_done_via_dryrun_adapter():
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    lead1, lead2 = str(uuid.uuid4()), str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(200, json=_template_payload(template_id, campaign_id))
    )

    leads_route = respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1), _lead_payload(lead2)]),
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
    patch_route = respx.patch(f"{BASE}/leads/{lead1}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead1))
    )
    respx.patch(f"{BASE}/leads/{lead2}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead2))
    )
    post_message_route = respx.post(f"{BASE}/messages").mock(
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
    release_route = respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.done
    assert task.sent == 2
    assert task.failed == 0
    assert leads_route.call_count == 2
    assert patch_route.called
    assert post_message_route.call_count == 2
    assert release_route.call_count == 2


@respx.mock
def test_send_task_waits_for_account_on_409():
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(200, json=_template_payload(template_id, campaign_id))
    )
    respx.get(f"{BASE}/leads").mock(return_value=httpx.Response(200, json=[_lead_payload(lead1)]))
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(409, json={"detail": "no account available"})
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.waiting_for_account
    assert task.sent == 0


@respx.mock
def test_send_task_fails_when_no_template_configured():
    campaign_id = str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id=None))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.failed
    assert task.error == "no template configured for campaign"


@respx.mock
def test_send_task_records_failed_delivery_and_cooldowns_on_flood():
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(200, json=_template_payload(template_id, campaign_id))
    )
    respx.get(f"{BASE}/leads").mock(
        side_effect=[httpx.Response(200, json=[_lead_payload(lead1)]), httpx.Response(200, json=[])]
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
    respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "lead_id": lead1,
                "account_id": account_id,
                "template_variant": "A",
                "text_sent": "Hi Fitness Club",
                "sent_at": "2026-01-01T00:00:00",
                "delivery_status": "failed",
                "reply_status": "none",
            },
        )
    )
    cooldown_route = respx.post(f"{BASE}/accounts/{account_id}/cooldown").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="cooldown"))
    )
    release_route = respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    import app.tasks as tasks_module
    from app.adapters.base import SendResult

    class _FloodAdapter:
        def send_message(self, lead, account, text):
            return SendResult(success=False, error="flood", flood_detected=True)

    original_get_adapter = tasks_module.get_adapter
    tasks_module.get_adapter = lambda platform, storage_state=None: _FloodAdapter()
    try:
        task = create_task(uuid.UUID(campaign_id))
        run_send_task(uuid.UUID(campaign_id))
    finally:
        tasks_module.get_adapter = original_get_adapter

    assert task.status == SendTaskStatus.done
    assert task.failed == 1
    assert task.sent == 0
    assert cooldown_route.called
    assert not release_route.called


@respx.mock
def test_send_task_does_not_loop_forever_on_non_flood_failure():
    """Регрессия: провал отправки без флуда не должен зацикливать run_send_task — лид
    остаётся status=new в Data Service (виден в /leads на каждом вызове), но задача должна
    сама прекратить его повторную обработку в рамках этого запуска и дойти до done."""
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    lead1 = str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(200, json=_template_payload(template_id, campaign_id))
    )
    # Лид остаётся status=new в Data Service на КАЖДОМ вызове — status там не меняется,
    # т.к. отправка проваливается без флуда. Без фикса это зациклило бы run_send_task.
    leads_route = respx.get(f"{BASE}/leads").mock(
        return_value=httpx.Response(200, json=[_lead_payload(lead1)])
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
    respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "lead_id": lead1,
                "account_id": account_id,
                "template_variant": "A",
                "text_sent": "Hi Fitness Club",
                "sent_at": "2026-01-01T00:00:00",
                "delivery_status": "failed",
                "reply_status": "none",
            },
        )
    )
    release_route = respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    import app.tasks as tasks_module
    from app.adapters.base import SendResult

    class _RejectingAdapter:
        def send_message(self, lead, account, text):
            return SendResult(success=False, error="rejected", flood_detected=False)

    original_get_adapter = tasks_module.get_adapter
    tasks_module.get_adapter = lambda platform, storage_state=None: _RejectingAdapter()
    try:
        task = create_task(uuid.UUID(campaign_id))
        run_send_task(uuid.UUID(campaign_id))
    finally:
        tasks_module.get_adapter = original_get_adapter

    assert task.status == SendTaskStatus.done
    assert task.failed == 1
    assert task.sent == 0
    assert leads_route.call_count == 2  # 1-й раз обрабатывает лид, 2-й раз видит его же и останавливается
    assert release_route.called

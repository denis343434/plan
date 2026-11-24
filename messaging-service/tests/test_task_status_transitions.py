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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))

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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
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
def test_send_task_records_pending_delivery_on_network_error_and_does_not_cooldown():
    # network_error (плохой интернет/страница не прогрузилась) не проблема аккаунта — не должен
    # уходить в cooldown, как flood/session_expired, и должен писаться как delivery_status=
    # "pending", а не "failed", иначе лид молча выпал бы из авто-повтора обычным следующим
    # запуском кампании (см. previously_failed_lead_ids в run_send_task и запрос пользователя).
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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
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
                "delivery_status": "pending",
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

    class _NetworkErrorAdapter:
        def send_message(self, lead, account, text):
            return SendResult(success=False, error="net::ERR_CONNECTION_RESET", network_error=True)

    original_get_adapter = tasks_module.get_adapter
    tasks_module.get_adapter = lambda platform, storage_state=None: _NetworkErrorAdapter()
    try:
        task = create_task(uuid.UUID(campaign_id))
        run_send_task(uuid.UUID(campaign_id))
    finally:
        tasks_module.get_adapter = original_get_adapter

    assert task.status == SendTaskStatus.done
    assert task.failed == 1
    assert task.sent == 0
    assert not cooldown_route.called
    assert release_route.called
    import json

    posted_body = json.loads(post_message_route.calls.last.request.content)
    assert posted_body["delivery_status"] == "pending"


@respx.mock
def test_send_task_skips_leads_with_prior_failed_message_by_default():
    """Лид, у которого уже есть сообщение с delivery_status=failed (провал в предыдущем запуске
    кампании), не должен подхватываться обычным запуском — иначе бот писал бы ему заново на
    каждом старте кампании, хотя лид всё ещё status=new (см. _process_lead)."""
    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    lead1, lead2 = str(uuid.uuid4()), str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(200, json=_template_payload(template_id, campaign_id))
    )
    respx.get(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid.uuid4()),
                    "lead_id": lead1,
                    "account_id": str(uuid.uuid4()),
                    "template_variant": "A",
                    "text_sent": "Hi",
                    "sent_at": "2026-01-01T00:00:00",
                    "delivery_status": "failed",
                    "reply_status": "none",
                }
            ],
        )
    )
    leads_route = respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1), _lead_payload(lead2)]),
            httpx.Response(200, json=[_lead_payload(lead1)]),
        ]
    )
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
    respx.patch(f"{BASE}/leads/{lead2}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead2))
    )
    post_message_route = respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "lead_id": lead2,
                "account_id": account_id,
                "template_variant": "A",
                "text_sent": "Hi Fitness Club",
                "sent_at": "2026-01-01T00:00:00",
                "delivery_status": "sent",
                "reply_status": "none",
            },
        )
    )
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.done
    assert task.sent == 1
    assert task.failed == 0
    # только lead2 реально обработан — lead1 отфильтрован ещё до похода за аккаунтом
    assert post_message_route.call_count == 1
    assert post_message_route.calls.last.request.content.find(lead2.encode()) != -1
    assert leads_route.call_count == 2


@respx.mock
def test_send_task_retry_failed_includes_previously_failed_leads():
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
    # GET /messages НЕ мокаем — retry_failed=True должен пропускать этот запрос целиком,
    # иначе тест упал бы на "not mocked".
    leads_route = respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
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
    respx.patch(f"{BASE}/leads/{lead1}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead1))
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
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id), retry_failed=True)

    assert task.status == SendTaskStatus.done
    assert task.sent == 1
    assert post_message_route.call_count == 1
    assert leads_route.call_count == 2


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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
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


@respx.mock
def test_send_task_pages_past_full_batch_of_previously_failed_leads():
    """Регрессия: если первые _BATCH_SIZE лидов status=new все ранее провалились, задача не должна
    останавливаться — пропущенные лиды остаются в выборке, поэтому нужно листать дальше по offset."""
    import app.tasks as tasks_module

    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    failed_ids = [str(uuid.uuid4()) for _ in range(tasks_module._BATCH_SIZE)]
    fresh = str(uuid.uuid4())

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(200, json=_template_payload(template_id, campaign_id))
    )
    respx.get(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid.uuid4()),
                    "lead_id": lead_id,
                    "account_id": account_id,
                    "template_variant": "A",
                    "text_sent": "Hi",
                    "sent_at": "2026-01-01T00:00:00",
                    "delivery_status": "failed",
                    "reply_status": "none",
                }
                for lead_id in failed_ids
            ],
        )
    )

    def _leads(request):
        offset = int(request.url.params["offset"])
        pool = failed_ids + ([fresh] if not sent else [])
        return httpx.Response(200, json=[_lead_payload(i) for i in pool[offset : offset + tasks_module._BATCH_SIZE]])

    sent: list[str] = []
    leads_route = respx.get(f"{BASE}/leads").mock(side_effect=_leads)
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200,
            json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"},
        )
    )

    def _patch(request):
        sent.append(fresh)
        return httpx.Response(200, json=_lead_payload(fresh))

    respx.patch(f"{BASE}/leads/{fresh}/status").mock(side_effect=_patch)
    respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "lead_id": fresh,
                "account_id": account_id,
                "template_variant": "A",
                "text_sent": "Hi Fitness Club",
                "sent_at": "2026-01-01T00:00:00",
                "delivery_status": "sent",
                "reply_status": "none",
            },
        )
    )
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.done
    assert task.sent == 1
    assert leads_route.call_count == 3  # offset 0 (все провалены) → offset 50 (fresh) → offset 50 (пусто)

@respx.mock
def test_unreachable_lead_is_skipped_and_rejected_not_counted_as_error(monkeypatch):
    """Р“СЂСѓРїРїР° СЃРѕ СЃРІРѕРёРј СЃР°Р№С‚РѕРј / Р±РµР· РєРЅРѕРїРєРё "РќР°РїРёСЃР°С‚СЊ" вЂ” СЌС‚Рѕ РЅРµ РѕС€РёР±РєР° РѕС‚РїСЂР°РІРєРё, Р° РїСЂРѕРїСѓСЃРє:
    Р»РёРґ РїРµСЂРµРІРѕРґРёС‚СЃСЏ РІ rejected, СЃС‡С‘С‚С‡РёРє skipped СЂР°СЃС‚С‘С‚, failed РЅРµ СЂР°СЃС‚С‘С‚, СЃРѕРѕР±С‰РµРЅРёРµ РІ Р‘Р”
    РЅРµ РїРёС€РµС‚СЃСЏ (РёРЅР°С‡Рµ РїСЂРѕРїСѓСЃРєРё Р¶РіР»Рё Р±С‹ С‡Р°СЃРѕРІРѕР№ Р»РёРјРёС‚ Р°РєРєР°СѓРЅС‚Р°)."""
    from app.adapters.base import SendResult

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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
            httpx.Response(200, json=[]),
        ]
    )
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    rejected_route = respx.patch(f"{BASE}/leads/{lead1}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead1))
    )
    post_message_route = respx.post(f"{BASE}/messages").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    class _UnreachableAdapter:
        def send_message(self, lead, account, text, image=None):
            return SendResult(success=False, error="site", has_site=True, unreachable=True)

    monkeypatch.setattr(
        "app.tasks.get_adapter", lambda platform, storage_state=None: _UnreachableAdapter()
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.done
    assert task.skipped == 1
    assert task.failed == 0
    assert task.sent == 0
    assert rejected_route.called
    assert post_message_route.call_count == 0


@respx.mock
def test_browser_crash_mid_send_does_not_kill_the_whole_task(monkeypatch):
    """TargetClosedError (Р·Р°РєСЂС‹Р»Рё РѕРєРЅРѕ Р±СЂР°СѓР·РµСЂР°) СЂР°РЅСЊС€Рµ РІС‹Р»РµС‚Р°Р» РёР· _process_lead Рё СѓР±РёРІР°Р» Р·Р°РґР°С‡Сѓ:
    РѕСЃС‚Р°РІС€РёРµСЃСЏ Р»РёРґС‹ РґР°Р¶Рµ РЅРµ РїСЂРѕР±РѕРІР°Р»Рё РїРёСЃР°С‚СЊ. РўРµРїРµСЂСЊ СЌС‚Рѕ С‚СЂР°РЅР·РёРµРЅС‚РЅС‹Р№ СЃР±РѕР№ РѕРґРЅРѕРіРѕ Р»РёРґР°."""
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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
            httpx.Response(200, json=[]),
        ]
    )
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    class _CrashingAdapter:
        def send_message(self, lead, account, text, image=None):
            raise RuntimeError(
                "Page.wait_for_selector: Target page, context or browser has been closed"
            )

    monkeypatch.setattr(
        "app.tasks.get_adapter", lambda platform, storage_state=None: _CrashingAdapter()
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.done  # Р·Р°РґР°С‡Р° РґРѕС€Р»Р° РґРѕ РєРѕРЅС†Р°, Р° РЅРµ СѓРјРµСЂР»Р°
    assert task.failed == 1  # СЃР±РѕР№ Р·Р°С„РёРєСЃРёСЂРѕРІР°РЅ, РЅРѕ Р»РёРґ РЅРµ "СЃРіРѕСЂРµР»" РЅР°РІСЃРµРіРґР°
    assert task.sent == 0


@respx.mock
def test_challenge_blocks_run_without_cooldown_or_lead_poisoning(monkeypatch):
    """РђРЅС‚РёР±РѕС‚-РїСЂРѕРІРµСЂРєР° VK вЂ” РЅРµ С„Р»СѓРґ: РѕСЃС‚Р°РЅР°РІР»РёРІР°РµРј СЂР°СЃСЃС‹Р»РєСѓ СЃ РїРѕРЅСЏС‚РЅРѕР№ РѕС€РёР±РєРѕР№, РЅРѕ Р°РєРєР°СѓРЅС‚
    РќР• РѕС‚РїСЂР°РІР»СЏРµРј РІ РєСѓР»РґР°СѓРЅ Рё Р»РёРґ РЅРµ РїРѕРјРµС‡Р°РµРј, С‡С‚РѕР±С‹ РїРѕСЃР»Рµ РїСЂРѕС…РѕР¶РґРµРЅРёСЏ РїСЂРѕРІРµСЂРєРё РІСЃС‘ РїСЂРѕРґРѕР»Р¶РёР»РѕСЃСЊ."""
    from app.adapters.base import SendResult

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
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/leads").mock(
        side_effect=[
            httpx.Response(200, json=[_lead_payload(lead1)]),
            httpx.Response(200, json=[]),
        ]
    )
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )
    cooldown_route = respx.post(f"{BASE}/accounts/{account_id}/cooldown").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="cooldown"))
    )
    release_route = respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )
    patch_route = respx.patch(f"{BASE}/leads/{lead1}/status").mock(
        return_value=httpx.Response(200, json=_lead_payload(lead1))
    )
    put_session_route = respx.put(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200, json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"}
        )
    )

    class _ChallengeAdapter:
        challenge_passed = True  # человек прошёл проверку в видимом браузере

        def storage_state(self):
            return {"cookies": [], "origins": []}

        def send_message(self, lead, account, text, image=None):
            return SendResult(success=False, blocked=True, error="VK anti-bot challenge")

    monkeypatch.setattr(
        "app.tasks.get_adapter", lambda platform, storage_state=None: _ChallengeAdapter()
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.failed
    assert task.error == "VK anti-bot challenge"
    assert not cooldown_route.called
    assert not patch_route.called
    assert release_route.called
    assert put_session_route.called  # обновлённая после проверки сессия сохранена


@respx.mock
def test_unreachable_lead_does_not_shift_offset_for_remaining_leads(monkeypatch):
    """Регрессия: rejected-лид (невалидный номер в WhatsApp / группа с сайтом в VK) исчезает
    из выборки status=new, поэтому offset пагинации не должен сдвигаться на него — иначе
    следующий обычный лид пропускается. Особенно заметно на кампаниях по Яндекс.Картам, где
    невалидных номеров много."""
    import app.tasks as tasks_module
    from app.adapters.base import SendResult

    campaign_id = str(uuid.uuid4())
    template_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    unreachable, sent_lead, after = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    monkeypatch.setattr(tasks_module, "_BATCH_SIZE", 2)

    respx.get(f"{BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, template_id))
    )
    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/templates/{template_id}").mock(
        return_value=httpx.Response(200, json=_template_payload(template_id, campaign_id))
    )
    respx.get(f"{BASE}/messages").mock(return_value=httpx.Response(200, json=[]))

    # Имитируем Data Service: обработанные лиды (contacted/rejected) пропадают из status=new.
    remaining = [unreachable, sent_lead, after]

    def _leads(request):
        offset = int(request.url.params["offset"])
        return httpx.Response(
            200,
            json=[_lead_payload(lead_id) for lead_id in remaining[offset : offset + tasks_module._BATCH_SIZE]],
        )

    leads_route = respx.get(f"{BASE}/leads").mock(side_effect=_leads)
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id))
    )
    respx.get(f"{BASE}/accounts/{account_id}/session").mock(
        return_value=httpx.Response(
            200,
            json={"account_id": account_id, "storage_state": {}, "updated_at": "2026-01-01T00:00:00"},
        )
    )

    def _patch(request):
        lead_id = request.url.path.split("/")[-2]
        if lead_id in remaining:
            remaining.remove(lead_id)
        return httpx.Response(200, json=_lead_payload(lead_id))

    for lead_id in (unreachable, sent_lead, after):
        respx.patch(f"{BASE}/leads/{lead_id}/status").mock(side_effect=_patch)
    post_message_route = respx.post(f"{BASE}/messages").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    class _MixedAdapter:
        def send_message(self, lead, account, text, image=None):
            if lead["id"] == unreachable:
                return SendResult(success=False, error="номер не найден", unreachable=True)
            return SendResult(success=True)

    monkeypatch.setattr(
        tasks_module, "get_adapter", lambda platform, storage_state=None: _MixedAdapter()
    )

    task = create_task(uuid.UUID(campaign_id))
    run_send_task(uuid.UUID(campaign_id))

    assert task.status == SendTaskStatus.done
    assert task.skipped == 1
    assert task.sent == 2  # sent_lead и after — after не пропущен из-за сдвига offset
    assert leads_route.call_count == 3
    assert [call.request.url.params["offset"] for call in leads_route.calls] == ["0", "0", "0"]
    assert post_message_route.call_count == 2


import uuid

import httpx
import respx

from app.adapters.base import CaptchaDetectedError
from app.config import settings
from app.schemas.parse import ParseFiltersSchema
from app.tasks import TaskStatus, create_task, run_parse_task

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
def test_vk_task_goes_queued_running_done_via_fake_adapter():
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
        return_value=httpx.Response(
            200, json={"inserted": 3, "skipped": 0, "lead_ids": [str(uuid.uuid4()) for _ in range(3)]}
        )
    )
    release_route = respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    task = create_task("vk", "fitness")
    assert task.status == TaskStatus.queued

    run_parse_task(task.task_id, "vk", "fitness", ParseFiltersSchema(), None)

    assert task.status == TaskStatus.done
    assert task.found == 3
    assert task.inserted == 3
    assert task.skipped == 0
    assert bulk_route.called
    assert release_route.called


@respx.mock
def test_vk_task_fails_when_no_account_available():
    respx.post(f"{BASE}/accounts/next-available").mock(
        return_value=httpx.Response(409, json={"detail": "no account available"})
    )

    task = create_task("vk", "fitness")
    run_parse_task(task.task_id, "vk", "fitness", ParseFiltersSchema(), None)

    assert task.status == TaskStatus.failed
    assert task.error == "no account available"


@respx.mock
def test_captcha_cooldowns_account_and_does_not_release_it(monkeypatch):
    """Регрессия: cooldown() при капче не должен затираться безусловным release() в finally."""
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
    respx.post(f"{BASE}/leads/bulk").mock(
        return_value=httpx.Response(200, json={"inserted": 0, "skipped": 0, "lead_ids": []})
    )
    cooldown_route = respx.post(f"{BASE}/accounts/{account_id}/cooldown").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="cooldown"))
    )
    release_route = respx.post(f"{BASE}/accounts/{account_id}/release").mock(
        return_value=httpx.Response(200, json=_account_payload(account_id, status="active"))
    )

    class _CaptchaAdapter:
        def search_communities(self, keyword, filters):
            raise CaptchaDetectedError("captcha detected", partial_leads=[])

    monkeypatch.setattr("app.tasks.get_adapter", lambda platform, storage_state=None: _CaptchaAdapter())

    task = create_task("vk", "fitness")
    run_parse_task(task.task_id, "vk", "fitness", ParseFiltersSchema(), None)

    assert task.status == TaskStatus.failed
    assert cooldown_route.called
    assert not release_route.called


def test_stub_platform_task_completes_done_without_touching_accounts():
    task = create_task("tg", "fitness")
    run_parse_task(task.task_id, "tg", "fitness", ParseFiltersSchema(), None)

    # NotImplementedAdapter returns [] — no leads means nothing to insert, no HTTP calls expected.
    assert task.status == TaskStatus.done
    assert task.found == 0
    assert task.inserted == 0

import uuid

import httpx
import respx

from app.config import settings
from app.orchestration import OrchestrationPhase, create_task, run_campaign_flow

DATA_BASE = settings.DATA_SERVICE_URL
PARSER_BASE = settings.PARSER_SERVICE_URL
MESSAGING_BASE = settings.MESSAGING_SERVICE_URL


def _campaign_payload(campaign_id: str, status: str = "draft") -> dict:
    return {
        "id": campaign_id,
        "name": "test",
        "platform": "vk",
        "keyword": "fitness",
        "template_id": None,
        "status": status,
        "created_at": "2026-01-01T00:00:00",
    }


def _parse_status(task_id: str, status: str, error: str | None = None) -> dict:
    return {"task_id": task_id, "status": status, "found": 0, "inserted": 0, "skipped": 0, "error": error}


def _send_status(campaign_id: str, status: str, error: str | None = None) -> dict:
    return {"campaign_id": campaign_id, "status": status, "sent": 0, "failed": 0, "skipped": 0, "error": error}


@respx.mock
def test_flow_completes_through_parsing_and_messaging():
    campaign_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )
    patch_route = respx.patch(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, status="running"))
    )
    respx.post(f"{PARSER_BASE}/parse").mock(
        return_value=httpx.Response(202, json=_parse_status(task_id, "queued"))
    )
    respx.get(f"{PARSER_BASE}/parse/{task_id}/status").mock(
        return_value=httpx.Response(200, json=_parse_status(task_id, "done"))
    )
    respx.post(f"{MESSAGING_BASE}/campaigns/{campaign_id}/send").mock(
        return_value=httpx.Response(202, json=_send_status(campaign_id, "queued"))
    )
    respx.get(f"{MESSAGING_BASE}/campaigns/{campaign_id}/send-status").mock(
        return_value=httpx.Response(200, json=_send_status(campaign_id, "done"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_campaign_flow(uuid.UUID(campaign_id))

    assert task.phase == OrchestrationPhase.done
    assert task.error is None
    assert patch_route.call_count == 2  # running, then completed


@respx.mock
def test_flow_stops_at_parsing_failure_and_pauses_campaign():
    campaign_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )
    patch_route = respx.patch(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, status="paused"))
    )
    respx.post(f"{PARSER_BASE}/parse").mock(
        return_value=httpx.Response(202, json=_parse_status(task_id, "queued"))
    )
    respx.get(f"{PARSER_BASE}/parse/{task_id}/status").mock(
        return_value=httpx.Response(200, json=_parse_status(task_id, "failed", error="no account available"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_campaign_flow(uuid.UUID(campaign_id))

    assert task.phase == OrchestrationPhase.failed
    assert "parser" in task.error
    assert patch_route.call_count == 2  # running, then paused


@respx.mock
def test_flow_stops_when_messaging_waits_for_account():
    campaign_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )
    patch_route = respx.patch(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, status="paused"))
    )
    respx.post(f"{PARSER_BASE}/parse").mock(
        return_value=httpx.Response(202, json=_parse_status(task_id, "queued"))
    )
    respx.get(f"{PARSER_BASE}/parse/{task_id}/status").mock(
        return_value=httpx.Response(200, json=_parse_status(task_id, "done"))
    )
    respx.post(f"{MESSAGING_BASE}/campaigns/{campaign_id}/send").mock(
        return_value=httpx.Response(202, json=_send_status(campaign_id, "queued"))
    )
    respx.get(f"{MESSAGING_BASE}/campaigns/{campaign_id}/send-status").mock(
        return_value=httpx.Response(200, json=_send_status(campaign_id, "waiting_for_account"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_campaign_flow(uuid.UUID(campaign_id))

    assert task.phase == OrchestrationPhase.failed
    assert "messaging" in task.error
    assert patch_route.call_count == 2


@respx.mock
def test_flow_times_out_when_parser_never_finishes(monkeypatch):
    monkeypatch.setattr(settings, "POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(settings, "POLL_TIMEOUT_SEC", 0.05)

    campaign_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )
    patch_route = respx.patch(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, status="paused"))
    )
    respx.post(f"{PARSER_BASE}/parse").mock(
        return_value=httpx.Response(202, json=_parse_status(task_id, "queued"))
    )
    respx.get(f"{PARSER_BASE}/parse/{task_id}/status").mock(
        return_value=httpx.Response(200, json=_parse_status(task_id, "running"))
    )

    task = create_task(uuid.UUID(campaign_id))
    run_campaign_flow(uuid.UUID(campaign_id))

    assert task.phase == OrchestrationPhase.timeout
    assert patch_route.call_count == 2


@respx.mock
def test_flow_fails_when_parser_service_unreachable():
    campaign_id = str(uuid.uuid4())

    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )
    patch_route = respx.patch(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, status="paused"))
    )
    respx.post(f"{PARSER_BASE}/parse").mock(return_value=httpx.Response(500, json={"detail": "boom"}))

    task = create_task(uuid.UUID(campaign_id))
    run_campaign_flow(uuid.UUID(campaign_id))

    assert task.phase == OrchestrationPhase.failed
    assert patch_route.call_count == 2

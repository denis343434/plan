import json
import uuid

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

DATA_BASE = settings.DATA_SERVICE_URL
PARSER_BASE = settings.PARSER_SERVICE_URL
MESSAGING_BASE = settings.MESSAGING_SERVICE_URL

_LEAD_STATUSES = ("new", "queued", "contacted", "replied", "rejected")


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


def _mock_lead_counts(campaign_id: str, counts: dict[str, int]) -> None:
    for status, count in counts.items():
        respx.get(
            f"{DATA_BASE}/leads", params={"campaign_id": campaign_id, "status": status, "limit": 1000}
        ).mock(return_value=httpx.Response(200, json=[{"id": str(uuid.uuid4())} for _ in range(count)]))


@respx.mock
def test_create_campaign_proxies_to_data_service_as_is():
    campaign_id = str(uuid.uuid4())
    create_route = respx.post(f"{DATA_BASE}/campaigns").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )

    with TestClient(app) as client:
        resp = client.post("/campaigns", json={"name": "test", "platform": "vk", "keyword": "fitness"})

    assert resp.status_code == 201
    assert resp.json()["id"] == campaign_id

    sent_body = json.loads(create_route.calls.last.request.content)
    assert sent_body == {"name": "test", "platform": "vk", "keyword": "fitness"}


@respx.mock
def test_get_campaign_status_aggregates_lead_stats():
    campaign_id = str(uuid.uuid4())
    counts = {"new": 2, "queued": 0, "contacted": 5, "replied": 1, "rejected": 0}

    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )
    _mock_lead_counts(campaign_id, counts)

    with TestClient(app) as client:
        resp = client.get(f"/campaigns/{campaign_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == counts
    assert body["phase"] == "idle"
    assert body["error"] is None


@respx.mock
def test_get_campaign_status_404_when_campaign_missing():
    campaign_id = str(uuid.uuid4())
    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )

    with TestClient(app) as client:
        resp = client.get(f"/campaigns/{campaign_id}")

    assert resp.status_code == 404


@respx.mock
def test_start_campaign_runs_flow_to_done_and_status_endpoint_reflects_it():
    campaign_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    respx.get(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id))
    )
    respx.patch(f"{DATA_BASE}/campaigns/{campaign_id}").mock(
        return_value=httpx.Response(200, json=_campaign_payload(campaign_id, status="completed"))
    )
    respx.post(f"{PARSER_BASE}/parse").mock(
        return_value=httpx.Response(
            202, json={"task_id": task_id, "status": "queued", "found": 0, "inserted": 0, "skipped": 0, "error": None}
        )
    )
    respx.get(f"{PARSER_BASE}/parse/{task_id}/status").mock(
        return_value=httpx.Response(
            200, json={"task_id": task_id, "status": "done", "found": 1, "inserted": 1, "skipped": 0, "error": None}
        )
    )
    respx.post(f"{MESSAGING_BASE}/campaigns/{campaign_id}/send").mock(
        return_value=httpx.Response(
            202, json={"campaign_id": campaign_id, "status": "queued", "sent": 0, "failed": 0, "skipped": 0, "error": None}
        )
    )
    respx.get(f"{MESSAGING_BASE}/campaigns/{campaign_id}/send-status").mock(
        return_value=httpx.Response(
            200, json={"campaign_id": campaign_id, "status": "done", "sent": 1, "failed": 0, "skipped": 0, "error": None}
        )
    )
    _mock_lead_counts(campaign_id, {status: 0 for status in _LEAD_STATUSES})

    with TestClient(app) as client:
        start_resp = client.post(f"/campaigns/{campaign_id}/start")
        assert start_resp.status_code == 202

        status_resp = client.get(f"/campaigns/{campaign_id}")
        assert status_resp.json()["phase"] == "done"


@respx.mock
def test_start_campaign_twice_while_in_progress_does_not_spawn_second_flow():
    campaign_id = str(uuid.uuid4())

    from app.orchestration import OrchestrationPhase, OrchestrationTask, TASKS

    TASKS[uuid.UUID(campaign_id)] = OrchestrationTask(
        campaign_id=uuid.UUID(campaign_id), phase=OrchestrationPhase.parsing
    )

    with TestClient(app) as client:
        resp = client.post(f"/campaigns/{campaign_id}/start")

    assert resp.status_code == 202
    assert resp.json()["phase"] == "parsing"

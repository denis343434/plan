import uuid

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

BASE = settings.DATA_SERVICE_URL


@respx.mock
def test_post_followup_runs_task_and_status_endpoint_reflects_it():
    campaign_id = str(uuid.uuid4())

    respx.get(f"{BASE}/templates").mock(return_value=httpx.Response(200, json=[]))

    with TestClient(app) as client:
        resp = client.post(f"/campaigns/{campaign_id}/followup")
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"

        status_resp = client.get(f"/campaigns/{campaign_id}/followup-status")
        body = status_resp.json()
        assert body["status"] == "failed"
        assert body["error"] == "no follow-up template configured for campaign"


def test_followup_status_for_unknown_campaign_is_404():
    with TestClient(app) as client:
        resp = client.get(f"/campaigns/{uuid.uuid4()}/followup-status")
        assert resp.status_code == 404


@respx.mock
def test_post_followup_twice_while_running_does_not_spawn_second_task():
    campaign_id = str(uuid.uuid4())

    from app.followup import FollowupTask, FollowupTaskStatus, TASKS

    TASKS[uuid.UUID(campaign_id)] = FollowupTask(campaign_id=uuid.UUID(campaign_id), status=FollowupTaskStatus.running)

    with TestClient(app) as client:
        resp = client.post(f"/campaigns/{campaign_id}/followup")
        assert resp.status_code == 202
        assert resp.json()["status"] == "running"

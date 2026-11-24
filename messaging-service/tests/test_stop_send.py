import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.tasks import SendTaskStatus, create_task


def test_stop_requests_running_send_to_finish_current_lead():
    campaign_id = uuid.uuid4()
    task = create_task(campaign_id)
    task.status = SendTaskStatus.running

    with TestClient(app) as client:
        response = client.post(f"/campaigns/{campaign_id}/stop")

    assert response.status_code == 200
    # Пока идёт текущий лид, статус ещё running — задача сама выставит stopped сразу после него.
    assert response.json()["status"] == "running"
    assert task.stop_requested is True


def test_stop_marks_queued_or_waiting_send_as_stopped_immediately():
    campaign_id = uuid.uuid4()
    task = create_task(campaign_id)
    task.status = SendTaskStatus.waiting_for_account

    with TestClient(app) as client:
        response = client.post(f"/campaigns/{campaign_id}/stop")

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert task.status == SendTaskStatus.stopped


def test_stop_unknown_task_returns_404():
    with TestClient(app) as client:
        response = client.post(f"/campaigns/{uuid.uuid4()}/stop")
    assert response.status_code == 404

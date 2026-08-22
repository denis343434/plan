from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.schemas.send import SendStatusOut
from app.tasks import SendTask, SendTaskStatus, create_task, get_task, run_send_task

router = APIRouter(prefix="/campaigns", tags=["send"])


def _to_out(task: SendTask) -> SendStatusOut:
    return SendStatusOut(
        campaign_id=task.campaign_id,
        status=task.status,
        sent=task.sent,
        failed=task.failed,
        skipped=task.skipped,
        error=task.error,
    )


@router.post("/{campaign_id}/send", status_code=202, response_model=SendStatusOut)
def start_send(campaign_id: UUID, background_tasks: BackgroundTasks, retry_failed: bool = False) -> SendStatusOut:
    existing = get_task(campaign_id)
    if existing is not None and existing.status == SendTaskStatus.running:
        return _to_out(existing)

    task = create_task(campaign_id)
    background_tasks.add_task(run_send_task, campaign_id, retry_failed)
    return _to_out(task)


@router.get("/{campaign_id}/send-status", response_model=SendStatusOut)
def send_status(campaign_id: UUID) -> SendStatusOut:
    task = get_task(campaign_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"send task for campaign {campaign_id} not found")
    return _to_out(task)

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.followup import FollowupTask, FollowupTaskStatus, create_task, get_task, run_followup_task
from app.schemas.followup import FollowupStatusOut

router = APIRouter(prefix="/campaigns", tags=["followup"])


def _to_out(task: FollowupTask) -> FollowupStatusOut:
    return FollowupStatusOut(
        campaign_id=task.campaign_id,
        status=task.status,
        sent=task.sent,
        failed=task.failed,
        skipped=task.skipped,
        error=task.error,
    )


@router.post("/{campaign_id}/followup", status_code=202, response_model=FollowupStatusOut)
def start_followup(campaign_id: UUID, background_tasks: BackgroundTasks) -> FollowupStatusOut:
    existing = get_task(campaign_id)
    if existing is not None and existing.status == FollowupTaskStatus.running:
        return _to_out(existing)

    task = create_task(campaign_id)
    background_tasks.add_task(run_followup_task, campaign_id)
    return _to_out(task)


@router.get("/{campaign_id}/followup-status", response_model=FollowupStatusOut)
def followup_status(campaign_id: UUID) -> FollowupStatusOut:
    task = get_task(campaign_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"followup task for campaign {campaign_id} not found")
    return _to_out(task)

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.schemas.parse import ParseRequest, ParseTaskOut
from app.tasks import create_task, get_task, run_parse_task

router = APIRouter(prefix="/parse", tags=["parse"])


@router.post("", status_code=202, response_model=ParseTaskOut)
def start_parse(request: ParseRequest, background_tasks: BackgroundTasks) -> ParseTaskOut:
    task = create_task(request.platform, request.keyword)
    background_tasks.add_task(
        run_parse_task,
        task.task_id,
        request.platform,
        request.keyword,
        request.filters,
        request.campaign_id,
    )
    return ParseTaskOut(task_id=task.task_id, status=task.status)


@router.get("/{task_id}/status", response_model=ParseTaskOut)
def parse_status(task_id: UUID) -> ParseTaskOut:
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return ParseTaskOut(
        task_id=task.task_id,
        status=task.status,
        found=task.found,
        inserted=task.inserted,
        skipped=task.skipped,
        error=task.error,
    )

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.inbox import InboxCheckStatus, InboxCheckTask, create_task, get_task, run_inbox_check_task
from app.schemas.inbox import InboxCheckStatusOut

router = APIRouter(prefix="/inbox", tags=["inbox"])


def _to_out(task: InboxCheckTask) -> InboxCheckStatusOut:
    return InboxCheckStatusOut(
        account_id=task.account_id,
        status=task.status,
        checked=task.checked,
        total=task.total,
        replied=task.replied,
        error=task.error,
        results=[
            {
                "message_id": r.message_id,
                "lead_id": r.lead_id,
                "has_reply": r.has_reply,
                "preview": r.preview,
                "error": r.error,
            }
            for r in task.results
        ],
    )


@router.post("/check", status_code=202, response_model=InboxCheckStatusOut)
def start_check(account_id: UUID, background_tasks: BackgroundTasks, limit: int = 20) -> InboxCheckStatusOut:
    key = str(account_id)
    existing = get_task(key)
    if existing is not None and existing.status == InboxCheckStatus.running:
        return _to_out(existing)

    task = create_task(key)
    background_tasks.add_task(run_inbox_check_task, key, limit)
    return _to_out(task)


@router.get("/check-status", response_model=InboxCheckStatusOut)
def check_status(account_id: UUID) -> InboxCheckStatusOut:
    task = get_task(str(account_id))
    if task is None:
        raise HTTPException(status_code=404, detail=f"no inbox check task for account {account_id}")
    return _to_out(task)

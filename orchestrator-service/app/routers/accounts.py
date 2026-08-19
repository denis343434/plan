from uuid import UUID

from fastapi import APIRouter

from app.clients.data_service import DataServiceClient
from app.schemas.account import PauseRequest

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("/{account_id}/pause")
def pause_account(account_id: UUID, request: PauseRequest = PauseRequest()) -> dict:
    client = DataServiceClient()
    try:
        return client.cooldown_account(
            account_id,
            minutes=request.minutes,
            reason=request.reason or "paused via orchestrator",
            permanent=request.permanent,
        )
    finally:
        client.close()

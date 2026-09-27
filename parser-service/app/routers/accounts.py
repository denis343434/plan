from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.clients.data_service import DataServiceClient
from app.config import settings
from app.exceptions import DataServiceNotFoundError
from app.schemas.accounts import SessionCheckOut

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/{account_id}/session-check", response_model=SessionCheckOut)
def check_account_session(account_id: UUID, platform: str = "vk") -> SessionCheckOut:
    # platform приходит от вызывающего (desktop-client знает платформу аккаунта из своего кэша) —
    # Data Service не даёт получить один аккаунт по id отдельным эндпоинтом, поэтому не тянем его
    # сюда сами; дефолт "vk" сохраняет прежнее поведение для вызовов без параметра.
    client = DataServiceClient()
    try:
        session = client.get_session(str(account_id))
    except DataServiceNotFoundError:
        raise HTTPException(status_code=404, detail=f"account {account_id} not found")
    finally:
        client.close()

    storage_state = session.get("storage_state")
    if platform == "vk" and settings.VK_ADAPTER_MODE == "playwright":
        from app.adapters.vk import check_session  # lazy: avoid requiring playwright browsers in fake mode

        valid = check_session(storage_state)
    elif platform == "instagram" and settings.IG_ADAPTER_MODE == "playwright":
        from app.adapters.instagram import check_session  # lazy: same reason as VK above

        valid = check_session(storage_state)
    else:
        # fake-режим (или платформа без настоящего адаптера) не открывает браузер — единственный
        # доступный сигнал: сохраняли ли вообще сессию (см. has_session в Data Service), а не
        # жива ли она на самом деле.
        valid = bool(storage_state)
    return SessionCheckOut(account_id=str(account_id), valid=valid)

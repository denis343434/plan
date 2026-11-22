from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.crud import stats as crud
from app.database import get_db
from app.enums import Platform
from app.schemas.stats import StatsGroupBy, StatsOut

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/messages", response_model=StatsOut)
def message_stats(
    group_by: StatsGroupBy = StatsGroupBy.total,
    campaign_id: UUID | None = None,
    platform: Platform | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    reply_window_hours: int = Query(24, ge=1, le=24 * 90),
    tz: str = "Europe/Moscow",
    db: Session = Depends(get_db),
) -> StatsOut:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from позже date_to")
    if not crud.is_valid_timezone(db, tz):
        raise HTTPException(status_code=422, detail=f"неизвестный часовой пояс: {tz}")
    rows = crud.message_stats(
        db,
        group_by=group_by,
        campaign_id=campaign_id,
        platform=platform.value if platform else None,
        date_from=date_from,
        date_to=date_to,
        reply_window_hours=reply_window_hours,
        tz=tz,
    )
    return StatsOut(
        group_by=group_by,
        reply_window_hours=reply_window_hours,
        tz=tz,
        date_from=date_from,
        date_to=date_to,
        rows=rows,
    )

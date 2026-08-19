from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.crud import leads as crud
from app.database import get_db
from app.enums import LeadStatus, Platform
from app.schemas.lead import LeadBulkCreate, LeadBulkResult, LeadOut, LeadStatusUpdate

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post("/bulk", response_model=LeadBulkResult)
def bulk_create_leads(leads: LeadBulkCreate, db: Session = Depends(get_db)) -> LeadBulkResult:
    return crud.bulk_insert(db, leads)


@router.get("", response_model=list[LeadOut])
def list_leads(
    status: LeadStatus | None = None,
    platform: Platform | None = None,
    campaign_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[LeadOut]:
    return crud.list_leads(
        db, status=status, platform=platform, campaign_id=campaign_id, limit=limit, offset=offset
    )


@router.patch("/{lead_id}/status", response_model=LeadOut)
def update_lead_status(
    lead_id: UUID, update: LeadStatusUpdate, db: Session = Depends(get_db)
) -> LeadOut:
    return crud.update_status(db, lead_id, update.status)

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.crud import campaigns as crud
from app.database import get_db
from app.schemas.campaign import CampaignCreate, CampaignOut, CampaignUpdate

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.post("", response_model=CampaignOut)
def create_campaign(campaign: CampaignCreate, db: Session = Depends(get_db)) -> CampaignOut:
    return crud.create_campaign(db, campaign)


@router.get("", response_model=list[CampaignOut])
def list_campaigns(db: Session = Depends(get_db)) -> list[CampaignOut]:
    return crud.list_campaigns(db)


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: UUID, db: Session = Depends(get_db)) -> CampaignOut:
    return crud.get_campaign(db, campaign_id)


@router.patch("/{campaign_id}", response_model=CampaignOut)
def update_campaign(
    campaign_id: UUID, update: CampaignUpdate, db: Session = Depends(get_db)
) -> CampaignOut:
    return crud.update_campaign(db, campaign_id, update)


@router.delete("/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: UUID, db: Session = Depends(get_db)) -> None:
    crud.delete_campaign(db, campaign_id)

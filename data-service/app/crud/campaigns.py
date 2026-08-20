from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models.campaign import Campaign
from app.schemas.campaign import CampaignCreate, CampaignUpdate


def create_campaign(db: Session, campaign: CampaignCreate) -> Campaign:
    db_campaign = Campaign(**campaign.model_dump())
    db.add(db_campaign)
    db.commit()
    db.refresh(db_campaign)
    return db_campaign


def list_campaigns(db: Session) -> list[Campaign]:
    return list(db.execute(select(Campaign)).scalars().all())


def get_campaign(db: Session, campaign_id: UUID) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"campaign {campaign_id} not found")
    return campaign


def update_campaign(db: Session, campaign_id: UUID, update: CampaignUpdate) -> Campaign:
    campaign = get_campaign(db, campaign_id)
    for field, value in update.model_dump(exclude_unset=True).items():
        setattr(campaign, field, value)
    db.commit()
    db.refresh(campaign)
    return campaign


def delete_campaign(db: Session, campaign_id: UUID) -> None:
    # leads.campaign_id/templates.campaign_id — ON DELETE SET NULL (см. alembic-миграцию), т.е.
    # сами лиды и шаблоны не удаляются, только теряют привязку к этой кампании.
    campaign = get_campaign(db, campaign_id)
    db.delete(campaign)
    db.commit()

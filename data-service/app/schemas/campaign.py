from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.enums import CampaignStatus, Platform


class CampaignCreate(BaseModel):
    name: str
    platform: Platform
    keyword: str
    template_id: UUID | None = None


class CampaignUpdate(BaseModel):
    name: str | None = None
    status: CampaignStatus | None = None
    template_id: UUID | None = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    platform: Platform
    keyword: str
    template_id: UUID | None
    status: CampaignStatus
    created_at: datetime

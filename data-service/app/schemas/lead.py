from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.enums import LeadStatus, Platform


class LeadCreate(BaseModel):
    platform: Platform
    external_id: str
    group_url: str
    admin_contact: str | None = None
    title: str | None = None
    campaign_id: UUID | None = None


LeadBulkCreate = list[LeadCreate]


class LeadBulkResult(BaseModel):
    inserted: int
    skipped: int
    lead_ids: list[UUID]


class LeadStatusUpdate(BaseModel):
    status: LeadStatus


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    platform: Platform
    external_id: str
    group_url: str
    admin_contact: str | None
    title: str | None
    status: LeadStatus
    campaign_id: UUID | None
    found_at: datetime

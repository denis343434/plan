from uuid import UUID

from pydantic import BaseModel


class CampaignCreate(BaseModel):
    name: str
    platform: str
    keyword: str
    template_id: UUID | None = None


class CampaignStartResponse(BaseModel):
    campaign_id: UUID
    phase: str


class CampaignStatusOut(BaseModel):
    campaign: dict
    phase: str
    error: str | None = None
    stats: dict[str, int]

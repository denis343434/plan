from uuid import UUID

from pydantic import BaseModel


class CampaignCreate(BaseModel):
    name: str
    platform: str
    keyword: str
    template_id: UUID | None = None
    use_account: bool = True
    # Параметры источника (город/каналы для yandex_maps) — см. Campaign.parse_params в Data Service.
    parse_params: dict | None = None


class CampaignStartResponse(BaseModel):
    campaign_id: UUID
    phase: str


class CampaignStatusOut(BaseModel):
    campaign: dict
    phase: str
    error: str | None = None
    note: str | None = None
    stats: dict[str, int]
    progress: dict | None = None

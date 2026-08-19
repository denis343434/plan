from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TemplateCreate(BaseModel):
    campaign_id: UUID | None = None
    variant: str
    body: str


class TemplateUpdate(BaseModel):
    campaign_id: UUID | None = None
    variant: str | None = None
    body: str | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID | None
    variant: str
    body: str

from uuid import UUID

from pydantic import BaseModel


class ParseFiltersSchema(BaseModel):
    has_site: bool | None = None


class ParseRequest(BaseModel):
    platform: str
    keyword: str
    filters: ParseFiltersSchema = ParseFiltersSchema()
    campaign_id: UUID | None = None


class ParseTaskOut(BaseModel):
    task_id: UUID
    status: str  # queued | running | done | failed
    found: int = 0
    inserted: int = 0
    skipped: int = 0
    error: str | None = None

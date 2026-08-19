from uuid import UUID

from pydantic import BaseModel


class SendStatusOut(BaseModel):
    campaign_id: UUID
    status: str  # queued | running | done | waiting_for_account | failed
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None

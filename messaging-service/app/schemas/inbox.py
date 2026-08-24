from uuid import UUID

from pydantic import BaseModel


class InboxCheckResultItem(BaseModel):
    message_id: UUID
    lead_id: UUID
    has_reply: bool
    preview: str | None = None
    error: str | None = None


class InboxCheckStatusOut(BaseModel):
    account_id: UUID
    status: str  # queued | running | done | failed
    checked: int = 0
    total: int = 0
    replied: int = 0
    error: str | None = None
    has_more: bool = False
    session_expired: bool = False
    results: list[InboxCheckResultItem] = []

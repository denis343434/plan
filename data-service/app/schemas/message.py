from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.enums import DeliveryStatus, ReplyStatus


class MessageCreate(BaseModel):
    lead_id: UUID
    account_id: UUID
    template_variant: str | None = None
    text_sent: str
    delivery_status: DeliveryStatus = DeliveryStatus.pending
    reply_status: ReplyStatus = ReplyStatus.none
    error_reason: str | None = None


class MessageReplyUpdate(BaseModel):
    reply_status: ReplyStatus
    reply_preview: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lead_id: UUID
    account_id: UUID
    template_variant: str | None
    text_sent: str
    sent_at: datetime
    delivery_status: DeliveryStatus
    reply_status: ReplyStatus
    error_reason: str | None
    reply_preview: str | None
    replied_at: datetime | None

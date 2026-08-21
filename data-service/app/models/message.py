from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPK


class Message(UUIDPK, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "delivery_status IN ('sent','failed','pending')", name="ck_messages_delivery_status"
        ),
        CheckConstraint(
            "reply_status IN ('none','replied','no_reply')", name="ck_messages_reply_status"
        ),
        Index("ix_messages_account_sent_at", "account_id", "sent_at"),
    )

    lead_id: Mapped[UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    template_variant: Mapped[str | None] = mapped_column(default=None)
    text_sent: Mapped[str] = mapped_column(nullable=False)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    delivery_status: Mapped[str] = mapped_column(
        nullable=False, default="pending", server_default="pending"
    )
    reply_status: Mapped[str] = mapped_column(nullable=False, default="none", server_default="none")
    error_reason: Mapped[str | None] = mapped_column(default=None)
    reply_preview: Mapped[str | None] = mapped_column(default=None)
    replied_at: Mapped[datetime | None] = mapped_column(default=None)

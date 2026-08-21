from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPK


class Lead(UUIDPK, Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_leads_platform_external_id"),
        CheckConstraint(
            "status IN ('new','queued','contacted','replied','rejected')",
            name="ck_leads_status",
        ),
        Index("ix_leads_status_platform_campaign", "status", "platform", "campaign_id"),
    )

    platform: Mapped[str] = mapped_column(nullable=False)
    external_id: Mapped[str] = mapped_column(nullable=False)
    group_url: Mapped[str] = mapped_column(nullable=False)
    admin_contact: Mapped[str | None] = mapped_column(default=None)
    title: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(nullable=False, default="new", server_default="new")
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), default=None
    )
    found_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    chat_href: Mapped[str | None] = mapped_column(default=None)

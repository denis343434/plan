from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPK


class Campaign(UUIDPK, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','running','paused','completed')",
            name="ck_campaigns_status",
        ),
    )

    name: Mapped[str] = mapped_column(nullable=False)
    platform: Mapped[str] = mapped_column(nullable=False)
    keyword: Mapped[str] = mapped_column(nullable=False)
    # FK to templates.id added in migration (op.create_foreign_key) — logical cycle with templates.campaign_id.
    template_id: Mapped[UUID | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(nullable=False, default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

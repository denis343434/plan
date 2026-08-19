from datetime import datetime

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPK


class Account(UUIDPK, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("purpose IN ('parsing','messaging','both')", name="ck_accounts_purpose"),
        CheckConstraint(
            "status IN ('active','flood','banned','cooldown','locked')",
            name="ck_accounts_status",
        ),
        Index("ix_accounts_status_platform_purpose", "status", "platform", "purpose"),
    )

    platform: Mapped[str] = mapped_column(nullable=False)
    login: Mapped[str] = mapped_column(nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(nullable=False)
    proxy: Mapped[str | None] = mapped_column(default=None)
    user_agent: Mapped[str | None] = mapped_column(default=None)
    viewport: Mapped[str | None] = mapped_column(default=None)
    hourly_limit: Mapped[int] = mapped_column(nullable=False)
    daily_limit: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, default="active", server_default="active")
    warmup_stage: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    cooldown_until: Mapped[datetime | None] = mapped_column(default=None)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    locked_task_ref: Mapped[str | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)

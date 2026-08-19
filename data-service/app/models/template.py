from uuid import UUID

from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPK


class Template(UUIDPK, Base):
    __tablename__ = "templates"

    # FK to campaigns.id added in migration (op.create_foreign_key) — logical cycle with campaigns.template_id.
    campaign_id: Mapped[UUID | None] = mapped_column(default=None)
    variant: Mapped[str] = mapped_column(nullable=False)
    body: Mapped[str] = mapped_column(nullable=False)

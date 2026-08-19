from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models.lead import Lead
from app.schemas.lead import LeadBulkResult, LeadCreate


def bulk_insert(db: Session, leads: list[LeadCreate]) -> LeadBulkResult:
    if not leads:
        return LeadBulkResult(inserted=0, skipped=0, lead_ids=[])

    values = [lead.model_dump(mode="json") for lead in leads]
    stmt = (
        pg_insert(Lead)
        .values(values)
        .on_conflict_do_nothing(index_elements=["platform", "external_id"])
        .returning(Lead.id)
    )
    result = db.execute(stmt)
    inserted_ids = [row[0] for row in result.fetchall()]
    db.commit()

    return LeadBulkResult(
        inserted=len(inserted_ids),
        skipped=len(leads) - len(inserted_ids),
        lead_ids=inserted_ids,
    )


def list_leads(
    db: Session,
    status: str | None = None,
    platform: str | None = None,
    campaign_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Lead]:
    stmt = select(Lead)
    if status is not None:
        stmt = stmt.where(Lead.status == status)
    if platform is not None:
        stmt = stmt.where(Lead.platform == platform)
    if campaign_id is not None:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    stmt = stmt.order_by(Lead.found_at).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def update_status(db: Session, lead_id: UUID, status: str) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise NotFoundError(f"lead {lead_id} not found")
    lead.status = status
    db.commit()
    db.refresh(lead)
    return lead

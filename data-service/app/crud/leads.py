from uuid import UUID

from sqlalchemy import select, update
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
        .returning(Lead.id, Lead.platform, Lead.external_id)
    )
    result = db.execute(stmt)
    inserted_rows = result.fetchall()
    inserted_ids = [row.id for row in inserted_rows]
    inserted_keys = {(row.platform, row.external_id) for row in inserted_rows}

    # Лиды, уже существующие в базе (например, найдены раньше кампанией, которую потом удалили —
    # при удалении campaign_id у её лидов зануляется, см. Lead.campaign_id ondelete=SET NULL),
    # при повторном обнаружении текущей кампанией нужно перепривязать к ней. Иначе такой лид
    # молча "теряется": on_conflict_do_nothing его пропускает, он остаётся ничьим и не появляется
    # в списке лидов кампании, хотя парсер только что нашёл его снова. Перепривязываем только
    # если лид сейчас ничей (campaign_id IS NULL) — не отбираем лиды у чужой активной кампании.
    for value in values:
        campaign_id = value.get("campaign_id")
        key = (value["platform"], value["external_id"])
        if campaign_id is None or key in inserted_keys:
            continue
        db.execute(
            update(Lead)
            .where(
                Lead.platform == key[0],
                Lead.external_id == key[1],
                Lead.campaign_id.is_(None),
            )
            .values(campaign_id=campaign_id)
        )

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

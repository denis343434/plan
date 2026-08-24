from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models.template import Template
from app.schemas.template import TemplateCreate, TemplateUpdate


def create_template(db: Session, template: TemplateCreate) -> Template:
    # Один вариант (A/B) на кампанию — не более одного шаблона: повторное сохранение того же
    # варианта обновляет существующую запись вместо создания дубликата (см. uq_templates_
    # campaign_id_variant в миграции 0006 — то же самое ограничение продублировано на уровне
    # БД). Раньше desktop-client каждый клик "Сохранить шаблон" слал голый POST — 4 клика
    # по одному и тому же варианту A давали 4 одинаковые строки в БД.
    existing = None
    if template.campaign_id is not None:
        existing = db.execute(
            select(Template).where(
                Template.campaign_id == template.campaign_id,
                Template.variant == template.variant,
            )
        ).scalar_one_or_none()
    if existing is not None:
        existing.body = template.body
        db.commit()
        db.refresh(existing)
        return existing

    db_template = Template(**template.model_dump())
    db.add(db_template)
    db.commit()
    db.refresh(db_template)
    return db_template


def list_templates(db: Session) -> list[Template]:
    return list(db.execute(select(Template)).scalars().all())


def get_template(db: Session, template_id: UUID) -> Template:
    template = db.get(Template, template_id)
    if template is None:
        raise NotFoundError(f"template {template_id} not found")
    return template


def update_template(db: Session, template_id: UUID, update: TemplateUpdate) -> Template:
    template = get_template(db, template_id)
    for field, value in update.model_dump(exclude_unset=True).items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return template


def delete_template(db: Session, template_id: UUID) -> None:
    # campaigns.template_id — ON DELETE SET NULL (см. fk_campaigns_template_id_templates в
    # миграции 0001), кампания, ссылавшаяся на этот шаблон, просто останется без template_id.
    template = get_template(db, template_id)
    db.delete(template)
    db.commit()

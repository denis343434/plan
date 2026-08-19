from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models.template import Template
from app.schemas.template import TemplateCreate, TemplateUpdate


def create_template(db: Session, template: TemplateCreate) -> Template:
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

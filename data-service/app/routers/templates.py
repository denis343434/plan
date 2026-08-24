from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.crud import templates as crud
from app.database import get_db
from app.schemas.template import TemplateCreate, TemplateOut, TemplateUpdate

router = APIRouter(prefix="/templates", tags=["templates"])


@router.post("", response_model=TemplateOut)
def create_template(template: TemplateCreate, db: Session = Depends(get_db)) -> TemplateOut:
    return crud.create_template(db, template)


@router.get("", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)) -> list[TemplateOut]:
    return crud.list_templates(db)


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: UUID, db: Session = Depends(get_db)) -> TemplateOut:
    return crud.get_template(db, template_id)


@router.patch("/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: UUID, update: TemplateUpdate, db: Session = Depends(get_db)
) -> TemplateOut:
    return crud.update_template(db, template_id, update)


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: UUID, db: Session = Depends(get_db)) -> None:
    crud.delete_template(db, template_id)

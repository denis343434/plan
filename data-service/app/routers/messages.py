from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.crud import messages as crud
from app.database import get_db
from app.schemas.message import MessageCreate, MessageOut

router = APIRouter(prefix="/messages", tags=["messages"])


@router.post("", response_model=MessageOut)
def create_message(message: MessageCreate, db: Session = Depends(get_db)) -> MessageOut:
    return crud.create_message(db, message)


@router.get("", response_model=list[MessageOut])
def list_messages(
    lead_id: UUID | None = None,
    account_id: UUID | None = None,
    db: Session = Depends(get_db),
) -> list[MessageOut]:
    return crud.list_messages(db, lead_id=lead_id, account_id=account_id)

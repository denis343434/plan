from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError
from app.models.session import Session as SessionModel
from app.security import decrypt_json, encrypt_json


def upsert_session(db: Session, account_id: UUID, storage_state: dict) -> SessionModel:
    stmt = select(SessionModel).where(SessionModel.account_id == account_id)
    session = db.execute(stmt).scalar_one_or_none()
    encrypted = encrypt_json(storage_state)

    if session is None:
        session = SessionModel(account_id=account_id, storage_state_enc=encrypted)
        db.add(session)
    else:
        session.storage_state_enc = encrypted

    db.commit()
    db.refresh(session)
    return session


def get_session(db: Session, account_id: UUID) -> tuple[SessionModel, dict]:
    stmt = select(SessionModel).where(SessionModel.account_id == account_id)
    session = db.execute(stmt).scalar_one_or_none()
    if session is None:
        raise NotFoundError(f"session for account {account_id} not found")
    return session, decrypt_json(session.storage_state_enc)

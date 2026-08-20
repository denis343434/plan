from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.crud import accounts as crud
from app.crud import sessions as sessions_crud
from app.database import get_db
from app.enums import AccountPurpose, AccountStatus, Platform
from app.schemas.account import (
    AccountCreate,
    AccountOut,
    CooldownRequest,
    NextAvailableRequest,
)
from app.schemas.session import SessionIn, SessionOut

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _has_session(db: Session, account_id: UUID) -> bool:
    return bool(sessions_crud.existing_session_account_ids(db, [account_id]))


@router.post("", response_model=AccountOut)
def create_account(account: AccountCreate, db: Session = Depends(get_db)) -> AccountOut:
    db_account = crud.create_account(db, account)
    return crud._to_out(db_account, 0, 0, False)


@router.get("", response_model=list[AccountOut])
def list_accounts(
    status: AccountStatus | None = None,
    platform: Platform | None = None,
    purpose: AccountPurpose | None = None,
    db: Session = Depends(get_db),
) -> list[AccountOut]:
    return crud.list_accounts(db, status=status, platform=platform, purpose=purpose)


@router.post("/next-available", response_model=AccountOut)
def next_available(request: NextAvailableRequest, db: Session = Depends(get_db)) -> AccountOut:
    return crud.next_available(
        db,
        platform=request.platform,
        purpose=request.purpose,
        lock_ttl_seconds=request.lock_ttl_seconds,
        task_ref=request.task_ref,
    )


@router.post("/{account_id}/lock", response_model=AccountOut)
def lock_account(
    account_id: UUID,
    lock_ttl_seconds: int = 900,
    task_ref: str | None = None,
    db: Session = Depends(get_db),
) -> AccountOut:
    account = crud.lock(db, account_id, lock_ttl_seconds, task_ref)
    usage = crud.usage_counts(db, [account.id])
    return crud._to_out(account, *usage.get(account.id, (0, 0)), _has_session(db, account.id))


@router.post("/{account_id}/release", response_model=AccountOut)
def release_account(account_id: UUID, db: Session = Depends(get_db)) -> AccountOut:
    account = crud.release(db, account_id)
    usage = crud.usage_counts(db, [account.id])
    return crud._to_out(account, *usage.get(account.id, (0, 0)), _has_session(db, account.id))


@router.post("/{account_id}/cooldown", response_model=AccountOut)
def cooldown_account(
    account_id: UUID, request: CooldownRequest, db: Session = Depends(get_db)
) -> AccountOut:
    account = crud.cooldown(db, account_id, request.minutes, request.permanent, request.reason)
    usage = crud.usage_counts(db, [account.id])
    return crud._to_out(account, *usage.get(account.id, (0, 0)), _has_session(db, account.id))


@router.post("/{account_id}/activate", response_model=AccountOut)
def activate_account(account_id: UUID, db: Session = Depends(get_db)) -> AccountOut:
    account = crud.activate(db, account_id)
    usage = crud.usage_counts(db, [account.id])
    return crud._to_out(account, *usage.get(account.id, (0, 0)), _has_session(db, account.id))


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: UUID, db: Session = Depends(get_db)) -> None:
    crud.delete_account(db, account_id)


@router.put("/{account_id}/session", response_model=SessionOut)
def put_session(account_id: UUID, session_in: SessionIn, db: Session = Depends(get_db)) -> SessionOut:
    session = sessions_crud.upsert_session(db, account_id, session_in.storage_state)
    return SessionOut(
        account_id=session.account_id,
        storage_state=session_in.storage_state,
        updated_at=session.updated_at,
    )


@router.get("/{account_id}/session", response_model=SessionOut)
def get_session(account_id: UUID, db: Session = Depends(get_db)) -> SessionOut:
    session, storage_state = sessions_crud.get_session(db, account_id)
    return SessionOut(
        account_id=session.account_id, storage_state=storage_state, updated_at=session.updated_at
    )

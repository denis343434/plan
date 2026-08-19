from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crud.messages import usage_counts
from app.exceptions import NoAccountAvailableError, NotFoundError
from app.models.account import Account
from app.schemas.account import AccountCreate, AccountOut


def create_account(db: Session, account: AccountCreate) -> Account:
    db_account = Account(**account.model_dump())
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return db_account


def _to_out(account: Account, hourly_used: int, daily_used: int) -> AccountOut:
    return AccountOut(
        id=account.id,
        platform=account.platform,
        login=account.login,
        purpose=account.purpose,
        proxy=account.proxy,
        user_agent=account.user_agent,
        viewport=account.viewport,
        hourly_limit=account.hourly_limit,
        daily_limit=account.daily_limit,
        status=account.status,
        warmup_stage=account.warmup_stage,
        cooldown_until=account.cooldown_until,
        locked_until=account.locked_until,
        locked_task_ref=account.locked_task_ref,
        last_used_at=account.last_used_at,
        hourly_used=hourly_used,
        daily_used=daily_used,
    )


def list_accounts(
    db: Session,
    status: str | None = None,
    platform: str | None = None,
    purpose: str | None = None,
) -> list[AccountOut]:
    stmt = select(Account)
    if status is not None:
        stmt = stmt.where(Account.status == status)
    if platform is not None:
        stmt = stmt.where(Account.platform == platform)
    if purpose is not None:
        stmt = stmt.where(Account.purpose == purpose)
    accounts = list(db.execute(stmt).scalars().all())

    usage = usage_counts(db, [a.id for a in accounts])
    return [_to_out(a, *usage.get(a.id, (0, 0))) for a in accounts]


def next_available(
    db: Session,
    platform: str,
    purpose: str,
    lock_ttl_seconds: int,
    task_ref: str | None,
) -> AccountOut:
    now = datetime.now(timezone.utc)

    stmt = (
        select(Account)
        .where(Account.platform == platform)
        .where(Account.purpose.in_([purpose, "both"]))
        .where(
            (Account.status == "active")
            | ((Account.status == "locked") & (Account.locked_until < now))
        )
        .order_by(Account.warmup_stage, Account.last_used_at.asc().nulls_first())
        .limit(20)
        .with_for_update(skip_locked=True)
    )
    candidates = list(db.execute(stmt).scalars().all())
    if not candidates:
        raise NoAccountAvailableError("no active/unlocked accounts match criteria")

    usage = usage_counts(db, [a.id for a in candidates])

    for account in candidates:
        hourly_used, daily_used = usage.get(account.id, (0, 0))
        if hourly_used >= account.hourly_limit or daily_used >= account.daily_limit:
            continue

        account.status = "locked"
        account.locked_until = now + timedelta(seconds=lock_ttl_seconds)
        account.locked_task_ref = task_ref
        account.last_used_at = now
        db.commit()
        db.refresh(account)
        return _to_out(account, hourly_used, daily_used)

    raise NoAccountAvailableError("all candidate accounts are rate-limited")


def _get_locked(db: Session, account_id: UUID) -> Account:
    stmt = select(Account).where(Account.id == account_id).with_for_update()
    account = db.execute(stmt).scalar_one_or_none()
    if account is None:
        raise NotFoundError(f"account {account_id} not found")
    return account


def lock(db: Session, account_id: UUID, lock_ttl_seconds: int, task_ref: str | None) -> Account:
    account = _get_locked(db, account_id)
    now = datetime.now(timezone.utc)
    account.status = "locked"
    account.locked_until = now + timedelta(seconds=lock_ttl_seconds)
    account.locked_task_ref = task_ref
    account.last_used_at = now
    db.commit()
    db.refresh(account)
    return account


def release(db: Session, account_id: UUID) -> Account:
    account = _get_locked(db, account_id)
    account.status = "active"
    account.locked_until = None
    account.locked_task_ref = None
    db.commit()
    db.refresh(account)
    return account


def cooldown(
    db: Session, account_id: UUID, minutes: int, permanent: bool, reason: str | None
) -> Account:
    account = _get_locked(db, account_id)
    if permanent:
        account.status = "banned"
        account.cooldown_until = None
    else:
        account.status = "cooldown"
        account.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    account.locked_until = None
    account.locked_task_ref = None
    db.commit()
    db.refresh(account)
    return account


def get_account(db: Session, account_id: UUID) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise NotFoundError(f"account {account_id} not found")
    return account

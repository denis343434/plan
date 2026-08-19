from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.enums import AccountPurpose, AccountStatus, Platform


class AccountCreate(BaseModel):
    platform: Platform
    login: str
    purpose: AccountPurpose
    proxy: str | None = None
    user_agent: str | None = None
    viewport: str | None = None
    hourly_limit: int
    daily_limit: int


class AccountOut(BaseModel):
    id: UUID
    platform: Platform
    login: str
    purpose: AccountPurpose
    proxy: str | None
    user_agent: str | None
    viewport: str | None
    hourly_limit: int
    daily_limit: int
    status: AccountStatus
    warmup_stage: int
    cooldown_until: datetime | None
    locked_until: datetime | None
    locked_task_ref: str | None
    last_used_at: datetime | None
    hourly_used: int
    daily_used: int


class NextAvailableRequest(BaseModel):
    platform: Platform
    purpose: AccountPurpose
    lock_ttl_seconds: int = 900
    task_ref: str | None = None


class CooldownRequest(BaseModel):
    minutes: int
    permanent: bool = False
    reason: str | None = None

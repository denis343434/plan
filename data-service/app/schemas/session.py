from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SessionIn(BaseModel):
    storage_state: dict


class SessionOut(BaseModel):
    account_id: UUID
    storage_state: dict
    updated_at: datetime

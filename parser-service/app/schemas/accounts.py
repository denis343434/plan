from pydantic import BaseModel


class SessionCheckOut(BaseModel):
    account_id: str
    valid: bool

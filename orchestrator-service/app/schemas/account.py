from pydantic import BaseModel

# 7 дней — "большое число" минут по умолчанию для ручной аварийной паузы аккаунта.
_DEFAULT_PAUSE_MINUTES = 10080


class PauseRequest(BaseModel):
    minutes: int = _DEFAULT_PAUSE_MINUTES
    permanent: bool = False
    reason: str | None = None

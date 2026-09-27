from uuid import UUID

from pydantic import BaseModel


class ParseFiltersSchema(BaseModel):
    has_site: bool | None = None
    max_groups: int | None = None


class ParseRequest(BaseModel):
    platform: str
    keyword: str
    filters: ParseFiltersSchema = ParseFiltersSchema()
    campaign_id: UUID | None = None
    # True (по умолчанию) — парсинг через сохранённую сессию аккаунта (прежнее единственное
    # поведение), False — анонимным браузером без логина. См. app/tasks.py::run_parse_task.
    use_account: bool = True


class ParseTaskOut(BaseModel):
    task_id: UUID
    status: str  # queued | running | done | failed
    found: int = 0
    inserted: int = 0
    skipped: int = 0
    error: str | None = None
    progress_checked: int = 0
    progress_total: int = 0

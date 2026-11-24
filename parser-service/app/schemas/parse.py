from uuid import UUID

from pydantic import BaseModel


class ParseFiltersSchema(BaseModel):
    has_site: bool | None = None
    max_groups: int | None = None
    # yandex_maps — см. ParseFilters в adapters/base.py. country добавился позже (выбор страны
    # в desktop-client), поэтому None должен оставаться рабочим значением для старых кампаний.
    city: str | None = None
    country: str | None = None
    channels: list[str] | None = None
    whatsapp_by_phone: bool = False
    search_socials: bool = False
    # Устаревшее имя search_socials (кампании, созданные до 2026-10-04) — читается как оно.
    tg_by_phone: bool = False


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

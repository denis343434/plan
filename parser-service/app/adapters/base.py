from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class ParseFilters:
    has_site: bool | None = None
    # Верхняя граница числа кандидатов, которых стоит проверять/возвращать за один прогон
    # поиска — None означает "без ограничения" (берём всё, что нашлось на странице выдачи).
    max_groups: int | None = None


@dataclass
class RawLead:
    external_id: str
    group_url: str
    admin_contact: str | None = None
    title: str | None = None


class SourceAdapter(Protocol):
    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[RawLead]: ...


class CaptchaDetectedError(Exception):
    """Raised by an adapter when VK anti-bot/captcha is detected mid-scrape.

    Carries whatever leads were already collected so the caller can still
    persist a partial result instead of losing it.
    """

    def __init__(self, message: str, partial_leads: list[RawLead] | None = None) -> None:
        super().__init__(message)
        self.partial_leads = partial_leads or []

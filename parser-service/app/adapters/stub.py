import logging

from app.adapters.base import ParseFilters, RawLead

logger = logging.getLogger(__name__)


class NotImplementedAdapter:
    """tg/instagram — интерфейс адаптера общий, реализация ещё не готова.

    Возвращает пустой список вместо исключения, чтобы Оркестратор мог дёргать
    любой platform, не падая.
    """

    def __init__(self, platform: str) -> None:
        self._platform = platform

    def search_communities(self, keyword: str, filters: ParseFilters) -> list[RawLead]:
        logger.info("platform %s not supported yet", self._platform)
        return []

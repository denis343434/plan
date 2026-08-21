import logging
from typing import Callable

from app.adapters.base import ParseFilters, RawLead

logger = logging.getLogger(__name__)


class NotImplementedAdapter:
    """tg/instagram — интерфейс адаптера общий, реализация ещё не готова.

    Возвращает пустой список вместо исключения, чтобы Оркестратор мог дёргать
    любой platform, не падая.
    """

    def __init__(self, platform: str) -> None:
        self._platform = platform

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]:
        logger.info("platform %s not supported yet", self._platform)
        if on_progress is not None:
            on_progress(0, 0, 0)
        return []

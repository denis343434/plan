import time
from typing import Callable

from app.adapters.base import ParseFilters, RawLead


class FakeVkAdapter:
    """Детерминированные лиды без запуска браузера — для CI/dev (VK_ADAPTER_MODE=fake).

    Всё равно предполагается, что вызывающий код (tasks.run_parse_task) прогоняет её через
    настоящий next_available_account/get_session/release_account в Data Service, чтобы
    locking-логика проверялась по-настоящему, а не только сам факт "парсинга".
    """

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[RawLead]:
        count = 3 if filters.max_groups is None else min(3, filters.max_groups)
        leads = [
            RawLead(
                external_id=f"fake-{keyword}-{i}",
                group_url=f"https://vk.com/fake_{keyword}_{i}",
                title=f"{keyword} group {i}",
            )
            for i in range(count)
        ]
        if on_progress is not None:
            for checked in range(len(leads) + 1):
                on_progress(checked, len(leads))
                time.sleep(0.1)  # имитация прогресса, чтобы прогресс-бар в UI был виден и в fake-режиме
        return leads

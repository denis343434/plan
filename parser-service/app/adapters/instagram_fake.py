import time
from typing import Callable

from app.adapters.base import ParseFilters, RawLead


class FakeInstagramAdapter:
    """Детерминированные лиды без запуска браузера — для CI/dev (IG_ADAPTER_MODE=fake).

    Зеркалит FakeVkAdapter (см. app/adapters/vk_fake.py) — тот же принцип: даже в fake-режиме
    вызывающий код (tasks.run_parse_task) должен прогонять настоящий
    next_available_account/get_session/release_account в Data Service, чтобы locking-логика
    проверялась по-настоящему, а не только сам факт "парсинга".
    """

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]:
        count = 3 if filters.max_groups is None else min(3, filters.max_groups)
        leads = [
            RawLead(
                external_id=f"fake_ig_{keyword}_{i}",
                group_url=f"https://www.instagram.com/fake_ig_{keyword}_{i}/",
                title=f"{keyword} account {i}",
            )
            for i in range(count)
        ]
        if on_progress is not None:
            on_progress(0, len(leads), 0)
            for checked in range(1, len(leads) + 1):
                on_progress(checked, len(leads), checked)
                time.sleep(0.1)  # имитация прогресса, чтобы прогресс-бар в UI был виден и в fake-режиме
        return leads

from app.adapters.base import ParseFilters, RawLead


class FakeVkAdapter:
    """Детерминированные лиды без запуска браузера — для CI/dev (VK_ADAPTER_MODE=fake).

    Всё равно предполагается, что вызывающий код (tasks.run_parse_task) прогоняет её через
    настоящий next_available_account/get_session/release_account в Data Service, чтобы
    locking-логика проверялась по-настоящему, а не только сам факт "парсинга".
    """

    def search_communities(self, keyword: str, filters: ParseFilters) -> list[RawLead]:
        return [
            RawLead(
                external_id=f"fake-{keyword}-{i}",
                group_url=f"https://vk.com/fake_{keyword}_{i}",
                title=f"{keyword} group {i}",
            )
            for i in range(3)
        ]

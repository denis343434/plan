import time
import zlib
from typing import Callable

from app.adapters import contacts
from app.adapters.base import ParseFilters, RawLead


class FakeYandexMapsAdapter:
    """Детерминированные организации без браузера — для CI/dev (YANDEX_ADAPTER_MODE=fake,
    GOOGLE_ADAPTER_MODE=fake).

    Проходит через тот же выбор канала (contacts.pick_lead), что и настоящий адаптер, — у
    каждой фейковой организации свой набор ссылок, так что видно, как работает приоритет каналов.
    """

    def __init__(self, slug_prefix: str = "q") -> None:
        self._slug_prefix = slug_prefix

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]:
        # Ники мессенджеров — только латиница, поэтому не из самого (обычно кириллического)
        # запроса, а из его детерминированного хэша. Страна входит в хэш: «кафе, Брест,
        # Беларусь» и «кафе, Брест, Россия» — разные организации.
        slug = f"{self._slug_prefix}{zlib.crc32(f'{keyword}|{filters.city}|{filters.country}'.encode()):08x}"
        orgs = [
            ["https://vk.com/fake_{0}_1".format(slug), "https://t.me/fake_{0}_1".format(slug)],
            ["https://t.me/fake_{0}_2".format(slug)],
            ["https://wa.me/7900{0:07d}".format(zlib.crc32(slug.encode()) % 10**7), "https://facebook.com/fake.{0}.3".format(slug)],
        ]
        limit = len(orgs) if filters.max_groups is None else min(len(orgs), filters.max_groups)
        leads: list[RawLead] = []
        for checked, hrefs in enumerate(orgs, start=1):
            lead = contacts.pick_lead(contacts.channel_leads(hrefs), filters.channels, filters.known_external_ids)
            if lead is not None and len(leads) < limit:
                lead.title = f"{keyword} {filters.city or ''} {filters.country or ''} #{checked}".strip()
                leads.append(lead)
            if on_progress is not None:
                on_progress(checked, len(orgs), len(leads))
                time.sleep(0.1)  # имитация прогресса, как в FakeVkAdapter
        return leads

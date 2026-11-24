import httpx
import respx

from app.clients.data_service import DataServiceClient
from app.config import settings

BASE = settings.DATA_SERVICE_URL


def _lead(index: int, platform: str = "vk") -> dict:
    return {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "platform": platform,
        "external_id": f"group_{index}",
        "group_url": f"https://vk.com/group_{index}",
        "admin_contact": None,
        "title": None,
        "status": "new",
        "campaign_id": None,
        "found_at": "2026-01-01T00:00:00",
    }


@respx.mock
def test_list_leads_reads_all_pages_not_just_first():
    """Регрессия: dedup-список должен включать ВСЕ лиды, а не первые 5000 по found_at.

    На "Строительство домов" хвост из ~2300 новых лидов не попадал в dedup, парсер заново
    проверял уже известные группы (прогресс "подходит: 19"), а в кампанию вставлялась одна
    новая — остальные отбрасывал unique-индекс (platform, external_id).
    """
    page_size = DataServiceClient._LEADS_PAGE_SIZE

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        if offset == 0:
            return httpx.Response(200, json=[_lead(i) for i in range(limit)])
        return httpx.Response(200, json=[_lead(page_size)])

    route = respx.get(f"{BASE}/leads").mock(side_effect=handler)
    client = DataServiceClient()
    try:
        leads = client.list_leads()
    finally:
        client.close()

    assert len(leads) == page_size + 1
    assert leads[-1]["external_id"] == f"group_{page_size}"
    assert [int(call.request.url.params["offset"]) for call in route.calls] == [0, page_size]


@respx.mock
def test_list_leads_passes_platform_filter_on_every_page():
    page_size = DataServiceClient._LEADS_PAGE_SIZE
    seen_platforms: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_platforms.append(request.url.params.get("platform"))
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(200, json=[_lead(offset)] if offset else [_lead(i) for i in range(page_size)])

    respx.get(f"{BASE}/leads").mock(side_effect=handler)
    client = DataServiceClient()
    try:
        leads = client.list_leads(platform="tg")
    finally:
        client.close()

    assert len(leads) == page_size + 1
    assert seen_platforms == ["tg", "tg"]

import json
import uuid

import httpx
import pytest
import respx

from app.adapters import contacts
from app.adapters.base import ParseFilters
from app.adapters.yandex_maps import _search_query
from app.adapters.yandex_maps_fake import FakeYandexMapsAdapter
from app.config import settings
from app.schemas.parse import ParseFiltersSchema
from app.tasks import TaskStatus, create_task, run_parse_task

BASE = settings.DATA_SERVICE_URL


@pytest.fixture(autouse=True)
def _fake_map_adapters(monkeypatch):
    # Локальный .env может включать реальный браузер (*_ADAPTER_MODE=playwright) — тесты им не ходят.
    monkeypatch.setattr(settings, "YANDEX_ADAPTER_MODE", "fake")
    monkeypatch.setattr(settings, "GOOGLE_ADAPTER_MODE", "fake")


# Ссылки — как они реально выглядят в карточках Яндекс Карт (проверено вживую 2026-10-03).
def test_channel_leads_parses_real_card_links():
    found = contacts.channel_leads([
        "https://vk.ru/anatomy_kazan",
        "http://anatomy.clinic/",
        "https://t.me/anatomy_kazan",
        "https://wa.me/+79931345133?text=%D0%9E%D0%B1%D1%80",
        "https://t.me/mapsyandex",
    ])
    assert found["vk"].external_id == "anatomy_kazan"
    assert found["vk"].group_url == "https://vk.com/anatomy_kazan"
    assert found["tg"].external_id == "anatomy_kazan"
    assert found["whatsapp"].external_id == "79931345133"
    assert found["whatsapp"].group_url == "https://wa.me/79931345133"


def test_whatsapp_8_prefix_and_api_link_normalized():
    assert contacts.whatsapp_lead("https://wa.me/89600333053?text=hi").external_id == "79600333053"
    assert contacts.whatsapp_lead("https://api.whatsapp.com/send?phone=79001112233").external_id == "79001112233"
    assert contacts.whatsapp_lead("https://wa.me/message/ABCDEF") is None
    assert contacts.whatsapp_lead("https://chat.whatsapp.com/Invite123") is None


def test_yandex_own_channel_and_service_links_ignored():
    assert contacts.tg_lead("https://t.me/mapsyandex") is None
    assert contacts.tg_lead("https://t.me/joinchat/abc") is None
    assert contacts.tg_lead("https://t.me/+AbCdEfGh") is None
    assert contacts.tg_lead("https://t.me/+79991234567").external_id == "+79991234567"
    assert contacts.vk_lead("https://vk.com/share.php?url=x") is None
    assert contacts.vk_lead("https://vk.ru/club241182458").external_id == "club241182458"


def test_is_external_site():
    assert contacts.is_external_site("https://toplaserpro116.ru/")
    assert not contacts.is_external_site("https://keratin-liri.clients.site/")
    assert not contacts.is_external_site("https://vk.com/club1")
    assert not contacts.is_external_site("https://www.instagram.com/x")
    assert not contacts.is_external_site("https://yandex.ru/maps/org/1/")


def test_pick_lead_follows_priority_and_skips_known_org():
    found = contacts.channel_leads(["https://vk.com/cafe", "https://t.me/cafe_bot"])
    assert contacts.pick_lead(found, ("tg", "vk")).platform == "tg"
    assert contacts.pick_lead(found, ("vk", "tg")).platform == "vk"
    assert contacts.pick_lead(found, ("whatsapp",)) is None
    # Организации уже писали через приоритетный канал — второй раз через другой не берём.
    assert contacts.pick_lead(found, ("vk", "tg"), frozenset({"vk:cafe"})) is None


def test_phone_helpers():
    assert contacts.phone_digits("+7 (960) 033-30-53") == "79600333053"
    assert contacts.is_mobile_for_whatsapp("79600333053")  # Россия, мобильный
    assert not contacts.is_mobile_for_whatsapp("78432000000")  # Россия, городской


def test_mobile_for_whatsapp_accepts_kazakhstan_and_rejects_landlines():
    # Регрессия: в кампании «ресторан — Алматы» WhatsApp по номеру не срабатывал вообще,
    # потому что проверялись только российские +7 9xx. Казахстанские мобильные — +7 70x/77x.
    assert contacts.is_mobile_for_whatsapp("77084145046")  # +7 708 ...
    assert contacts.is_mobile_for_whatsapp("77767006969")  # +7 776 ...
    assert contacts.is_mobile_for_whatsapp("77475621355")  # +7 747 ...
    assert not contacts.is_mobile_for_whatsapp("77272615601")  # Алматы, городской +7 727
    assert contacts.is_mobile_for_whatsapp("375291131188")  # Беларусь, +375 29
    assert not contacts.is_mobile_for_whatsapp("375172000000")  # Беларусь, Минск +375 17


def test_fake_adapter_respects_channels():
    leads = FakeYandexMapsAdapter().search_communities("кафе", ParseFilters(city="Казань", channels=("tg",)))
    assert [lead.platform for lead in leads] == ["tg", "tg"]


def test_search_query_appends_country():
    assert _search_query("кафе", ParseFilters(city="Минск", country="Беларусь")) == "кафе Минск Беларусь"
    assert _search_query("кафе", ParseFilters(city="Казань", country="Россия")) == "кафе Казань Россия"
    # Старые кампании без страны — прежний запрос «вид деятельности + город».
    assert _search_query("кафе", ParseFilters(city="Казань")) == "кафе Казань"
    assert _search_query("кафе", ParseFilters(city="Казань", country="   ")) == "кафе Казань"
    assert _search_query("кафе", ParseFilters(country="Беларусь")) == "кафе Беларусь"


def test_parse_filters_schema_passes_country():
    assert ParseFiltersSchema(city="Минск").country is None
    assert ParseFiltersSchema(city="Минск", country="Беларусь").country == "Беларусь"


def test_fake_adapter_separates_same_city_in_different_countries():
    belarus = FakeYandexMapsAdapter().search_communities(
        "кафе", ParseFilters(city="Брест", country="Беларусь")
    )
    russia = FakeYandexMapsAdapter().search_communities(
        "кафе", ParseFilters(city="Брест", country="Россия")
    )
    assert {lead.external_id for lead in belarus}.isdisjoint(lead.external_id for lead in russia)


@respx.mock
def test_yandex_task_passes_country_from_campaign_filters():
    respx.get(f"{BASE}/leads").mock(return_value=httpx.Response(200, json=[]))
    bulk_route = respx.post(f"{BASE}/leads/bulk").mock(
        return_value=httpx.Response(200, json={"inserted": 1, "skipped": 0, "lead_ids": []})
    )

    task = create_task("yandex_maps", "кафе")
    run_parse_task(
        task.task_id, "yandex_maps", "кафе",
        ParseFiltersSchema(city="Минск", country="Беларусь", channels=["tg"]),
        uuid.uuid4(), use_account=False,
    )

    assert task.status == TaskStatus.done, task.error
    sent = json.loads(bulk_route.calls.last.request.content)
    # Fake-адаптер кладёт страну в заголовок лида — значит filters.country дошёл до адаптера.
    assert all("Беларусь" in lead["title"] for lead in sent)


@respx.mock
def test_yandex_task_writes_leads_with_channel_platform_and_no_account():
    respx.get(f"{BASE}/leads").mock(return_value=httpx.Response(200, json=[]))
    bulk_route = respx.post(f"{BASE}/leads/bulk").mock(
        return_value=httpx.Response(200, json={"inserted": 3, "skipped": 0, "lead_ids": []})
    )
    next_available = respx.post(f"{BASE}/accounts/next-available")

    campaign_id = uuid.uuid4()
    task = create_task("yandex_maps", "кафе")
    run_parse_task(
        task.task_id, "yandex_maps", "кафе",
        ParseFiltersSchema(city="Казань", channels=["vk", "tg", "whatsapp"]),
        campaign_id, use_account=False,
    )

    assert task.status == TaskStatus.done, task.error
    assert not next_available.called
    sent = json.loads(bulk_route.calls.last.request.content)
    assert [lead["platform"] for lead in sent] == ["vk", "tg", "whatsapp"]
    assert all(lead["campaign_id"] == str(campaign_id) for lead in sent)


def test_google_redirect_links_unwrapped():
    found = contacts.channel_leads([
        "https://www.google.com/url?q=https://t.me/coffee_kzn&sa=U&ved=x",
        "https://www.google.com/url?q=https%3A%2F%2Fvk.com%2Fcoffee_kzn",
    ])
    assert found["tg"].external_id == "coffee_kzn"
    assert found["vk"].external_id == "coffee_kzn"
    assert contacts.is_external_site("https://www.google.com/url?q=https://coffee.ru/")
    assert not contacts.is_external_site("https://coffee-kzn.business.site/")
    assert not contacts.is_external_site("https://www.google.com/maps/place/x")


def test_google_fake_leads_do_not_collide_with_yandex():
    filters = ParseFilters(city="Казань")
    yandex = FakeYandexMapsAdapter().search_communities("кафе", filters)
    google = FakeYandexMapsAdapter(slug_prefix="g").search_communities("кафе", filters)
    assert {lead.external_id for lead in yandex}.isdisjoint(lead.external_id for lead in google)


@respx.mock
def test_google_task_writes_leads_with_channel_platform_and_no_account():
    respx.get(f"{BASE}/leads").mock(return_value=httpx.Response(200, json=[]))
    bulk_route = respx.post(f"{BASE}/leads/bulk").mock(
        return_value=httpx.Response(200, json={"inserted": 3, "skipped": 0, "lead_ids": []})
    )
    next_available = respx.post(f"{BASE}/accounts/next-available")

    task = create_task("google_maps", "кафе")
    run_parse_task(
        task.task_id, "google_maps", "кафе",
        ParseFiltersSchema(city="Казань", channels=["tg", "facebook"]),
        uuid.uuid4(), use_account=False,
    )

    assert task.status == TaskStatus.done, task.error
    assert not next_available.called
    sent = json.loads(bulk_route.calls.last.request.content)
    assert [lead["platform"] for lead in sent] == ["tg", "tg", "facebook"]


def test_facebook_links():
    assert contacts.facebook_lead("https://www.facebook.com/CoffeeKzn/").external_id == "coffeekzn"
    assert contacts.facebook_lead("https://m.me/coffee.kzn").group_url == "https://www.facebook.com/coffee.kzn"
    assert contacts.facebook_lead("https://www.facebook.com/profile.php?id=100064").external_id == "100064"
    assert contacts.facebook_lead("https://www.facebook.com/people/Coffee/61550000/").external_id == "61550000"
    assert contacts.facebook_lead("https://www.google.com/url?q=https://facebook.com/coffeekzn").external_id == "coffeekzn"
    assert contacts.facebook_lead("https://www.facebook.com/sharer/sharer.php?u=x") is None
    assert contacts.facebook_lead("https://www.facebook.com/groups/123") is None
    assert contacts.facebook_lead("https://vk.com/coffee") is None
    assert not contacts.is_external_site("https://www.facebook.com/coffeekzn")
    assert contacts.pick_lead(
        contacts.channel_leads(["https://t.me/coffee_kzn", "https://facebook.com/coffeekzn"]), ("facebook", "tg")
    ).platform == "facebook"


def test_tg_lead_from_phone():
    lead = contacts.tg_lead_from_phone(contacts.phone_digits("+44 20 7379 7962"))
    assert (lead.platform, lead.external_id, lead.group_url) == ("tg", "+442073797962", "https://t.me/+442073797962")
    # Тот же ключ, что у ссылки t.me/+номер — dedup общий.
    assert contacts.tg_lead("https://t.me/+442073797962").external_id == lead.external_id
    assert contacts.tg_lead_from_phone(contacts.phone_digits("8 (495) 626-02-82")).external_id == "+74956260282"
    assert contacts.tg_lead_from_phone("02073797962") is None
    assert contacts.tg_lead_from_phone(None) is None

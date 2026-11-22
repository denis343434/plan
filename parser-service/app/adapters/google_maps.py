"""Парсер Google Карт: организации по "вид деятельности + город" → лиды по каналам связи.

Тот же сценарий, что adapters/yandex_maps.py: аккаунт не нужен, выдача открыта на одной вкладке
и докручивается, карточки организаций открываются на второй. Из карточки берём ссылки на мессенджеры/соцсети (вкладка
клиента «Google карты» сейчас предлагает только Telegram и Facebook); выбор канала —
contacts.pick_lead, по одному лиду на организацию.

Разметка (по web-версии google.com/maps на момент написания, вживую на этом проекте НЕ проверена —
при первом прогоне включить Playwright «На экране» и при расхождении обновить селекторы ниже):
  - выдача: div[role=feed], ссылки организаций a[href*="/maps/place/"] (aria-label — название);
    конец выдачи — надпись "Больше результатов нет" / "You've reached the end of the list";
  - карточка: div[role=main] h1 — название, a[data-item-id=authority] — сайт,
    button[data-item-id^="phone:tel:"] — телефон (номер прямо в data-item-id),
    button[data-item-id=address] — адрес; внешние ссылки обёрнуты в google.com/url?q=….
Город: запрос "ресторан Лондон" Google понимает как "ресторан с Лондоном в названии рядом с
пользователем" (проверено вживую 2026-10-04 — из РФ выдавал московские рестораны). Поэтому сначала
открываем сам город, берём его координаты из URL (…/@51.52,-0.10,11z) и ищем вид деятельности уже
там: /maps/search/<keyword>/@lat,lng,13z.
Без входа в Google открывается "ограниченная версия Google Карт" (и headless, и на экране) — выдача
в ней тоже докручивается, на парсинг это не влияет.
Поиск в соцсетях (filters.search_socials): если в карточке нет ссылки на канал из приоритета —
Telegram пробуем по номеру телефона, Instagram/Facebook ищем по названию через
adapters/social_lookup.py и берём профиль, только если совпали название и телефон или адрес.
Оригинальное название: при hl=ru Google переводит заголовок ("Ресторан Нару"), а оригинал
("Naru") показывает подзаголовком h2 рядом с h1 — по нему и ищем.
Если запрос узкий и Google сразу открыл единственную карточку вместо списка — не обрабатываем
(как и у Яндекса: для рассылки такой запрос слишком узкий).
"""

import logging
import random
import re
import time
from typing import Callable
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters import contacts
from app.adapters.base import CaptchaDetectedError, ParseFilters, RawLead
from app.adapters.social_lookup import SocialLookup
from app.adapters.social_match import OrgCard
from app.config import settings

logger = logging.getLogger(__name__)

_SEARCH_URL_TEMPLATE = "https://www.google.com/maps/search/{query}?hl=ru"
_SEARCH_AT_URL_TEMPLATE = "https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z?hl=ru"
_COORDS_RE = re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),")
# Масштаб поиска по городу: 13z — центр и ближние районы крупного города (Google и так расширяет
# выдачу при докрутке).
_CITY_SEARCH_ZOOM = 13
_GEOCODE_TIMEOUT_MS = 15_000
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_FEED_SELECTOR = 'div[role="feed"]'
_PLACE_LINK_SELECTOR = 'div[role="feed"] a[href*="/maps/place/"]'
_CARD_SELECTOR = 'div[role="main"] h1'
_CARD_ROOT_SELECTOR = 'div[role="main"]'
_SITE_SELECTOR = 'a[data-item-id="authority"]'
_PHONE_SELECTOR = 'button[data-item-id^="phone:tel:"]'
_ADDRESS_SELECTOR = 'button[data-item-id="address"]'
_END_OF_LIST_MARKERS = ("больше результатов нет", "you've reached the end of the list")
# Идентификатор места в ссылке: …/data=!4m7!3m6!1s0x46ae…:0x8d5f…!8m2…
_PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)

_RENDER_TIMEOUT_MS = 30_000
_CARD_TIMEOUT_MS = 20_000
# Как у Яндекса: выдача догружается порциями, конец — несколько пустых скроллов подряд
# (или явная надпись "больше результатов нет").
_SCROLL_WAIT_MS = 1500
_NO_GROWTH_STALLS_TO_STOP = 3
# Google сам отдаёт не больше ~120 мест на один запрос — это просто страховка.
_MAX_ORGS_WITHOUT_LIMIT = 500

_CAPTCHA_URL_MARKER = "/sorry/"
_CAPTCHA_SELECTOR = "form#captcha-form, iframe[src*='recaptcha']"
_CONSENT_URL_MARKER = "consent.google."
_CONSENT_BUTTON_SELECTOR = (
    'button:has-text("Принять все"), button:has-text("Accept all"), '
    'form[action*="consent"] button[type="submit"]'
)

_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_SUBSTRINGS = ("doubleclick.net", "googlesyndication.com", "google-analytics.com")

_CARD_LINKS_JS = "(root) => Array.from(root.querySelectorAll('a[href]')).map(a => a.href)"
# Оригинальное название — h2 в блоке заголовка, на два уровня выше h1 (проверено вживую 2026-10-04).
_SUBTITLE_JS = """() => {
  const h1 = document.querySelector('div[role="main"] h1');
  const box = h1 && h1.parentElement && h1.parentElement.parentElement;
  const h2 = box && box.querySelector('h2');
  return h2 ? h2.innerText.trim() : null;
}"""


def _block_heavy_resources(context) -> None:
    def handler(route) -> None:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCE_TYPES or any(
            s in request.url for s in _BLOCKED_URL_SUBSTRINGS
        ):
            route.abort()
            return
        route.continue_()

    context.route("**/*", handler)


def _sleep_random() -> None:
    time.sleep(random.uniform(settings.GOOGLE_MIN_DELAY_SEC, settings.GOOGLE_MAX_DELAY_SEC))


def _place_key(href: str) -> str:
    match = _PLACE_ID_RE.search(href)
    return match.group(1).lower() if match else href.split("?", 1)[0]


class GoogleMapsAdapter:
    def __init__(self, social_sessions: dict[str, dict] | None = None) -> None:
        # {"instagram": storage_state, "facebook": storage_state} — аккаунты для поиска в соцсетях.
        self._social_sessions = social_sessions or {}
        self._lookup: SocialLookup | None = None

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]:
        leads: list[RawLead] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=settings.VK_HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                locale="ru-RU", user_agent=_USER_AGENT, viewport={"width": 1400, "height": 900}
            )
            _block_heavy_resources(context)
            if filters.search_socials and self._social_sessions:
                self._lookup = SocialLookup(browser, self._social_sessions)
            try:
                list_page = context.new_page()
                card_page = context.new_page()
                if self._open_search(list_page, keyword, filters.city, leads):
                    self._walk_results(list_page, card_page, filters, leads, on_progress)
            finally:
                if self._lookup is not None:
                    self._lookup.close()
                    self._lookup = None
                browser.close()
        return leads

    def _open_search(self, page: Page, keyword: str, city: str | None, leads: list[RawLead]) -> bool:
        coords = self._city_coords(page, city, leads) if city else None
        if coords is not None:
            query = keyword
            url = _SEARCH_AT_URL_TEMPLATE.format(query=quote(keyword), lat=coords[0], lng=coords[1], zoom=_CITY_SEARCH_ZOOM)
        else:
            # Город не нашёлся на карте — хотя бы текстом в запросе (как раньше).
            query = f"{keyword} {city}".strip() if city else keyword
            url = _SEARCH_URL_TEMPLATE.format(query=quote(query))
        page.goto(url, wait_until="domcontentloaded")
        self._accept_consent(page)
        self._wait_out_captcha(page, leads)
        try:
            page.wait_for_selector(_FEED_SELECTOR, timeout=_RENDER_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            self._wait_out_captcha(page, leads)
            logger.info("google maps: no results list for %r", query)
            return False
        return True

    def _city_coords(self, page: Page, city: str, leads: list[RawLead]) -> tuple[str, str] | None:
        """Открывает город на карте и ждёт, пока Google допишет в URL его координаты."""
        page.goto(_SEARCH_URL_TEMPLATE.format(query=quote(city)), wait_until="domcontentloaded")
        self._accept_consent(page)
        self._wait_out_captcha(page, leads)
        step_ms = 500
        for _ in range(_GEOCODE_TIMEOUT_MS // step_ms):
            # Пока город не открылся, в URL нет "/place/" — там координаты ещё "домашние" (по IP).
            match = _COORDS_RE.search(page.url)
            if match and "/place/" in page.url:
                return match.group(1), match.group(2)
            page.wait_for_timeout(step_ms)
        logger.warning("google maps: could not locate city %r on the map (url %s)", city, page.url)
        return None

    def _walk_results(
        self,
        list_page: Page,
        card_page: Page,
        filters: ParseFilters,
        leads: list[RawLead],
        on_progress: Callable[[int, int, int], None] | None,
    ) -> None:
        seen_places: set[str] = set()
        checked = 0
        stalls = 0
        known = filters.known_external_ids

        def report() -> None:
            if on_progress is not None:
                on_progress(checked, len(seen_places), len(leads))

        report()
        while True:
            fresh = self._collect_places(list_page, seen_places)
            if not fresh:
                if (
                    stalls >= _NO_GROWTH_STALLS_TO_STOP
                    or len(seen_places) >= _MAX_ORGS_WITHOUT_LIMIT
                    or self._end_of_list(list_page)
                ):
                    return
                stalls += 1
                self._scroll_feed(list_page)
                continue
            stalls = 0
            report()

            for place_url, title in fresh:
                lead = self._read_card(card_page, place_url, title, filters, known, leads)
                checked += 1
                if lead is not None:
                    leads.append(lead)
                report()
                if filters.max_groups is not None and len(leads) >= filters.max_groups:
                    return
                _sleep_random()

            self._scroll_feed(list_page)

    def _collect_places(self, page: Page, seen_places: set[str]) -> list[tuple[str, str | None]]:
        fresh: list[tuple[str, str | None]] = []
        for link in page.query_selector_all(_PLACE_LINK_SELECTOR):
            href = link.get_attribute("href")
            if not href:
                continue
            key = _place_key(href)
            if key in seen_places:
                continue
            seen_places.add(key)
            title = (link.get_attribute("aria-label") or "").strip()
            fresh.append((href, title or None))
        return fresh

    def _scroll_feed(self, page: Page) -> None:
        try:
            feed = page.locator(_FEED_SELECTOR)
            if feed.count():
                feed.first.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        except Exception:  # noqa: BLE001 - не смогли докрутить — следующая итерация увидит "нет роста"
            logger.debug("google maps: scroll failed", exc_info=True)
        page.wait_for_timeout(_SCROLL_WAIT_MS)

    @staticmethod
    def _end_of_list(page: Page) -> bool:
        try:
            text = (page.locator(_FEED_SELECTOR).first.inner_text(timeout=2000) or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in text for m in _END_OF_LIST_MARKERS)

    def _read_card(
        self,
        page: Page,
        place_url: str,
        title: str | None,
        filters: ParseFilters,
        known: frozenset[str],
        leads: list[RawLead],
    ) -> RawLead | None:
        try:
            page.goto(place_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - одна недогрузившаяся карточка не роняет весь прогон
            logger.warning("google maps: failed to open %s: %s", place_url, exc)
            return None
        self._accept_consent(page)
        self._wait_out_captcha(page, leads)
        try:
            page.wait_for_selector(_CARD_SELECTOR, timeout=_CARD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            self._wait_out_captcha(page, leads)
            logger.warning("google maps: card did not render %s", place_url)
            return None

        root = page.query_selector(_CARD_ROOT_SELECTOR)
        hrefs: list[str] = page.evaluate(_CARD_LINKS_JS, root) if root else []

        site = self._attr(page, _SITE_SELECTOR, "href")
        has_site = bool(site) and contacts.is_external_site(site)
        if filters.has_site is not None and has_site != filters.has_site:
            return None

        phone_id = self._attr(page, _PHONE_SELECTOR, "data-item-id") or ""
        phone = phone_id.removeprefix("phone:tel:") or None
        found = contacts.channel_leads(hrefs)
        digits = contacts.phone_digits(phone)
        if filters.whatsapp_by_phone and "whatsapp" not in found and contacts.is_ru_mobile(digits):
            found["whatsapp"] = contacts.whatsapp_lead_from_phone(digits)

        address = self._address(page)
        names = self._names(page, title)
        lead, how = self._pick_channel(found, filters, known, OrgCard(names, digits, address))
        if lead is None:
            return None

        lead.title = names[-1] if names else title
        lead.admin_contact = _contact_summary(phone, address, site, place_url, how)
        return lead

    def _pick_channel(
        self, found: dict[str, RawLead], filters: ParseFilters, known: frozenset[str], org: OrgCard
    ) -> tuple[RawLead | None, str | None]:
        """Первый канал из приоритета, по которому есть контакт: ссылка из карточки, а если её нет и
        включён поиск — Telegram по номеру / найденный и сверенный профиль Instagram/Facebook.
        Уже известный контакт (писали раньше) — организацию пропускаем целиком, как pick_lead."""
        for channel in filters.channels:
            lead, how = found.get(channel), None
            if lead is None and filters.search_socials:
                if channel == "tg":
                    lead, how = contacts.tg_lead_from_phone(org.phone_digits), "Telegram по номеру из карточки"
                elif self._lookup is not None and self._lookup.available(channel):
                    hit = self._lookup.find(channel, org, _search_queries(org, filters.city))
                    if hit is not None:
                        lead, result = hit
                        how = f"найден поиском, совпали: {', '.join(result.reasons)}"
            if lead is None:
                continue
            if f"{lead.platform}:{lead.external_id}" in known:
                return None, None
            return lead, how
        return None, None

    def _names(self, page: Page, title: str | None) -> list[str]:
        """[оригинальное название, название из выдачи] — без повторов, оригинал первым."""
        names: list[str] = []
        try:
            subtitle = page.evaluate(_SUBTITLE_JS)
        except Exception:  # noqa: BLE001
            subtitle = None
        for name in (subtitle, title or self._text(page, _CARD_SELECTOR)):
            if name and name not in names:
                names.append(name)
        return names

    def _accept_consent(self, page: Page) -> None:
        """Экран согласия на cookies (consent.google.com) — показывается не во всех регионах."""
        if _CONSENT_URL_MARKER not in page.url:
            return
        try:
            page.locator(_CONSENT_BUTTON_SELECTOR).first.click(timeout=10_000)
            page.wait_for_load_state("domcontentloaded")
        except Exception:  # noqa: BLE001 - без согласия дальше просто не дорисуется выдача
            logger.warning("google maps: failed to accept consent screen", exc_info=True)

    def _wait_out_captcha(self, page: Page, leads: list[RawLead]) -> None:
        """Капча Google (google.com/sorry): как у Яндекса — на экране ждём ручного прохождения,
        в фоне сдаёмся с уже собранными лидами."""
        if not self._captcha_shown(page):
            return
        if settings.VK_HEADLESS:
            raise CaptchaDetectedError(
                "Google показал капчу — переключите Playwright в режим «На экране» и пройдите её вручную",
                partial_leads=list(leads),
            )
        logger.warning("google maps: captcha shown, waiting up to %ss for manual solve", settings.GOOGLE_CAPTCHA_WAIT_SEC)
        deadline = time.monotonic() + settings.GOOGLE_CAPTCHA_WAIT_SEC
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            if not self._captcha_shown(page):
                return
        raise CaptchaDetectedError("капча Google не пройдена вовремя", partial_leads=list(leads))

    @staticmethod
    def _captcha_shown(page: Page) -> bool:
        if _CAPTCHA_URL_MARKER in page.url:
            return True
        try:
            return page.locator(_CAPTCHA_SELECTOR).count() > 0
        except Exception:  # noqa: BLE001 - страница могла уйти в навигацию посреди проверки
            return False

    def _address(self, page: Page) -> str | None:
        # aria-label кнопки — "Адрес: ул. Баумана, 1, Казань…"; текст кнопки содержит ещё и иконку.
        label = self._attr(page, _ADDRESS_SELECTOR, "aria-label")
        if label:
            return label.split(":", 1)[-1].strip() or None
        return self._text(page, _ADDRESS_SELECTOR)

    @staticmethod
    def _text(page: Page, selector: str) -> str | None:
        el = page.query_selector(selector)
        if el is None:
            return None
        text = (el.inner_text() or "").strip()
        return text.splitlines()[0].strip() if text else None

    @staticmethod
    def _attr(page: Page, selector: str, name: str) -> str | None:
        el = page.query_selector(selector)
        return el.get_attribute(name) if el else None


def _address_locality(address: str | None) -> str | None:
    """Город латиницей из адреса Google ("230 Shaftesbury Ave, London WC2H 8EG, Великобритания" →
    "London") — соцсети иностранных заведений по-русски не найти."""
    for part in (address or "").split(",")[1:]:
        words = [w for w in part.split() if not any(ch.isdigit() for ch in w)]
        candidate = " ".join(words).strip()
        if candidate and re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", candidate) and not candidate.isupper():
            return candidate
    return None


def _search_queries(org: OrgCard, city: str | None) -> list[str]:
    """Запросы в порядке убывания точности; SocialLookup останавливается на первом, давшем кандидатов."""
    locality = _address_locality(org.address)
    queries: list[str] = []
    for name in org.names:
        is_latin = not re.search(r"[а-яё]", name.lower())
        place = locality if (is_latin and locality) else city
        for query in (f"{name} {place}" if place else None, name):
            if query and query not in queries:
                queries.append(query)
    return queries[:3]


def _contact_summary(
    phone: str | None, address: str | None, site: str | None, place_url: str, how: str | None = None
) -> str:
    parts = [how] if how else []
    if phone:
        parts.append(f"тел. {phone}")
    if address:
        parts.append(address)
    if site:
        parts.append(f"сайт: {site}")
    parts.append(f"G.Карты: {place_url.split('?', 1)[0]}")
    return " · ".join(parts)

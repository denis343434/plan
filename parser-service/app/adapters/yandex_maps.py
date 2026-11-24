"""Парсер Яндекс Карт: организации по "вид деятельности + город" → лиды по каналам связи.

Аккаунт не нужен (карты публичные). Список выдачи открыт на одной вкладке и докручивается
порциями, карточки организаций открываются на второй — так список не теряет позицию.

Из карточки берём ссылки на VK / Telegram / WhatsApp и на каждую организацию отдаём ОДИН лид —
первый канал из filters.channels (порядок = приоритет), по которому есть контакт. Лид пишется с
платформой канала (RawLead.platform), рассылка дальше идёт обычным путём по lead["platform"].

Разметка проверена вживую 2026-10-03 (headless, "салон красоты Казань"):
  - выдача: li.search-snippet-view, ссылка a.link-overlay → /maps/org/<slug>/<org_id>/;
  - карточка: h1 — название, .business-urls-view__link — сайт, .card-phones-view__phone-number —
    телефон (виден без клика "Показать телефон"), кнопки мессенджеров — обычные <a> с href
    vk.ru/…, t.me/…, wa.me/…, внутри .business-card-view;
  - .card-social-links-view — соцсети самих Яндекс Карт (t.me/mapsyandex), не организации.
Headless Chromium с дефолтным user-agent ("HeadlessChrome") Яндекс режет страницей "limited" —
поэтому подменяем UA на обычный десктопный Chrome.
"""

import logging
import random
import time
from typing import Callable
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters import contacts
from app.adapters.base import CaptchaDetectedError, ParseFilters, RawLead
from app.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://yandex.ru"
_SEARCH_URL_TEMPLATE = _BASE_URL + "/maps/?text={query}"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_SNIPPET_SELECTOR = "li.search-snippet-view"
_SNIPPET_LINK_SELECTOR = "a.link-overlay"
_SNIPPET_TITLE_SELECTOR = ".search-business-snippet-view__title"
_CARD_SELECTOR = ".business-card-view"
_CARD_TITLE_SELECTOR = "h1"
_SITE_SELECTOR = ".business-urls-view__link"
_PHONE_SELECTOR = ".card-phones-view__phone-number"
_ADDRESS_SELECTOR = ".business-contacts-view__address-link, .orgpage-header-view__address"
_YANDEX_SOCIAL_BLOCK = ".card-social-links-view"

_RENDER_TIMEOUT_MS = 30_000
_CARD_TIMEOUT_MS = 20_000
# Выдача докручивается по 1-3 карточки за скролл (проверено вживую) — даём ей время дорисоваться
# и требуем несколько пустых скроллов подряд, прежде чем считать выдачу законченной (тот же
# приём, что _NO_GROWTH_STALLS_TO_STOP в adapters/vk.py).
_SCROLL_WAIT_MS = 1500
_NO_GROWTH_STALLS_TO_STOP = 3
# Защита от бесконечной докрутки, если max_groups не задан: Яндекс и сам не отдаёт больше
# нескольких сотен организаций на один запрос.
_MAX_ORGS_WITHOUT_LIMIT = 500

_CAPTCHA_URL_MARKER = "showcaptcha"
_CAPTCHA_SELECTOR = ".CheckboxCaptcha, .AdvancedCaptcha, form[action*='checkcaptcha']"
_LIMITED_TEXT = "limited"

_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_SUBSTRINGS = ("mc.yandex.ru", "an.yandex.ru", "yandex.ru/ads", "doubleclick.net")

# Все ссылки карточки, кроме блока соцсетей самих Яндекс Карт.
_CARD_LINKS_JS = """
(root) => Array.from(root.querySelectorAll('a[href]'))
  .filter(a => !a.closest('%s'))
  .map(a => a.href)
""" % _YANDEX_SOCIAL_BLOCK


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
    time.sleep(random.uniform(settings.YANDEX_MIN_DELAY_SEC, settings.YANDEX_MAX_DELAY_SEC))


def _search_query(keyword: str, filters: ParseFilters) -> str:
    """«кафе Алматы Казахстан»: страна уточняет город, когда он есть в нескольких странах.

    Проверено вживую 2026-11-22: Яндекс корректно переходит в регион по такому запросу
    (Минск → /157/minsk/, Алматы → /162/almaty/), а без страны «кафе Берлин» он может принять
    город за название организации. Пустые части (старые кампании без страны) пропускаем.
    """
    return " ".join(part.strip() for part in (keyword, filters.city, filters.country) if part and part.strip())


class YandexMapsAdapter:
    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]:
        query = _search_query(keyword, filters)
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
            try:
                list_page = context.new_page()
                card_page = context.new_page()
                self._open_search(list_page, query, leads)
                self._walk_results(list_page, card_page, filters, leads, on_progress)
            finally:
                browser.close()
        return leads

    def _open_search(self, page: Page, query: str, leads: list[RawLead]) -> None:
        page.goto(_SEARCH_URL_TEMPLATE.format(query=quote(query)), wait_until="domcontentloaded")
        self._wait_out_captcha(page, leads)
        try:
            page.wait_for_selector(_SNIPPET_SELECTOR, timeout=_RENDER_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            self._wait_out_captcha(page, leads)
            # Ни одной организации по запросу (или Яндекс сразу открыл единственную карточку —
            # такой запрос слишком узкий для рассылки, отдельно не обрабатываем).
            logger.info("yandex maps: no results list for %r", query)

    def _walk_results(
        self,
        list_page: Page,
        card_page: Page,
        filters: ParseFilters,
        leads: list[RawLead],
        on_progress: Callable[[int, int, int], None] | None,
    ) -> None:
        seen_orgs: set[str] = set()
        checked = 0
        stalls = 0
        known = filters.known_external_ids

        def report() -> None:
            if on_progress is not None:
                on_progress(checked, len(seen_orgs), len(leads))

        report()
        while True:
            fresh = self._collect_snippets(list_page, seen_orgs)
            if not fresh:
                if stalls >= _NO_GROWTH_STALLS_TO_STOP or len(seen_orgs) >= _MAX_ORGS_WITHOUT_LIMIT:
                    return
                stalls += 1
                self._scroll_list(list_page)
                continue
            stalls = 0
            report()

            for org_url, snippet_title in fresh:
                lead = self._read_card(card_page, org_url, snippet_title, filters, known, leads)
                checked += 1
                if lead is not None:
                    leads.append(lead)
                report()
                if filters.max_groups is not None and len(leads) >= filters.max_groups:
                    return
                _sleep_random()

            self._scroll_list(list_page)

    def _collect_snippets(self, page: Page, seen_orgs: set[str]) -> list[tuple[str, str | None]]:
        fresh: list[tuple[str, str | None]] = []
        for snippet in page.query_selector_all(_SNIPPET_SELECTOR):
            link = snippet.query_selector(_SNIPPET_LINK_SELECTOR)
            href = link.get_attribute("href") if link else None
            if not href or "/org/" not in href:
                continue
            # /maps/org/<slug>/<org_id>/ — один и тот же org_id иногда приходит с разными
            # хвостами (/reviews/, ?ll=…), дедуп по самому id.
            parts = [p for p in href.split("?", 1)[0].split("/") if p]
            org_id = next((p for p in reversed(parts) if p.isdigit()), href)
            if org_id in seen_orgs:
                continue
            seen_orgs.add(org_id)
            title_el = snippet.query_selector(_SNIPPET_TITLE_SELECTOR)
            title = (title_el.inner_text() or "").strip() if title_el else None
            fresh.append((_BASE_URL + href if href.startswith("/") else href, title or None))
        return fresh

    def _scroll_list(self, page: Page) -> None:
        snippets = page.locator(_SNIPPET_SELECTOR)
        try:
            if snippets.count():
                snippets.last.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001 - не смогли докрутить — следующая итерация увидит "нет роста"
            logger.debug("yandex maps: scroll failed", exc_info=True)
        page.wait_for_timeout(_SCROLL_WAIT_MS)

    def _read_card(
        self,
        page: Page,
        org_url: str,
        snippet_title: str | None,
        filters: ParseFilters,
        known: frozenset[str],
        leads: list[RawLead],
    ) -> RawLead | None:
        try:
            page.goto(org_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - одна недогрузившаяся карточка не роняет весь прогон
            logger.warning("yandex maps: failed to open %s: %s", org_url, exc)
            return None
        self._wait_out_captcha(page, leads)
        try:
            page.wait_for_selector(_CARD_SELECTOR, timeout=_CARD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            self._wait_out_captcha(page, leads)
            logger.warning("yandex maps: card did not render %s", org_url)
            return None

        card = page.query_selector(_CARD_SELECTOR)
        hrefs: list[str] = page.evaluate(_CARD_LINKS_JS, card) if card else []

        site = self._attr(page, _SITE_SELECTOR, "href")
        has_site = bool(site) and contacts.is_external_site(site)
        if filters.has_site is not None and has_site != filters.has_site:
            return None

        phone = self._text(page, _PHONE_SELECTOR)
        found = contacts.channel_leads(hrefs)
        if filters.whatsapp_by_phone and "whatsapp" not in found:
            digits = contacts.phone_digits(phone)
            if contacts.is_mobile_for_whatsapp(digits):
                found["whatsapp"] = contacts.whatsapp_lead_from_phone(digits)

        lead = contacts.pick_lead(found, filters.channels, known)
        if lead is None:
            self._log_skipped_card(org_url, snippet_title, found, filters, known, phone)
            return None

        lead.title = snippet_title or self._text(page, _CARD_TITLE_SELECTOR)
        lead.admin_contact = _contact_summary(phone, self._text(page, _ADDRESS_SELECTOR), site, org_url)
        return lead

    @staticmethod
    def _log_skipped_card(
        org_url: str,
        title: str | None,
        found: dict[str, RawLead],
        filters: ParseFilters,
        known: frozenset[str],
        phone: str | None,
    ) -> None:
        """Пишет в лог, почему карточка с WhatsApp/мобильным телефоном не стала лидом.

        Без этого «не взял организацию с одним WhatsApp» выглядело одинаково для трёх разных
        случаев: номер уже есть у другого лида (дубль контакта), телефон не выглядит мобильным,
        или канал не выбран в приоритете."""
        digits = contacts.phone_digits(phone)
        has_wa = "whatsapp" in found
        phone_only_possible = not has_wa and contacts.is_mobile_for_whatsapp(digits)
        if not has_wa and not phone_only_possible:
            return  # карточка вообще не про WhatsApp — не засоряем лог

        known_channels = [ch for ch, lead in found.items() if f"{ch}:{lead.external_id}" in known]
        if known_channels:
            reason = f"контакт уже известен ({', '.join(known_channels)})"
        elif not any(ch in found for ch in filters.channels):
            reason = "найденные каналы не входят в приоритет рассылки"
        elif phone_only_possible and not filters.whatsapp_by_phone:
            reason = "есть только мобильный телефон, «WhatsApp по номеру» выключен"
        elif phone_only_possible:
            reason = "есть только телефон, но номер не распознан как мобильный для WhatsApp"
        else:
            reason = "не прошёл фильтры"
        logger.info("yandex maps: карточка %r (%s) пропущена: %s", title, org_url, reason)

    def _wait_out_captcha(self, page: Page, leads: list[RawLead]) -> None:
        """Капча Яндекса: в видимом браузере даём человеку пройти её руками, в фоне — сдаёмся
        с уже собранными лидами (CaptchaDetectedError, как у VK-адаптера)."""
        if not self._captcha_shown(page):
            return
        if settings.VK_HEADLESS:
            raise CaptchaDetectedError(
                "Яндекс показал капчу — переключите Playwright в режим «На экране» и пройдите её вручную",
                partial_leads=list(leads),
            )
        logger.warning("yandex maps: captcha shown, waiting up to %ss for manual solve", settings.YANDEX_CAPTCHA_WAIT_SEC)
        deadline = time.monotonic() + settings.YANDEX_CAPTCHA_WAIT_SEC
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            if not self._captcha_shown(page):
                return
        raise CaptchaDetectedError("капча Яндекса не пройдена вовремя", partial_leads=list(leads))

    @staticmethod
    def _captcha_shown(page: Page) -> bool:
        if _CAPTCHA_URL_MARKER in page.url:
            return True
        try:
            if page.locator(_CAPTCHA_SELECTOR).count() > 0:
                return True
            body = page.locator("body")
            return body.count() > 0 and (body.inner_text(timeout=2000) or "").strip() == _LIMITED_TEXT
        except Exception:  # noqa: BLE001 - страница могла уйти в навигацию посреди проверки
            return False

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


def _contact_summary(phone: str | None, address: str | None, site: str | None, org_url: str) -> str:
    parts = []
    if phone:
        parts.append(f"тел. {phone}")
    if address:
        parts.append(address)
    if site:
        parts.append(f"сайт: {site}")
    parts.append(f"Я.Карты: {org_url.split('?', 1)[0]}")
    return " · ".join(parts)

import logging
import random
import re
import time
from typing import Callable

from playwright._impl._errors import TargetClosedError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters.base import CaptchaDetectedError, ParseFilters, RawLead
from app.config import settings

logger = logging.getLogger(__name__)

# Instagram не даёт прямого URL на "результаты поиска по ключу" (в отличие от VK) — единственный
# путь для живого пользователя: открыть боковую панель поиска и напечатать запрос, дождавшись
# выпадающего списка подсказок. Панель открывается кликом по пункту навигации с aria-label
# "Search" (интерфейс Instagram на английском независимо от локали браузера — подтверждено по
# публичной разметке, НЕ проверено вживую через devtools на реальном аккаунте — при первом
# реальном прогоне свериться и обновить селектор по образцу parser-service/app/adapters/vk.py).
_SEARCH_NAV_SELECTOR = 'a[href="#"][aria-label="Search"], svg[aria-label="Search"]'
_SEARCH_INPUT_SELECTOR = 'input[placeholder="Search"], input[aria-label="Search input"]'
# Каждая строка подсказки — ссылка на профиль или на хэштег. Хэштеги ведут на
# "/explore/tags/<tag>/" — исключаем их отдельно (see _parse_suggestion), а не полагаемся на то,
# что визуально их можно отличить по иконке/тексту.
_SUGGESTION_ITEM_SELECTOR = 'a[role="link"][href^="/"]'
_CAPTCHA_OR_CHECKPOINT_TEXT = ("подтвердите, что вы", "help us confirm", "checkpoint_required", "captcha")
_LOGIN_REQUIRED_TEXT = ("log in to continue", "войдите, чтобы продолжить", "войти в instagram")

# Внешняя ссылка в био профиля — Instagram рендерит её единственным <a> внутри шапки профиля с
# href, указывающим НЕ на instagram.com (в отличие от остальных ссылок на странице, которые все
# внутренние). Тот же приём, от которого в своё время отказались в parser-service/app/adapters/vk.py
# (там сначала тоже пробовали регексом искать домен по всему тексту страницы и словили false
# positive на рекламных виджетах) — здесь оправдан, потому что у Instagram-профиля попросту нет
# сайдбара с посторонней рекламой, единственная внешняя ссылка на всей странице — это и есть
# сайт из био. НЕ проверено вживую — при первом реальном прогоне свериться через
# IG_HEADLESS=false и обновить селектор/приём по образцу остального кода этого файла.
_PROFILE_HEADER_SELECTOR = "header section"
_INSTAGRAM_HOST_PATTERN = re.compile(r"^https?://(www\.)?instagram\.com", re.IGNORECASE)

_SESSION_CHECK_TIMEOUT_MS = 15_000


class SessionExpiredError(Exception):
    """Instagram показал экран повторного входа вместо запрошенной страницы — сохранённая сессия
    аккаунта протухла, дальнейший обход тем же браузером/аккаунтом бессмысленный. Отдельный класс
    (не общий с VK) — по тому же принципу, что и parser-service/app/adapters/vk.py::SessionExpiredError."""


def _goto_with_retry(page: Page, url: str, attempts: int = 2) -> None:
    """Открытие instagram.com изредка падает транзитным сбоем сети/ответа (таймаут,
    ERR_HTTP_RESPONSE_CODE_FAILURE — подтверждено вживую, см. scripts/instagram_manual_login.py)
    — без ретрая единственный такой сбой сразу роняет всю задачу парсинга ошибкой "internal
    error", хотя следующая попытка почти всегда проходит."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except TargetClosedError:
            raise  # браузер мёртв целиком — см. тот же приём в _has_bio_link, ретраить бессмысленно
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(2.0)
    raise last_exc


# Свежий Playwright-профиль (даже с валидным storage_state) иногда получает от Instagram не
# сразу настоящую ленту, а промежуточный экран подтверждения личности — "Continue as
# <username>" / "Use another profile" / "Create new account" (подтверждено вживую 2026-09-18,
# скриншот: обычная лента подменяется этим экраном без единого элемента настоящего интерфейса
# — ни поиска, ни сайдбара). На нём вообще нет ничего, что ищет остальной код этого файла,
# поэтому без обработки вызывающий код просто зависает в wait_for_selector на 30-40с и потом
# падает с "не догрузился". "Use another profile" — текст, специфичный именно для этого
# экрана, не пересекается с реальным интерфейсом ленты.
_CONTINUE_SPLASH_MARKER = "Use another profile"


def _dismiss_continue_splash(page: Page) -> None:
    """Best-effort попытка проскочить экран подтверждения личности (см. _CONTINUE_SPLASH_MARKER
    выше) — НЕ проверено вживую до конца: сам клик по кнопке "Continue" в part of live-тестов
    иногда не давал видимого эффекта (возможно, из-за анти-бот throttling при частых
    автоматических заходах одним и тем же аккаунтом за короткое время). При первом реальном
    прогоне после паузы — свериться через IG_HEADLESS=false и обновить по образцу остального
    кода файла, если сюда стабильно не помогает."""
    try:
        if _CONTINUE_SPLASH_MARKER not in (page.inner_text("body") or ""):
            return
    except Exception:
        return
    for btn in page.query_selector_all('div[role="button"]'):
        if (btn.inner_text() or "").strip() == "Continue":
            try:
                btn.click(force=True, timeout=5000)
            except Exception:
                pass
            break
    page.wait_for_timeout(3000)


def _raise_if_login_required(page: Page) -> None:
    content = (page.inner_text("body") or "").lower()
    if any(marker in content for marker in _LOGIN_REQUIRED_TEXT):
        raise SessionExpiredError(
            "Instagram требует повторного входа — сохранённая сессия аккаунта протухла, "
            "нужно перелогиниться (кнопка «Войти в Instagram» в панели)"
        )


# Тот же приём и список типов/доменов, что уже проверен вживую в parser-service/app/adapters/vk.py
# (см. parser-speedup-ideas.md) — не трогаем сам JS/CSS Instagram, это SPA.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_SUBSTRINGS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com/tr",
    "connect.facebook.net",
)


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


def check_session(storage_state: dict | None) -> bool:
    """Реальная проверка через Playwright: жива ли сохранённая Instagram-сессия.

    Открывает instagram.com с сохранённым storage_state и ждёт пункт навигации поиска
    (_SEARCH_NAV_SELECTOR) — он рисуется только в авторизованном интерфейсе; при протухшей
    сессии вместо этого показывается форма логина."""
    if not storage_state:
        return False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.IG_HEADLESS)
        context = browser.new_context(storage_state=storage_state)
        _block_heavy_resources(context)
        page = context.new_page()
        try:
            _goto_with_retry(page, "https://www.instagram.com/")
            _dismiss_continue_splash(page)
            page.wait_for_selector(_SEARCH_NAV_SELECTOR, timeout=_SESSION_CHECK_TIMEOUT_MS)
            return True
        except PlaywrightTimeoutError:
            return False
        except TargetClosedError:
            raise
        except Exception:
            logger.warning("instagram check_session: failed to load instagram.com, treating session as invalid", exc_info=True)
            return False
        finally:
            browser.close()


class InstagramParserAdapter:
    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[RawLead]:
        leads: list[RawLead] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.IG_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            _block_heavy_resources(context)
            page = context.new_page()
            try:
                candidates = self._search_accounts(page, keyword, filters, leads)

                want_site = filters.has_site is True
                total = len(candidates)
                if on_progress is not None:
                    on_progress(0, total, 0)
                for checked, lead in enumerate(candidates, start=1):
                    has_site = self._has_bio_link(page, lead.group_url)
                    if has_site == want_site:
                        leads.append(lead)
                        self._sleep_random()
                    if on_progress is not None:
                        on_progress(checked, total, len(leads))
                    if filters.max_groups is not None and len(leads) >= filters.max_groups:
                        break
            finally:
                context.close()
                browser.close()
        return leads

    def _search_accounts(
        self, page: Page, keyword: str, filters: ParseFilters, collected_so_far: list[RawLead]
    ) -> list[RawLead]:
        """Открывает поисковую панель Instagram, вводит keyword и собирает аккаунты из выпадающего
        списка подсказок — прямой аналог VkParserAdapter._open_communities_search, только без
        отдельной страницы результатов (у Instagram весь список — подсказки в той же панели)."""
        _goto_with_retry(page, "https://www.instagram.com/")
        _dismiss_continue_splash(page)
        try:
            search_nav = page.wait_for_selector(_SEARCH_NAV_SELECTOR, timeout=30_000)
        except PlaywrightTimeoutError:
            _raise_if_login_required(page)
            raise RuntimeError("Instagram не догрузился — пункт меню поиска не появился за 30с")
        self._raise_if_captcha(page, collected_so_far)

        search_nav.click()
        try:
            search_input = page.wait_for_selector(_SEARCH_INPUT_SELECTOR, timeout=10_000)
        except PlaywrightTimeoutError:
            return []  # панель поиска не открылась — не гадаем, пустой результат
        search_input.click()
        search_input.type(keyword, delay=random.uniform(30, 80))
        page.wait_for_timeout(1500)  # выпадающий список подсказок обновляется асинхронно
        self._raise_if_captcha(page, collected_so_far)

        if filters.max_groups is not None:
            self._scroll_suggestions_until_enough_new(page, filters)

        seen_usernames: set[str] = set()
        candidates: list[RawLead] = []
        for item in page.query_selector_all(_SUGGESTION_ITEM_SELECTOR):
            lead = self._parse_suggestion(item)
            if lead is None or lead.external_id in seen_usernames:
                continue
            seen_usernames.add(lead.external_id)
            if lead.external_id in filters.known_external_ids:
                continue
            candidates.append(lead)
        return candidates

    def _scroll_suggestions_until_enough_new(self, page: Page, filters: ParseFilters) -> None:
        # Панель подсказок Instagram докручивается как обычный скроллящийся контейнер, а не вся
        # страница — прямой аналог _scroll_until_enough_new_candidates в parser-service/app/
        # adapters/vk.py, но без отдельной секции результатов.
        def count_new() -> int:
            seen: set[str] = set()
            total = 0
            for item in page.query_selector_all(_SUGGESTION_ITEM_SELECTOR):
                lead = self._parse_suggestion(item)
                if lead is None or lead.external_id in seen:
                    continue
                seen.add(lead.external_id)
                if lead.external_id not in filters.known_external_ids:
                    total += 1
            return total

        max_groups = filters.max_groups
        stalls = 0
        total_loaded = len(page.query_selector_all(_SUGGESTION_ITEM_SELECTOR))
        for _ in range(25):
            if max_groups is not None and count_new() >= max_groups:
                return
            page.mouse.wheel(0, 800)
            page.wait_for_timeout(1200)
            current_total = len(page.query_selector_all(_SUGGESTION_ITEM_SELECTOR))
            if current_total == total_loaded:
                stalls += 1
                if stalls >= 2:
                    return
                continue
            stalls = 0
            total_loaded = current_total

    def _parse_suggestion(self, item) -> RawLead | None:
        href = item.get_attribute("href") or ""
        if not href.startswith("/") or href.startswith("/explore/"):
            return None  # хэштег/раздел — не профиль аккаунта
        username = href.strip("/")
        if not username or "/" in username:
            return None  # составной путь вида "/user/reels/" — не корневой профиль
        title = (item.inner_text() or "").strip().split("\n")[0] or None
        return RawLead(
            external_id=username,
            group_url=f"https://www.instagram.com/{username}/",
            title=title,
        )

    def _has_bio_link(self, page: Page, profile_url: str) -> bool | None:
        # Лишний переход на страницу каждого кандидата — тот же компромисс, что и в
        # parser-service/app/adapters/vk.py::_has_external_site: в самой панели поиска домен из
        # био не показывается, приходится открывать профиль.
        for attempt in range(2):
            try:
                page.goto(profile_url, wait_until="domcontentloaded")
                break
            except TargetClosedError:
                raise  # браузер мёртв целиком — см. тот же приём в vk.py, дальше перебирать бессмысленно
            except Exception:
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                logger.warning("failed to load profile page %s, skipping (unknown bio-link status)", profile_url, exc_info=True)
                return None

        try:
            page.wait_for_selector(_PROFILE_HEADER_SELECTOR, timeout=settings.IG_BIO_CHECK_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            _raise_if_login_required(page)
            return False  # страница загрузилась, шапка профиля не появилась — считаем, что ссылки нет
        except TargetClosedError:
            raise
        except Exception:
            logger.warning("failed to check bio link for %s, skipping (unknown bio-link status)", profile_url, exc_info=True)
            return None

        header = page.query_selector(_PROFILE_HEADER_SELECTOR)
        if header is None:
            return False
        for link in header.query_selector_all("a[href]"):
            href = link.get_attribute("href") or ""
            if href.startswith("http") and not _INSTAGRAM_HOST_PATTERN.match(href):
                return True
        return False

    def _raise_if_captcha(self, page: Page, collected_so_far: list[RawLead]) -> None:
        content = (page.inner_text("body") or "").lower()
        if any(marker in content for marker in _CAPTCHA_OR_CHECKPOINT_TEXT):
            raise CaptchaDetectedError("Instagram captcha/checkpoint detected", collected_so_far)

    def _sleep_random(self) -> None:
        time.sleep(random.uniform(settings.IG_MIN_DELAY_SEC, settings.IG_MAX_DELAY_SEC))

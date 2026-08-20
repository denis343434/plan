import logging
import random
import re
import time
from typing import Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters.base import CaptchaDetectedError, ParseFilters, RawLead
from app.config import settings

logger = logging.getLogger(__name__)

# Первая попытка была сканировать весь видимый текст страницы группы регексом на домен —
# отбросили: VK показывает в сайдбаре ротирующуюся рекламу (случайные реальные домены типа
# "cian.ru", "school.ru"), это давало сплошные false positive почти на каждой группе.
# Вместо этого используем структурированное поле VK "официальный сайт сообщества"
# (data-testid="group-info-site", проверено вживую) — если оно заполнено, у группы есть
# сайт; regex здесь — валидация, что там реально доменоподобная строка, а не мусор/пусто.
_SITE_FIELD_SELECTOR = '[data-testid="group-info-site"]'
_SITE_DOMAIN_PATTERN = re.compile(r"[a-zа-яё0-9][a-zа-яё0-9-]*\.[a-zа-яё]{2,}", re.IGNORECASE)

_SEARCH_URL = "https://vk.com/search/communities?q={keyword}"

# Проверено вживую 2026-08-20 через реальный залогиненный аккаунт: старые
# "/search/groups?c[q]=" и SearchGroupsList__* — рудимент дореформенного VK, современный
# поиск отдаёт 0 результатов по этому URL (поле поиска в шапке остаётся пустым). Актуальный
# путь — /search/communities?q=, карточки — VKUI RichCell с data-testid="group_item_desktop_list".
_GROUP_CARD_SELECTOR = '[data-testid="group_item_desktop_list"]'
_GROUP_LINK_SELECTOR = "a.vkuiAvatar__host"
_GROUP_TITLE_SELECTOR = '[class*="vkuiHeadline"]'  # компонент не рендерит "__host", только "__level*"/"__density*"
_CAPTCHA_SELECTOR = "div.captcha, form[action*='captcha']"


class VkParserAdapter:
    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def search_communities(
        self,
        keyword: str,
        filters: ParseFilters,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[RawLead]:
        leads: list[RawLead] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            page = context.new_page()
            try:
                # wait_until="load" (дефолт) у VK почти всегда упирается в таймаут: страница —
                # тяжёлый SPA с фоновыми вебсокетами/long-polling (онлайн-статус, аналитика),
                # из-за которых событие "load" может не наступить вовсе, хотя сам контент уже
                # отрисован. "domcontentloaded" + явное ожидание карточек — надёжнее.
                page.goto(_SEARCH_URL.format(keyword=keyword), wait_until="domcontentloaded")
                self._sleep_random()
                self._raise_if_captcha(page, leads)

                try:
                    page.wait_for_selector(_GROUP_CARD_SELECTOR, timeout=15_000)
                except PlaywrightTimeoutError:
                    pass  # ни одной группы по запросу — не ошибка, просто пустой результат

                candidates = [
                    lead
                    for card in page.query_selector_all(_GROUP_CARD_SELECTOR)
                    if (lead := self._parse_card(card, filters)) is not None
                ]

                # filters.has_site is True — явный запрос "только с сайтом", инвертируем;
                # False/None (дефолт) — обычное поведение парсера: сайт есть → лид не нужен
                # (весь смысл инструмента — искать сообщества БЕЗ своего сайта, см. architecture.md).
                want_site = filters.has_site is True
                total = len(candidates)
                if on_progress is not None:
                    on_progress(0, total)
                for checked, lead in enumerate(candidates, start=1):
                    has_site = self._has_external_site(page, lead.group_url)
                    if has_site == want_site:
                        leads.append(lead)
                        self._sleep_random()
                    if on_progress is not None:
                        on_progress(checked, total)
                    if filters.max_groups is not None and len(leads) >= filters.max_groups:
                        break  # набрали нужное количество — дальше кандидатов не проверяем
            finally:
                context.close()
                browser.close()
        return leads

    def _has_external_site(self, page: Page, group_url: str) -> bool:
        # Лишний реальный переход на страницу каждого кандидата — дороже по времени и риску
        # антибана, чем просто разбор карточек поиска, но иначе сайт не проверить: в самой
        # выдаче поиска домен нигде не показывается.
        try:
            page.goto(group_url, wait_until="domcontentloaded")
            page.wait_for_selector(_SITE_FIELD_SELECTOR, timeout=15_000)
        except PlaywrightTimeoutError:
            return False  # поле не появилось за разумное время — у группы просто нет сайта
        except Exception:
            logger.warning("failed to check site presence for %s, treating as no site", group_url, exc_info=True)
            return False
        el = page.query_selector(_SITE_FIELD_SELECTOR)
        if el is None:
            return False
        return bool(_SITE_DOMAIN_PATTERN.search(el.inner_text() or ""))

    def _parse_card(self, card, filters: ParseFilters) -> RawLead | None:
        link = card.query_selector(_GROUP_LINK_SELECTOR)
        if link is None:
            return None
        href = link.get_attribute("href") or ""
        # href вида "/flowerssv.moscow?search_track_code=..." — отбрасываем query-строку,
        # иначе она попадёт в external_id и сломает дедупликацию по (platform, external_id).
        external_id = href.split("?", 1)[0].lstrip("/")
        if not external_id:
            return None

        title_el = card.query_selector(_GROUP_TITLE_SELECTOR)
        title = title_el.inner_text().strip() if title_el is not None else None

        return RawLead(
            external_id=external_id,
            group_url=f"https://vk.com/{external_id}",
            title=title,
        )

    def _raise_if_captcha(self, page: Page, collected_so_far: list[RawLead]) -> None:
        if page.query_selector(_CAPTCHA_SELECTOR) is not None:
            raise CaptchaDetectedError("VK captcha/anti-bot check detected", collected_so_far)

    def _sleep_random(self) -> None:
        time.sleep(random.uniform(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC))

import logging
import random
import time

from playwright.sync_api import Page, sync_playwright

from app.adapters.base import CaptchaDetectedError, ParseFilters, RawLead
from app.config import settings

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://vk.com/search/groups?c[q]={keyword}"

# Селекторы поддерживаются вручную по факту разметки VK — живьём не проверялись
# в этой сессии (нет реального аккаунта), см. plan-parser-service.md, раздел "Тесты".
_GROUP_CARD_SELECTOR = "div.SearchGroupsList__item"
_GROUP_LINK_SELECTOR = "a.SearchGroupsList__link"
_GROUP_TITLE_SELECTOR = "div.SearchGroupsList__title"
_CAPTCHA_SELECTOR = "div.captcha, form[action*='captcha']"


class VkParserAdapter:
    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def search_communities(self, keyword: str, filters: ParseFilters) -> list[RawLead]:
        leads: list[RawLead] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            page = context.new_page()
            try:
                page.goto(_SEARCH_URL.format(keyword=keyword))
                self._sleep_random()
                self._raise_if_captcha(page, leads)

                for card in page.query_selector_all(_GROUP_CARD_SELECTOR):
                    lead = self._parse_card(card, filters)
                    if lead is not None:
                        leads.append(lead)
            finally:
                context.close()
                browser.close()
        return leads

    def _parse_card(self, card, filters: ParseFilters) -> RawLead | None:
        link = card.query_selector(_GROUP_LINK_SELECTOR)
        if link is None:
            return None
        href = link.get_attribute("href") or ""
        external_id = href.rsplit("/", 1)[-1]
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

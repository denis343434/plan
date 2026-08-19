import logging
import random
import time

from playwright.sync_api import Page, sync_playwright

from app.adapters.base import SendResult
from app.config import settings

logger = logging.getLogger(__name__)

# VK message deep-link для сообщества: sel=-{group_id} адресует диалог с сообществом.
_MESSAGE_URL = "https://vk.com/im?sel=-{external_id}"

# Селекторы поддерживаются вручную по факту разметки VK — живьём не проверялись
# в этой сессии (нет реального аккаунта), см. plan-messaging-service.md, раздел "Verification".
_MESSAGE_INPUT_SELECTOR = "div.ChatInput__inputWrap [contenteditable='true']"
_SEND_BUTTON_SELECTOR = "button.ChatInput__sendBtn"
_CAPTCHA_SELECTOR = "div.captcha, form[action*='captcha'], div.im-page--service-message"
_FLOOD_TEXT_MARKERS = ("много сообщений", "flood", "captcha")


class VkSendAdapter:
    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def send_message(self, lead: dict, account: dict, text: str) -> SendResult:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            page = context.new_page()
            try:
                page.goto(_MESSAGE_URL.format(external_id=lead["external_id"]))
                self._sleep_random()

                flood = self._detect_flood(page)
                if flood is not None:
                    return SendResult(success=False, error=flood, flood_detected=True)

                page.click(_MESSAGE_INPUT_SELECTOR)
                self._sleep_random()
                self._type_like_human(page, text)

                page.click(_SEND_BUTTON_SELECTOR)
                self._sleep_random()

                flood = self._detect_flood(page)
                if flood is not None:
                    return SendResult(success=False, error=flood, flood_detected=True)

                return SendResult(success=True)
            except Exception as exc:  # unexpected Playwright/DOM failure — reported as a delivery failure
                logger.exception("vk send failed for lead %s", lead.get("id"))
                return SendResult(success=False, error=str(exc))
            finally:
                context.close()
                browser.close()

    def _detect_flood(self, page: Page) -> str | None:
        if page.query_selector(_CAPTCHA_SELECTOR) is not None:
            return "VK captcha/anti-bot check detected"
        content = page.content().lower()
        for marker in _FLOOD_TEXT_MARKERS:
            if marker in content:
                return f"VK flood/anti-bot marker detected: {marker}"
        return None

    def _type_like_human(self, page: Page, text: str) -> None:
        for char in text:
            page.type(_MESSAGE_INPUT_SELECTOR, char, delay=random.uniform(30, 120))

    def _sleep_random(self) -> None:
        time.sleep(random.uniform(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC))

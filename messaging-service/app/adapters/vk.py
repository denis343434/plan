import logging
import random
import time
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters.base import SendResult
from app.config import settings

logger = logging.getLogger(__name__)

# Проверено вживую 2026-08-20: "vk.com/im?sel=-{external_id}" ломается, если external_id —
# screen_name сообщества (не число), а не сам числовой ID группы — именно так теперь выглядит
# external_id (см. parser-service/app/adapters/vk.py, современный поиск отдаёт screen_name).
# VK не резолвит screen_name в sel= — открывается пустой мессенджер без выбранного диалога.
# Рабочий путь — как у живого пользователя: зайти на страницу сообщества и взять href из
# кнопки "Написать сообщение" (data-testid="group_action_send_message") — в нём уже
# зашит настоящий числовой ID (/im/convo/-<id>...), резолвить screen_name самим не нужно.
_SEND_MESSAGE_BUTTON_SELECTOR = '[data-testid="group_action_send_message"]'

# Селекторы проверены вживую 2026-08-20 через реальный залогиненный аккаунт (см. также
# parser-service/app/adapters/vk.py — тот же VKUI, тот же принцип подбора).
_MESSAGE_INPUT_SELECTOR = ".ComposerInput__input"
_SEND_BUTTON_SELECTOR = ".ConvoComposer__sendButton--submit"
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
                # wait_until="load" (дефолт) у VK почти всегда упирается в таймаут — страница
                # держит вебсокет для реалтайм-чата, из-за которого "load" может не наступить
                # вовсе, хотя сам контент уже отрисован. "domcontentloaded" + явные ожидания
                # конкретных элементов — надёжнее.
                page.goto(lead["group_url"], wait_until="domcontentloaded")
                try:
                    send_btn = page.wait_for_selector(_SEND_MESSAGE_BUTTON_SELECTOR, timeout=20_000)
                except PlaywrightTimeoutError:
                    # Не проблема аккаунта/кода — либо сообщество отключило приём сообщений
                    # от посторонних (кнопки физически нет), либо VK не успел её отрисовать
                    # за 20с. В обоих случаях это лид-специфичная неудача, не флуд/бан аккаунта.
                    return SendResult(
                        success=False,
                        error="у сообщества недоступна кнопка «Написать сообщение» — либо приём сообщений отключён, либо страница не успела прогрузиться",
                    )
                chat_href = send_btn.get_attribute("href")
                page.goto(urljoin(page.url, chat_href), wait_until="domcontentloaded")
                self._sleep_random()

                flood = self._detect_flood(page)
                if flood is not None:
                    return SendResult(success=False, error=flood, flood_detected=True)

                try:
                    page.wait_for_selector(_MESSAGE_INPUT_SELECTOR, timeout=20_000)
                except PlaywrightTimeoutError:
                    return SendResult(
                        success=False,
                        error="чат открылся, но поле ввода сообщения не появилось за 20с (медленный рендер VK)",
                    )
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
        # page.content() — это ВЕСЬ HTML, включая инлайновые <script> с конфигом фронтенда,
        # где почти на любой странице VK встречаются строки вроде "fix_captcha_hitman_show"
        # (фича-флаги) — по ним "captcha"/"flood" ловились как false positive на каждой
        # реальной странице. inner_text("body") — только видимый пользователю текст.
        content = page.inner_text("body").lower()
        for marker in _FLOOD_TEXT_MARKERS:
            if marker in content:
                return f"VK flood/anti-bot marker detected: {marker}"
        return None

    def _type_like_human(self, page: Page, text: str) -> None:
        for char in text:
            page.type(_MESSAGE_INPUT_SELECTOR, char, delay=random.uniform(30, 120))

    def _sleep_random(self) -> None:
        time.sleep(random.uniform(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC))

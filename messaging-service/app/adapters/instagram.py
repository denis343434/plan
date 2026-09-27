import logging
import random
import re
import time
from typing import Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters.base import ReplyCheckResult, SendResult, SessionExpiredError
from app.config import settings

logger = logging.getLogger(__name__)

_SLOW_RENDER_TIMEOUT_MS = 40_000

# У Instagram кнопка "Message"/"Написать" на чужом профиле открывает DM-композер независимо от
# того, подписан ли отправитель на адресата (сообщение просто уйдёт в "Запросы" адресата — см.
# осознанное решение пользователя не подписываться заранее). НЕ проверено вживую — при первом
# реальном прогоне свериться через IG_HEADLESS=false и обновить селектор по образцу
# messaging-service/app/adapters/vk.py.
_MESSAGE_BUTTON_SELECTOR = 'header div[role="button"]:has-text("Message"), header div[role="button"]:has-text("Написать")'
_MESSAGE_INPUT_SELECTOR = 'textarea[placeholder="Message..."], div[aria-label="Message"][contenteditable="true"], textarea[placeholder="Сообщение..."]'
_FLOOD_TEXT_MARKERS = (
    "action blocked",
    "try again later",
    "temporarily blocked",
    "мы временно ограничили",
    "попробуйте позже",
)
_LOGIN_REQUIRED_TEXT = ("log in to continue", "войдите, чтобы продолжить", "войти в instagram")

_INBOX_URL = "https://www.instagram.com/direct/inbox/"
_THREAD_ITEM_SELECTOR = 'div[role="listitem"]'
_THREAD_ROW_LINK_SELECTOR = 'a[href^="/"]'
_MESSAGE_ROW_SELECTOR = 'div[role="row"]'


def _raise_if_login_required(page: Page) -> None:
    content = (page.inner_text("body") or "").lower()
    if any(marker in content for marker in _LOGIN_REQUIRED_TEXT):
        raise SessionExpiredError(
            "Instagram требует повторного входа — сохранённая сессия аккаунта протухла, "
            "нужно перелогиниться (кнопка «Войти в Instagram» в панели)"
        )


# Свежий Playwright-профиль (даже с валидным storage_state) иногда получает от Instagram не
# сразу настоящую страницу, а промежуточный экран подтверждения личности — "Continue as
# <username>" / "Use another profile" / "Create new account" (подтверждено вживую 2026-09-18
# в parser-service/app/adapters/instagram.py — тот же принцип и здесь, тот же признак). Без
# обработки кнопка "Message"/поле ввода просто никогда не появляются, и send_message зависает
# в wait_for_selector на все _SLOW_RENDER_TIMEOUT_MS впустую.
_CONTINUE_SPLASH_MARKER = "Use another profile"


def _dismiss_continue_splash(page: Page) -> None:
    """Best-effort попытка проскочить экран подтверждения личности — НЕ проверено вживую до
    конца (см. тот же комментарий и предупреждение в parser-service/app/adapters/instagram.py)."""
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


def _sleep_random() -> None:
    time.sleep(random.uniform(settings.IG_MIN_DELAY_SEC, settings.IG_MAX_DELAY_SEC))


class _PageLoadError(Exception):
    """page.goto сам не смог загрузить страницу — сбой транспорта, не признак того, что этому
    лиду нельзя написать. Тот же принцип, что и messaging-service/app/adapters/vk.py::_PageLoadError."""


def _goto_or_raise(page: Page, url: str) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        raise _PageLoadError(str(exc)) from exc


class InstagramSendAdapter:
    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def send_message(self, lead: dict, account: dict, text: str, image: dict | None = None) -> SendResult:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.IG_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            _block_heavy_resources(context)
            page = context.new_page()
            try:
                _goto_or_raise(page, lead["group_url"])
                _dismiss_continue_splash(page)

                try:
                    message_btn = page.wait_for_selector(_MESSAGE_BUTTON_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    _raise_if_login_required(page)
                    return SendResult(
                        success=False,
                        error="у профиля недоступна кнопка «Message» — либо приём сообщений отключён, "
                        "либо страница не успела прогрузиться",
                    )

                message_btn.click()
                _sleep_random()

                flood = self._detect_flood(page)
                if flood is not None:
                    return SendResult(success=False, error=flood, flood_detected=True)

                try:
                    page.wait_for_selector(_MESSAGE_INPUT_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    return SendResult(
                        success=False,
                        error="чат открылся, но поле ввода сообщения не появилось за 40с (медленный рендер Instagram)",
                    )

                if image is not None:
                    logger.warning(
                        "InstagramSendAdapter: image attachment requested but not implemented for lead %s",
                        lead.get("id"),
                    )

                page.click(_MESSAGE_INPUT_SELECTOR)
                _sleep_random()
                self._type_like_human(page, text)
                page.keyboard.press("Enter")
                _sleep_random()

                flood = self._detect_flood(page)
                if flood is not None:
                    return SendResult(success=False, error=flood, flood_detected=True)

                return SendResult(success=True)
            except _PageLoadError as exc:
                logger.warning("failed to load Instagram page for lead %s, will retry: %s", lead.get("id"), exc)
                return SendResult(success=False, error=str(exc), network_error=True)
            except SessionExpiredError as exc:
                return SendResult(success=False, error=str(exc), session_expired=True)
            except Exception as exc:  # unexpected Playwright/DOM failure — reported as a delivery failure
                logger.exception("instagram send failed for lead %s", lead.get("id"))
                return SendResult(success=False, error=str(exc))
            finally:
                context.close()
                browser.close()

    def _detect_flood(self, page: Page) -> str | None:
        content = page.inner_text("body").lower()
        for marker in _FLOOD_TEXT_MARKERS:
            if marker in content:
                return f"Instagram flood/anti-bot marker detected: {marker}"
        return None

    def _type_like_human(self, page: Page, text: str) -> None:
        for char in text:
            page.type(_MESSAGE_INPUT_SELECTOR, char, delay=random.uniform(30, 120))


class InstagramInboxAdapter:
    """Читает Direct-инбокс уже отправленных лидов, чтобы найти входящие ответы.

    В отличие от VkInboxAdapter — без многопоточного полного обхода: сопоставление идёт по
    списку тредов инбокса (аналог "только непрочитанные" у VK), поштучный обход профиль-за-
    профилем у Instagram не имеет прямого аналога send_message (нет отдельной страницы диалога
    по URL лида) и не проверен вживую — оставлен как единственная стратегия на MVP."""

    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def check_replies(
        self, leads: list[dict], on_progress: Callable[[int, int], None] | None = None
    ) -> dict[str, ReplyCheckResult]:
        results: dict[str, ReplyCheckResult] = {}
        total = len(leads)
        if total == 0:
            return results

        leads_by_external_id = {
            (lead.get("external_id") or "").strip("/"): lead for lead in leads if lead.get("external_id")
        }

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.IG_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            _block_heavy_resources(context)
            try:
                page = context.new_page()
                try:
                    page.goto(_INBOX_URL, wait_until="domcontentloaded")
                    _dismiss_continue_splash(page)
                    page.wait_for_selector(_THREAD_ITEM_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    _raise_if_login_required(page)
                    # Список тредов не отрисовался — не гадаем по каждому лиду отдельно, честно
                    # сообщаем "нет ответа" (тот же принцип, что и у VK при пустом списке).
                    for lead in leads:
                        results[lead["id"]] = ReplyCheckResult(has_reply=False, error="inbox list did not load")
                        if on_progress is not None:
                            on_progress(len(results), total)
                    return results

                for item in page.query_selector_all(_THREAD_ITEM_SELECTOR):
                    external_id = self._thread_username(item)
                    lead = leads_by_external_id.get(external_id) if external_id else None
                    if lead is None or lead["id"] in results:
                        continue
                    has_reply = self._thread_has_unread(item)
                    results[lead["id"]] = ReplyCheckResult(has_reply=has_reply)
                    if on_progress is not None:
                        on_progress(len(results), total)

                for lead in leads:
                    if lead["id"] not in results:
                        results[lead["id"]] = ReplyCheckResult(has_reply=False)
                        if on_progress is not None:
                            on_progress(len(results), total)
                return results
            finally:
                context.close()
                browser.close()

    def _thread_username(self, item) -> str | None:
        link = item.query_selector(_THREAD_ROW_LINK_SELECTOR)
        if link is None:
            return None
        href = link.get_attribute("href") or ""
        return href.strip("/") or None

    def _thread_has_unread(self, item) -> bool:
        # Instagram помечает непрочитанный тред жирным начертанием заголовка/точкой-индикатором —
        # надёжного data-атрибута публично не задокументировано. Эвристика по классу "жирный
        # текст" не проверена вживую, при первом реальном прогоне свериться через
        # IG_HEADLESS=false и обновить по образцу остального кода этого файла.
        text = (item.inner_text() or "")
        return "•" in text or bool(re.search(r"\bunread\b", text, re.IGNORECASE))

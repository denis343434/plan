import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from urllib.parse import urljoin

from playwright.sync_api import ElementHandle, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.adapters.base import ReplyCheckResult, SendResult
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

# Проверено вживую 2026-08-21 через реальный залогиненный аккаунт (переписка с ответившим
# сообществом). Переписка с сообществом (business-messaging, не личный диалог) рендерится VK
# в "безбабловом" ("WithoutBubble") стиле — плоский список строк с аватаркой+именем автора,
# а не классические пузыри "своё/чужое" по бокам; отсюда и то, что первая версия этого файла
# ничего не находила (`.ConvoMessage`/`[data-testid="message_bubble"]` тут просто не существуют).
# Раньше "своё/чужое" определялось эвристикой по горизонтальному выравниванию — в этом стиле
# у всех сообщений одинаковый левый отступ (аватарки слева независимо от автора), эвристика
# всегда бы промахивалась. Надёжный признак — href на автора: у сообщества это /<external_id>
# лида (см. _is_incoming), у своих сообщений — ссылка на профиль залогиненного аккаунта.
_MESSAGE_ITEM_SELECTOR = ".ConvoMessageWithoutBubble"
_MESSAGE_AUTHOR_LINK_SELECTOR = ".ConvoMessageHeader__authorLink"
_MESSAGE_TEXT_SELECTOR = ".ConvoMessageWithoutBubble__text"


def _sleep_random() -> None:
    time.sleep(random.uniform(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC))


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
                _sleep_random()

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
                _sleep_random()
                self._type_like_human(page, text)

                page.click(_SEND_BUTTON_SELECTOR)
                _sleep_random()

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


class VkInboxAdapter:
    """Читает диалоги уже отправленных лидов, чтобы найти входящие ответы.

    Один браузерный контекст на весь батч лидов (не один на лида, как в VkSendAdapter) —
    иначе повторный запуск/закрытие Chromium на каждый чат сделал бы проверку 20+ лидов
    минутами и выглядело бы для VK подозрительнее, чем один сеанс просмотра нескольких чатов.
    Внутри этого одного контекста лиды разбираются несколькими Page параллельно
    (settings.INBOX_CHECK_CONCURRENCY) — те же куки/логин, просто несколько вкладок вместо
    одной последовательной очереди.
    """

    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def check_replies(
        self, leads: list[dict], on_progress: Callable[[int, int], None] | None = None
    ) -> dict[str, ReplyCheckResult]:
        results: dict[str, ReplyCheckResult] = {}
        total = len(leads)
        if total == 0:
            return results

        progress_lock = threading.Lock()
        checked = 0

        def report_progress() -> None:
            nonlocal checked
            with progress_lock:
                checked += 1
                current = checked
            if on_progress is not None:
                on_progress(current, total)

        def worker(page: Page, leads_slice: list[dict]) -> None:
            for lead in leads_slice:
                results[lead["id"]] = self._check_one(page, lead)
                report_progress()
                _sleep_random()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
            # Несколько Page в одном BrowserContext — те же куки/логин, что и раньше (один
            # аккаунт), просто несколько параллельных "вкладок" вместо одной последовательной —
            # так и снижаем ~40с/лид без множественных запусков Chromium или второй сессии.
            context = browser.new_context(storage_state=self._storage_state or None)
            try:
                page_count = max(1, min(settings.INBOX_CHECK_CONCURRENCY, total))
                pages = [context.new_page() for _ in range(page_count)]
                buckets: list[list[dict]] = [[] for _ in pages]
                for i, lead in enumerate(leads):
                    buckets[i % len(pages)].append(lead)

                with ThreadPoolExecutor(max_workers=len(pages)) as executor:
                    futures = [
                        executor.submit(worker, page, bucket)
                        for page, bucket in zip(pages, buckets)
                        if bucket
                    ]
                    for future in futures:
                        future.result()
            finally:
                context.close()
                browser.close()
        return results

    def _check_one(self, page: Page, lead: dict) -> ReplyCheckResult:
        try:
            cached_href = lead.get("chat_href")
            if cached_href:
                page.goto(urljoin(page.url, cached_href), wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(_MESSAGE_ITEM_SELECTOR, timeout=20_000)
                except PlaywrightTimeoutError:
                    # Мы уже отправляли этому лиду сообщение, значит переписка не может быть
                    # пустой — таймаут тут значит, что кэш протух (сообщество сменило ID, ссылка
                    # больше никуда не ведёт), а не "ответа ещё нет". Переразрешаем один раз через
                    # страницу сообщества, как раньше.
                    cached_href = None

            resolved_href: str | None = None
            if not cached_href:
                page.goto(lead["group_url"], wait_until="domcontentloaded")
                try:
                    send_btn = page.wait_for_selector(_SEND_MESSAGE_BUTTON_SELECTOR, timeout=20_000)
                except PlaywrightTimeoutError:
                    return ReplyCheckResult(
                        has_reply=False,
                        error="кнопка «Написать сообщение» недоступна — переписку не открыть",
                    )
                chat_href = send_btn.get_attribute("href")
                resolved_href = chat_href
                page.goto(urljoin(page.url, chat_href), wait_until="domcontentloaded")

                try:
                    page.wait_for_selector(_MESSAGE_ITEM_SELECTOR, timeout=20_000)
                except PlaywrightTimeoutError:
                    # переписки ещё нет или не успела отрисоваться
                    return ReplyCheckResult(has_reply=False, resolved_chat_href=resolved_href)

            items = page.query_selector_all(_MESSAGE_ITEM_SELECTOR)
            if not items:
                return ReplyCheckResult(has_reply=False, resolved_chat_href=resolved_href)

            last_author_href = self._last_author_href(items)
            if last_author_href is None or not self._is_incoming(last_author_href, lead):
                # последнее сообщение — наше собственное
                return ReplyCheckResult(has_reply=False, resolved_chat_href=resolved_href)

            preview = self._last_message_text(items)
            return ReplyCheckResult(has_reply=True, preview=preview, resolved_chat_href=resolved_href)
        except Exception as exc:  # unexpected Playwright/DOM failure — не роняем весь батч
            logger.exception("reply check failed for lead %s", lead.get("id"))
            return ReplyCheckResult(has_reply=False, error=str(exc))

    def _last_author_href(self, items: list[ElementHandle]) -> str | None:
        # VK схлопывает заголовок (аватар+имя) у подряд идущих сообщений одного автора —
        # у самого последнего сообщения его может не быть, тогда идём назад до ближайшего
        # сообщения, у которого заголовок есть (оно и определяет автора всей этой пачки).
        for item in reversed(items):
            link = item.query_selector(_MESSAGE_AUTHOR_LINK_SELECTOR)
            if link is not None:
                href = link.get_attribute("href")
                if href:
                    return href
        return None

    def _is_incoming(self, author_href: str, lead: dict) -> bool:
        # У сообщества-лида href на автора — "/<external_id>" (см. parser-service/app/adapters/vk.py,
        # тот же external_id, которым лид дедуплицируется) — у своих сообщений там ссылка на
        # профиль залогиненного аккаунта, никогда на сам лид.
        external_id = (lead.get("external_id") or "").strip("/")
        return bool(external_id) and author_href.strip("/") == external_id

    def _last_message_text(self, items: list[ElementHandle]) -> str | None:
        last = items[-1]
        text_el = last.query_selector(_MESSAGE_TEXT_SELECTOR)
        text = (text_el.inner_text() if text_el is not None else last.inner_text()) or ""
        text = text.strip().replace("\n", " ")[:300]
        return text or None

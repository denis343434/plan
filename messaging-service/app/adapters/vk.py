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

# Было 20_000 везде в этом файле — проверено вживую 2026-08-21 (диагностика парсер-сервиса,
# та же VK-сессия/среда): реальный контент VK иногда прогружается только через ~18-20с после
# domcontentloaded (страница до этого висит в скелетон-заглушке). 20с таймаут стоял впритык к
# этому и регулярно ловил "не успело отрисоваться" как настоящую ошибку (кнопка "Написать
# сообщение" недоступна / поле ввода не появилось) — хотя по факту элемент просто ещё не
# отрисовался. Единый таймаут с запасом вместо разрозненных 20_000 по всему файлу.
_SLOW_RENDER_TIMEOUT_MS = 40_000

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

# ПРЕЖНИЙ подход (общий бейдж непрочитанных в левом меню, "#l_msg [data-testid=...]") дважды
# ловил ложное срабатывание вживую на бизнес-переписке (см. историю в git) и был выброшен.
# Взамен — список диалогов vk.com/im с включённым встроенным тумблером VK "Только
# непрочитанные" (нижний колонтитул списка): когда он активен, VK сам показывает в списке
# ТОЛЬКО диалоги с непрочитанным — не нужно самостоятельно проверять счётчик у каждого
# элемента, достаточно собрать заголовки того, что осталось видно. Подтверждено пользователем
# 2026-08-21 на реальном DOM:
#   <label class="ConvoList__footer">
#     ...
#     <span class="ConvoList__footerText">Только непрочитанные</span>
#     <div class="ConvoList__footerSwitch">
#       <label class="vkuiSwitch__host ...">
#         <input class="vkuiSwitch__inputNative ..." type="checkbox" role="switch" aria-checked="true">
#         ...
#       </label>
#     </div>
#   </label>
#   ...
#   <div data-itemkey="convo_-235257158" class="ConvoList__item ...">
#     <h3 class="ConvoTitle__author" title="Тренажерный зал «YaGOda» Южный город">...</h3>
#     ...
#   </div>
# title у .ConvoTitle__author совпадает с lead["title"], который уже сохранён в базе при
# парсинге — сопоставляем по нему, без доп. запросов на резолв id. Пользователь подтвердил
# вживую: просто открыть vk.com/im, не кликая в чат, счётчики непрочитанных НЕ сбрасывает.
_ONLY_UNREAD_SWITCH_SELECTOR = '.ConvoList__footer input[role="switch"]'
_ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR = ".ConvoList__footer .vkuiSwitch__host"
_CONVO_ITEM_SELECTOR = ".ConvoList__item"
_CONVO_TITLE_SELECTOR = ".ConvoTitle__author"


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
                    send_btn = page.wait_for_selector(_SEND_MESSAGE_BUTTON_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    # Не проблема аккаунта/кода — либо сообщество отключило приём сообщений
                    # от посторонних (кнопки физически нет), либо VK не успел её отрисовать
                    # за _SLOW_RENDER_TIMEOUT_MS. В обоих случаях это лид-специфичная неудача,
                    # не флуд/бан аккаунта.
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
                    page.wait_for_selector(_MESSAGE_INPUT_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    return SendResult(
                        success=False,
                        error="чат открылся, но поле ввода сообщения не появилось за 40с (медленный рендер VK)",
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

    ВАЖНО про потоки: Playwright sync API жёстко привязывает Page/Browser/BrowserContext к
    OS-потоку, в котором был создан их sync_playwright() — обращение к ним из другого потока
    падает с "Cannot switch to a different thread" (проверено вживую 2026-08-21: с одним общим
    BrowserContext на несколько потоков падал КАЖДЫЙ лид, исключение тихо ловилось в _check_one
    и превращалось в has_reply=False — реальные ответы, например от "Кафе Миндаль", никогда не
    находились). Поэтому параллелизм здесь — не несколько Page в одном общем браузере, а
    несколько ПОЛНОСТЬЮ независимых sync_playwright()/Browser/BrowserContext, каждый в своём
    потоке (см. _run_worker), с одним и тем же storage_state (один и тот же залогиненный
    аккаунт). Это отдельные параллельные VK-сессии одного аккаунта, а не "вкладки одного
    браузера" — потенциально заметнее для антибот-систем VK, чем прежнее (нерабочее) намерение.
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

        checked = 0
        progress_lock = threading.Lock()

        def report_progress() -> None:
            nonlocal checked
            with progress_lock:
                checked += 1
                current = checked
            if on_progress is not None:
                on_progress(current, total)

        leads_to_check = leads
        if settings.INBOX_CHECK_FILTER_BY_UNREAD_LIST:
            unread_titles = self._read_unread_titles_gate()
            if unread_titles is None:
                logger.info(
                    "inbox check: could not read VK unread dialog list, falling back to full scan of %d lead(s)",
                    total,
                )
            else:
                leads_to_check = []
                for lead in leads:
                    title = (lead.get("title") or "").strip()
                    # Без title сопоставить с диалогом нечем — не гадаем, проверяем лида напрямую,
                    # а не молча считаем его "без ответа".
                    if not title or title in unread_titles:
                        leads_to_check.append(lead)
                    else:
                        results[lead["id"]] = ReplyCheckResult(has_reply=False)
                        report_progress()
                logger.info(
                    "inbox check: unread dialog list matched %d/%d lead(s) by title, skipping the rest",
                    len(leads_to_check),
                    total,
                )

        if not leads_to_check:
            return results

        worker_count = max(1, min(settings.INBOX_CHECK_CONCURRENCY, len(leads_to_check)))
        buckets: list[list[dict]] = [[] for _ in range(worker_count)]
        for i, lead in enumerate(leads_to_check):
            buckets[i % worker_count].append(lead)

        def run_worker(leads_slice: list[dict]) -> None:
            if not leads_slice:
                return
            thread_name = threading.current_thread().name
            logger.info(
                "inbox check: worker %s starting, %d lead(s) in bucket", thread_name, len(leads_slice)
            )
            # Собственный независимый Playwright-драйвер целиком внутри этого потока — см.
            # предупреждение о потоках в докстринге класса.
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
                context = browser.new_context(storage_state=self._storage_state or None)
                try:
                    page = context.new_page()
                    for lead in leads_slice:
                        logger.info(
                            "inbox check: worker %s checking lead %s", thread_name, lead["id"]
                        )
                        results[lead["id"]] = self._check_one(page, lead)
                        report_progress()
                        _sleep_random()
                finally:
                    context.close()
                    browser.close()

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(run_worker, bucket) for bucket in buckets if bucket]
            for future in futures:
                future.result()
        return results

    def _read_unread_titles_gate(self) -> set[str] | None:
        """Разовая проверка списка диалогов VK — отдельная короткая Playwright-сессия в
        вызывающем потоке, до того как запускаются потоки-воркеры для самого обхода."""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            try:
                return self._list_unread_conversation_titles(context.new_page())
            finally:
                context.close()
                browser.close()

    def _list_unread_conversation_titles(self, page: Page) -> set[str] | None:
        """Заголовки диалогов в списке vk.com/im, у которых есть непрочитанные сообщения.

        None означает "не удалось определить" (страница/список не прогрузились) — в этом
        случае вызывающий код обязан пойти по полному циклу, а не гадать.
        """
        try:
            page.goto("https://vk.com/im", wait_until="domcontentloaded")
        except Exception:
            return None

        try:
            page.wait_for_selector(_ONLY_UNREAD_SWITCH_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            return None  # список диалогов вообще не отрисовался — не гадаем

        switch = page.query_selector(_ONLY_UNREAD_SWITCH_SELECTOR)
        if switch is not None and switch.get_attribute("aria-checked") != "true":
            # Кликаем по видимому визуальному переключателю, а не по спрятанному нативному
            # input — так же, как это делает живой пользователь.
            clickable = page.query_selector(_ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR) or switch
            clickable.click()

        # Список диалогов перестраивается под фильтр асинхронным запросом к VK, а не мгновенной
        # DOM-фильтрацией — раньше здесь стоял фиксированный wait_for_timeout(1000+500), который
        # систематически ловил список ещё в процессе перестройки и возвращал ПУСТОЙ (но при этом
        # формально успешный, не None) набор заголовков. Из-за этого вызывающий код в inbox.py
        # ни разу не уходил в фоллбэк на полный обход и молча писал reply_status=no_reply на
        # реальные непрочитанные ответы — подтверждено логами продакшена 2026-08-22: "unread
        # dialog list matched 0/20" в каждом без исключения запуске проверки, ни одного лога
        # "worker starting" (сама переписка ни разу не открывалась). Ждём, пока список
        # действительно осядет — количество элементов должно перестать меняться.
        self._wait_for_stable_convo_list(page)

        titles: set[str] = set()
        for item in page.query_selector_all(_CONVO_ITEM_SELECTOR):
            title_el = item.query_selector(_CONVO_TITLE_SELECTOR)
            if title_el is None:
                continue
            title = (title_el.get_attribute("title") or title_el.inner_text() or "").strip()
            if title:
                titles.add(title)
        return titles

    def _wait_for_stable_convo_list(self, page: Page) -> None:
        """Ждёт, пока число элементов списка диалогов перестанет меняться между опросами.

        Минимальная стартовая пауза нужна отдельно от опроса стабильности: сразу после клика по
        фильтру список какое-то время всё ещё показывает СТАРОЕ (до переключения) содержимое —
        без неё опрос успел бы застать этот старый список "стабильным" и выйти раньше, чем VK
        вообще начал перестройку под новый фильтр.
        """
        page.wait_for_timeout(1000)

        deadline = time.monotonic() + _SLOW_RENDER_TIMEOUT_MS / 1000
        last_count = None
        stable_reads = 0
        while time.monotonic() < deadline:
            count = len(page.query_selector_all(_CONVO_ITEM_SELECTOR))
            stable_reads = stable_reads + 1 if count == last_count else 1
            last_count = count
            if stable_reads >= 3:
                return
            page.wait_for_timeout(400)

    def _check_one(self, page: Page, lead: dict) -> ReplyCheckResult:
        try:
            page.goto(lead["group_url"], wait_until="domcontentloaded")
            try:
                send_btn = page.wait_for_selector(_SEND_MESSAGE_BUTTON_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                return ReplyCheckResult(
                    has_reply=False,
                    error="кнопка «Написать сообщение» недоступна — переписку не открыть",
                )
            chat_href = send_btn.get_attribute("href")
            page.goto(urljoin(page.url, chat_href), wait_until="domcontentloaded")

            try:
                page.wait_for_selector(_MESSAGE_ITEM_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                return ReplyCheckResult(has_reply=False)  # переписки ещё нет или не успела отрисоваться

            items = page.query_selector_all(_MESSAGE_ITEM_SELECTOR)
            if not items:
                return ReplyCheckResult(has_reply=False)

            last_author_href = self._last_author_href(items)
            if last_author_href is None or not self._is_incoming(last_author_href, lead):
                return ReplyCheckResult(has_reply=False)  # последнее сообщение — наше собственное

            preview = self._last_message_text(items)
            return ReplyCheckResult(has_reply=True, preview=preview)
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

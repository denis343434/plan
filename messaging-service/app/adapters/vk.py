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

# ВНИМАНИЕ: в отличие от остальных селекторов этого файла, эти НЕ проверены вживую — прикрепление
# фото к ручному ответу (app/reply.py) добавлено без доступа к реальному залогиненному VK-аккаунту
# для проверки. Это типовая разметка VKUI-composer'а, подобранная по аналогии; если прикрепление
# не срабатывает на реальном прогоне — открыть страницу с VK_HEADLESS=false, найти актуальную
# разметку через devtools и обновить константы + этот комментарий по образцу остальных в файле.
_ATTACH_BUTTON_SELECTOR = (
    '[data-testid="attach_button"], .ConvoComposer__attachIcon, .ConvoComposer__attachButton, '
    'button[aria-label*="рикреп" i]'
)
_FILE_INPUT_SELECTOR = 'input[type="file"]'
_ATTACHMENT_PREVIEW_SELECTOR = (
    '.ComposerAttachment, .ConvoComposer__attachment, [data-testid="composer_attachment"]'
)

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
# Раньше диалоги из этого списка сопоставлялись с лидами по тексту заголовка (title у
# .ConvoTitle__author vs lead["title"], сохранённый при парсинге) — ненадёжно: заголовок
# диалога и headline страницы сообщества извлекаются в разных местах разными селекторами и
# регулярно расходятся текстом (пробелы/регистр/обрезка), из-за чего реальные новые ответы
# молча помечались "нет ответа" без единого похода в саму переписку — подтверждено логами
# 2026-08-23 ("unread dialog list matched 2/62"). Теперь по этому списку идём напрямую: кликаем
# по каждому .ConvoList__item (как обычный пользователь — открыть его иначе нельзя, прямого
# href на конкретный диалог в разметке списка нет), читаем последнее сообщение и сопоставляем
# лида по href автора (тот же надёжный признак, что и в _check_one/_is_incoming), а не по
# тексту. Пользователь подтвердил вживую: просто открыть vk.com/im, не кликая в чат, счётчики
# непрочитанных НЕ сбрасывает — но сам клик по диалогу, разумеется, сбрасывает бейдж именно у
# него, поэтому список нужно перечитывать заново после каждого клика (см. _walk_unread_dialogs).
_ONLY_UNREAD_SWITCH_SELECTOR = '.ConvoList__footer input[role="switch"]'
_ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR = ".ConvoList__footer .vkuiSwitch__host"
_CONVO_ITEM_SELECTOR = ".ConvoList__item"

# Сколько подряд опросов пустого (0 элементов) списка диалогов нужно, прежде чем поверить, что
# непрочитанных действительно нет — см. докстринг _wait_for_stable_convo_list. Заметно больше,
# чем порог в 3 для непустого списка: пустой DOM сразу после переключения фильтра — самый частый
# ложный "стабильный" результат, реальные элементы VK досылает асинхронно с задержкой.
_STABLE_EMPTY_READS_REQUIRED = 15


def _sleep_random() -> None:
    time.sleep(random.uniform(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC))


class VkSendAdapter:
    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def send_message(self, lead: dict, account: dict, text: str, image: dict | None = None) -> SendResult:
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
                if image is not None:
                    if not self._attach_image(page, image):
                        return SendResult(
                            success=False,
                            error="не удалось прикрепить изображение — не найден элемент загрузки файла "
                            "в редакторе сообщения (селекторы не подтверждены вживую, см. комментарий "
                            "у _ATTACH_BUTTON_SELECTOR)",
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

    def _attach_image(self, page: Page, image: dict) -> bool:
        """image — Playwright FilePayload: {"name", "mimeType", "buffer"}. Возвращает False, если
        не нашли ни готовый <input type="file">, ни кнопку прикрепления — см. предупреждение у
        _ATTACH_BUTTON_SELECTOR, это не проверенный вживую путь."""
        file_input = page.query_selector(_FILE_INPUT_SELECTOR)
        if file_input is not None:
            file_input.set_input_files(image)
            self._wait_for_attachment_preview(page)
            return True

        attach_btn = page.query_selector(_ATTACH_BUTTON_SELECTOR)
        if attach_btn is None:
            return False
        try:
            with page.expect_file_chooser(timeout=_SLOW_RENDER_TIMEOUT_MS) as chooser_info:
                attach_btn.click()
            chooser_info.value.set_files(image)
        except PlaywrightTimeoutError:
            return False

        self._wait_for_attachment_preview(page)
        return True

    def _wait_for_attachment_preview(self, page: Page) -> None:
        try:
            page.wait_for_selector(_ATTACHMENT_PREVIEW_SELECTOR, timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        # Сам предпросмотр может дорисоваться позже контейнера, а точный признак "загрузка
        # завершена" не подтверждён вживую — доп. пауза как подстраховка (см. предупреждение
        # у _ATTACH_BUTTON_SELECTOR).
        time.sleep(2.0)


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
            leads_by_external_id: dict[str, dict] = {}
            unidentifiable_ids: set[str] = set()
            for lead in leads:
                external_id = (lead.get("external_id") or "").strip("/")
                if external_id:
                    leads_by_external_id[external_id] = lead
                else:
                    # Без external_id сопоставить диалог с лидом нечем (см. _is_incoming) — не
                    # гадаем, что "нет ответа", проверяем такого лида напрямую в полном обходе
                    # ниже, как и раньше делали для лидов без title.
                    unidentifiable_ids.add(lead["id"])

            unread_ok = self._walk_unread_dialogs(leads_by_external_id, results, report_progress)
            if not unread_ok:
                logger.info(
                    "inbox check: could not read VK unread dialog list, falling back to full scan of %d lead(s)",
                    total,
                )
                leads_to_check = leads
            else:
                # Список непрочитанных диалогов уже пройден целиком (см. _walk_unread_dialogs) —
                # всё, что там реально нашлось и совпало с лидом, уже в results. Остальным
                # опознаваемым pending-лидам открывать нечего: раз их диалога не было среди
                # непрочитанных, нового входящего у них нет.
                for lead in leads:
                    if lead["id"] not in unidentifiable_ids and lead["id"] not in results:
                        results[lead["id"]] = ReplyCheckResult(has_reply=False)
                        report_progress()
                logger.info(
                    "inbox check: unread dialog walk matched %d/%d lead(s), %d without external_id go to full scan",
                    sum(1 for r in results.values() if r.has_reply),
                    total,
                    len(unidentifiable_ids),
                )
                leads_to_check = [lead for lead in leads if lead["id"] in unidentifiable_ids]

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

    def _walk_unread_dialogs(
        self,
        leads_by_external_id: dict[str, dict],
        results: dict[str, ReplyCheckResult],
        report_progress: Callable[[], None],
    ) -> bool:
        """Открывает vk.com/im с включённым тумблером "Только непрочитанные" и последовательно
        кликает по каждому диалогу, который VK там реально показывает — читает последнее
        сообщение и сопоставляет автора с лидом по href (см. _is_incoming), а не по тексту
        заголовка (см. историю выше про title-матчинг, ненадёжно). Пишет результат в `results`
        сразу для всех совпавших лидов — отдельная короткая Playwright-сессия, до воркеров
        полного обхода.

        Возвращает False, если список диалогов вообще не удалось прочитать (страница/тумблер не
        отрисовались) — тогда вызывающий код обязан пойти в полный обход, а не гадать.
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.VK_HEADLESS)
            context = browser.new_context(storage_state=self._storage_state or None)
            try:
                page = context.new_page()
                try:
                    page.goto("https://vk.com/im", wait_until="domcontentloaded")
                    page.wait_for_selector(_ONLY_UNREAD_SWITCH_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
                except Exception:
                    return False  # страница/список диалогов вообще не отрисовались — не гадаем

                switch = page.query_selector(_ONLY_UNREAD_SWITCH_SELECTOR)
                if switch is not None and switch.get_attribute("aria-checked") != "true":
                    # Кликаем по видимому визуальному переключателю, а не по спрятанному
                    # нативному input — так же, как это делает живой пользователь.
                    clickable = page.query_selector(_ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR) or switch
                    clickable.click()

                # Список диалогов перестраивается под фильтр асинхронным запросом к VK, а не
                # мгновенной DOM-фильтрацией — раньше здесь стоял фиксированный
                # wait_for_timeout(1000+500), который систематически ловил список ещё в процессе
                # перестройки и возвращал пустой набор. Ждём, пока список действительно осядет.
                self._wait_for_stable_convo_list(page)

                seen_keys: set[str] = set()
                # Верхняя граница на случай неожиданной разметки/зацикливания — реальных
                # непрочитанных диалогов у одного аккаунта рассылки столько не бывает.
                for _ in range(200):
                    item = self._next_unseen_convo_item(page, seen_keys)
                    if item is None:
                        break
                    external_id, result = self._check_one_unread_dialog(page, item)
                    lead = leads_by_external_id.get(external_id) if external_id else None
                    if lead is not None and lead["id"] not in results:
                        results[lead["id"]] = result
                        report_progress()
                    _sleep_random()
                    # Открытый диалог обычно пропадает из отфильтрованного списка (стал
                    # прочитанным) — ждём, пока список снова осядет, прежде чем брать следующий.
                    self._wait_for_stable_convo_list(page)
                return True
            finally:
                context.close()
                browser.close()

    def _next_unseen_convo_item(self, page: Page, seen_keys: set[str]) -> ElementHandle | None:
        for item in page.query_selector_all(_CONVO_ITEM_SELECTOR):
            key = item.get_attribute("data-itemkey") or ""
            if key and key not in seen_keys:
                seen_keys.add(key)
                return item
        return None

    def _check_one_unread_dialog(self, page: Page, item: ElementHandle) -> tuple[str | None, ReplyCheckResult]:
        """Открывает диалог кликом по элементу списка (прямого href на конкретный диалог в
        разметке списка нет — открыть иначе, чем кликом, нельзя) и читает последнее сообщение.
        Возвращает (href автора без слэшей или None, результат) — сопоставление с лидом делает
        вызывающий код (_walk_unread_dialogs), у него есть полный leads_by_external_id."""
        try:
            item.click()
            try:
                page.wait_for_selector(_MESSAGE_ITEM_SELECTOR, timeout=_SLOW_RENDER_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                return None, ReplyCheckResult(
                    has_reply=False, error="диалог открылся, но сообщения не прогрузились за 40с"
                )

            items = page.query_selector_all(_MESSAGE_ITEM_SELECTOR)
            if not items:
                return None, ReplyCheckResult(has_reply=False)

            last_author_href = self._last_author_href(items)
            if last_author_href is None:
                return None, ReplyCheckResult(has_reply=False)

            # Диалог в отфильтрованном "только непрочитанные" списке — значит есть непрочитанное
            # входящее по определению VK (собственные отправленные сообщения бейдж не ставят).
            # Если href автора последнего сообщения при этом не совпадёт ни с одним нашим
            # external_id (см. _walk_unread_dialogs) — это либо чужая переписка не из наших
            # лидов, либо VK не успел досчитать бейдж досюда; сопоставление отфильтрует такое
            # само, без доп. проверки "наше/чужое" здесь.
            preview = self._last_message_text(items)
            return last_author_href.strip("/"), ReplyCheckResult(has_reply=True, preview=preview)
        except Exception as exc:  # unexpected Playwright/DOM failure — не роняем весь обход
            logger.exception("inbox check: failed to read an unread dialog from vk.com/im list")
            return None, ReplyCheckResult(has_reply=False, error=str(exc))

    def _wait_for_stable_convo_list(self, page: Page) -> None:
        """Ждёт, пока число элементов списка диалогов перестанет меняться между опросами.

        Минимальная стартовая пауза нужна отдельно от опроса стабильности: сразу после клика по
        фильтру/диалогу список какое-то время всё ещё показывает СТАРОЕ содержимое — без неё
        опрос успел бы застать этот старый список "стабильным" и выйти раньше, чем VK вообще
        начал перестройку.

        0 — самый опасный "стабильный" результат: VK чистит DOM под новый фильтр МГНОВЕННО, а
        сами непрочитанные диалоги подгружает отдельным асинхронным запросом с ощутимой
        задержкой — три подряд опроса пустого списка (~1.2с) относительно легко укладываются в
        это окно "уже пусто, но реальные элементы ещё не долетели". Подтверждено вживую
        2026-08-23: `_walk_unread_dialogs` отработал за ~2с и решил, что непрочитанных нет,
        хотя у VK в этот момент реально висело 3 непрочитанных диалога (см. историю выше про
        title-матчинг — багфикс того же класса, что и тогда, просто по 0-элементам, а не по
        тексту заголовка). Для count=0 требуем кратно больше стабильных чтений, прежде чем
        поверить, что список действительно пуст, а не просто ещё не начал заполняться.
        """
        page.wait_for_timeout(1000)

        deadline = time.monotonic() + _SLOW_RENDER_TIMEOUT_MS / 1000
        last_count = None
        stable_reads = 0
        while time.monotonic() < deadline:
            count = len(page.query_selector_all(_CONVO_ITEM_SELECTOR))
            stable_reads = stable_reads + 1 if count == last_count else 1
            last_count = count
            required = 3 if count else _STABLE_EMPTY_READS_REQUIRED
            if stable_reads >= required:
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

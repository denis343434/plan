import logging
import threading

logger = logging.getLogger(__name__)

# Playwright Sync API допускает только ОДИН активный sync_playwright() на поток — второй
# __enter__, пока первый не закрыт, падает с "It looks like you are using Playwright Sync API
# inside the asyncio loop" (поймано вживую 2026-09-28: пул держал открытым браузер первого
# аккаунта, и на следующем лиде с другим аккаунтом вся рассылка падала). Поэтому драйвер общий
# на поток (со счётчиком ссылок), а вот Browser у каждого аккаунта свой — из этого драйвера.
_local = threading.local()


def _acquire_playwright(factory):
    """factory — sync_playwright из модуля адаптера (так его подменяют тесты)."""
    state = getattr(_local, "playwright", None)
    if state is None:
        pw_cm = factory()
        pw = pw_cm.__enter__()
        state = _local.playwright = {"cm": pw_cm, "pw": pw, "refs": 0}
    state["refs"] += 1
    return state["pw"]


def _release_playwright() -> None:
    state = getattr(_local, "playwright", None)
    if state is None:
        return
    state["refs"] -= 1
    if state["refs"] <= 0:
        _local.playwright = None
        try:
            state["cm"].__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - закрытие не должно ронять вызывающий код
            logger.debug("failed to stop playwright", exc_info=True)


class PersistentBrowserMixin:
    """Один Browser/BrowserContext на всё время работы адаптера вместо запуска Chromium с нуля на
    каждое сообщение (раньше send_message каждый раз делал sync_playwright() → launch() → close(),
    а это ~1-3с чистого оверхеда на КАЖДЫЙ лид).

    Использование: `with adapter:` (или явные open()/close()) — внутри send_message открывает
    новую Page в уже запущенном контексте и закрывает только её, тот же паттерн, что и в
    VkInboxAdapter._walk_unread_dialogs. Без open() адаптер работает как раньше — одноразовый
    браузер на один вызов (reply.py, тесты).

    ВАЖНО про потоки: Playwright sync API привязан к OS-потоку, в котором был создан
    sync_playwright() (см. докстринг VkInboxAdapter) — open(), send_message() и close() обязаны
    вызываться из одного и того же потока. Это так для run_send_task/run_followup_task: каждая
    задача целиком выполняется в своём потоке BackgroundTasks.

    Модуль специально не импортирует playwright (см. registry.py — в fake-режиме он не нужен):
    драйвер создаёт _playwright_factory(), браузер — _launch() конкретного адаптера.
    """

    _browser = None
    _context = None

    def _playwright_factory(self):
        """Возвращает sync_playwright() — контекст-менеджер драйвера."""
        raise NotImplementedError

    def _launch(self, pw):
        """Возвращает (Browser, BrowserContext), запущенные из драйвера pw."""
        raise NotImplementedError

    def _start(self):
        pw = _acquire_playwright(self._playwright_factory)
        try:
            return self._launch(pw)
        except BaseException:
            _release_playwright()
            raise

    def open(self):
        if self._context is None:
            self._browser, self._context = self._start()
        return self

    def storage_state(self) -> dict | None:
        """Актуальные cookies/storage текущего контекста — чтобы сохранить в Data Service
        обновлённую сессию (например после ручного прохождения антибот-проверки VK)."""
        if self._context is None:
            return None
        try:
            return self._context.storage_state()
        except Exception:  # noqa: BLE001 - закрытый/упавший браузер
            return None

    def close(self) -> None:
        if self._context is None:
            return
        browser, context = self._browser, self._context
        self._browser = self._context = None
        _close_quietly(browser, context)

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _with_page(self, fn):
        """Вызывает fn(page) на свежей Page — в постоянном браузере, если он открыт, иначе в
        одноразовом (старое поведение)."""
        if self._context is None:
            browser, context = self._start()
            try:
                return fn(context.new_page())
            finally:
                _close_quietly(browser, context)

        # Браузер мог упасть/быть закрыт между лидами (OOM, пользователь закрыл headed-окно) —
        # без переоткрытия все оставшиеся лиды этого аккаунта молча провалились бы с
        # TargetClosedError.
        if not self._browser.is_connected():
            logger.warning("persistent browser disconnected, relaunching")
            self.close()
            self.open()
        page = None
        try:
            page = self._context.new_page()
        except Exception:
            # Браузер мог умереть между is_connected() и new_page() (закрыли окно, краш
            # процесса) — перезапускаем постоянный браузер и пробуем ещё раз, вместо того
            # чтобы уронить всю рассылку на этом лиде.
            logger.warning("failed to open page in persistent browser, relaunching", exc_info=True)
            self.close()
            self.open()
            page = self._context.new_page()
        try:
            return fn(page)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001 - страница могла уже закрыться вместе с браузером
                logger.debug("failed to close page", exc_info=True)


def _close_quietly(browser, context) -> None:
    """Закрывает контекст и браузер и отпускает ссылку на общий драйвер потока."""
    for closer in (context.close, browser.close):
        try:
            closer()
        except Exception:  # noqa: BLE001 - закрытие не должно ронять вызывающий код
            logger.debug("failed to close playwright resource", exc_info=True)
    _release_playwright()

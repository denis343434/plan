"""Отправка WhatsApp через установленное приложение WhatsApp Desktop (Windows Store).

Зачем: WhatsApp Web периодически разлогинивается («требует повторного входа»), и рассылка
встаёт. Настольное приложение уже авторизовано на компьютере и остаётся открытым между лидами.

Как работает:
  1. Открываем чат по номеру через deep link whatsapp://send?phone=<digits> (без текста —
     чтобы не смешиваться с черновиками и не упираться в лимит длины URL).
  2. Находим окно WhatsApp Desktop через UI Automation (pywinauto), разворачиваем/фокусируем.
     Найденный handle кэшируется на все следующие лиды — приложение между сообщениями не
     перезапускается, а полный перебор окон Desktop(...).windows() на каждое сообщение стоил
     заметное время.
  3. Ждём не фиксированные N секунд, а появления нужного чата: раз в _UI_POLL_INTERVAL_SEC
     читаем именованные узлы окна (без координат — они запрашиваются лениво только у пары
     кандидатов). Как только в шапке появился номер/название лида — идём дальше; если WhatsApp
     показал «номер недействителен» — сразу выходим, не выжидая остаток таймаута (для лидов из
     Яндекс.Карт это частая ситуация).
  4. Кликаем поле ввода, очищаем его, печатаем текст (кириллица через эмуляцию клавиш) и жмём
     Enter.
  5. Приложение НЕ закрываем — следующее сообщение откроется в нём же.

Это UI-автоматизация чужого приложения: она зависит от версии/размера окна WhatsApp Desktop.
Координаты поля ввода вынесены в настройки (WA_DESKTOP_COMPOSE_*), их можно подправить.
Работает только при локальном запуске messaging-service на Windows — из Docker до окна
приложения не достучаться.
"""

import logging
import os
import re
import time
from dataclasses import dataclass

from app.adapters.base import SendResult
from app.config import settings

logger = logging.getLogger(__name__)

_WINDOW_TITLE = "WhatsApp"
_WINDOW_CLASS = "WinUIDesktopWin32WindowClass"
_CONTACT_INFO_PREFIXES = ("данные контакта", "contact info", "информация о контакте")
_PANEL_TITLES = ("данные контакта", "contact info", "информация о контакте")
_LOGIN_MARKERS = (
    "use whatsapp on your computer",
    "чтобы использовать whatsapp на компьютере",
    "войдите по номеру телефона",
    "отсканируйте qr",
    "scan the qr code",
    "link with phone number",
)
_INVALID_NUMBER_MARKERS = (
    "phone number shared via url is invalid",
    "phone number shared via url is not valid",
    "недействительный номер",
    "номер не зарегистрирован",
    "not on whatsapp",
    "нет в whatsapp",
    "invalid phone number",
)

# Шапка чата и правая карточка контакта — доли/пиксели от прямоугольника окна (подобрано на
# окне WhatsApp Desktop 1408x852, см. WA_DESKTOP_COMPOSE_* в config.py).
_HEADER_LEFT_RATIO = 0.30
_HEADER_MAX_TOP_OFFSET = 220
_PANEL_LEFT_RATIO = 0.60

# Пауза перед первым чтением UI (deep link ещё переключает чат) и период опроса, пока чат не
# откроется. Итоговый потолок — WA_DESKTOP_CHAT_DELAY_SEC.
_UI_INITIAL_DELAY_SEC = 0.4
_UI_POLL_INTERVAL_SEC = 0.5


@dataclass
class _UiItem:
    """Один именованный узел UIA. rectangle() запрашивается лениво: полный обход с чтением
    координат КАЖДОГО узла был самой дорогой частью отправки, а координаты нужны только
    единицам кандидатов (шапка чата, заголовок карточки контакта)."""

    text: str
    element: object
    _rect: object = None
    _rect_ready: bool = False

    def rect(self):
        if not self._rect_ready:
            self._rect_ready = True
            try:
                self._rect = self.element.rectangle()
            except Exception:  # noqa: BLE001 - узел мог исчезнуть между чтением и запросом
                self._rect = None
        return self._rect


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _normalize(value: str | None) -> str:
    """Для сравнения названий: регистр, ё/е, лишние пробелы."""
    text = (value or "").lower().replace("ё", "е")
    return " ".join(text.split())


class WhatsAppDesktopAdapter:
    def __init__(self, storage_state: dict | None = None) -> None:
        # storage_state не нужен: авторизация живёт в самом приложении WhatsApp Desktop.
        self._storage_state = storage_state or {}
        self._window = None

    def send_message(
        self, lead: dict, account: dict, text: str, image: dict | None = None
    ) -> SendResult:
        if os.name != "nt":
            return SendResult(
                success=False,
                error="WhatsApp Desktop доступен только при локальном запуске на Windows",
                network_error=True,
            )
        if image is not None:
            return SendResult(
                success=False,
                error="WhatsApp Desktop: отправка изображений через приложение не поддерживается",
            )
        number = _digits(str(lead.get("external_id") or ""))
        if not number:
            return SendResult(success=False, error="WhatsApp Desktop: у лида нет номера телефона")

        started = time.monotonic()
        try:
            logger.info("whatsapp desktop: opening chat for %s", number)
            os.startfile(f"whatsapp://send?phone={number}")
            window = self._get_window()
            items, window_rect, state = self._wait_for_chat(window, number, lead.get("title"))
            if state == "login":
                return SendResult(
                    success=False,
                    error="WhatsApp Desktop просит войти — откройте приложение и войдите в аккаунт",
                    session_expired=True,
                )
            if state == "invalid":
                return SendResult(
                    success=False,
                    error="WhatsApp Desktop: номер не найден в WhatsApp",
                    unreachable=True,
                )
            header = self._chat_header(items, window_rect)
            if not self._header_matches(header, number, lead.get("title")):
                if header is None or not self._verify_via_contact_card(window, header, number):
                    return SendResult(
                        success=False,
                        error=(
                            "WhatsApp Desktop: не удалось подтвердить, что открыт нужный чат "
                            f"(номер {number}) — сообщение не отправлено"
                        ),
                        network_error=True,
                    )
            self._type_and_send(window, text)
            logger.info("whatsapp desktop: sent to %s in %.1fs", number, time.monotonic() - started)
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001 - сбой UI-автоматизации не должен ронять всю задачу
            logger.exception("whatsapp desktop: send failed for %s", number)
            return SendResult(success=False, error=f"WhatsApp Desktop: {exc}", network_error=True)

    def close(self) -> None:
        """Ничего не закрываем: приложение должно остаться открытым для следующих лидов."""
        return None

    # ---------- окно и состояние ----------
    def _get_window(self):
        """Кэшированный handle окна: между лидами приложение то же самое. Если окно закрыли/
        перезапустили — обёртка pywinauto бросает исключение на обращении, ищем заново."""
        if self._window is not None:
            try:
                self._window.window_text()
                self._focus_window(self._window)
                return self._window
            except Exception:  # noqa: BLE001 - окно закрыто/перезапущено
                self._window = None
        self._window = self._wait_for_window()
        return self._window

    @staticmethod
    def _focus_window(window) -> None:
        try:
            if window.is_minimized():
                window.restore()
            window.set_focus()
            time.sleep(0.1)
        except Exception:  # noqa: BLE001 - фокус не критичен для чтения, но нужен для клика
            logger.debug("whatsapp desktop: failed to focus window", exc_info=True)

    @staticmethod
    def _wait_for_window():
        from pywinauto import Desktop

        deadline = time.monotonic() + settings.WA_DESKTOP_WINDOW_TIMEOUT_SEC
        while time.monotonic() < deadline:
            for window in Desktop(backend="uia").windows():
                try:
                    if window.window_text() == _WINDOW_TITLE and window.class_name() == _WINDOW_CLASS:
                        if window.is_minimized():
                            window.restore()
                        window.set_focus()
                        time.sleep(0.3)
                        return window
                except Exception:  # noqa: BLE001 - окно могло закрыться между перечислением и проверкой
                    continue
            time.sleep(1)
        raise RuntimeError("окно WhatsApp Desktop не найдено (приложение не запущено?)")

    @staticmethod
    def _read_ui(window) -> list[_UiItem]:
        """Один проход по дереву вместо прежних 3-4 обходов на сообщение.

        Координаты здесь не читаются (это и было самой дорогой частью): _UiItem.rect() запросит
        их лениво только у единичных кандидатов — шапки чата и заголовка карточки контакта.
        Обходим все типы узлов, а не только Text/Button, чтобы не потерять маркеры экрана входа
        и «номер недействителен», которые провайдер UIA может отдать как Pane/Edit.
        """
        try:
            descendants = window.descendants()
        except Exception:  # noqa: BLE001 - страница посреди перерисовки
            return []
        items: list[_UiItem] = []
        for item in descendants:
            try:
                text = (item.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                items.append(_UiItem(text=text, element=item))
        return items

    @staticmethod
    def _window_rect(window):
        try:
            return window.rectangle()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _detect_state(items: list[_UiItem]) -> str | None:
        texts = [item.text.lower() for item in items]
        if any(marker in text for text in texts for marker in _LOGIN_MARKERS):
            return "login"
        if any(marker in text for text in texts for marker in _INVALID_NUMBER_MARKERS):
            return "invalid"
        return None

    def _wait_for_chat(self, window, number: str, title: str | None):
        """Ждёт нужный чат, но не дольше WA_DESKTOP_CHAT_DELAY_SEC. Возвращает последний
        снимок UI, прямоугольник окна и состояние login/invalid (или None)."""
        deadline = time.monotonic() + max(0.1, settings.WA_DESKTOP_CHAT_DELAY_SEC)
        time.sleep(_UI_INITIAL_DELAY_SEC)
        items: list[_UiItem] = []
        window_rect = self._window_rect(window)
        while True:
            items = self._read_ui(window)
            rect = self._window_rect(window)
            if rect is not None:
                window_rect = rect
            state = self._detect_state(items)
            if state is not None:
                return items, window_rect, state

            if window_rect is not None and self._panel_open(items, window_rect):
                self._press_escape()
                time.sleep(0.3)
                items = self._read_ui(window)
                rect = self._window_rect(window)
                if rect is not None:
                    window_rect = rect
                state = self._detect_state(items)
                if state is not None:
                    return items, window_rect, state

            if self._header_matches(self._chat_header(items, window_rect), number, title):
                return items, window_rect, None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return items, window_rect, None
            time.sleep(min(_UI_POLL_INTERVAL_SEC, remaining))

    def _chat_header(self, items: list[_UiItem], window_rect):
        """Узел шапки открытого чата («Данные контакта +7 …» / «… Лёха»)."""
        if window_rect is None:
            return None
        left = window_rect.left + window_rect.width() * _HEADER_LEFT_RATIO
        header_bottom = window_rect.top + _HEADER_MAX_TOP_OFFSET
        for item in items:
            if not self._is_header_name(item.text):
                continue
            item_rect = item.rect()
            if item_rect is None:
                continue
            if item_rect.left >= left and item_rect.top <= header_bottom:
                return item
        return None

    @staticmethod
    def _header_matches(header: _UiItem | None, number: str, title: str | None) -> bool:
        if header is None:
            return False
        if number and number in _digits(header.text):
            return True
        return bool(title and _normalize(title) in _normalize(header.text))

    def _verify_via_contact_card(self, window, header: _UiItem, number: str) -> bool:
        """Сохранённый контакт может называться иначе — проверяем телефон в карточке контакта."""
        try:
            header.element.click_input()
        except Exception:  # noqa: BLE001
            logger.debug("whatsapp desktop: contact info verification failed", exc_info=True)
            return False
        time.sleep(0.8)
        try:
            found = any(number in _digits(item.text) for item in self._read_ui(window))
        except Exception:  # noqa: BLE001
            found = False
        self._press_escape()
        time.sleep(0.3)
        if not found:
            logger.warning(
                "whatsapp desktop: chat verification failed (header=%r, number=%s)",
                header.text,
                number,
            )
        return found

    @staticmethod
    def _is_header_name(name: str) -> bool:
        lowered = _normalize(name)
        return any(lowered.startswith(prefix) for prefix in _CONTACT_INFO_PREFIXES)

    def _panel_open(self, items: list[_UiItem], window_rect) -> bool:
        """Открыта ли правая карточка контакта. Жать Escape можно ТОЛЬКО в этом случае: в обычном
        чате Escape возвращает к списку чатов (проверено вживую)."""
        left = window_rect.left + window_rect.width() * _PANEL_LEFT_RATIO
        for item in items:
            if _normalize(item.text) not in _PANEL_TITLES:
                continue
            item_rect = item.rect()
            if item_rect is not None and item_rect.left >= left:
                return True
        return False

    @staticmethod
    def _press_escape() -> None:
        from pywinauto.keyboard import send_keys

        try:
            send_keys("{ESC}")
        except Exception:  # noqa: BLE001
            logger.debug("whatsapp desktop: failed to press Escape", exc_info=True)

    # ---------- ввод и отправка ----------
    @staticmethod
    def _escape_for_send_keys(text: str) -> str:
        """Экранирует символы, которые pywinauto считает управляющими.

        +^%~() — модификаторы/группировка, {} — служебные скобки. Переводы строк заменяем
        пробелами: Enter в WhatsApp отправляет сообщение, а Shift+Enter зависит от версии.
        """
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ").replace("\t", " ")
        return "".join(("{" + ch + "}") if ch in "+^%~(){}" else ch for ch in text)

    def _type_and_send(self, window, text: str) -> None:
        from pywinauto.keyboard import send_keys

        window.set_focus()
        time.sleep(0.2)
        rect = window.rectangle()
        x = int(rect.width() * settings.WA_DESKTOP_COMPOSE_X_RATIO)
        y = max(10, rect.height() - settings.WA_DESKTOP_COMPOSE_BOTTOM_OFFSET)
        window.click_input(coords=(x, y))
        time.sleep(0.3)

        # Чистим возможный черновик. Ctrl+A в webview иногда выделяет всю переписку, а не поле,
        # поэтому идём по самому полю: Home → Shift+End → Delete.
        send_keys("{HOME}")
        send_keys("+{END}")
        send_keys("{DELETE}")
        time.sleep(0.2)

        # Печатаем юникод напрямую: pywinauto шлёт Unicode-символы (проверено вживую), а вставка
        # через буфер обмена в WhatsApp Desktop периодически не срабатывает.
        send_keys(
            self._escape_for_send_keys(text),
            with_spaces=True,
            pause=settings.WA_DESKTOP_TYPING_PAUSE_SEC,
        )
        time.sleep(0.3)
        send_keys("{ENTER}")
        time.sleep(settings.WA_DESKTOP_SEND_SETTLE_SEC)

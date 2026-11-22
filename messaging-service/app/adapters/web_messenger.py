"""Отправка через веб-версии мессенджеров (Telegram Web, WhatsApp Web, Facebook Messenger) —
лиды из Яндекс/Google Карт.

Как и VK/Instagram — Playwright с сохранённой сессией аккаунта (storage_state из
parser-service/scripts/{tg,whatsapp}_manual_login.py). WhatsApp Web хранит авторизацию в
IndexedDB, поэтому скрипты логина сохраняют storage_state(indexed_db=True) — Playwright
восстанавливает его в new_context(storage_state=...) сам.

Общий сценарий: открыть чат по прямой ссылке → дождаться поля ввода → набрать текст (переносы
строк — Shift+Enter, обычный Enter в обоих мессенджерах отправляет) → Enter.

НЕ проверено вживую на реальных аккаунтах (селекторы — по актуальной разметке web.telegram.org/k
и web.whatsapp.com на момент написания). При первом прогоне переключить Playwright в режим
«На экране» и при расхождении обновить селекторы у подклассов.
"""

import logging
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app import ratelimit
from app.adapters.base import SendResult, SessionExpiredError
from app.adapters.browser_session import PersistentBrowserMixin
from app.adapters.proxy import parse_proxy_url
from app.config import settings

logger = logging.getLogger(__name__)

# Оба веб-клиента отказываются работать с "HeadlessChrome" в user-agent (WhatsApp показывает
# заглушку "обновите браузер") — подменяем на обычный десктопный Chrome.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_BLOCKED_RESOURCE_TYPES = {"media", "font"}


class _PageLoadError(Exception):
    """page.goto не загрузил страницу — транзитный сбой сети, не проблема лида (как в vk.py)."""


class WebMessengerSendAdapter(PersistentBrowserMixin):
    name = ""
    # Поле ввода сообщения в открытом чате.
    input_selector = ""
    # Через сколько сдаваться, если чат не открылся (веб-клиенты грузятся долго: синхронизация).
    open_timeout_ms = 60_000
    # Признаки того, что сессия протухла и показан экран входа (QR/номер телефона).
    login_markers: tuple[str, ...] = ()
    login_selector = ""
    # Признаки "такого пользователя/номера нет" — ошибка лида, не аккаунта.
    not_found_markers: tuple[str, ...] = ()
    # Чат открылся, но писать в него нельзя (канал/группа: вместо поля ввода — кнопка
    # «Вступить»/«Выключить звук»). Само поле при этом может оставаться в DOM скрытым.
    cant_write_selector = ""
    cant_write_markers: tuple[str, ...] = ()
    flood_markers: tuple[str, ...] = ()
    relogin_hint = ""

    def __init__(self, storage_state: dict) -> None:
        self._storage_state = storage_state

    def chat_url(self, lead: dict) -> str | None:
        raise NotImplementedError

    def proxy(self) -> dict | None:
        """proxy-аргумент chromium.launch; None — напрямую."""
        return None

    def _playwright_factory(self):
        return sync_playwright()

    def _launch(self, pw):
        launch_kwargs = {"headless": settings.VK_HEADLESS, "args": ["--disable-blink-features=AutomationControlled"]}
        proxy = self.proxy()
        if proxy is not None:
            logger.info("launching %s browser via proxy %s", self.name, proxy["server"])
            launch_kwargs["proxy"] = proxy
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                storage_state=self._storage_state or None,
                user_agent=_USER_AGENT,
                locale="ru-RU",
                viewport={"width": 1280, "height": 860},
            )

            def handler(route) -> None:
                if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", handler)
        except BaseException:
            browser.close()
            raise
        return browser, context

    def send_message(self, lead: dict, account: dict, text: str, image: dict | None = None) -> SendResult:
        return self._with_page(lambda page: self._send_on_page(page, lead, text, image))

    def _send_on_page(self, page: Page, lead: dict, text: str, image: dict | None) -> SendResult:
        url = self.chat_url(lead)
        if url is None:
            return SendResult(success=False, error=f"{self.name}: не удалось построить ссылку на чат из {lead.get('group_url')}")
        try:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                raise _PageLoadError(str(exc)) from exc

            if not self._wait_for_chat(page):
                self._raise_if_login_required(page)
                if self._cant_write(page):
                    return SendResult(
                        success=False,
                        error=f"{self.name}: нельзя написать — это канал или группа без права отправки сообщений",
                    )
                not_found = self._find_marker(page, self.not_found_markers)
                if not_found:
                    return SendResult(success=False, error=f"{self.name}: {not_found}")
                return SendResult(
                    success=False,
                    error=f"{self.name}: чат не открылся — поле ввода не появилось "
                    f"(нельзя написать этому контакту или клиент долго грузится)",
                )

            if image is not None:
                logger.warning("%s: image attachment not implemented, sending text only (lead %s)", self.name, lead.get("id"))

            page.click(f"{self.input_selector} >> visible=true", timeout=5000)
            ratelimit.settle()
            self._type_multiline(page, text)
            page.keyboard.press("Enter")
            ratelimit.settle()

            flood = self._find_marker(page, self.flood_markers)
            if flood:
                return SendResult(success=False, error=f"{self.name}: {flood}", flood_detected=True)
            return SendResult(success=True)
        except _PageLoadError as exc:
            logger.warning("%s: failed to load page for lead %s, will retry: %s", self.name, lead.get("id"), exc)
            return SendResult(success=False, error=str(exc), network_error=True)
        except SessionExpiredError as exc:
            return SendResult(success=False, error=str(exc), session_expired=True)
        except Exception as exc:  # unexpected Playwright/DOM failure — reported as a delivery failure
            logger.exception("%s send failed for lead %s", self.name, lead.get("id"))
            return SendResult(success=False, error=str(exc))

    def _wait_for_chat(self, page: Page) -> bool:
        """Ждёт поле ввода, но выходит раньше, если уже видно экран входа/ошибку "не найден" —
        иначе каждый такой лид простаивал бы полный open_timeout_ms."""
        step_ms = 1000
        for _ in range(self.open_timeout_ms // step_ms):
            # Только видимое поле: у каналов/групп оно бывает в DOM, но скрыто.
            if page.locator(f"{self.input_selector} >> visible=true").count() > 0:
                return True
            if self._login_shown(page) or self._cant_write(page) or self._find_marker(page, self.not_found_markers):
                return False
            page.wait_for_timeout(step_ms)
        return False

    def _cant_write(self, page: Page) -> bool:
        if self.cant_write_selector:
            try:
                if page.locator(f"{self.cant_write_selector} >> visible=true").count() > 0:
                    return True
            except Exception:  # noqa: BLE001 - страница посреди навигации
                return False
        return bool(self._find_marker(page, self.cant_write_markers))

    def _login_shown(self, page: Page) -> bool:
        if self.login_selector and page.locator(self.login_selector).count() > 0:
            return True
        return bool(self._find_marker(page, self.login_markers))

    def _raise_if_login_required(self, page: Page) -> None:
        if self._login_shown(page):
            raise SessionExpiredError(f"{self.name} требует повторного входа — {self.relogin_hint}")

    @staticmethod
    def _find_marker(page: Page, markers: tuple[str, ...]) -> str | None:
        if not markers:
            return None
        try:
            content = (page.inner_text("body", timeout=2000) or "").lower()
        except Exception:  # noqa: BLE001 - страница посреди навигации
            return None
        return next((m for m in markers if m in content), None)

    def _type_multiline(self, page: Page, text: str) -> None:
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            for chunk in ratelimit.type_chunks(line):
                page.keyboard.type(chunk, delay=ratelimit.typing_delay_ms())


class TelegramSendAdapter(WebMessengerSendAdapter):
    name = "Telegram"
    input_selector = 'div.input-message-input[contenteditable="true"]'
    login_selector = ".page-sign, .page-signQR"
    login_markers = ("log in to telegram", "вход в telegram", "войти по номеру телефона")
    not_found_markers = (
        "username not found", "пользователь не найден", "no user has", "нет пользователя",
        "this username is not occupied",
        # Чат по номеру (tgaddr=resolve?phone=…), номера нет в Telegram или он скрыт
        # приватностью — всплывающее уведомление (проверено вживую 2026-10-04).
        "user doesn't seem to exist", "пользователя не существует", "пользователь не существует",
    )
    # Нижняя панель вместо поля ввода: «Join/Вступить», «Mute/Выключить звук», «Request to join» и т.п.
    cant_write_selector = ".chat-input-control-button, .chat-input-control .btn-primary"
    flood_markers = ("too many requests", "flood_wait", "слишком много попыток", "too many attempts")
    relogin_hint = "нужно перелогиниться (кнопка «Войти в Telegram» в панели)"

    def proxy(self) -> dict | None:
        # Telegram в РФ без прокси открывается нестабильно — свой TG_PROXY, независимо от VK_PROXY.
        return parse_proxy_url(settings.TG_PROXY, "TG_PROXY")

    def chat_url(self, lead: dict) -> str | None:
        # external_id: "username" или "+79991234567" (см. parser-service/app/adapters/contacts.py).
        ext = (lead.get("external_id") or "").strip()
        if not ext:
            return None
        if ext.startswith("+"):
            tgaddr = quote(f"tg://resolve?phone={ext[1:]}", safe="")
            return f"https://web.telegram.org/k/#?tgaddr={tgaddr}"
        return f"https://web.telegram.org/k/#@{ext}"


class FacebookSendAdapter(WebMessengerSendAdapter):
    """Messenger внутри facebook.com: чат со страницей организации по её нику/id
    (external_id из parser-service/app/adapters/contacts.py::facebook_lead)."""

    name = "Facebook"
    input_selector = 'div[role="main"] div[role="textbox"][contenteditable="true"]'
    login_selector = 'form[action*="login"] input[name="email"], input#email[name="email"]'
    login_markers = ("log in to facebook", "вход на facebook", "войдите на facebook", "забыли пароль?")
    not_found_markers = (
        "this content isn't available", "этот контент сейчас недоступен", "контент недоступен",
        "this page isn't available", "эта страница недоступна",
    )
    cant_write_markers = (
        "you can't message this", "you can't reply to this conversation",
        "вы не можете отправить сообщение", "вы не можете ответить",
    )
    flood_markers = (
        "you can't use this feature right now", "сейчас вы не можете использовать эту функцию",
        "you're temporarily blocked", "вы временно заблокированы", "we limit how often",
    )
    relogin_hint = "нужно перелогиниться (кнопка «Войти в Facebook» в панели)"

    def proxy(self) -> dict | None:
        if settings.FB_PROXY:
            return parse_proxy_url(settings.FB_PROXY, "FB_PROXY")
        return parse_proxy_url(settings.TG_PROXY, "TG_PROXY")

    def chat_url(self, lead: dict) -> str | None:
        ext = (lead.get("external_id") or "").strip()
        return f"https://www.facebook.com/messages/t/{quote(ext, safe='.')}" if ext else None


class WhatsAppSendAdapter(WebMessengerSendAdapter):
    name = "WhatsApp"
    input_selector = 'footer div[contenteditable="true"]'
    login_selector = 'canvas[aria-label*="QR"], canvas[aria-label*="Scan"]'
    login_markers = ("use whatsapp on your computer", "чтобы использовать whatsapp на компьютере", "войдите по номеру телефона")
    not_found_markers = (
        "phone number shared via url is invalid",
        "номер телефона, переданный по ссылке, недействителен",
        "недействительный номер",
    )
    flood_markers = ("this account is not allowed to use whatsapp", "аккаунт заблокирован", "your account has been banned")
    relogin_hint = "нужно перелогиниться (кнопка «Войти в WhatsApp» в панели)"
    # WhatsApp Web синхронизирует чаты при каждом открытии — первая загрузка бывает медленной.
    open_timeout_ms = 90_000

    def chat_url(self, lead: dict) -> str | None:
        digits = "".join(ch for ch in (lead.get("external_id") or "") if ch.isdigit())
        return f"https://web.whatsapp.com/send?phone={digits}" if digits else None

from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.vk import (
    _MESSAGE_INPUT_SELECTOR,
    _SEND_BUTTON_SELECTOR,
    _SEND_MESSAGE_BUTTON_SELECTOR,
    _SITE_FIELD_SELECTOR,
    VkSendAdapter,
)

_LEAD = {"id": "lead-1", "group_url": "https://vk.com/some_group", "external_id": "777"}
_ACCOUNT = {"id": "acc-1"}


def _no_site_wait_for_selector(send_btn: MagicMock):
    """selector-осведомлённый side_effect для page.wait_for_selector: имитирует "поля сайта
    нет" для _SITE_FIELD_SELECTOR, а на кнопку "Написать сообщение" отвечает send_btn — так же,
    как настоящая страница группы без сайта. Остальные селекторы (поле ввода сообщения) не
    находит, чтобы тест не улетал дальше happy-path, если это не нужно конкретному тесту."""

    def wait_for_selector(selector, timeout=None):
        if selector == _SITE_FIELD_SELECTOR:
            raise PlaywrightTimeoutError("site field never appeared")
        if selector == _SEND_MESSAGE_BUTTON_SELECTOR:
            return send_btn
        raise PlaywrightTimeoutError(f"unexpected selector in test: {selector}")

    return wait_for_selector


def _fake_playwright_with_page(page: MagicMock) -> MagicMock:
    pw_cm = MagicMock()
    pw = MagicMock()
    pw_cm.__enter__.return_value = pw
    pw_cm.__exit__.return_value = False

    browser = MagicMock()
    pw.chromium.launch.return_value = browser
    context = MagicMock()
    browser.new_context.return_value = context
    context.new_page.return_value = page
    return pw_cm


def test_send_message_reports_network_error_when_group_page_never_loads():
    # Плохой интернет: самый первый goto (страница группы) падает — это транспортный сбой,
    # не проблема этого лида/сообщения (см. запрос пользователя — тот же принцип, что и в
    # parser-service::_has_external_site, где сбой goto раньше молча трактовался как "нет сайта").
    page = MagicMock(url="https://vk.com/some_group")
    page.goto.side_effect = PlaywrightTimeoutError("navigation timed out")
    page.locator.return_value.count.return_value = 0

    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.network_error is True
    assert result.session_expired is False
    assert result.flood_detected is False


def test_send_message_reports_network_error_when_chat_page_never_loads():
    # Первая навигация (страница группы) проходит нормально, вторая (открытие чата) падает.
    page = MagicMock(url="https://vk.com/some_group")
    page.locator.return_value.count.return_value = 0
    send_btn = MagicMock()
    send_btn.get_attribute.return_value = "/im/convo/-777?sel=-777"
    page.wait_for_selector.side_effect = _no_site_wait_for_selector(send_btn)

    calls = {"n": 0}

    def goto(url, wait_until=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Exception("net::ERR_CONNECTION_RESET")

    page.goto.side_effect = goto

    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.network_error is True


def test_send_message_missing_button_on_loaded_page_is_not_a_network_error():
    # Страница группы ЗАГРУЗИЛАСЬ (goto прошёл) — кнопки "Написать сообщение" просто нет
    # (или не успела отрисоваться за отведённое время). Это лид-специфичный случай, а не
    # сетевой сбой — поведение (и его классификация) должно остаться прежним.
    page = MagicMock(url="https://vk.com/some_group")
    page.locator.return_value.count.return_value = 0
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("button never appeared")

    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.network_error is False


def test_send_message_skips_group_with_external_site_without_writing():
    # Парсер иногда по ошибке пропускает сообщество с собственным сайтом (см. запрос
    # пользователя 2026-08-25) — рассылка должна сама проверить это перед тем, как начать
    # писать, и не открывать чат вовсе, если сайт есть.
    page = MagicMock(url="https://vk.com/some_group")
    page.locator.return_value.count.return_value = 0
    site_field = MagicMock()
    site_field.inner_text.return_value = "example.com"
    page.wait_for_selector.return_value = MagicMock()  # сам факт появления поля
    page.query_selector.return_value = site_field

    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.has_site is True
    # Ни разу не переходили к открытию чата и не печатали — только страница группы.
    assert page.goto.call_count == 1
    page.click.assert_not_called()
    page.type.assert_not_called()


def test_send_message_proceeds_when_group_has_no_site():
    page = MagicMock(url="https://vk.com/some_group")
    page.locator.return_value.count.return_value = 0
    page.inner_text.return_value = "обычный текст страницы, без признаков флуда"
    page.query_selector.return_value = None  # ни капчи, ни (после таймаута) поля сайта

    send_btn = MagicMock()
    send_btn.get_attribute.return_value = "/im/convo/-777?sel=-777"

    def wait_for_selector(selector, timeout=None):
        if selector == _SITE_FIELD_SELECTOR:
            raise PlaywrightTimeoutError("site field never appeared")
        if selector == _SEND_MESSAGE_BUTTON_SELECTOR:
            return send_btn
        if selector == _MESSAGE_INPUT_SELECTOR:
            return MagicMock()
        raise PlaywrightTimeoutError(f"unexpected selector in test: {selector}")

    page.wait_for_selector.side_effect = wait_for_selector

    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is True
    assert result.has_site is False
    page.click.assert_any_call(_SEND_BUTTON_SELECTOR)


def test_send_message_proceeds_when_site_check_fails_unexpectedly():
    # Неоднозначный сбой самой проверки (не таймаут, не сеть — см. _PageLoadError отдельно) —
    # не блокируем реальную отправку из-за неопределённости, тот же принцип, что и у остальных
    # "мягких" проверок в этом файле.
    page = MagicMock(url="https://vk.com/some_group")
    page.locator.return_value.count.return_value = 0
    page.inner_text.return_value = "обычный текст страницы, без признаков флуда"
    page.query_selector.return_value = None

    send_btn = MagicMock()
    send_btn.get_attribute.return_value = "/im/convo/-777?sel=-777"

    def wait_for_selector(selector, timeout=None):
        if selector == _SITE_FIELD_SELECTOR:
            raise RuntimeError("boom")
        if selector == _SEND_MESSAGE_BUTTON_SELECTOR:
            return send_btn
        if selector == _MESSAGE_INPUT_SELECTOR:
            return MagicMock()
        raise PlaywrightTimeoutError(f"unexpected selector in test: {selector}")

    page.wait_for_selector.side_effect = wait_for_selector

    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is True

from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.vk import VkSendAdapter

_LEAD = {"id": "lead-1", "group_url": "https://vk.com/some_group", "external_id": "777"}
_ACCOUNT = {"id": "acc-1"}


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
    page.wait_for_selector.return_value = send_btn

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

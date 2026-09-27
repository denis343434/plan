from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.instagram import _MESSAGE_BUTTON_SELECTOR, _MESSAGE_INPUT_SELECTOR, InstagramSendAdapter

_LEAD = {"id": "lead-1", "group_url": "https://www.instagram.com/some_user/", "external_id": "some_user"}
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


def test_send_message_reports_network_error_when_profile_page_never_loads():
    page = MagicMock()
    page.goto.side_effect = PlaywrightTimeoutError("navigation timed out")
    page.inner_text.return_value = ""

    adapter = InstagramSendAdapter(storage_state={})
    with patch("app.adapters.instagram.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.network_error is True


def test_send_message_reports_session_expired_when_login_wall_shown():
    page = MagicMock()
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("message button never appeared")
    page.inner_text.return_value = "Log in to continue"

    adapter = InstagramSendAdapter(storage_state={})
    with patch("app.adapters.instagram.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.session_expired is True


def test_send_message_reports_failure_when_message_button_missing_and_no_login_wall():
    page = MagicMock()
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("message button never appeared")
    page.inner_text.return_value = "some other page content"

    adapter = InstagramSendAdapter(storage_state={})
    with patch("app.adapters.instagram.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.session_expired is False
    assert result.network_error is False


def test_send_message_reports_flood_detected_after_clicking_message_button():
    page = MagicMock()
    message_btn = MagicMock()

    def wait_for_selector(selector, timeout=None):
        if selector == _MESSAGE_BUTTON_SELECTOR:
            return message_btn
        raise PlaywrightTimeoutError(f"unexpected selector in test: {selector}")

    page.wait_for_selector.side_effect = wait_for_selector
    page.inner_text.return_value = "Action Blocked"

    adapter = InstagramSendAdapter(storage_state={})
    with patch("app.adapters.instagram.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is False
    assert result.flood_detected is True


def test_send_message_success_happy_path():
    page = MagicMock()
    message_btn = MagicMock()
    calls = {"n": 0}

    def wait_for_selector(selector, timeout=None):
        if selector == _MESSAGE_BUTTON_SELECTOR:
            return message_btn
        if selector == _MESSAGE_INPUT_SELECTOR:
            return MagicMock()
        raise PlaywrightTimeoutError(f"unexpected selector in test: {selector}")

    def inner_text(_=None):
        calls["n"] += 1
        return ""  # ни один из вызовов не содержит flood-маркеров

    page.wait_for_selector.side_effect = wait_for_selector
    page.inner_text.side_effect = inner_text

    adapter = InstagramSendAdapter(storage_state={})
    with patch("app.adapters.instagram.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        with patch("app.adapters.instagram._sleep_random"):
            result = adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert result.success is True

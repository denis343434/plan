import threading
import time
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.base import ReplyCheckResult
from app.adapters.vk import (
    _MESSAGE_AUTHOR_LINK_SELECTOR,
    _MESSAGE_ITEM_SELECTOR,
    _MESSAGE_TEXT_SELECTOR,
    _SEND_MESSAGE_BUTTON_SELECTOR,
    VkInboxAdapter,
)
from app.config import settings


def _make_message_item(author_href: str, text: str) -> MagicMock:
    author_link = MagicMock()
    author_link.get_attribute.return_value = author_href
    text_el = MagicMock()
    text_el.inner_text.return_value = text

    item = MagicMock()

    def query_selector(selector: str):
        if selector == _MESSAGE_AUTHOR_LINK_SELECTOR:
            return author_link
        if selector == _MESSAGE_TEXT_SELECTOR:
            return text_el
        return None

    item.query_selector.side_effect = query_selector
    return item


def _fake_playwright_context_manager() -> MagicMock:
    pw_cm = MagicMock()
    pw = MagicMock()
    pw_cm.__enter__.return_value = pw
    pw_cm.__exit__.return_value = False

    browser = MagicMock()
    pw.chromium.launch.return_value = browser
    context = MagicMock()
    browser.new_context.return_value = context
    context.new_page.side_effect = lambda: MagicMock(url="https://vk.com/some_group")
    return pw_cm


def test_check_one_uses_cached_chat_href_and_skips_group_navigation():
    page = MagicMock(url="https://vk.com/some_group")
    lead = {
        "id": "lead-1",
        "group_url": "https://vk.com/some_group",
        "chat_href": "/im/convo/-777?sel=-777",
        "external_id": "777",
    }
    page.query_selector_all.return_value = [_make_message_item("/777", "Привет!")]

    adapter = VkInboxAdapter(storage_state={})
    result = adapter._check_one(page, lead)

    assert page.goto.call_count == 1  # только переход по кэшированной ссылке, без страницы группы
    wait_selectors = [call.args[0] for call in page.wait_for_selector.call_args_list]
    assert wait_selectors == [_MESSAGE_ITEM_SELECTOR]  # кнопку "Написать сообщение" не искали
    assert result.has_reply is True
    assert result.preview == "Привет!"
    assert result.resolved_chat_href is None  # кэш свежий, перезаписывать нечего


def test_check_one_falls_back_to_group_page_when_cached_href_is_stale():
    page = MagicMock(url="https://vk.com/some_group")
    lead = {
        "id": "lead-1",
        "group_url": "https://vk.com/some_group",
        "chat_href": "/im/convo/-777",
        "external_id": "777",
    }

    send_btn = MagicMock()
    send_btn.get_attribute.return_value = "/im/convo/-777?sel=-777"

    seen_message_item_calls: list[None] = []

    def wait_for_selector(selector: str, timeout: int = 20_000):
        if selector == _SEND_MESSAGE_BUTTON_SELECTOR:
            return send_btn
        if selector == _MESSAGE_ITEM_SELECTOR:
            seen_message_item_calls.append(None)
            if len(seen_message_item_calls) == 1:
                raise PlaywrightTimeoutError("stale cached href")
            return MagicMock()
        raise AssertionError(f"unexpected selector {selector}")

    page.wait_for_selector.side_effect = wait_for_selector
    page.query_selector_all.return_value = []

    adapter = VkInboxAdapter(storage_state={})
    result = adapter._check_one(page, lead)

    # переход по протухшему кэшу + страница группы + свежая ссылка на чат = 3 навигации
    assert page.goto.call_count == 3
    assert result.has_reply is False
    assert result.resolved_chat_href == "/im/convo/-777?sel=-777"


def test_check_replies_respects_configured_concurrency(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "MAX_DELAY_SEC", 0.0)

    leads = [{"id": f"lead-{i}"} for i in range(5)]

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_check_one(self, page, lead):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    with patch("app.adapters.vk.sync_playwright", return_value=_fake_playwright_context_manager()):
        adapter = VkInboxAdapter(storage_state={})
        progress_calls: list[tuple[int, int]] = []
        results = adapter.check_replies(leads, on_progress=lambda checked, total: progress_calls.append((checked, total)))

    assert len(results) == 5
    assert 1 <= max_active <= 2
    assert sorted(progress_calls) == [(i, 5) for i in range(1, 6)]

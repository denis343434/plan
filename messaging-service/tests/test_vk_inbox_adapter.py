import threading
import time
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.base import ReplyCheckResult
from app.adapters.vk import (
    _CONVO_ITEM_SELECTOR,
    _CONVO_TITLE_SELECTOR,
    _MESSAGE_AUTHOR_LINK_SELECTOR,
    _MESSAGE_ITEM_SELECTOR,
    _MESSAGE_TEXT_SELECTOR,
    _ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR,
    _ONLY_UNREAD_SWITCH_SELECTOR,
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


def _make_convo_item(title: str | None) -> MagicMock:
    title_el = None
    if title is not None:
        title_el = MagicMock()
        title_el.get_attribute.return_value = title
        title_el.inner_text.return_value = title

    item = MagicMock()

    def query_selector(selector: str):
        if selector == _CONVO_TITLE_SELECTOR:
            return title_el
        return None

    item.query_selector.side_effect = query_selector
    return item


def _make_im_page(switch_checked: bool, items: list[MagicMock]) -> tuple[MagicMock, MagicMock]:
    """Мок Page для vk.com/im: тумблер "Только непрочитанные" + список диалогов."""
    page = MagicMock()
    switch = MagicMock()
    switch.get_attribute.return_value = "true" if switch_checked else "false"
    clickable = MagicMock()

    def query_selector(selector: str):
        if selector == _ONLY_UNREAD_SWITCH_SELECTOR:
            return switch
        if selector == _ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR:
            return clickable
        return None

    page.query_selector.side_effect = query_selector
    page.query_selector_all.return_value = items
    return page, clickable


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


def test_check_one_navigates_group_page_then_chat_every_time():
    page = MagicMock(url="https://vk.com/some_group")
    lead = {
        "id": "lead-1",
        "group_url": "https://vk.com/some_group",
        "external_id": "777",
    }

    send_btn = MagicMock()
    send_btn.get_attribute.return_value = "/im/convo/-777?sel=-777"

    def wait_for_selector(selector: str, timeout: int = 20_000):
        if selector == _SEND_MESSAGE_BUTTON_SELECTOR:
            return send_btn
        if selector == _MESSAGE_ITEM_SELECTOR:
            return MagicMock()
        raise AssertionError(f"unexpected selector {selector}")

    page.wait_for_selector.side_effect = wait_for_selector
    page.query_selector_all.return_value = [_make_message_item("/777", "Привет!")]

    adapter = VkInboxAdapter(storage_state={})
    result = adapter._check_one(page, lead)

    # страница группы (чтобы взять href) + сама переписка = 2 навигации, каждый раз заново
    assert page.goto.call_count == 2
    assert result.has_reply is True
    assert result.preview == "Привет!"


def test_list_unread_conversation_titles_activates_switch_when_not_checked():
    page, clickable = _make_im_page(
        switch_checked=False,
        items=[_make_convo_item(title="Кафе «Миндаль»"), _make_convo_item(title="Тренажерный зал «YaGOda»")],
    )

    adapter = VkInboxAdapter(storage_state={})
    titles = adapter._list_unread_conversation_titles(page)

    clickable.click.assert_called_once()  # тумблер был выключен — обязаны включить
    assert titles == {"Кафе «Миндаль»", "Тренажерный зал «YaGOda»"}
    page.goto.assert_called_once_with("https://vk.com/im", wait_until="domcontentloaded")


def test_list_unread_conversation_titles_does_not_reclick_switch_when_already_checked():
    page, clickable = _make_im_page(switch_checked=True, items=[_make_convo_item(title="Кафе «Миндаль»")])

    adapter = VkInboxAdapter(storage_state={})
    titles = adapter._list_unread_conversation_titles(page)

    clickable.click.assert_not_called()  # уже включён — лишний клик выключил бы фильтр
    assert titles == {"Кафе «Миндаль»"}


def test_list_unread_conversation_titles_returns_empty_set_when_list_empty():
    page, _ = _make_im_page(switch_checked=True, items=[])

    adapter = VkInboxAdapter(storage_state={})
    assert adapter._list_unread_conversation_titles(page) == set()


def test_list_unread_conversation_titles_returns_none_on_navigation_failure():
    page = MagicMock()
    page.goto.side_effect = Exception("navigation failed")

    adapter = VkInboxAdapter(storage_state={})
    assert adapter._list_unread_conversation_titles(page) is None


def test_list_unread_conversation_titles_returns_none_when_switch_never_renders():
    page = MagicMock()
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("footer did not render")

    adapter = VkInboxAdapter(storage_state={})
    assert adapter._list_unread_conversation_titles(page) is None
    page.query_selector_all.assert_not_called()  # даже не пытаемся читать список, если он не отрисовался


def test_check_replies_skips_leads_not_in_unread_dialog_list_when_filter_enabled(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", True)
    monkeypatch.setattr(VkInboxAdapter, "_list_unread_conversation_titles", lambda self, page: {"Есть ответ"})

    leads = [
        {"id": "lead-with-reply", "title": "Есть ответ"},
        {"id": "lead-without-reply", "title": "Без ответа"},
    ]

    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        return ReplyCheckResult(has_reply=True, preview="привет")

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        progress_calls: list[tuple[int, int]] = []
        results = adapter.check_replies(leads, on_progress=lambda checked, total: progress_calls.append((checked, total)))

    # только лид, чьё название совпало со списком непрочитанных диалогов, реально открывается
    assert checked_leads == ["lead-with-reply"]
    assert results["lead-with-reply"].has_reply is True
    assert results["lead-without-reply"].has_reply is False
    assert sorted(progress_calls) == [(1, 2), (2, 2)]


def test_check_replies_always_checks_leads_without_title_even_when_filter_enabled(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", True)
    monkeypatch.setattr(VkInboxAdapter, "_list_unread_conversation_titles", lambda self, page: set())

    leads = [{"id": "lead-no-title", "title": None}]
    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        adapter.check_replies(leads)

    # без title сопоставить не с чем — не гадаем, что "нет ответа", проверяем напрямую
    assert checked_leads == ["lead-no-title"]


def test_check_replies_runs_full_scan_when_filter_disabled(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", False)

    def fail_list_unread(self, page):
        raise AssertionError("dialog list must not be read when filter is disabled")

    monkeypatch.setattr(VkInboxAdapter, "_list_unread_conversation_titles", fail_list_unread)

    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    leads = [{"id": f"lead-{i}", "title": f"T{i}"} for i in range(3)]

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        results = adapter.check_replies(leads)

    assert len(results) == 3
    assert sorted(checked_leads) == ["lead-0", "lead-1", "lead-2"]


def test_check_replies_runs_full_scan_when_unread_dialog_list_undetermined(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", True)
    monkeypatch.setattr(VkInboxAdapter, "_list_unread_conversation_titles", lambda self, page: None)

    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    leads = [{"id": f"lead-{i}", "title": f"T{i}"} for i in range(3)]

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        results = adapter.check_replies(leads)

    assert len(results) == 3
    assert sorted(checked_leads) == ["lead-0", "lead-1", "lead-2"]


def test_check_replies_uses_independent_playwright_driver_per_worker(monkeypatch):
    # Каждый воркер-поток обязан завести СВОЙ независимый sync_playwright()/Browser/Context —
    # общая Page на несколько потоков в реальности падает с Playwright ("Cannot switch to a
    # different thread", проверено вживую 2026-08-21, см. докстринг VkInboxAdapter). Тест бьёт
    # 4 лида на concurrency=2 воркера и проверяет: одновременно работает не больше 2, у каждого
    # воркера своя Page (не общая на всех), все лиды в итоге проверены.
    monkeypatch.setattr(settings, "INBOX_CHECK_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "MAX_DELAY_SEC", 0.0)

    leads = [{"id": f"lead-{i}"} for i in range(4)]

    active = 0
    max_active = 0
    lock = threading.Lock()
    page_id_by_lead: dict[str, int] = {}

    def fake_check_one(self, page, lead):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        page_id_by_lead[lead["id"]] = id(page)
        time.sleep(0.05)
        with lock:
            active -= 1
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        progress_calls: list[tuple[int, int]] = []
        results = adapter.check_replies(leads, on_progress=lambda checked, total: progress_calls.append((checked, total)))

    assert len(results) == 4
    assert 1 <= max_active <= 2  # concurrency=2 — параллельно работает не больше двух воркеров
    assert len(set(page_id_by_lead.values())) <= 2  # не больше независимых Page, чем воркеров
    assert sorted(progress_calls) == [(i, 4) for i in range(1, 5)]

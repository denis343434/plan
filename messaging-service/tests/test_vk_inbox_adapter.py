import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.adapters.base import ReplyCheckResult
from app.adapters.vk import (
    _CONVO_ITEM_SELECTOR,
    _MESSAGE_AUTHOR_LINK_SELECTOR,
    _MESSAGE_ITEM_SELECTOR,
    _MESSAGE_TEXT_SELECTOR,
    _ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR,
    _ONLY_UNREAD_SWITCH_SELECTOR,
    _SEND_MESSAGE_BUTTON_SELECTOR,
    SessionExpiredError,
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


def _make_im_walk_page(
    switch_checked: bool, dialogs: list[tuple[str, list[MagicMock]]]
) -> tuple[MagicMock, MagicMock]:
    """Мок Page для vk.com/im: тумблер "Только непрочитанные" + список диалогов, где клик по
    диалогу с данным data-itemkey переключает то, что вернёт query_selector_all для сообщений
    (см. _check_one_unread_dialog — открывает диалог кликом, потом читает _MESSAGE_ITEM_SELECTOR)."""
    page = MagicMock()
    switch = MagicMock()
    switch.get_attribute.return_value = "true" if switch_checked else "false"
    clickable = MagicMock()

    state = {"current_messages": []}
    convo_items = []
    for key, messages in dialogs:
        convo_item = MagicMock()
        convo_item.get_attribute.return_value = key

        def _click(messages=messages):
            state["current_messages"] = messages

        convo_item.click.side_effect = _click
        convo_items.append(convo_item)

    def query_selector(selector: str):
        if selector == _ONLY_UNREAD_SWITCH_SELECTOR:
            return switch
        if selector == _ONLY_UNREAD_SWITCH_CLICKABLE_SELECTOR:
            return clickable
        return None

    def query_selector_all(selector: str):
        if selector == _CONVO_ITEM_SELECTOR:
            return convo_items
        if selector == _MESSAGE_ITEM_SELECTOR:
            return state["current_messages"]
        return []

    page.query_selector.side_effect = query_selector
    page.query_selector_all.side_effect = query_selector_all
    page.locator.return_value.count.return_value = 0  # экрана "войдите заново" нет
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


def test_check_one_navigates_group_page_then_chat_every_time():
    page = MagicMock(url="https://vk.com/some_group")
    page.locator.return_value.count.return_value = 0  # экрана "войдите заново" нет
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


def test_check_one_raises_session_expired_when_login_required():
    # VK показал экран "Выберите аккаунт для входа" вместо страницы группы — сессия протухла.
    # Раньше это тихо ловилось общим except Exception и превращалось в обычный ReplyCheckResult
    # с ошибкой (см. запрос пользователя 2026-08-24 — 55 лидов подряд с одной и той же ошибкой
    # вместо одной понятной остановки). Кнопка "Написать сообщение" в такой ситуации не
    # отрисуется — проверка стоит именно после этого таймаута, а не сразу после goto
    # (instant-проверка сразу после goto ничего не находила, экран мог ещё не отрисоваться).
    page = MagicMock(url="https://vk.com/some_group")
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("send button did not render")
    page.locator.return_value.count.return_value = 1
    lead = {"id": "lead-1", "group_url": "https://vk.com/some_group", "external_id": "777"}

    adapter = VkInboxAdapter(storage_state={})
    with pytest.raises(SessionExpiredError):
        adapter._check_one(page, lead)


def test_walk_unread_dialogs_activates_switch_when_not_checked():
    page, clickable = _make_im_walk_page(switch_checked=False, dialogs=[])

    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        adapter = VkInboxAdapter(storage_state={})
        results: dict[str, ReplyCheckResult] = {}
        ok = adapter._walk_unread_dialogs({}, results, lambda: None)

    clickable.click.assert_called_once()  # тумблер был выключен — обязаны включить
    assert ok is True
    page.goto.assert_called_once_with("https://vk.com/im", wait_until="domcontentloaded")


def test_walk_unread_dialogs_does_not_reclick_switch_when_already_checked():
    page, clickable = _make_im_walk_page(switch_checked=True, dialogs=[])

    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        adapter = VkInboxAdapter(storage_state={})
        adapter._walk_unread_dialogs({}, {}, lambda: None)

    clickable.click.assert_not_called()  # уже включён — лишний клик выключил бы фильтр


def test_walk_unread_dialogs_returns_false_on_navigation_failure():
    page = MagicMock()
    page.goto.side_effect = Exception("navigation failed")

    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        adapter = VkInboxAdapter(storage_state={})
        assert adapter._walk_unread_dialogs({}, {}, lambda: None) is False


def test_walk_unread_dialogs_returns_false_when_switch_never_renders():
    page = MagicMock()
    page.locator.return_value.count.return_value = 0  # экрана "войдите заново" нет
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("footer did not render")

    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        adapter = VkInboxAdapter(storage_state={})
        assert adapter._walk_unread_dialogs({}, {}, lambda: None) is False
    page.query_selector_all.assert_not_called()  # даже не пытаемся читать список, если он не отрисовался


def test_walk_unread_dialogs_raises_session_expired_when_login_required():
    # Тумблер "Только непрочитанные" не отрисуется, если вместо /im показан экран повторного
    # входа — проверка на "требуется вход" стоит именно после этого таймаута, а не сразу после
    # goto (см. запрос пользователя 2026-08-24: instant-проверка сразу после goto ничего не
    # находила — реальный экран входа мог ещё не отрисоваться).
    page, _ = _make_im_walk_page(switch_checked=True, dialogs=[])
    page.wait_for_selector.side_effect = PlaywrightTimeoutError("footer did not render")
    page.locator.return_value.count.return_value = 1

    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        adapter = VkInboxAdapter(storage_state={})
        with pytest.raises(SessionExpiredError):
            adapter._walk_unread_dialogs({}, {}, lambda: None)


def test_walk_unread_dialogs_matches_by_author_href_not_title(monkeypatch):
    monkeypatch.setattr(settings, "MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "MAX_DELAY_SEC", 0.0)
    # Диалог с непрочитанным от лида "lead-1" (external_id="777") + диалог с непрочитанным от
    # кого-то постороннего, не входящего в pending-лидов этого прогона — второй должен быть
    # тихо пропущен (не наш лид), а не упасть с ошибкой.
    dialogs = [
        ("convo_-777", [_make_message_item("/777", "Здравствуйте, интересует!")]),
        ("convo_-999", [_make_message_item("/999", "какой-то чужой диалог")]),
    ]
    page, _ = _make_im_walk_page(switch_checked=True, dialogs=dialogs)

    leads_by_external_id = {"777": {"id": "lead-1", "external_id": "777"}}

    with patch("app.adapters.vk.sync_playwright", side_effect=lambda: _fake_playwright_with_page(page)):
        adapter = VkInboxAdapter(storage_state={})
        results: dict[str, ReplyCheckResult] = {}
        progress_calls: list[int] = []
        ok = adapter._walk_unread_dialogs(
            leads_by_external_id, results, lambda: progress_calls.append(1)
        )

    assert ok is True
    assert results.keys() == {"lead-1"}
    assert results["lead-1"].has_reply is True
    assert results["lead-1"].preview == "Здравствуйте, интересует!"
    assert len(progress_calls) == 1  # прогресс только для реально совпавшего лида


def test_check_replies_uses_unread_walk_results_and_skips_full_scan_when_filter_enabled(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", True)

    def fake_walk(self, leads_by_external_id, results, report_progress):
        results["lead-with-reply"] = ReplyCheckResult(has_reply=True, preview="привет")
        report_progress()
        return True

    monkeypatch.setattr(VkInboxAdapter, "_walk_unread_dialogs", fake_walk)

    def fail_check_one(self, page, lead):
        raise AssertionError("full scan must not run when the unread walk succeeded")

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fail_check_one)

    leads = [
        {"id": "lead-with-reply", "external_id": "777"},
        {"id": "lead-without-reply", "external_id": "888"},
    ]

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        progress_calls: list[tuple[int, int]] = []
        results = adapter.check_replies(leads, on_progress=lambda checked, total: progress_calls.append((checked, total)))

    assert results["lead-with-reply"].has_reply is True
    assert results["lead-without-reply"].has_reply is False
    assert sorted(progress_calls) == [(1, 2), (2, 2)]


def test_check_replies_always_checks_leads_without_external_id_even_when_filter_enabled(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", True)
    monkeypatch.setattr(VkInboxAdapter, "_walk_unread_dialogs", lambda self, m, r, p: True)

    leads = [{"id": "lead-no-external-id", "external_id": None}]
    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        adapter.check_replies(leads)

    # без external_id сопоставить не с чем — не гадаем, что "нет ответа", проверяем напрямую
    assert checked_leads == ["lead-no-external-id"]


def test_check_replies_runs_full_scan_when_filter_disabled(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", False)

    def fail_walk(self, leads_by_external_id, results, report_progress):
        raise AssertionError("unread dialog list must not be read when filter is disabled")

    monkeypatch.setattr(VkInboxAdapter, "_walk_unread_dialogs", fail_walk)

    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    leads = [{"id": f"lead-{i}", "external_id": str(i)} for i in range(3)]

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        results = adapter.check_replies(leads)

    assert len(results) == 3
    assert sorted(checked_leads) == ["lead-0", "lead-1", "lead-2"]


def test_check_replies_runs_full_scan_when_unread_dialog_list_undetermined(monkeypatch):
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", True)
    monkeypatch.setattr(VkInboxAdapter, "_walk_unread_dialogs", lambda self, m, r, p: False)

    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    leads = [{"id": f"lead-{i}", "external_id": str(i)} for i in range(3)]

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
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", False)

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


def test_check_replies_stops_bucket_and_raises_when_session_expired(monkeypatch):
    # Как только воркер натыкается на экран повторного входа — не долбит им же оставшихся
    # лидов в своём бакете (см. запрос пользователя 2026-08-24: раньше все 55 лидов подряд
    # получали одну и ту же ошибку вместо одной понятной остановки).
    monkeypatch.setattr(settings, "INBOX_CHECK_FILTER_BY_UNREAD_LIST", False)
    monkeypatch.setattr(settings, "INBOX_CHECK_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "MIN_DELAY_SEC", 0.0)
    monkeypatch.setattr(settings, "MAX_DELAY_SEC", 0.0)

    checked_leads: list[str] = []

    def fake_check_one(self, page, lead):
        checked_leads.append(lead["id"])
        if lead["id"] == "lead-0":
            raise SessionExpiredError("сессия протухла")
        return ReplyCheckResult(has_reply=False)

    monkeypatch.setattr(VkInboxAdapter, "_check_one", fake_check_one)

    leads = [{"id": f"lead-{i}", "external_id": str(i)} for i in range(3)]

    with patch("app.adapters.vk.sync_playwright", side_effect=_fake_playwright_context_manager):
        adapter = VkInboxAdapter(storage_state={})
        with pytest.raises(SessionExpiredError):
            adapter.check_replies(leads)

    assert checked_leads == ["lead-0"]

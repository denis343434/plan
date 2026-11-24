import uuid
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app import ratelimit
from app.adapter_pool import SendAdapterPool
from app.adapters.base import SendResult
from app.adapters.vk import VkSendAdapter
from app.config import settings
from app.tasks import SendTask, _process_lead

_LEAD = {"id": "lead-1", "group_url": "https://vk.com/some_group", "external_id": "777"}
_ACCOUNT = {"id": "acc-1"}


def _fake_playwright():
    pw_cm = MagicMock()
    pw = pw_cm.__enter__.return_value
    browser = pw.chromium.launch.return_value
    browser.is_connected.return_value = True
    context = browser.new_context.return_value

    def new_page():
        page = MagicMock(url="https://vk.com/some_group")
        page.goto.side_effect = PlaywrightTimeoutError("navigation timed out")
        return page

    context.new_page.side_effect = new_page
    return pw_cm, pw, browser, context


def test_opened_adapter_reuses_one_browser_for_many_messages():
    pw_cm, pw, browser, context = _fake_playwright()
    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", return_value=pw_cm):
        with adapter:
            adapter.send_message(_LEAD, _ACCOUNT, "hello")
            adapter.send_message(_LEAD, _ACCOUNT, "hello")
            adapter.send_message(_LEAD, _ACCOUNT, "hello")
            assert not browser.close.called

    assert pw.chromium.launch.call_count == 1
    assert context.new_page.call_count == 3
    browser.close.assert_called_once()
    pw_cm.__exit__.assert_called_once()


def test_opened_adapter_relaunches_disconnected_browser():
    pw_cm, pw, browser, _ = _fake_playwright()
    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", return_value=pw_cm):
        with adapter:
            adapter.send_message(_LEAD, _ACCOUNT, "hello")
            browser.is_connected.return_value = False
            adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert pw.chromium.launch.call_count == 2


def test_not_opened_adapter_keeps_one_shot_browser():
    pw_cm, pw, browser, _ = _fake_playwright()
    adapter = VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", return_value=pw_cm):
        adapter.send_message(_LEAD, _ACCOUNT, "hello")
        adapter.send_message(_LEAD, _ACCOUNT, "hello")

    assert pw.chromium.launch.call_count == 2
    assert browser.close.call_count == 2


def test_type_chunks_splits_by_words_without_losing_text():
    text = "  Здравствуйте, Кафе Миндаль!\nХотим  предложить сайт. "
    chunks = ratelimit.type_chunks(text)
    assert "".join(chunks) == text
    assert chunks[1] == "Здравствуйте, "


def test_typing_speed_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "TYPING_DELAY_MIN_MS", 0.0)
    monkeypatch.setattr(settings, "TYPING_DELAY_MAX_MS", 0.0)
    page = MagicMock()
    VkSendAdapter(storage_state={})._type_like_human(page, "one two three")
    assert page.type.call_count == 3
    assert all(call.kwargs["delay"] == 0.0 for call in page.type.call_args_list)


class _FakeAdapter:
    def __init__(self):
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True


def _pool(monkeypatch, max_open=2):
    monkeypatch.setattr(settings, "SEND_MAX_OPEN_BROWSERS", max_open)
    client = MagicMock()
    client.get_session.side_effect = lambda account_id: {"storage_state": {"account": account_id}}
    created: list[_FakeAdapter] = []

    def factory(platform, storage_state):
        adapter = _FakeAdapter()
        created.append(adapter)
        return adapter

    return SendAdapterPool(client, factory), client, created


def test_pool_reuses_adapter_per_account(monkeypatch):
    pool, client, created = _pool(monkeypatch)
    first = pool.get({"id": "a"}, "vk")
    assert pool.get({"id": "a"}, "vk") is first
    assert first.opened
    assert client.get_session.call_count == 1
    assert len(created) == 1


def test_pool_evicts_least_recently_used(monkeypatch):
    pool, _, created = _pool(monkeypatch, max_open=2)
    a = pool.get({"id": "a"}, "vk")
    b = pool.get({"id": "b"}, "vk")
    pool.get({"id": "a"}, "vk")  # "a" свежее, чем "b"
    pool.get({"id": "c"}, "vk")
    assert b.closed and not a.closed
    pool.close()
    assert all(adapter.closed for adapter in created)


def test_pool_discard_closes_and_forgets_adapter(monkeypatch):
    pool, client, _ = _pool(monkeypatch)
    a = pool.get({"id": "a"}, "vk")
    pool.discard({"id": "a"}, "vk")
    assert a.closed
    assert pool.get({"id": "a"}, "vk") is not a
    assert client.get_session.call_count == 2


def test_delay_for_platform_uses_instagram_range(monkeypatch):
    calls = []
    monkeypatch.setattr(ratelimit, "delay", lambda lo, hi: calls.append((lo, hi)))
    ratelimit.delay_for_platform("instagram")
    ratelimit.delay_for_platform("vk")
    assert calls == [
        (settings.IG_MIN_DELAY_SEC, settings.IG_MAX_DELAY_SEC),
        (settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC),
    ]


def test_whatsapp_cycle_is_random_110_to_125_and_accounts_for_elapsed(monkeypatch):
    monkeypatch.setattr(settings, "WA_CYCLE_MIN_SEC", 110.0)
    monkeypatch.setattr(settings, "WA_CYCLE_MAX_SEC", 125.0)
    sleeps: list[float] = []
    monkeypatch.setattr(
        ratelimit, "_sleep", lambda seconds, should_stop=None: sleeps.append(seconds)
    )
    ranges: list[tuple[float, float]] = []

    def fake_uniform(lo, hi):
        ranges.append((lo, hi))
        return 118.0

    monkeypatch.setattr(ratelimit.random, "uniform", fake_uniform)

    ratelimit.delay_for_platform("whatsapp", elapsed_sec=10.0)
    ratelimit.delay_for_platform("whatsapp", elapsed_sec=125.0)

    assert ranges == [(110.0, 125.0), (110.0, 125.0)]
    assert sleeps == [108.0, 0.0]  # цель 118с минус уже потраченное время


def test_manual_whatsapp_reply_keeps_legacy_pause(monkeypatch):
    monkeypatch.setattr(settings, "WA_CYCLE_MIN_SEC", 110.0)
    calls = []
    monkeypatch.setattr(ratelimit, "delay", lambda lo, hi: calls.append((lo, hi)))

    ratelimit.delay_for_platform("whatsapp")  # elapsed_sec=None — ручной ответ

    assert calls == [(settings.WA_MIN_DELAY_SEC, settings.WA_MAX_DELAY_SEC)]


def test_two_opened_accounts_share_one_playwright_driver_per_thread():
    # Регрессия 2026-09-28: второй аккаунт в пуле открывал второй sync_playwright() в том же
    # потоке, пока первый ещё жив — настоящий Playwright на это падает "...Sync API inside the
    # asyncio loop". Фейк ниже падает так же.
    active = {"n": 0}
    pw_cm, pw, browser, _ = _fake_playwright()

    def enter():
        if active["n"]:
            raise RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop.")
        active["n"] += 1
        return pw

    def exit_(*_):
        active["n"] -= 1

    pw_cm.__enter__.side_effect = enter
    pw_cm.__exit__.side_effect = exit_

    first, second = VkSendAdapter(storage_state={}), VkSendAdapter(storage_state={})
    with patch("app.adapters.vk.sync_playwright", return_value=pw_cm):
        first.open()
        second.open()
        first.send_message(_LEAD, _ACCOUNT, "hello")
        second.send_message(_LEAD, _ACCOUNT, "hello")
        # одноразовая отправка (reply.py) в том же потоке тоже не должна падать
        VkSendAdapter(storage_state={}).send_message(_LEAD, _ACCOUNT, "hello")
        first.close()
        assert active["n"] == 1  # второй аккаунт ещё открыт — драйвер жив
        second.close()

    assert pw.chromium.launch.call_count == 3
    assert pw_cm.__enter__.call_count == 1
    assert active["n"] == 0


class _PauseClient:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.messages: list[dict] = []
        self.released: list[str] = []

    def next_available_account(self, **kwargs):
        return {"id": "acc-1", "login": "wa"}

    def release_account(self, account_id):
        self.released.append(account_id)

    def patch_lead_status(self, lead_id, status):
        self.statuses.append((lead_id, status))

    def post_message(self, payload):
        self.messages.append(payload)


class _ResultAdapter:
    def __init__(self, result: SendResult) -> None:
        self.result = result

    def send_message(self, lead, account, text, image=None):
        return self.result


class _PausePool:
    def __init__(self, result: SendResult) -> None:
        self.result = result

    def get(self, account, platform):
        return _ResultAdapter(self.result)

    def discard(self, account, platform):
        return None


def _run_process_lead(monkeypatch, result: SendResult):
    pauses: list[str] = []
    monkeypatch.setattr(
        ratelimit, "delay_for_platform", lambda platform, **kwargs: pauses.append(platform)
    )
    client = _PauseClient()
    task = SendTask(campaign_id=uuid.uuid4())
    lead = {
        "id": "lead-1",
        "platform": "whatsapp",
        "title": "Кафе Миндаль",
        "group_url": "https://wa.me/79518677086",
    }
    templates = [{"body": "Здравствуйте, {{org_name}}", "variant": "A"}]
    ok = _process_lead(client, _PausePool(result), task, lead, templates)
    return ok, pauses, client, task


def test_unreachable_lead_does_not_pay_inter_lead_pause(monkeypatch):
    ok, pauses, client, task = _run_process_lead(
        monkeypatch,
        SendResult(success=False, error="номер не найден в WhatsApp", unreachable=True),
    )

    assert ok
    assert pauses == []
    assert task.skipped == 1
    assert client.statuses == [("lead-1", "rejected")]
    assert client.messages == []


def test_sent_lead_still_pays_inter_lead_pause(monkeypatch):
    ok, pauses, client, task = _run_process_lead(monkeypatch, SendResult(success=True))

    assert ok
    assert pauses == ["whatsapp"]
    assert task.sent == 1
    assert client.statuses == [("lead-1", "contacted")]
    assert client.messages[0]["delivery_status"] == "sent"


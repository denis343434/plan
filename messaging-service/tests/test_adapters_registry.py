from app.adapters.dryrun import DryRunAdapter, DryRunInboxAdapter
from app.adapters.registry import get_adapter, get_inbox_adapter
from app.config import settings


def test_tg_and_instagram_always_use_dryrun_adapter():
    for platform in ("tg", "instagram"):
        adapter = get_adapter(platform)
        assert isinstance(adapter, DryRunAdapter)


def test_vk_uses_dryrun_adapter_by_default():
    assert settings.VK_ADAPTER_MODE == "fake"
    adapter = get_adapter("vk")
    assert isinstance(adapter, DryRunAdapter)


def test_dryrun_adapter_send_message_reports_success():
    adapter = DryRunAdapter("vk")
    result = adapter.send_message({"id": "lead-1"}, {"id": "acc-1"}, "hello")
    assert result.success is True
    assert result.flood_detected is False


def test_vk_uses_playwright_adapter_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "VK_ADAPTER_MODE", "playwright")

    adapter = get_adapter("vk", storage_state={"cookies": []})

    from app.adapters.vk import VkSendAdapter

    assert isinstance(adapter, VkSendAdapter)


def test_tg_ignores_playwright_mode_and_still_uses_dryrun(monkeypatch):
    monkeypatch.setattr(settings, "VK_ADAPTER_MODE", "playwright")

    adapter = get_adapter("tg")

    assert isinstance(adapter, DryRunAdapter)


def test_vk_uses_dryrun_inbox_adapter_by_default():
    assert settings.VK_ADAPTER_MODE == "fake"
    adapter = get_inbox_adapter("vk")
    assert isinstance(adapter, DryRunInboxAdapter)


def test_dryrun_inbox_adapter_reports_no_replies():
    adapter = DryRunInboxAdapter()
    results = adapter.check_replies([{"id": "lead-1"}, {"id": "lead-2"}])
    assert results["lead-1"].has_reply is False
    assert results["lead-2"].has_reply is False


def test_vk_uses_playwright_inbox_adapter_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "VK_ADAPTER_MODE", "playwright")

    adapter = get_inbox_adapter("vk", storage_state={"cookies": []})

    from app.adapters.vk import VkInboxAdapter

    assert isinstance(adapter, VkInboxAdapter)

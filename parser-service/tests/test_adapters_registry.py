from app.adapters.base import ParseFilters
from app.adapters.registry import get_adapter
from app.adapters.stub import NotImplementedAdapter
from app.adapters.vk_fake import FakeVkAdapter
from app.config import settings


def test_stub_adapter_returns_empty_list_without_raising():
    for platform in ("tg", "instagram"):
        adapter = get_adapter(platform)
        assert isinstance(adapter, NotImplementedAdapter)
        assert adapter.search_communities("keyword", ParseFilters()) == []


def test_vk_uses_fake_adapter_by_default():
    assert settings.VK_ADAPTER_MODE == "fake"
    adapter = get_adapter("vk")
    assert isinstance(adapter, FakeVkAdapter)
    leads = adapter.search_communities("fitness", ParseFilters())
    assert len(leads) == 3
    assert all(lead.external_id.startswith("fake-fitness-") for lead in leads)


def test_vk_uses_playwright_adapter_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "VK_ADAPTER_MODE", "playwright")

    adapter = get_adapter("vk", storage_state={"cookies": []})

    from app.adapters.vk import VkParserAdapter

    assert isinstance(adapter, VkParserAdapter)

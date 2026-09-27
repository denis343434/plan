from app.adapters.base import ParseFilters
from app.adapters.instagram_fake import FakeInstagramAdapter
from app.adapters.registry import get_adapter
from app.adapters.stub import NotImplementedAdapter
from app.adapters.vk_fake import FakeVkAdapter
from app.config import settings


def test_stub_adapter_returns_empty_list_without_raising():
    for platform in ("tg",):
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


def test_vk_playwright_adapter_without_storage_state_is_anonymous(monkeypatch):
    # use_account=False (см. app/tasks.py) вызывает get_adapter без storage_state вовсе —
    # адаптер должен получить "пустой" storage_state, а не упасть на None.
    monkeypatch.setattr(settings, "VK_ADAPTER_MODE", "playwright")

    adapter = get_adapter("vk")

    from app.adapters.vk import VkParserAdapter

    assert isinstance(adapter, VkParserAdapter)
    assert not adapter._storage_state


def test_instagram_uses_fake_adapter_by_default():
    assert settings.IG_ADAPTER_MODE == "fake"
    adapter = get_adapter("instagram")
    assert isinstance(adapter, FakeInstagramAdapter)
    leads = adapter.search_communities("fitness", ParseFilters())
    assert len(leads) == 3
    assert all(lead.external_id.startswith("fake_ig_fitness_") for lead in leads)


def test_instagram_uses_playwright_adapter_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "IG_ADAPTER_MODE", "playwright")

    adapter = get_adapter("instagram", storage_state={"cookies": []})

    from app.adapters.instagram import InstagramParserAdapter

    assert isinstance(adapter, InstagramParserAdapter)

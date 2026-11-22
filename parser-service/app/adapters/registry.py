from app.adapters.base import SourceAdapter
from app.adapters.instagram_fake import FakeInstagramAdapter
from app.adapters.stub import NotImplementedAdapter
from app.adapters.vk_fake import FakeVkAdapter
from app.adapters.yandex_maps_fake import FakeYandexMapsAdapter
from app.config import settings


def get_adapter(
    platform: str, storage_state: dict | None = None, social_sessions: dict[str, dict] | None = None
) -> SourceAdapter:
    if platform == "vk":
        if settings.VK_ADAPTER_MODE == "playwright":
            from app.adapters.vk import VkParserAdapter  # lazy: avoid requiring playwright browsers in fake mode

            return VkParserAdapter(storage_state or {})
        return FakeVkAdapter()
    if platform == "instagram":
        if settings.IG_ADAPTER_MODE == "playwright":
            from app.adapters.instagram import InstagramParserAdapter  # lazy: same reason as VK above

            return InstagramParserAdapter(storage_state or {})
        return FakeInstagramAdapter()
    if platform == "yandex_maps":
        if settings.YANDEX_ADAPTER_MODE == "playwright":
            from app.adapters.yandex_maps import YandexMapsAdapter  # lazy: same reason as VK above

            return YandexMapsAdapter()
        return FakeYandexMapsAdapter()
    if platform == "google_maps":
        if settings.GOOGLE_ADAPTER_MODE == "playwright":
            from app.adapters.google_maps import GoogleMapsAdapter  # lazy: same reason as VK above

            return GoogleMapsAdapter(social_sessions)
        # Свой префикс ников, чтобы фейковые лиды Google не совпадали с яндексовыми (dedup).
        return FakeYandexMapsAdapter(slug_prefix="g")
    return NotImplementedAdapter(platform)

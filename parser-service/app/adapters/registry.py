from app.adapters.base import SourceAdapter
from app.adapters.stub import NotImplementedAdapter
from app.adapters.vk_fake import FakeVkAdapter
from app.config import settings


def get_adapter(platform: str, storage_state: dict | None = None) -> SourceAdapter:
    if platform == "vk":
        if settings.VK_ADAPTER_MODE == "playwright":
            from app.adapters.vk import VkParserAdapter  # lazy: avoid requiring playwright browsers in fake mode

            return VkParserAdapter(storage_state or {})
        return FakeVkAdapter()
    return NotImplementedAdapter(platform)

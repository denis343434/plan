from app.adapters.base import SendAdapter
from app.adapters.dryrun import DryRunAdapter
from app.config import settings


def get_adapter(platform: str, storage_state: dict | None = None) -> SendAdapter:
    if platform == "vk" and settings.VK_ADAPTER_MODE == "playwright":
        from app.adapters.vk import VkSendAdapter  # lazy: avoid requiring playwright browsers in fake mode

        return VkSendAdapter(storage_state or {})
    return DryRunAdapter(platform)

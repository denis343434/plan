from app.adapters.base import InboxAdapter, SendAdapter
from app.adapters.dryrun import DryRunAdapter, DryRunInboxAdapter
from app.config import settings


def get_adapter(platform: str, storage_state: dict | None = None) -> SendAdapter:
    if platform == "vk" and settings.VK_ADAPTER_MODE == "playwright":
        from app.adapters.vk import VkSendAdapter  # lazy: avoid requiring playwright browsers in fake mode

        return VkSendAdapter(storage_state or {})
    return DryRunAdapter(platform)


def get_inbox_adapter(platform: str, storage_state: dict | None = None) -> InboxAdapter:
    if platform == "vk" and settings.VK_ADAPTER_MODE == "playwright":
        from app.adapters.vk import VkInboxAdapter  # lazy: avoid requiring playwright browsers in fake mode

        return VkInboxAdapter(storage_state or {})
    return DryRunInboxAdapter()

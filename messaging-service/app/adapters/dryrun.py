import logging

from app.adapters.base import SendResult

logger = logging.getLogger(__name__)


class DryRunAdapter:
    """Общий для tg/instagram (заглушки без реальной реализации) и для VK_ADAPTER_MODE=fake.

    Не открывает браузер и не шлёт реальное сообщение — просто логирует "would send" и
    сообщает об успехе, чтобы весь пайплайн (аккаунт/лимиты/запись messages/contacted)
    можно было гонять целиком уже сейчас.
    """

    def __init__(self, platform: str) -> None:
        self._platform = platform

    def send_message(self, lead: dict, account: dict, text: str) -> SendResult:
        logger.info(
            "would send via %s to lead %s (account %s): %s",
            self._platform,
            lead.get("id"),
            account.get("id"),
            text,
        )
        return SendResult(success=True)

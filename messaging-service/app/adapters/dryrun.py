import logging
from typing import Callable

from app.adapters.base import ReplyCheckResult, SendResult

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


class DryRunInboxAdapter:
    """Fake-режим (VK_ADAPTER_MODE=fake) и tg/instagram — без браузера, без реальных ответов.

    Как и DryRunAdapter, нужен, чтобы весь пайплайн (эндпоинт, запись reply_status в БД,
    десктоп-клиент) можно было гонять и тестировать без живого VK-аккаунта.
    """

    def check_replies(
        self, leads: list[dict], on_progress: Callable[[int, int], None] | None = None
    ) -> dict[str, ReplyCheckResult]:
        total = len(leads)
        results: dict[str, ReplyCheckResult] = {}
        for checked, lead in enumerate(leads, start=1):
            results[lead["id"]] = ReplyCheckResult(has_reply=False)
            if on_progress is not None:
                on_progress(checked, total)
        return results

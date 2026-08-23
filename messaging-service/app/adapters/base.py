from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class SendResult:
    success: bool
    error: str | None = None
    flood_detected: bool = False


class SendAdapter(Protocol):
    # image, если задан — Playwright FilePayload: {"name": str, "mimeType": str, "buffer": bytes}.
    # Только для ручного ответа (см. app/reply.py) — обычная кампания текстом-по-шаблону его не
    # передаёт, поэтому параметр опциональный со значением по умолчанию None.
    def send_message(
        self, lead: dict, account: dict, text: str, image: dict | None = None
    ) -> SendResult: ...


@dataclass
class ReplyCheckResult:
    has_reply: bool
    preview: str | None = None
    error: str | None = None


class InboxAdapter(Protocol):
    def check_replies(
        self, leads: list[dict], on_progress: Callable[[int, int], None] | None = None
    ) -> dict[str, ReplyCheckResult]: ...

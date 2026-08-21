from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class SendResult:
    success: bool
    error: str | None = None
    flood_detected: bool = False


class SendAdapter(Protocol):
    def send_message(self, lead: dict, account: dict, text: str) -> SendResult: ...


@dataclass
class ReplyCheckResult:
    has_reply: bool
    preview: str | None = None
    error: str | None = None


class InboxAdapter(Protocol):
    def check_replies(
        self, leads: list[dict], on_progress: Callable[[int, int], None] | None = None
    ) -> dict[str, ReplyCheckResult]: ...

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SendResult:
    success: bool
    error: str | None = None
    flood_detected: bool = False


class SendAdapter(Protocol):
    def send_message(self, lead: dict, account: dict, text: str) -> SendResult: ...

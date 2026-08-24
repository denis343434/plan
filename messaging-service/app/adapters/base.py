from dataclasses import dataclass
from typing import Callable, Protocol


class SessionExpiredError(Exception):
    """VK показал экран повторного входа вместо запрошенной страницы (см. _raise_if_login_required
    в adapters/vk.py) — сохранённая сессия аккаунта протухла, дальнейший обход тем же
    браузером/аккаунтом бессмысленный. Живёт в base.py (не в vk.py), потому что inbox.py/tasks.py
    должны ловить её явным except, не подтягивая playwright лениво (см. registry.py — playwright
    не нужен в fake-режиме)."""


@dataclass
class SendResult:
    success: bool
    error: str | None = None
    flood_detected: bool = False
    # VK показал экран повторного входа вместо ожидаемой страницы (см. SessionExpiredError в
    # adapters/vk.py) — проблема аккаунта, не этого лида. Как и flood_detected, сигнализирует
    # вызывающему коду отправить аккаунт в cooldown вместо повторной попытки на следующем лиде
    # той же мёртвой сессией.
    session_expired: bool = False


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

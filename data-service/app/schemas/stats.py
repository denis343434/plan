from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class StatsGroupBy(StrEnum):
    total = "total"
    campaign = "campaign"
    # Кампания + вариант шаблона (A/B и повторное сообщение отдельно) — "на данное сообщение".
    variant = "variant"
    # День отправки (когорта): сколько из отправленных в этот день ответили.
    day = "day"
    platform = "platform"


class MessageKind(StrEnum):
    initial = "initial"
    followup = "followup"


class StatsRow(BaseModel):
    # Заполнены только поля ключа выбранной группировки, остальные None.
    campaign_id: UUID | None = None
    campaign_name: str | None = None
    kind: MessageKind | None = None
    template_variant: str | None = None
    day: date | None = None
    platform: str | None = None

    # Все попытки отправки (sent + failed + pending), без ручных ответов из окна "Кому написали".
    attempted: int
    sent: int
    failed: int
    pending: int
    leads: int
    # Ответ лида засчитывается одному сообщению — последнему отправленному до этого ответа
    # (см. app/crud/stats.py), так что первое сообщение и повтор не делят один ответ на двоих.
    replied: int
    replied_in_window: int
    reply_rate: float | None
    reply_rate_in_window: float | None
    median_reply_hours: float | None


class StatsOut(BaseModel):
    group_by: StatsGroupBy
    reply_window_hours: int
    tz: str
    date_from: date | None
    date_to: date | None
    rows: list[StatsRow]

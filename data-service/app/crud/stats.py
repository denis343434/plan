"""Статистика рассылок: сколько отправлено и сколько ответили (в т.ч. за первые N часов).

Считается прямо из messages/leads/campaigns — отдельного хранилища под статистику нет, всё
нужное и так пишут messaging-service (tasks.py/followup.py — отправка, inbox.py — ответы).

Что считается "сообщением кампании": всё, у чего есть template_variant. Ручные ответы из окна
"Кому написали" (messaging-service/app/reply.py) пишутся с template_variant=None — это уже
переписка, а не попытка привлечь лида, и в статистику не попадают. Повторное сообщение
отличается зарезервированным вариантом FOLLOWUP_VARIANT.

Атрибуция ответа: inbox.py проверяет переписку целиком и помечает replied ВСЕ ещё не отвеченные
сообщения лида — и первое, и повтор. Если считать reply_status как есть, один ответ засчитался
бы обоим. Поэтому берём момент первого ответа лида (min replied_at по любым его сообщениям) и
отдаём его последнему отправленному сообщению кампании, ушедшему не позже этого момента.

replied_at — момент, когда проверка входящих НАШЛА ответ (inbox.py), а не когда лид его
написал: если "Проверить ответы" запускать редко, "ответили за 24 часа" будет занижено.
"""

from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas.stats import StatsGroupBy

# Должен совпадать с messaging-service/app/templating.py::FOLLOWUP_VARIANT.
FOLLOWUP_VARIANT = "F"

# Колонки ключа группировки: (выражение в SELECT, выражение в GROUP BY/ORDER BY). Только из
# этого словаря — в SQL ничего от пользователя напрямую не подставляется.
_GROUPINGS: dict[StatsGroupBy, list[tuple[str, str]]] = {
    StatsGroupBy.total: [],
    StatsGroupBy.campaign: [
        ("o.campaign_id AS campaign_id", "o.campaign_id"),
        ("c.name AS campaign_name", "c.name"),
    ],
    StatsGroupBy.variant: [
        ("o.campaign_id AS campaign_id", "o.campaign_id"),
        ("c.name AS campaign_name", "c.name"),
        ("o.kind AS kind", "o.kind"),
        ("o.template_variant AS template_variant", "o.template_variant"),
    ],
    StatsGroupBy.day: [
        ("timezone(:tz, timezone('UTC', o.sent_at))::date AS day", "1"),
    ],
    StatsGroupBy.platform: [
        ("o.platform AS platform", "o.platform"),
    ],
}

_ORDER_BY: dict[StatsGroupBy, str] = {
    StatsGroupBy.total: "",
    StatsGroupBy.campaign: "ORDER BY c.name NULLS LAST, o.campaign_id",
    StatsGroupBy.variant: "ORDER BY c.name NULLS LAST, o.campaign_id, o.kind DESC, o.template_variant",
    StatsGroupBy.day: "ORDER BY 1 DESC",
    StatsGroupBy.platform: "ORDER BY o.platform",
}


def is_valid_timezone(db: Session, tz: str) -> bool:
    """Имя пояса проверяет сам Postgres — не зависим от tzdata в образе data-service."""
    return db.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = :tz)"), {"tz": tz}
    ).scalar_one()


def message_stats(
    db: Session,
    group_by: StatsGroupBy,
    campaign_id: UUID | None = None,
    platform: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    reply_window_hours: int = 24,
    tz: str = "UTC",
) -> list[dict]:
    """Строки статистики по выбранной группировке. Даты периода — по дню отправки в поясе tz,
    обе границы включительно. sent_at/replied_at в БД — naive UTC (now() сервера Postgres)."""
    filters = ["m.template_variant IS NOT NULL"]
    params: dict = {
        "followup_variant": FOLLOWUP_VARIANT,
        "window_hours": reply_window_hours,
        "tz": tz,
    }
    if campaign_id is not None:
        filters.append("l.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if platform is not None:
        filters.append("l.platform = :platform")
        params["platform"] = platform
    # Границы периода переводим в UTC, а не sent_at в местное время — так фильтр остаётся
    # по голой колонке и может идти по ix_messages_sent_at.
    if date_from is not None:
        filters.append("m.sent_at >= timezone('UTC', timezone(:tz, CAST(:date_from AS timestamp)))")
        params["date_from"] = date_from
    if date_to is not None:
        filters.append(
            "m.sent_at < timezone('UTC', timezone(:tz, CAST(:date_to AS timestamp) + interval '1 day'))"
        )
        params["date_to"] = date_to

    grouping = _GROUPINGS[group_by]
    key_select = "".join(f"{expr}, " for expr, _ in grouping)
    group_clause = ("GROUP BY " + ", ".join(g for _, g in grouping)) if grouping else ""

    sql = f"""
        WITH outbound AS (
            SELECT m.id, m.lead_id, m.template_variant, m.sent_at, m.delivery_status,
                   CASE WHEN m.template_variant = :followup_variant THEN 'followup' ELSE 'initial' END AS kind,
                   l.campaign_id, l.platform
            FROM messages m
            JOIN leads l ON l.id = m.lead_id
            WHERE {' AND '.join(filters)}
        ),
        first_reply AS (
            SELECT r.lead_id, min(r.replied_at) AS reply_at
            FROM messages r
            WHERE r.reply_status = 'replied' AND r.replied_at IS NOT NULL
              AND r.lead_id IN (SELECT lead_id FROM outbound)
            GROUP BY r.lead_id
        ),
        attributed AS (
            -- Без фильтров периода/кампании: ответ принадлежит последнему отправленному
            -- сообщению кампании вообще, а не последнему из попавших в выборку.
            SELECT DISTINCT ON (s.lead_id) s.id AS message_id, fr.reply_at
            FROM messages s
            JOIN first_reply fr ON fr.lead_id = s.lead_id AND s.sent_at <= fr.reply_at
            WHERE s.delivery_status = 'sent' AND s.template_variant IS NOT NULL
            ORDER BY s.lead_id, s.sent_at DESC
        )
        SELECT {key_select}
            count(*) AS attempted,
            count(*) FILTER (WHERE o.delivery_status = 'sent') AS sent,
            count(*) FILTER (WHERE o.delivery_status = 'failed') AS failed,
            count(*) FILTER (WHERE o.delivery_status = 'pending') AS pending,
            count(DISTINCT o.lead_id) AS leads,
            count(a.message_id) AS replied,
            count(a.message_id) FILTER (
                WHERE a.reply_at - o.sent_at <= make_interval(hours => CAST(:window_hours AS integer))
            ) AS replied_in_window,
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY extract(epoch FROM a.reply_at - o.sent_at) / 3600.0
            ) FILTER (WHERE a.message_id IS NOT NULL) AS median_reply_hours
        FROM outbound o
        LEFT JOIN attributed a ON a.message_id = o.id
        LEFT JOIN campaigns c ON c.id = o.campaign_id
        {group_clause}
        {_ORDER_BY[group_by]}
    """

    rows = []
    for row in db.execute(text(sql), params).mappings():
        item = dict(row)
        sent = item["sent"]
        item["reply_rate"] = round(item["replied"] / sent, 4) if sent else None
        item["reply_rate_in_window"] = round(item["replied_in_window"] / sent, 4) if sent else None
        if item["median_reply_hours"] is not None:
            item["median_reply_hours"] = round(float(item["median_reply_hours"]), 2)
        rows.append(item)
    # Агрегат без GROUP BY на пустой выборке всё равно отдаёт одну строку с нулями — это и
    # нужно для total; для остальных группировок пустая выборка = пустой список.
    return rows

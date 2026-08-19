from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.message import Message
from app.schemas.message import MessageCreate


def create_message(db: Session, message: MessageCreate) -> Message:
    db_message = Message(**message.model_dump())
    db.add(db_message)
    db.commit()
    db.refresh(db_message)
    return db_message


def list_messages(
    db: Session, lead_id: UUID | None = None, account_id: UUID | None = None
) -> list[Message]:
    stmt = select(Message)
    if lead_id is not None:
        stmt = stmt.where(Message.lead_id == lead_id)
    if account_id is not None:
        stmt = stmt.where(Message.account_id == account_id)
    stmt = stmt.order_by(Message.sent_at)
    return list(db.execute(stmt).scalars().all())


def usage_counts(db: Session, account_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
    """Return {account_id: (hourly_used, daily_used)} — sliding-window counts from `messages`."""
    if not account_ids:
        return {}

    hour_ago = func.now() - text("interval '1 hour'")
    day_ago = func.now() - text("interval '1 day'")

    stmt = (
        select(
            Message.account_id,
            func.count().filter(Message.sent_at >= hour_ago).label("hourly"),
            func.count().filter(Message.sent_at >= day_ago).label("daily"),
        )
        .where(Message.account_id.in_(account_ids))
        .group_by(Message.account_id)
    )

    counted = {row.account_id: (row.hourly, row.daily) for row in db.execute(stmt)}
    return {aid: counted.get(aid, (0, 0)) for aid in account_ids}

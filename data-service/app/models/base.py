from uuid import UUID, uuid4

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDPK:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

"""add parse_params to campaigns

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Параметры парсинга, специфичные для источника (город/каналы связи для Яндекс Карт) —
    # JSON, а не отдельные колонки: у VK/Instagram их нет вовсе, а набор у новых источников
    # будет свой.
    op.add_column('campaigns', sa.Column('parse_params', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('campaigns', 'parse_params')

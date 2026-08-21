"""add chat_href to leads

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('leads', sa.Column('chat_href', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('leads', 'chat_href')

"""drop chat_href from leads (chat_href caching reverted)

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('leads', 'chat_href')


def downgrade() -> None:
    op.add_column('leads', sa.Column('chat_href', sa.String(), nullable=True))

"""add reply_preview/replied_at to messages

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('messages', sa.Column('reply_preview', sa.String(), nullable=True))
    op.add_column('messages', sa.Column('replied_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('messages', 'replied_at')
    op.drop_column('messages', 'reply_preview')

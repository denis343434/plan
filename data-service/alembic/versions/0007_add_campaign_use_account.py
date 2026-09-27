"""add use_account flag to campaigns

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Переключатель режима парсера для кампании: True (по умолчанию, совпадает с прежним
    # единственным поведением) — парсинг через сохранённую сессию аккаунта, False — анонимным
    # браузером без логина (см. запрос пользователя про режимы "в аккаунте"/"без").
    op.add_column(
        'campaigns',
        sa.Column('use_account', sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column('campaigns', 'use_account')

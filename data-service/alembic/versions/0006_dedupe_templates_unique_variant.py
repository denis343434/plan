"""dedupe templates and add unique(campaign_id, variant)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Раньше POST /templates всегда вставлял новую строку, даже для уже существующего варианта
    # A/B кампании — desktop-client бил голый POST на каждый клик "Сохранить шаблон", живые
    # кампании накопили по несколько одинаковых строк на один и тот же (campaign_id, variant).
    # Оставляем самую раннюю (min id) из каждой такой группы, остальные удаляем, прежде чем
    # добавить ограничение ниже. NULL campaign_id намеренно не трогаем — уникальность NULL'ов
    # UNIQUE-констрейнт и так не проверяет (в Postgres каждый NULL отличен от любого другого).
    op.execute(
        """
        DELETE FROM templates a
        USING templates b
        WHERE a.campaign_id IS NOT NULL
          AND a.campaign_id = b.campaign_id
          AND a.variant = b.variant
          AND a.id > b.id
        """
    )
    op.create_unique_constraint(
        'uq_templates_campaign_id_variant', 'templates', ['campaign_id', 'variant']
    )


def downgrade() -> None:
    op.drop_constraint('uq_templates_campaign_id_variant', 'templates', type_='unique')

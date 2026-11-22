"""add indexes for message stats

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16

"""
from typing import Sequence, Union

from alembic import op

revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # GET /stats джойнит messages -> leads и ищет первый ответ лида (см. app/crud/stats.py),
    # а followup.py/десктоп-клиент и так выбирают сообщения по lead_id — до этого у messages был
    # только индекс (account_id, sent_at), и всё это шло полным сканированием.
    op.create_index('ix_messages_lead_id_sent_at', 'messages', ['lead_id', 'sent_at'])
    # Фильтр статистики по периоду без account_id.
    op.create_index('ix_messages_sent_at', 'messages', ['sent_at'])
    # Существующий ix_leads_status_platform_campaign начинается со status — фильтру
    # "только эта кампания" он не помогает.
    op.create_index('ix_leads_campaign_id', 'leads', ['campaign_id'])


def downgrade() -> None:
    op.drop_index('ix_leads_campaign_id', table_name='leads')
    op.drop_index('ix_messages_sent_at', table_name='messages')
    op.drop_index('ix_messages_lead_id_sent_at', table_name='messages')

"""order recovery and cash-flow de-duplication

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('bot_orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meta', sa.JSON(), nullable=True))
        batch_op.create_index(batch_op.f('ix_bot_orders_status'), ['status'], unique=False)

    with op.batch_alter_table('bot_state', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cashflow_seen', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('bot_state', schema=None) as batch_op:
        batch_op.drop_column('cashflow_seen')

    with op.batch_alter_table('bot_orders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bot_orders_status'))
        batch_op.drop_column('meta')

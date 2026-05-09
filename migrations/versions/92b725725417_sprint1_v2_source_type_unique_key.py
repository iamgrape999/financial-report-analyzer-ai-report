"""sprint1_v2_source_type_unique_key

Revision ID: 92b725725417
Revises: c6735dbb75a2
Create Date: 2026-05-09 10:48:37.770321

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '92b725725417'
down_revision: Union[str, None] = 'c6735dbb75a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite requires batch mode for constraint changes (copy-and-move strategy)
    with op.batch_alter_table('canonical_facts', schema=None) as batch_op:
        batch_op.drop_constraint('uq_fact_key', type_='unique')
        batch_op.create_unique_constraint(
            'uq_fact_key',
            ['report_id', 'metric_name', 'entity', 'period', 'source_type']
        )


def downgrade() -> None:
    with op.batch_alter_table('canonical_facts', schema=None) as batch_op:
        batch_op.drop_constraint('uq_fact_key', type_='unique')
        batch_op.create_unique_constraint(
            'uq_fact_key',
            ['report_id', 'metric_name', 'entity', 'period']
        )

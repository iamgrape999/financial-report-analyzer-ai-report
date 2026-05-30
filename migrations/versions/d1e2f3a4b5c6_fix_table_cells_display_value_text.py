"""fix table_cells display_value to Text

Revision ID: d1e2f3a4b5c6
Revises: 6a097af08fe2
Create Date: 2026-05-14 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = '6a097af08fe2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use batch_alter_table so this is safe on SQLite (which lacks ALTER COLUMN
    # TYPE) as well as PostgreSQL.  On SQLite alembic recreates the table; on
    # PostgreSQL it emits a plain ALTER COLUMN.
    with op.batch_alter_table('table_cells') as batch_op:
        batch_op.alter_column(
            'display_value',
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('table_cells') as batch_op:
        batch_op.alter_column(
            'display_value',
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=True,
        )

"""fix_table_cells_column_id_text

Widen table_cells.column_id and row_id from VARCHAR(100/255) to TEXT so that
any pre-existing rows with long header-derived column_ids are preserved, and
future rows (now always "col_NNN" / "row_NNN") fit in any string column.

Revision ID: e5f6a7b8c9d0
Revises: d1e2f3a4b5c6
Create Date: 2026-05-15

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("table_cells") as batch_op:
        batch_op.alter_column(
            "column_id",
            existing_type=sa.String(length=100),
            type_=sa.Text(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "row_id",
            existing_type=sa.String(length=100),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("table_cells") as batch_op:
        batch_op.alter_column(
            "column_id",
            existing_type=sa.Text(),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "row_id",
            existing_type=sa.Text(),
            type_=sa.String(length=100),
            existing_nullable=False,
        )

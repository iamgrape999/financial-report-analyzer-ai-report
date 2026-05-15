"""fix_table_cells_all_text_columns

Widen table_cells.display_value, column_id, and row_id to TEXT.

display_value: production DB may still have VARCHAR(255) from old schema —
  AI-generated cell values can easily exceed 255 chars.
column_id / row_id: old builder stored raw header text (could be > 100 chars);
  new builder uses col_NNN / row_NNN (7 chars) but production needs widening
  to accept any existing data.

Note: main.py _safe_add_columns also runs these ALTERs on startup so the fix
is applied even without running alembic upgrade.

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
            "display_value",
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )
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
            "display_value",
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
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

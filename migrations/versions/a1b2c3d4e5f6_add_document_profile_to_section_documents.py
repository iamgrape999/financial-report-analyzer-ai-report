"""add document_profile and missing section_documents columns

Revision ID: a1b2c3d4e5f6
Revises: f6a7b8c9d0e1
Create Date: 2026-05-30

Adds document_profile (JSON) and the three columns that were present in the
SectionDocument model but never included in any prior migration:
document_type, file_format, etl_status.

Each add_column is wrapped in a try/except so the migration is idempotent on
existing deployments that bootstrapped via SQLAlchemy create_all().
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("document_type", sa.String(length=50), {"nullable": True}),
    ("file_format",   sa.String(length=10),  {"nullable": True}),
    ("etl_status",    sa.String(length=30),  {"nullable": True}),
    ("document_profile", sa.JSON(),          {"nullable": True}),
]


def upgrade() -> None:
    with op.batch_alter_table("section_documents") as batch_op:
        for col_name, col_type, kwargs in _COLUMNS:
            try:
                batch_op.add_column(sa.Column(col_name, col_type, **kwargs))
            except Exception:
                pass  # column already exists on deployments that used create_all


def downgrade() -> None:
    with op.batch_alter_table("section_documents") as batch_op:
        for col_name, _, _ in reversed(_COLUMNS):
            try:
                batch_op.drop_column(col_name)
            except Exception:
                pass

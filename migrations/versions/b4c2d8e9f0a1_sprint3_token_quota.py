"""sprint3_token_quota

Revision ID: b4c2d8e9f0a1
Revises: a3b1c2d4e5f6
Create Date: 2026-05-09 12:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4c2d8e9f0a1"
down_revision: Union[str, None] = "a3b1c2d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_token_quotas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("quota_date", sa.Date(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "quota_date", name="uq_user_quota_date"),
    )


def downgrade() -> None:
    op.drop_table("user_token_quotas")

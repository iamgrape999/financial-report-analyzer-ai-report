"""merge sprint3 and table_cells text fixes

Revision ID: 696edb29ea98
Revises: b4c2d8e9f0a1, e5f6a7b8c9d0
Create Date: 2026-05-30 03:12:21.666193

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '696edb29ea98'
down_revision: Union[str, None] = ('b4c2d8e9f0a1', 'e5f6a7b8c9d0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

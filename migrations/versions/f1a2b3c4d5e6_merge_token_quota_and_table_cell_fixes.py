"""merge token quota and table cell fix branches

Revision ID: f1a2b3c4d5e6
Revises: b4c2d8e9f0a1, e5f6a7b8c9d0
Create Date: 2026-05-30

"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, tuple[str, str], None] = ("b4c2d8e9f0a1", "e5f6a7b8c9d0")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge migration branches; schema changes are in parent revisions."""
    pass


def downgrade() -> None:
    """Downgrade is handled by the parent branch revisions."""
    pass

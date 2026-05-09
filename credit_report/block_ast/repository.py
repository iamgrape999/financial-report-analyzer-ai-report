from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.block_ast.models import BlockVersion, ReportBlock, TableCell


class BlockOptimisticLockError(Exception):
    pass


async def save_blocks(
    db: AsyncSession,
    blocks: list[dict],
    cells: list[dict],
) -> None:
    """Persist Block AST dicts to DB (upsert by block_id)."""
    for bd in blocks:
        existing = await db.get(ReportBlock, bd["id"])
        if existing:
            # Update existing block
            for k, v in bd.items():
                if k != "id":
                    setattr(existing, k, v)
        else:
            db.add(ReportBlock(**bd))

    for cd in cells:
        db.add(TableCell(**cd))


async def get_block(db: AsyncSession, block_id: str) -> Optional[ReportBlock]:
    result = await db.execute(select(ReportBlock).where(ReportBlock.id == block_id))
    return result.scalar_one_or_none()


async def get_blocks_for_section(
    db: AsyncSession, report_id: str, section_no: int
) -> list[ReportBlock]:
    result = await db.execute(
        select(ReportBlock)
        .where(ReportBlock.report_id == report_id, ReportBlock.section_no == section_no)
        .order_by(ReportBlock.id)
    )
    return list(result.scalars().all())


async def get_all_blocks(db: AsyncSession, report_id: str) -> list[ReportBlock]:
    result = await db.execute(
        select(ReportBlock)
        .where(ReportBlock.report_id == report_id)
        .order_by(ReportBlock.section_no, ReportBlock.id)
    )
    return list(result.scalars().all())


async def get_block_cells(db: AsyncSession, block_id: str) -> list[TableCell]:
    result = await db.execute(
        select(TableCell).where(TableCell.block_id == block_id)
    )
    return list(result.scalars().all())


async def update_block_content(
    db: AsyncSession,
    block_id: str,
    new_content: str,
    actor_id: str,
    reason: Optional[str],
    expected_version: int,
) -> ReportBlock:
    block = await get_block(db, block_id)
    if not block:
        raise ValueError(f"Block {block_id} not found")
    if block.version != expected_version:
        raise BlockOptimisticLockError(
            f"Block {block_id} version mismatch: expected {expected_version}, got {block.version}"
        )
    # Snapshot
    snap = BlockVersion(
        id=str(uuid.uuid4()),
        block_id=block.id,
        version=block.version,
        content=block.content,
        edited_by=actor_id,
        reason=reason,
    )
    db.add(snap)
    block.content = new_content
    block.version += 1
    block.last_edited_by = actor_id
    block.validation_status = "pending"
    return block


async def get_block_history(db: AsyncSession, block_id: str) -> list[BlockVersion]:
    result = await db.execute(
        select(BlockVersion)
        .where(BlockVersion.block_id == block_id)
        .order_by(BlockVersion.version)
    )
    return list(result.scalars().all())


async def mark_section_blocks_stale(db: AsyncSession, report_id: str, section_no: int) -> None:
    blocks = await get_blocks_for_section(db, report_id, section_no)
    for b in blocks:
        b.is_stale = True

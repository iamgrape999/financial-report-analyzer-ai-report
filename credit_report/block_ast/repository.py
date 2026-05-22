from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import delete as sql_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.block_ast.models import BlockVersion, ReportBlock, TableCell


class BlockOptimisticLockError(Exception):
    pass


async def save_blocks(
    db: AsyncSession,
    blocks: list[dict],
    cells: list[dict],
) -> None:
    """Persist Block AST dicts to DB (delete-then-insert by section).

    Deletes all existing blocks for the section before inserting fresh ones.
    This prevents orphaned blocks from previous generations that had a different
    block count or structure — the root cause of 100+ block duplication.
    """
    if not blocks:
        return

    report_id = blocks[0]["report_id"]
    section_no = blocks[0]["section_no"]

    # Find all existing blocks for this section
    old_ids_result = await db.execute(
        select(ReportBlock.id).where(
            ReportBlock.report_id == report_id,
            ReportBlock.section_no == section_no,
        )
    )
    old_ids = list(old_ids_result.scalars())
    if old_ids:
        # Delete dependents first (no DB-level cascade on these FKs)
        await db.execute(sql_delete(TableCell).where(TableCell.block_id.in_(old_ids)))
        await db.execute(sql_delete(BlockVersion).where(BlockVersion.block_id.in_(old_ids)))
        await db.execute(sql_delete(ReportBlock).where(ReportBlock.id.in_(old_ids)))

    # Insert the fresh block set
    for bd in blocks:
        db.add(ReportBlock(**bd))

    # Flush so ReportBlock PKs exist before FK-constrained TableCell inserts
    await db.flush()

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


async def mark_blocks_stale_by_fact(db: AsyncSession, fact_id: str) -> None:
    """Mark all ReportBlock rows stale whose TableCells are bound to the given fact."""
    result = await db.execute(
        select(TableCell.block_id).where(TableCell.fact_id == fact_id).distinct()
    )
    block_ids = [row[0] for row in result.all()]
    for block_id in block_ids:
        block = await db.get(ReportBlock, block_id)
        if block:
            block.is_stale = True

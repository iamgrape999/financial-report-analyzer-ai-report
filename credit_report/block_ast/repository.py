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
    """Persist Block AST dicts to DB (upsert by block_id).

    Cells are replaced atomically: existing cells for any block being re-processed
    are deleted before inserting the fresh set. This prevents duplicate TableCells
    when a section is regenerated.
    """
    for bd in blocks:
        existing = await db.get(ReportBlock, bd["id"])
        if existing:
            # Update existing block
            for k, v in bd.items():
                if k != "id":
                    setattr(existing, k, v)
        else:
            db.add(ReportBlock(**bd))

    # Flush so new ReportBlock rows exist in DB before FK-constrained TableCell inserts
    await db.flush()

    # Delete stale cells before inserting new ones (prevents duplicates on regeneration)
    block_ids_with_cells = {c["block_id"] for c in cells}
    if block_ids_with_cells:
        await db.execute(
            sql_delete(TableCell).where(TableCell.block_id.in_(block_ids_with_cells))
        )

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
        select(TableCell.block_id).where(TableCell.bound_fact_id == fact_id).distinct()
    )
    block_ids = [row[0] for row in result.all()]
    for block_id in block_ids:
        block = await db.get(ReportBlock, block_id)
        if block:
            block.is_stale = True

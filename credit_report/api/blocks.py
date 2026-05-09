from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.database import get_db
from credit_report.block_ast import repository as block_repo
from credit_report.block_ast.models import ReportBlock, TableCell
from credit_report.block_ast.repository import BlockOptimisticLockError
from credit_report.security.auth import get_current_user

router = APIRouter(prefix="/reports/{report_id}", tags=["blocks"])


class CellOut(BaseModel):
    id: str
    row_id: str
    column_id: str
    display_value: Optional[str]
    numeric_value: Optional[float]
    fact_id: Optional[str]
    binding_status: str
    version: int

    model_config = {"from_attributes": True}


class BlockOut(BaseModel):
    id: str
    section_no: int
    block_type: str
    content: Optional[str]
    columns_json: Optional[str]
    source_fact_ids: Optional[str]
    validation_status: str
    is_stale: bool
    version: int
    last_edited_by: Optional[str]

    model_config = {"from_attributes": True}


class BlockPatchIn(BaseModel):
    content: str
    reason: Optional[str] = None
    expected_version: int


class BlockHistoryOut(BaseModel):
    id: str
    version: int
    content: Optional[str]
    edited_by: Optional[str]
    reason: Optional[str]

    model_config = {"from_attributes": True}


@router.get("/blocks", response_model=list[BlockOut])
async def list_blocks(
    report_id: str,
    stale_only: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    blocks = await block_repo.get_all_blocks(db, report_id)
    if stale_only:
        blocks = [b for b in blocks if b.is_stale]
    return blocks


@router.get("/blocks/{block_id}", response_model=BlockOut)
async def get_block(
    report_id: str,
    block_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    block = await block_repo.get_block(db, block_id)
    if not block or block.report_id != report_id:
        raise HTTPException(status_code=404, detail="Block not found")
    return block


@router.patch("/blocks/{block_id}", response_model=BlockOut)
async def patch_block(
    report_id: str,
    block_id: str,
    payload: BlockPatchIn,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    block = await block_repo.get_block(db, block_id)
    if not block or block.report_id != report_id:
        raise HTTPException(status_code=404, detail="Block not found")
    try:
        updated = await block_repo.update_block_content(
            db, block_id, payload.content, current_user.id,
            payload.reason, payload.expected_version,
        )
        await db.commit()
        await db.refresh(updated)
        return updated
    except BlockOptimisticLockError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/blocks/{block_id}/history", response_model=list[BlockHistoryOut])
async def block_history(
    report_id: str,
    block_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    block = await block_repo.get_block(db, block_id)
    if not block or block.report_id != report_id:
        raise HTTPException(status_code=404, detail="Block not found")
    return await block_repo.get_block_history(db, block_id)


@router.get("/blocks/{block_id}/cells", response_model=list[CellOut])
async def list_cells(
    report_id: str,
    block_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    block = await block_repo.get_block(db, block_id)
    if not block or block.report_id != report_id:
        raise HTTPException(status_code=404, detail="Block not found")
    return await block_repo.get_block_cells(db, block_id)


@router.get("/sections/{section_no}/blocks", response_model=list[BlockOut])
async def section_blocks(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return await block_repo.get_blocks_for_section(db, report_id, section_no)

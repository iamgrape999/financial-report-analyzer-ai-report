from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.database import get_db
from credit_report.block_ast import repository as block_repo
from credit_report.block_ast.models import ReportBlock, TableCell
from credit_report.block_ast.repository import BlockOptimisticLockError
from credit_report.fact_store import repository as fact_repo
from credit_report.generation.claude_client import call_gemini_raw
from credit_report.security.auth import get_current_user

logger = logging.getLogger(__name__)

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


class BlockImproveIn(BaseModel):
    instruction: str
    expected_version: int


class BlockImproveOut(BaseModel):
    block_id: str
    current_version: int
    original_content: str
    suggested_content: str


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


@router.post("/blocks/{block_id}/improve", response_model=BlockImproveOut)
async def improve_block(
    report_id: str,
    block_id: str,
    payload: BlockImproveIn,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """AI-assisted paragraph improvement. Returns a suggestion; does NOT apply it."""
    block = await block_repo.get_block(db, block_id)
    if not block or block.report_id != report_id:
        raise HTTPException(status_code=404, detail="Block not found")
    if not block.content:
        raise HTTPException(status_code=400, detail="Block has no content to improve")

    facts_context = ""
    try:
        bound_ids = json.loads(block.source_fact_ids or "[]")
        if bound_ids:
            all_facts = await fact_repo.get_facts_for_report(db, report_id)
            bound_facts = [f for f in all_facts if f.id in bound_ids]
            if bound_facts:
                lines = [
                    f"- {f.metric_name} ({f.entity or ''} {f.period or ''}): "
                    f"{f.value_text or f.value} {f.currency or ''} {f.unit or ''}".strip()
                    for f in bound_facts
                ]
                facts_context = "BOUND FACTS (must be preserved exactly):\n" + "\n".join(lines)
    except Exception as _fe:
        logger.warning("improve_block: failed to load facts block=%s: %s", block_id, _fe)

    system_prompt = (
        "You are a senior credit analyst at an international commercial bank. "
        "Rewrite the provided paragraph following the analyst's instruction precisely. "
        "Rules:\n"
        "- Preserve ALL numbers, percentages, dates, and entity names exactly as given\n"
        "- Return ONLY the rewritten Markdown — no explanation, no preamble\n"
        "- Match the original length and formality level unless instructed otherwise\n"
        "- Do not add facts or figures not present in the original"
    )
    user_prompt = (
        f"ORIGINAL PARAGRAPH:\n{block.content}\n\n"
        + (f"{facts_context}\n\n" if facts_context else "")
        + f"ANALYST INSTRUCTION: {payload.instruction}\n\n"
        + "Return the improved paragraph as Markdown only."
    )

    try:
        suggested = await call_gemini_raw(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=min(len(block.content) * 3 + 512, 4096),
        )
    except Exception as e:
        logger.error("improve_block: LLM call failed block=%s: %s", block_id, e)
        raise HTTPException(status_code=503, detail=f"AI generation failed: {e}")

    logger.info(
        "improve_block: block=%s section=%d user=%s instruction_len=%d",
        block_id, block.section_no, current_user.id, len(payload.instruction),
    )
    return BlockImproveOut(
        block_id=block_id,
        current_version=block.version,
        original_content=block.content,
        suggested_content=suggested,
    )

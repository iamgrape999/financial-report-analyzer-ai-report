from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.block_ast import repository as block_repo
from credit_report.block_ast.models import ReportBlock, TableCell
from credit_report.block_ast.repository import BlockOptimisticLockError
from credit_report.fact_store import repository as fact_repo
from credit_report.generation.claude_client import call_gemini_raw
from credit_report.models import Report
from credit_report.security.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/{report_id}", tags=["blocks"])


async def _get_report_or_403(db: AsyncSession, report_id: str, current_user) -> Report:
    """Fetch report and enforce ownership (admin sees all, analyst sees own only)."""
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if current_user.role != "admin" and report.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return report


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
    instruction: str = Field(min_length=1, description="Non-empty improvement instruction")
    expected_version: Optional[int] = None

    @field_validator("instruction")
    @classmethod
    def instruction_not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("instruction must not be blank or whitespace-only")
        return v


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


@router.get("/blocks/stats")
async def block_stats(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return data-quality summary: block validation counts and cell binding rate."""
    from sqlalchemy import func, select as sa_select

    blocks = await block_repo.get_all_blocks(db, report_id)

    cell_result = await db.execute(
        sa_select(TableCell.binding_status, func.count().label("n"))
        .join(ReportBlock, TableCell.block_id == ReportBlock.id)
        .where(ReportBlock.report_id == report_id)
        .group_by(TableCell.binding_status)
    )
    cell_counts = {row.binding_status: row.n for row in cell_result}
    bound = cell_counts.get("bound", 0)
    unbound = cell_counts.get("unbound", 0)
    total_cells = bound + unbound

    pending = sum(1 for b in blocks if b.validation_status == "pending")
    passed = sum(1 for b in blocks if b.validation_status == "passed")
    failed = sum(1 for b in blocks if b.validation_status == "failed")
    stale = sum(1 for b in blocks if b.is_stale)

    return {
        "total_blocks": len(blocks),
        "pending": pending,
        "passed": passed,
        "failed": failed,
        "stale": stale,
        "total_cells": total_cells,
        "bound_cells": bound,
        "unbound_cells": unbound,
        "binding_rate_pct": round(bound / total_cells * 100) if total_cells else 0,
    }


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
    await _get_report_or_403(db, report_id, current_user)
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


@router.post("/blocks/{block_id}/validate")
async def validate_block(
    report_id: str,
    block_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Mark a block's validation_status as 'passed'."""
    block = await block_repo.get_block(db, block_id)
    if not block or block.report_id != report_id:
        raise HTTPException(status_code=404, detail="Block not found")
    block.validation_status = "passed"
    await db.commit()
    await db.refresh(block)
    await write_event(
        db,
        action="block.validated",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="block",
        target_id=block_id,
        after="validation_status=passed",
    )
    logger.info("validate_block: block=%s user=%s", block_id, current_user.id)
    return {"block_id": block_id, "validation_status": "passed", "version": block.version}


@router.post("/blocks/{block_id}/improve", response_model=BlockImproveOut)
async def improve_block(
    report_id: str,
    block_id: str,
    payload: BlockImproveIn,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """AI-assisted paragraph improvement. Returns a suggestion; does NOT apply it."""
    await _get_report_or_403(db, report_id, current_user)
    block = await block_repo.get_block(db, block_id)
    if not block or block.report_id != report_id:
        raise HTTPException(status_code=404, detail="Block not found")
    if not block.content or not block.content.strip():
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

    if not suggested:
        raise HTTPException(status_code=503, detail="AI returned an empty suggestion — please retry")

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

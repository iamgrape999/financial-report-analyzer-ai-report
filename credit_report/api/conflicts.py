from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.fact_store import repository as repo
from credit_report.fact_store.models import FactConflict
from credit_report.schemas import ConflictResponse, ResolveConflictRequest
from credit_report.security.auth import get_current_user, require_analyst
from credit_report.security.models import User

router = APIRouter(prefix="/reports/{report_id}/facts/conflicts", tags=["conflicts"])


@router.get("", response_model=list[ConflictResponse])
async def list_conflicts(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await repo.get_open_conflicts(db, report_id)


@router.get("/{conflict_id}", response_model=ConflictResponse)
async def get_conflict(
    report_id: str,
    conflict_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(FactConflict).where(
            FactConflict.id == conflict_id,
            FactConflict.report_id == report_id,
        )
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")
    return conflict


@router.post("/{conflict_id}/resolve", response_model=ConflictResponse)
async def resolve_conflict(
    report_id: str,
    conflict_id: str,
    payload: ResolveConflictRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    result = await db.execute(
        select(FactConflict).where(
            FactConflict.id == conflict_id,
            FactConflict.report_id == report_id,
        )
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")
    if conflict.status != "open":
        raise HTTPException(status_code=400, detail=f"Conflict is already '{conflict.status}'")

    resolved = await repo.resolve_conflict(
        db,
        conflict_id=conflict_id,
        chosen_fact_id=payload.chosen_fact_id,
        rejected_fact_ids=payload.rejected_fact_ids,
        resolution_reason=payload.resolution_reason,
        resolved_by=current_user.id,
    )

    await write_event(
        db,
        action="conflict.resolve",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="conflict",
        target_id=conflict_id,
        after=f"chosen={payload.chosen_fact_id}",
        reason=payload.resolution_reason,
    )
    return resolved


@router.post("/{conflict_id}/mark-unresolved")
async def mark_unresolved(
    report_id: str,
    conflict_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    result = await db.execute(
        select(FactConflict).where(
            FactConflict.id == conflict_id,
            FactConflict.report_id == report_id,
        )
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")

    conflict.status = "open"
    conflict.chosen_fact_id = None
    conflict.resolution_reason = None
    await write_event(
        db,
        action="conflict.mark_unresolved",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="conflict",
        target_id=conflict_id,
    )
    return {"status": "open", "conflict_id": conflict_id}

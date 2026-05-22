from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.fact_store import repository as repo
from credit_report.fact_store.models import CanonicalFact, FactConflict
from credit_report.models import Report
from credit_report.schemas import ConflictResponse, ResolveConflictRequest
from credit_report.security.auth import get_current_user, require_analyst
from credit_report.security.models import User


class MarkUnresolvedResponse(BaseModel):
    status: str
    conflict_id: str

router = APIRouter(prefix="/reports/{report_id}/facts/conflicts", tags=["conflicts"])


async def _assert_conflict_report_access(
    db: AsyncSession, report_id: str, current_user: User
) -> None:
    """Raise 404/403 if the caller does not own the report (admin exempt)."""
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if not report or report.is_deleted:
        raise HTTPException(status_code=404, detail="Report not found")
    if current_user.role != "admin" and report.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("", response_model=list[ConflictResponse])
async def list_conflicts(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_conflict_report_access(db, report_id, current_user)
    return await repo.get_open_conflicts(db, report_id)


@router.get("/{conflict_id}", response_model=ConflictResponse)
async def get_conflict(
    report_id: str,
    conflict_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_conflict_report_access(db, report_id, current_user)
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


@router.post("/{conflict_id}/mark-unresolved", response_model=MarkUnresolvedResponse)
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

    # When unresolving a previously resolved conflict, restore the involved facts
    # back to "conflicted" so the conflict can be re-resolved. Without this,
    # the chosen fact stays "approved" (can only → deprecated) and rejected facts
    # stay "deprecated" (terminal), making the conflict unresolvable again.
    if conflict.status == "resolved":
        for fid in (conflict.fact_a_id, conflict.fact_b_id):
            if not fid:
                continue
            fr = await db.execute(select(CanonicalFact).where(CanonicalFact.id == fid))
            fact = fr.scalar_one_or_none()
            if fact and fact.state in ("approved", "deprecated"):
                fact.state = "conflicted"
                fact.version += 1

    conflict.status = "open"
    conflict.chosen_fact_id = None
    conflict.resolution_reason = None
    conflict.resolved_by = None
    conflict.resolved_at = None
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

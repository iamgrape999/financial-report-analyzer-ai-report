from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import AuditEvent
from credit_report.audit.schemas import AuditEventSchema, AuditListResponse
from credit_report.database import get_db
from credit_report.models import Report
from credit_report.security.auth import get_current_user, require_reviewer
from credit_report.security.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/{report_id}/audit", tags=["audit"])

# Global audit router — admin-only endpoint to browse all events (not scoped to a report)
global_router = APIRouter(prefix="/audit", tags=["audit"])


async def _assert_audit_access(db: AsyncSession, report_id: str, current_user: User) -> None:
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if not report or report.is_deleted:
        raise HTTPException(status_code=404, detail="Report not found")
    if current_user.role != "admin" and report.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("", response_model=AuditListResponse)
async def get_audit_trail(
    report_id: str,
    skip: int = Query(default=0, ge=0, le=2_147_483_647),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_audit_access(db, report_id, current_user)
    result = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.report_id == report_id)
        .order_by(AuditEvent.timestamp.desc())
        .offset(skip)
        .limit(limit)
    )
    events = list(result.scalars().all())

    count_result = await db.execute(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.report_id == report_id)
    )
    total = count_result.scalar_one()

    logger.debug("get_audit_trail: report=%s total=%d page_events=%d user=%s", report_id, total, len(events), current_user.id)
    return AuditListResponse(
        events=[AuditEventSchema.model_validate(e) for e in events],
        total=total,
        page=skip // limit + 1 if limit else 1,
        page_size=limit,
    )


@global_router.get("/events", response_model=AuditListResponse)
async def get_global_audit_events(
    page_size: int = Query(default=50, ge=1, le=500),
    page: int = Query(default=1, ge=1, le=2_147_483_647),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Admin-only: browse all audit events across every report (newest first)."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    skip = (page - 1) * page_size
    result = await db.execute(
        select(AuditEvent)
        .order_by(AuditEvent.timestamp.desc())
        .offset(skip)
        .limit(page_size)
    )
    events = list(result.scalars().all())
    count_result = await db.execute(select(func.count()).select_from(AuditEvent))
    total = count_result.scalar_one()
    return AuditListResponse(
        events=[AuditEventSchema.model_validate(e) for e in events],
        total=total,
        page=page,
        page_size=page_size,
    )

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import AuditEvent
from credit_report.audit.schemas import AuditEventSchema, AuditListResponse
from credit_report.database import get_db
from credit_report.security.auth import get_current_user, require_reviewer
from credit_report.security.models import User

router = APIRouter(prefix="/reports/{report_id}/audit", tags=["audit"])


@router.get("", response_model=AuditListResponse)
async def get_audit_trail(
    report_id: str,
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.report_id == report_id)
        .order_by(AuditEvent.timestamp.desc())
        .offset(skip)
        .limit(limit)
    )
    events = list(result.scalars().all())

    count_result = await db.execute(
        select(AuditEvent).where(AuditEvent.report_id == report_id)
    )
    total = len(list(count_result.scalars().all()))

    return AuditListResponse(
        events=[AuditEventSchema.model_validate(e) for e in events],
        total=total,
        page=skip // limit + 1 if limit else 1,
        page_size=limit,
    )

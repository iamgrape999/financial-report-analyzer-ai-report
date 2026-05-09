from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from credit_report.database import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    target_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    before: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    after: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


async def write_event(
    db: AsyncSession,
    action: str,
    actor_user_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    report_id: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    before: Optional[Any] = None,
    after: Optional[Any] = None,
    reason: Optional[str] = None,
    extra: Optional[Any] = None,
) -> AuditEvent:
    event = AuditEvent(
        id=str(uuid.uuid4()),
        report_id=report_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=str(before) if before is not None else None,
        after=str(after) if after is not None else None,
        reason=reason,
        extra=str(extra) if extra is not None else None,
    )
    db.add(event)
    return event

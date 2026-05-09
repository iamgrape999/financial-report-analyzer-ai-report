from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from credit_report.database import Base


class UserTokenQuota(Base):
    """Tracks per-user daily Gemini token consumption for quota enforcement."""

    __tablename__ = "user_token_quotas"
    __table_args__ = (UniqueConstraint("user_id", "quota_date", name="uq_user_quota_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    quota_date: Mapped[date] = mapped_column(Date, nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SectionDocument(Base):
    """Metadata for a PDF document uploaded to a report for evidence retrieval."""

    __tablename__ = "section_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

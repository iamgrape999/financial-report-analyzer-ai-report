"""Top-level report and section models (DB + Pydantic schemas)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from credit_report.database import Base


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    industry: Mapped[str] = mapped_column(String(30), nullable=False, default="marine")
    report_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    borrower_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    booking_branch: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<Report {self.id} {self.industry} [{self.status}]>"


class SectionInput(Base):
    """Stores analyst JSON input verbatim per section."""
    __tablename__ = "section_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    section_no: Mapped[int] = mapped_column(Integer, nullable=False)
    input_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    saved_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SectionOutput(Base):
    """Stores generated Markdown output per section."""
    __tablename__ = "section_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    section_no: Mapped[int] = mapped_column(Integer, nullable=False)
    markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    model_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

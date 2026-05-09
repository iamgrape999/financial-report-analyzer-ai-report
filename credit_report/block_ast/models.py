from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from credit_report.database import Base


class ReportBlock(Base):
    """A logical unit of generated report content (paragraph, table, heading, chart)."""
    __tablename__ = "report_blocks"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)  # e.g. "7.C1.balance_sheet_table"
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    section_no: Mapped[int] = mapped_column(Integer, nullable=False)
    block_type: Mapped[str] = mapped_column(String(20), nullable=False)  # paragraph|table|heading|list|chart_image
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # raw Markdown content
    columns_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: [{column_id, label, type}]
    source_fact_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|passed|failed
    is_stale: Mapped[bool] = mapped_column(nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_edited_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    cells: Mapped[list["TableCell"]] = relationship("TableCell", back_populates="block", cascade="all, delete-orphan")
    versions: Mapped[list["BlockVersion"]] = relationship("BlockVersion", back_populates="block", cascade="all, delete-orphan")


class BlockVersion(Base):
    """Snapshot of a block before rewrite."""
    __tablename__ = "block_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    block_id: Mapped[str] = mapped_column(String(100), ForeignKey("report_blocks.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    edited_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    block: Mapped["ReportBlock"] = relationship("ReportBlock", back_populates="versions")


class TableCell(Base):
    """A single cell in a table block, with optional fact binding."""
    __tablename__ = "table_cells"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    block_id: Mapped[str] = mapped_column(String(100), ForeignKey("report_blocks.id"), nullable=False, index=True)
    row_id: Mapped[str] = mapped_column(String(100), nullable=False)
    column_id: Mapped[str] = mapped_column(String(100), nullable=False)
    display_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    numeric_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    fact_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # FK to canonical_facts (soft ref)
    binding_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unbound")  # bound|unbound
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    block: Mapped["ReportBlock"] = relationship("ReportBlock", back_populates="cells")

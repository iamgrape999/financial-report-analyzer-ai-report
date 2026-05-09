from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from credit_report.database import Base


class FXRate(Base):
    """Exchange rate used in a report — stored for audit lineage."""
    __tablename__ = "fx_rates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    from_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    rate_date: Mapped[str] = mapped_column(String(20), nullable=False)  # "YYYY-MM-DD"
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="internal_bank_rate_table")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_stale: Mapped[bool] = mapped_column(nullable=False, default=False)


class MappingRule(Base):
    """Approved mapping from source line item label to canonical metric."""
    __tablename__ = "mapping_rules"
    __table_args__ = (
        UniqueConstraint("report_id", "source_label", name="uq_mapping_rule"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_label: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_metric: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # income_statement|balance_sheet|cash_flow
    approved_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|approved|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class UnmappedLineItem(Base):
    """Line item from PDF/input that could not be auto-mapped."""
    __tablename__ = "unmapped_line_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_label: Mapped[str] = mapped_column(String(255), nullable=False)
    source_section: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_document_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    sample_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|mapped|skipped
    mapping_rule_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("mapping_rules.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CalculationResult(Base):
    """Stored output of a calculation with formula lineage."""
    __tablename__ = "calculation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    formula: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # human-readable formula
    input_fact_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array of fact_ids
    is_stale: Mapped[bool] = mapped_column(nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

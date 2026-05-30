from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
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


ETL_STATUS_VALUES = (
    "uploaded",
    "page_scanning",
    "page_scan_done",
    "etl_planned",
    "extracting",
    "low_coverage_failed",
    "partial_ready",
    "ready_for_review",
    "committed",
    "error",
)


class SectionDocument(Base):
    """Metadata for a document uploaded to a report for evidence retrieval and ETL."""

    __tablename__ = "section_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # document_type: annual_report | financial_statement | analyst_presentation |
    #   interim_report | valuation_report | charter_agreement | shipbuilding_contract |
    #   kyc_document | legal_document | external_report | other
    document_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="other")
    file_format: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # pdf|docx|pptx|txt|jpg|png
    etl_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default="uploaded")
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DocumentPage(Base):
    """Page-level manifest for full-document ETL coverage and citations."""

    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "pdf_page_no", name="uq_document_page_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    pdf_page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_page_start: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    printed_page_end: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    native_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vlm_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    merged_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    layout_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    processing_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default="processed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DocumentBlock(Base):
    """Paragraph/table/figure block extracted from a page, with evidence coordinates when available."""

    __tablename__ = "document_blocks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    block_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bbox: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    extraction_method: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    section_hint: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


class ExtractedTable(Base):
    """Raw and normalized table capture for section-specific financial extractors."""

    __tablename__ = "extracted_tables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    page_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    table_type: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    periods: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    raw_cells: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    normalized_rows: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    extraction_method: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class CandidateFact(Base):
    """Evidence-bound fact candidate produced before Smart Import commit."""

    __tablename__ = "candidate_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    section_no: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    metric_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    entity: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    period: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    value_numeric: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    value_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    scale: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    source_page_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_block_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    source_table_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    validation_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)


class SectionImportProposal(Base):
    """Reviewable Smart Import payload with evidence and coverage metadata."""

    __tablename__ = "section_import_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    section_no: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    proposed_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    evidence_map: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    coverage_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    missing_required_fields: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default="ready_for_review")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

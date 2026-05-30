"""page first document etl

Revision ID: f6a7b8c9d0e1
Revises: f1a2b3c4d5e6
Create Date: 2026-05-30 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.alter_column("section_documents", "etl_status", type_=sa.String(length=30), existing_type=sa.String(length=20))
    except Exception:
        pass

    op.create_table(
        "document_pages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("document_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("report_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("pdf_page_no", sa.Integer(), nullable=False),
        sa.Column("printed_page_start", sa.String(length=20), nullable=True),
        sa.Column("printed_page_end", sa.String(length=20), nullable=True),
        sa.Column("native_text", sa.Text(), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("vlm_text", sa.Text(), nullable=True),
        sa.Column("merged_text", sa.Text(), nullable=True),
        sa.Column("text_quality_score", sa.Float(), nullable=True),
        sa.Column("layout_type", sa.String(length=50), nullable=True),
        sa.Column("processing_status", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "pdf_page_no", name="uq_document_page_no"),
    )
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"])
    op.create_index("ix_document_pages_report_id", "document_pages", ["report_id"])

    op.create_table(
        "document_blocks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("page_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("block_type", sa.String(length=30), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("extraction_method", sa.String(length=30), nullable=True),
        sa.Column("section_hint", sa.String(length=100), nullable=True),
    )
    op.create_index("ix_document_blocks_page_id", "document_blocks", ["page_id"])

    op.create_table(
        "extracted_tables",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("document_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("page_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("table_type", sa.String(length=80), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("periods", sa.JSON(), nullable=True),
        sa.Column("raw_cells", sa.JSON(), nullable=True),
        sa.Column("normalized_rows", sa.JSON(), nullable=True),
        sa.Column("extraction_method", sa.String(length=30), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
    )
    op.create_index("ix_extracted_tables_document_id", "extracted_tables", ["document_id"])
    op.create_index("ix_extracted_tables_page_id", "extracted_tables", ["page_id"])

    op.create_table(
        "candidate_facts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("report_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("document_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("section_no", sa.Integer(), nullable=False, index=True),
        sa.Column("metric_key", sa.String(length=100), nullable=True),
        sa.Column("entity", sa.String(length=100), nullable=True),
        sa.Column("period", sa.String(length=30), nullable=True),
        sa.Column("value_numeric", sa.Numeric(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("unit", sa.String(length=30), nullable=True),
        sa.Column("scale", sa.String(length=30), nullable=True),
        sa.Column("source_page_no", sa.Integer(), nullable=True),
        sa.Column("source_block_id", sa.String(length=36), nullable=True),
        sa.Column("source_table_id", sa.String(length=36), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("validation_status", sa.String(length=30), nullable=True),
    )
    op.create_index("ix_candidate_facts_report_id", "candidate_facts", ["report_id"])
    op.create_index("ix_candidate_facts_document_id", "candidate_facts", ["document_id"])
    op.create_index("ix_candidate_facts_section_no", "candidate_facts", ["section_no"])

    op.create_table(
        "section_import_proposals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("report_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("section_no", sa.Integer(), nullable=False, index=True),
        sa.Column("proposed_json", sa.JSON(), nullable=True),
        sa.Column("evidence_map", sa.JSON(), nullable=True),
        sa.Column("coverage_score", sa.Float(), nullable=True),
        sa.Column("missing_required_fields", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_section_import_proposals_report_id", "section_import_proposals", ["report_id"])
    op.create_index("ix_section_import_proposals_section_no", "section_import_proposals", ["section_no"])


def downgrade() -> None:
    op.drop_index("ix_section_import_proposals_section_no", table_name="section_import_proposals")
    op.drop_index("ix_section_import_proposals_report_id", table_name="section_import_proposals")
    op.drop_table("section_import_proposals")
    op.drop_index("ix_candidate_facts_section_no", table_name="candidate_facts")
    op.drop_index("ix_candidate_facts_document_id", table_name="candidate_facts")
    op.drop_index("ix_candidate_facts_report_id", table_name="candidate_facts")
    op.drop_table("candidate_facts")
    op.drop_index("ix_extracted_tables_page_id", table_name="extracted_tables")
    op.drop_index("ix_extracted_tables_document_id", table_name="extracted_tables")
    op.drop_table("extracted_tables")
    op.drop_index("ix_document_blocks_page_id", table_name="document_blocks")
    op.drop_table("document_blocks")
    op.drop_index("ix_document_pages_report_id", table_name="document_pages")
    op.drop_index("ix_document_pages_document_id", table_name="document_pages")
    op.drop_table("document_pages")

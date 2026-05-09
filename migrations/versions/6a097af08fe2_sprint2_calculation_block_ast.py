"""sprint2_calculation_block_ast

Revision ID: 6a097af08fe2
Revises: 92b725725417
Create Date: 2026-05-09 10:53:43.694400

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '6a097af08fe2'
down_revision: Union[str, None] = '92b725725417'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('calculation_results',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('metric_name', sa.String(length=100), nullable=False),
    sa.Column('entity', sa.String(length=50), nullable=False),
    sa.Column('period', sa.String(length=20), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('value_text', sa.String(length=255), nullable=True),
    sa.Column('formula', sa.Text(), nullable=True),
    sa.Column('input_fact_ids', sa.Text(), nullable=True),
    sa.Column('is_stale', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_calculation_results_report_id'), 'calculation_results', ['report_id'], unique=False)
    op.create_table('fx_rates',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('from_currency', sa.String(length=10), nullable=False),
    sa.Column('to_currency', sa.String(length=10), nullable=False),
    sa.Column('rate', sa.Float(), nullable=False),
    sa.Column('rate_date', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('is_stale', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_fx_rates_report_id'), 'fx_rates', ['report_id'], unique=False)
    op.create_table('mapping_rules',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('source_label', sa.String(length=255), nullable=False),
    sa.Column('canonical_metric', sa.String(length=100), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=True),
    sa.Column('approved_by', sa.String(length=36), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('report_id', 'source_label', name='uq_mapping_rule')
    )
    op.create_index(op.f('ix_mapping_rules_report_id'), 'mapping_rules', ['report_id'], unique=False)
    op.create_table('report_blocks',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('section_no', sa.Integer(), nullable=False),
    sa.Column('block_type', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('columns_json', sa.Text(), nullable=True),
    sa.Column('source_fact_ids', sa.Text(), nullable=True),
    sa.Column('validation_status', sa.String(length=20), nullable=False),
    sa.Column('is_stale', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('last_edited_by', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_report_blocks_report_id'), 'report_blocks', ['report_id'], unique=False)
    op.create_table('block_versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('block_id', sa.String(length=100), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('edited_by', sa.String(length=36), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['block_id'], ['report_blocks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_block_versions_block_id'), 'block_versions', ['block_id'], unique=False)
    op.create_table('table_cells',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('block_id', sa.String(length=100), nullable=False),
    sa.Column('row_id', sa.String(length=100), nullable=False),
    sa.Column('column_id', sa.String(length=100), nullable=False),
    sa.Column('display_value', sa.String(length=255), nullable=True),
    sa.Column('numeric_value', sa.Float(), nullable=True),
    sa.Column('fact_id', sa.String(length=36), nullable=True),
    sa.Column('binding_status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['block_id'], ['report_blocks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_table_cells_block_id'), 'table_cells', ['block_id'], unique=False)
    op.create_table('unmapped_line_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('source_label', sa.String(length=255), nullable=False),
    sa.Column('source_section', sa.Integer(), nullable=True),
    sa.Column('source_document_id', sa.String(length=36), nullable=True),
    sa.Column('sample_value', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('mapping_rule_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['mapping_rule_id'], ['mapping_rules.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_unmapped_line_items_report_id'), 'unmapped_line_items', ['report_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_unmapped_line_items_report_id'), table_name='unmapped_line_items')
    op.drop_table('unmapped_line_items')
    op.drop_index(op.f('ix_table_cells_block_id'), table_name='table_cells')
    op.drop_table('table_cells')
    op.drop_index(op.f('ix_block_versions_block_id'), table_name='block_versions')
    op.drop_table('block_versions')
    op.drop_index(op.f('ix_report_blocks_report_id'), table_name='report_blocks')
    op.drop_table('report_blocks')
    op.drop_index(op.f('ix_mapping_rules_report_id'), table_name='mapping_rules')
    op.drop_table('mapping_rules')
    op.drop_index(op.f('ix_fx_rates_report_id'), table_name='fx_rates')
    op.drop_table('fx_rates')
    op.drop_index(op.f('ix_calculation_results_report_id'), table_name='calculation_results')
    op.drop_table('calculation_results')

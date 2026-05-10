"""sprint1_fact_store_rbac_audit

Revision ID: c6735dbb75a2
Revises: 
Create Date: 2026-05-09 10:43:14.010397

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c6735dbb75a2'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=True),
    sa.Column('actor_user_id', sa.String(length=36), nullable=True),
    sa.Column('actor_role', sa.String(length=20), nullable=True),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('target_type', sa.String(length=30), nullable=True),
    sa.Column('target_id', sa.String(length=100), nullable=True),
    sa.Column('before', sa.Text(), nullable=True),
    sa.Column('after', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('extra', sa.Text(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_events_action'), 'audit_events', ['action'], unique=False)
    op.create_index(op.f('ix_audit_events_report_id'), 'audit_events', ['report_id'], unique=False)
    op.create_table('canonical_facts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('metric_name', sa.String(length=100), nullable=False),
    sa.Column('entity', sa.String(length=50), nullable=False),
    sa.Column('period', sa.String(length=20), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('value_text', sa.String(length=255), nullable=True),
    sa.Column('currency', sa.String(length=10), nullable=True),
    sa.Column('unit', sa.String(length=20), nullable=True),
    sa.Column('display', sa.String(length=255), nullable=True),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('source_type', sa.String(length=30), nullable=False),
    sa.Column('source_priority', sa.Integer(), nullable=False),
    sa.Column('source_evidence_id', sa.String(length=36), nullable=True),
    sa.Column('source_section_no', sa.Integer(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('last_edited_by', sa.String(length=36), nullable=True),
    sa.Column('override_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('report_id', 'metric_name', 'entity', 'period', name='uq_fact_key')
    )
    op.create_index(op.f('ix_canonical_facts_report_id'), 'canonical_facts', ['report_id'], unique=False)
    op.create_table('reports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('industry', sa.String(length=30), nullable=False),
    sa.Column('report_type', sa.String(length=30), nullable=True),
    sa.Column('borrower_name', sa.String(length=255), nullable=True),
    sa.Column('booking_branch', sa.String(length=10), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_deleted', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('section_inputs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('section_no', sa.Integer(), nullable=False),
    sa.Column('input_json', sa.Text(), nullable=True),
    sa.Column('saved_by', sa.String(length=36), nullable=True),
    sa.Column('saved_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_section_inputs_report_id'), 'section_inputs', ['report_id'], unique=False)
    op.create_table('section_outputs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('section_no', sa.Integer(), nullable=False),
    sa.Column('markdown', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('model_id', sa.String(length=60), nullable=True),
    sa.Column('tokens_used', sa.Integer(), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_section_outputs_report_id'), 'section_outputs', ['report_id'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_table('fact_conflicts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('metric_name', sa.String(length=100), nullable=False),
    sa.Column('entity', sa.String(length=50), nullable=False),
    sa.Column('period', sa.String(length=20), nullable=False),
    sa.Column('fact_a_id', sa.String(length=36), nullable=False),
    sa.Column('fact_b_id', sa.String(length=36), nullable=False),
    sa.Column('value_a', sa.String(length=255), nullable=True),
    sa.Column('value_b', sa.String(length=255), nullable=True),
    sa.Column('source_a', sa.String(length=30), nullable=True),
    sa.Column('source_b', sa.String(length=30), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('chosen_fact_id', sa.String(length=36), nullable=True),
    sa.Column('resolution_reason', sa.Text(), nullable=True),
    sa.Column('resolved_by', sa.String(length=36), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.ForeignKeyConstraint(['fact_a_id'], ['canonical_facts.id'], ),
    sa.ForeignKeyConstraint(['fact_b_id'], ['canonical_facts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_fact_conflicts_report_id'), 'fact_conflicts', ['report_id'], unique=False)
    op.create_table('fact_dependencies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('fact_id', sa.String(length=36), nullable=False),
    sa.Column('dependent_type', sa.String(length=20), nullable=False),
    sa.Column('dependent_id', sa.String(length=100), nullable=False),
    sa.Column('is_stale', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['fact_id'], ['canonical_facts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_fact_dependencies_fact_id'), 'fact_dependencies', ['fact_id'], unique=False)
    op.create_table('fact_versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('fact_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('value_text', sa.String(length=255), nullable=True),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('edited_by', sa.String(length=36), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.ForeignKeyConstraint(['fact_id'], ['canonical_facts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_fact_versions_fact_id'), 'fact_versions', ['fact_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_fact_versions_fact_id'), table_name='fact_versions')
    op.drop_table('fact_versions')
    op.drop_index(op.f('ix_fact_dependencies_fact_id'), table_name='fact_dependencies')
    op.drop_table('fact_dependencies')
    op.drop_index(op.f('ix_fact_conflicts_report_id'), table_name='fact_conflicts')
    op.drop_table('fact_conflicts')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_section_outputs_report_id'), table_name='section_outputs')
    op.drop_table('section_outputs')
    op.drop_index(op.f('ix_section_inputs_report_id'), table_name='section_inputs')
    op.drop_table('section_inputs')
    op.drop_table('reports')
    op.drop_index(op.f('ix_canonical_facts_report_id'), table_name='canonical_facts')
    op.drop_table('canonical_facts')
    op.drop_index(op.f('ix_audit_events_report_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_action'), table_name='audit_events')
    op.drop_table('audit_events')

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.calculation_engine.models import MappingRule, UnmappedLineItem


# Well-known auto-mappings for standard financial line item labels
AUTO_MAPPING: dict[str, tuple[str, str]] = {
    # (canonical_metric, category)
    "revenue": ("revenue", "income_statement"),
    "net revenue": ("revenue", "income_statement"),
    "total revenue": ("revenue", "income_statement"),
    "operating profit": ("operating_profit", "income_statement"),
    "ebit": ("ebit", "income_statement"),
    "ebitda": ("ebitda", "income_statement"),
    "operating ebitda": ("op_ebitda", "income_statement"),
    "net income": ("net_income", "income_statement"),
    "profit after tax": ("net_income", "income_statement"),
    "total assets": ("total_assets", "balance_sheet"),
    "total equity": ("total_equity", "balance_sheet"),
    "total debt": ("total_debt", "balance_sheet"),
    "interest-bearing debt": ("interest_bearing_debt", "balance_sheet"),
    "cash and cash equivalents": ("cash", "balance_sheet"),
    "cash balance": ("cash", "balance_sheet"),
    "operating cash flow": ("operating_cash_flow", "cash_flow"),
    "cash from operations": ("operating_cash_flow", "cash_flow"),
    "capex": ("capex", "cash_flow"),
    "capital expenditure": ("capex", "cash_flow"),
    "interest expense": ("interest_expense", "income_statement"),
    "debt service": ("debt_service", "cash_flow"),
}


def auto_classify(label: str) -> Optional[tuple[str, str]]:
    """Return (canonical_metric, category) if label matches known auto-mappings."""
    normalized = label.strip().lower()
    return AUTO_MAPPING.get(normalized)


async def get_approved_rules(db: AsyncSession, report_id: str) -> list[MappingRule]:
    result = await db.execute(
        select(MappingRule).where(
            MappingRule.report_id == report_id,
            MappingRule.status == "approved",
        )
    )
    return list(result.scalars().all())


async def get_pending_rules(db: AsyncSession, report_id: str) -> list[MappingRule]:
    result = await db.execute(
        select(MappingRule).where(
            MappingRule.report_id == report_id,
            MappingRule.status == "pending",
        )
    )
    return list(result.scalars().all())


async def submit_mapping_rule(
    db: AsyncSession,
    report_id: str,
    source_label: str,
    canonical_metric: str,
    category: Optional[str],
    submitted_by: str,
    notes: Optional[str] = None,
) -> MappingRule:
    rule = MappingRule(
        id=str(uuid.uuid4()),
        report_id=report_id,
        source_label=source_label,
        canonical_metric=canonical_metric,
        category=category,
        status="pending",
        notes=notes,
    )
    db.add(rule)
    return rule


async def approve_mapping_rule(
    db: AsyncSession,
    rule_id: str,
    approved_by: str,
) -> MappingRule:
    result = await db.execute(select(MappingRule).where(MappingRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise ValueError(f"MappingRule {rule_id} not found")
    rule.status = "approved"
    rule.approved_by = approved_by
    rule.approved_at = datetime.now(timezone.utc)
    # Resolve any unmapped items with this label
    items_result = await db.execute(
        select(UnmappedLineItem).where(
            UnmappedLineItem.report_id == rule.report_id,
            UnmappedLineItem.source_label == rule.source_label,
            UnmappedLineItem.status == "pending",
        )
    )
    for item in items_result.scalars().all():
        item.status = "mapped"
        item.mapping_rule_id = rule.id
    return rule


async def queue_unmapped_item(
    db: AsyncSession,
    report_id: str,
    source_label: str,
    source_section: Optional[int] = None,
    sample_value: Optional[float] = None,
) -> UnmappedLineItem:
    """Add a line item to the unmapped queue if not already present."""
    result = await db.execute(
        select(UnmappedLineItem).where(
            UnmappedLineItem.report_id == report_id,
            UnmappedLineItem.source_label == source_label,
            UnmappedLineItem.status == "pending",
        )
    )
    existing = result.scalars().first()
    if existing:
        return existing

    item = UnmappedLineItem(
        id=str(uuid.uuid4()),
        report_id=report_id,
        source_label=source_label,
        source_section=source_section,
        sample_value=sample_value,
        status="pending",
    )
    db.add(item)
    return item


async def get_unmapped_queue(db: AsyncSession, report_id: str) -> list[UnmappedLineItem]:
    result = await db.execute(
        select(UnmappedLineItem).where(
            UnmappedLineItem.report_id == report_id,
            UnmappedLineItem.status == "pending",
        )
    )
    return list(result.scalars().all())

"""
Interest-Bearing Debt (IBD) aggregation.

IBD = sum of all interest-bearing facilities (loans, bonds, lease liabilities if mapped).
Analyst or mapping rules control which line items count as interest-bearing.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.calculation_engine.models import CalculationResult, MappingRule
from sqlalchemy import select


INTEREST_BEARING_METRICS = {
    "interest_bearing_debt",
    "total_debt",
    "bank_loans",
    "bonds_payable",
    "lease_liabilities",
    "short_term_borrowings",
    "long_term_borrowings",
}


def is_interest_bearing(canonical_metric: str) -> bool:
    return canonical_metric in INTEREST_BEARING_METRICS


async def calculate_ibd(
    db: AsyncSession,
    report_id: str,
    entity: str,
    period: str,
    line_items: list[dict],  # [{label, value, canonical_metric (from mapping)}]
) -> tuple[float, str, list[str]]:
    """
    Sum all interest-bearing line items.

    Returns (total_ibd, formula_str, input_labels).
    """
    included = [(item["label"], item["value"]) for item in line_items
                if is_interest_bearing(item.get("canonical_metric", ""))]
    total = sum(v for _, v in included)
    parts = " + ".join(f"{label}({v:,.1f})" for label, v in included) or "0"
    formula = f"IBD = {parts} = {total:,.1f}"
    input_labels = [label for label, _ in included]
    return round(total, 4), formula, input_labels


async def store_ibd(
    db: AsyncSession,
    report_id: str,
    entity: str,
    period: str,
    value: float,
    formula: str,
    input_labels: list[str],
) -> CalculationResult:
    result = CalculationResult(
        id=str(uuid.uuid4()),
        report_id=report_id,
        metric_name="interest_bearing_debt",
        entity=entity,
        period=period,
        value=value,
        value_text=f"{value:,.1f}",
        formula=formula,
        input_fact_ids=json.dumps(input_labels),  # stores labels, not fact_ids for this calc
        is_stale=False,
        version=1,
    )
    db.add(result)
    return result

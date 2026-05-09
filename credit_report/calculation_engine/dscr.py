"""
Debt Service Coverage Ratio (DSCR) calculation.

DSCR = Operating Cash Flow / Debt Service (Principal + Interest)

Stores formula lineage in CalculationResult for full auditability.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.calculation_engine.models import CalculationResult


def calculate_dscr(
    operating_cash_flow: float,
    principal_repayment: float,
    interest_expense: float,
    ocf_fact_id: Optional[str] = None,
    principal_fact_id: Optional[str] = None,
    interest_fact_id: Optional[str] = None,
) -> tuple[Optional[float], str, list[str]]:
    """
    Returns (dscr_value, formula_string, input_fact_ids).

    DSCR = OCF / (Principal + Interest)
    If debt_service == 0, DSCR is undefined (returns None).
    """
    debt_service = principal_repayment + interest_expense
    if debt_service == 0:
        formula = (
            f"DSCR = OCF / (Principal + Interest) = {operating_cash_flow:,.1f} / 0 = N/M"
        )
        fact_ids = [fid for fid in [ocf_fact_id, principal_fact_id, interest_fact_id] if fid]
        return None, formula, fact_ids

    dscr = operating_cash_flow / debt_service
    formula = (
        f"DSCR = OCF / (Principal + Interest) = {operating_cash_flow:,.1f} / "
        f"({principal_repayment:,.1f} + {interest_expense:,.1f}) = {dscr:.2f}x"
    )
    fact_ids = [fid for fid in [ocf_fact_id, principal_fact_id, interest_fact_id] if fid]
    return round(dscr, 4), formula, fact_ids


async def store_dscr(
    db: AsyncSession,
    report_id: str,
    entity: str,
    period: str,
    dscr_value: Optional[float],
    formula: str,
    input_fact_ids: list[str],
) -> CalculationResult:
    result = CalculationResult(
        id=str(uuid.uuid4()),
        report_id=report_id,
        metric_name="dscr",
        entity=entity,
        period=period,
        value=dscr_value,
        value_text=f"{dscr_value:.2f}x" if dscr_value is not None else "N/M",
        formula=formula,
        input_fact_ids=json.dumps(input_fact_ids),
        is_stale=False,
        version=1,
    )
    db.add(result)
    return result

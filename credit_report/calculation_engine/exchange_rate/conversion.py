from __future__ import annotations

from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.calculation_engine.exchange_rate.rate_table import get_rate


class MissingFXRateError(Exception):
    pass


async def convert(
    db: AsyncSession,
    report_id: str,
    amount: float,
    from_currency: str,
    to_currency: str,
) -> tuple[float, dict]:
    """
    Convert amount from_currency → to_currency using stored report rate.

    Returns (converted_amount, lineage_dict) where lineage_dict contains
    rate, rate_date, source for audit trail.

    Raises MissingFXRateError if no rate is stored for this report+pair.
    """
    if from_currency == to_currency:
        return amount, {"rate": 1.0, "rate_date": "N/A", "source": "identity"}

    rate_obj = await get_rate(db, report_id, from_currency, to_currency)
    if not rate_obj:
        # Try reverse pair
        rate_obj_rev = await get_rate(db, report_id, to_currency, from_currency)
        if rate_obj_rev:
            converted = amount / rate_obj_rev.rate
            lineage = {
                "rate": round(1 / rate_obj_rev.rate, 6),
                "rate_date": rate_obj_rev.rate_date,
                "source": rate_obj_rev.source,
                "note": f"Inverted from {to_currency}/{from_currency} rate {rate_obj_rev.rate}",
            }
            return round(converted, 4), lineage
        raise MissingFXRateError(
            f"No FX rate stored for {from_currency}/{to_currency} in report {report_id}. "
            "Set rate via PUT /api/credit-report/reports/{id}/fx-rates first."
        )

    converted = amount * rate_obj.rate
    lineage = {
        "rate": rate_obj.rate,
        "rate_date": rate_obj.rate_date,
        "source": rate_obj.source,
    }
    return round(converted, 4), lineage


def dual_currency_display(
    original: float,
    original_currency: str,
    converted: float,
    converted_currency: str,
    original_unit: str = "million",
    converted_unit: str = "million",
) -> str:
    """Format dual-currency display string: 'TWD253.4bn / USD8.2bn'."""
    def _fmt(val: float, unit: str) -> str:
        if unit == "billion":
            return f"{val:,.1f}bn"
        elif unit == "million":
            return f"{val:,.1f}m"
        return f"{val:,.2f}"

    return f"{original_currency}{_fmt(original, original_unit)} / {converted_currency}{_fmt(converted, converted_unit)}"

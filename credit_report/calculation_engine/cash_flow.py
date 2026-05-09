"""
Cash flow classification: operating / investing / financing.

Standard classification rules for common line items.
Non-standard items go to unmapped queue for analyst approval.
"""
from __future__ import annotations

from typing import Optional

CF_CATEGORIES: dict[str, str] = {
    # Operating
    "net_income": "operating",
    "ebitda": "operating",
    "operating_profit": "operating",
    "depreciation": "operating",
    "amortization": "operating",
    "working_capital_change": "operating",
    "operating_cash_flow": "operating",
    "cash_from_operations": "operating",
    # Investing
    "capex": "investing",
    "capital_expenditure": "investing",
    "vessel_purchase": "investing",
    "acquisition": "investing",
    "disposal_proceeds": "investing",
    # Financing
    "debt_repayment": "financing",
    "new_borrowings": "financing",
    "dividends_paid": "financing",
    "interest_paid": "financing",
    "lease_repayment": "financing",
    "interest_bearing_debt": "financing",
}


def classify_cash_flow(canonical_metric: str) -> Optional[str]:
    """Return 'operating', 'investing', 'financing', or None if unclassified."""
    return CF_CATEGORIES.get(canonical_metric)


def summarize_cash_flows(
    line_items: list[dict],  # [{label, value, canonical_metric, category}]
) -> dict[str, float]:
    """
    Aggregate line items into operating/investing/financing totals.
    Items with category=None are excluded (requires manual mapping first).
    """
    totals: dict[str, float] = {"operating": 0.0, "investing": 0.0, "financing": 0.0}
    for item in line_items:
        cat = item.get("category") or classify_cash_flow(item.get("canonical_metric", ""))
        if cat in totals:
            totals[cat] += float(item.get("value", 0))
    return totals

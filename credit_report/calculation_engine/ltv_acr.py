"""
LTV / ACR (Asset Cover Ratio) calculations for collateralized vessel loans.

LTV = Loan Outstanding / Asset Value
ACR = Asset Value / Loan Outstanding (inverse of LTV x 100)
Balloon LTV = Final balloon amount / Depreciated asset value at maturity
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class LTVRow:
    year: float          # years post-delivery (0, 0.5, 1, 2, ...)
    loan_outstanding: float  # USD millions
    loan_outstanding_pct: float  # % of original facility
    asset_value_25yr: float      # value under 25yr SLD
    ltv_25yr_pct: float
    asset_value_20yr: float      # value under 20yr SLD
    ltv_20yr_pct: float


def straight_line_depreciation(
    initial_value: float,
    useful_life_years: float,
    residual_value_pct: float = 5.0,
    year: float = 0,
) -> float:
    """Return asset value at <year> years old using SLD."""
    residual = initial_value * residual_value_pct / 100
    annual_dep = (initial_value - residual) / useful_life_years
    value = initial_value - annual_dep * year
    return max(value, residual)


def build_ltv_table(
    facility_amount: float,
    initial_asset_value: float,
    amortization_schedule: list[dict],  # [{year, outstanding_pct}]
    useful_life_25yr: float = 25.0,
    useful_life_20yr: float = 20.0,
    residual_pct: float = 5.0,
) -> list[LTVRow]:
    """
    Build LTV table across all amortization periods.

    amortization_schedule: [{"year": float, "outstanding_pct": float}]
    Returns list of LTVRow (one per schedule row).
    """
    rows = []
    for entry in amortization_schedule:
        yr = float(entry["year"])
        loan_pct = float(entry["outstanding_pct"])
        loan = facility_amount * loan_pct / 100
        val_25 = straight_line_depreciation(initial_asset_value, useful_life_25yr, residual_pct, yr)
        val_20 = straight_line_depreciation(initial_asset_value, useful_life_20yr, residual_pct, yr)
        ltv_25 = (loan / val_25 * 100) if val_25 > 0 else 0
        ltv_20 = (loan / val_20 * 100) if val_20 > 0 else 0
        rows.append(LTVRow(
            year=yr,
            loan_outstanding=round(loan, 2),
            loan_outstanding_pct=loan_pct,
            asset_value_25yr=round(val_25, 2),
            ltv_25yr_pct=round(ltv_25, 1),
            asset_value_20yr=round(val_20, 2),
            ltv_20yr_pct=round(ltv_20, 1),
        ))
    return rows


def acr_from_ltv(ltv_pct: float) -> float:
    """ACR = 100 / LTV x 100 (expressed as %)."""
    if ltv_pct == 0:
        return 0.0
    return round(100 / ltv_pct * 100, 1)


def balloon_ltv_summary(
    balloon_amount: float,
    asset_value_25yr: float,
    asset_value_20yr: float,
) -> dict:
    """Return balloon LTV and ACR under both depreciation assumptions."""
    ltv_25 = (balloon_amount / asset_value_25yr * 100) if asset_value_25yr > 0 else 0
    ltv_20 = (balloon_amount / asset_value_20yr * 100) if asset_value_20yr > 0 else 0
    return {
        "balloon_amount": round(balloon_amount, 2),
        "ltv_25yr_pct": round(ltv_25, 1),
        "acr_25yr_pct": acr_from_ltv(ltv_25),
        "asset_value_25yr": round(asset_value_25yr, 2),
        "ltv_20yr_pct": round(ltv_20, 1),
        "acr_20yr_pct": acr_from_ltv(ltv_20),
        "asset_value_20yr": round(asset_value_20yr, 2),
    }

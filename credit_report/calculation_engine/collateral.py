"""
Collateral calculations: LTC, current LTV/ACR, RG coverage.
"""
from __future__ import annotations

from typing import Optional


def ltc(facility_amount: float, contract_price: float) -> tuple[float, str]:
    """Loan-to-Cost. Returns (pct, formula_str)."""
    if contract_price == 0:
        return 0.0, "LTC: N/M (contract price = 0)"
    val = facility_amount / contract_price * 100
    formula = f"LTC = Facility / Contract Price = {facility_amount:,.2f} / {contract_price:,.2f} = {val:.1f}%"
    return round(val, 2), formula


def current_ltv(
    loan_outstanding: float,
    asset_value: float,
) -> tuple[float, float, str]:
    """
    Current LTV and ACR.
    Returns (ltv_pct, acr_pct, formula_str).
    """
    if asset_value == 0:
        return 0.0, 0.0, "LTV: N/M (asset value = 0)"
    ltv = loan_outstanding / asset_value * 100
    acr = asset_value / loan_outstanding * 100 if loan_outstanding > 0 else 0
    formula = (
        f"LTV = Loan / Asset = {loan_outstanding:,.2f} / {asset_value:,.2f} = {ltv:.1f}% "
        f"| ACR = {acr:.1f}%"
    )
    return round(ltv, 2), round(acr, 2), formula


def rg_coverage(
    rg_amount: float,
    cub_exposure: float,
) -> tuple[Optional[float], str]:
    """
    Refund Guarantee coverage of CUB exposure.
    Returns (coverage_pct, formula_str).
    """
    if cub_exposure == 0:
        return None, "RG Coverage: N/M (CUB exposure = 0)"
    coverage = rg_amount / cub_exposure * 100
    formula = f"RG Coverage = RG Amount / CUB Exposure = {rg_amount:,.2f} / {cub_exposure:,.2f} = {coverage:.0f}%"
    return round(coverage, 1), formula

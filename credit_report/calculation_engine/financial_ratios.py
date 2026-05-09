"""
Core financial ratio calculations.

All functions return (value, formula_str, input_fact_ids).
formula_str is human-readable (e.g. "EBITDA / Interest Expense = 3878 / 86.6 = 44.8x").
input_fact_ids is a list of fact_ids used as inputs (for lineage tracking).
"""
from __future__ import annotations

from typing import Optional


def safe_divide(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """Return numerator/denominator, None if denominator is zero/None."""
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def debt_to_ebitda(
    total_debt: float,
    ebitda: float,
    debt_fact_id: Optional[str] = None,
    ebitda_fact_id: Optional[str] = None,
) -> tuple[Optional[float], str, list[str]]:
    val = safe_divide(total_debt, ebitda)
    formula = f"Total Debt / EBITDA = {total_debt:,.1f} / {ebitda:,.1f}"
    if val is not None:
        formula += f" = {val:.2f}x"
    fact_ids = [fid for fid in [debt_fact_id, ebitda_fact_id] if fid]
    return val, formula, fact_ids


def interest_coverage(
    ebitda: float,
    interest_expense: float,
    ebitda_fact_id: Optional[str] = None,
    interest_fact_id: Optional[str] = None,
) -> tuple[Optional[float], str, list[str]]:
    val = safe_divide(ebitda, interest_expense)
    formula = f"EBITDA / Interest Expense = {ebitda:,.1f} / {interest_expense:,.1f}"
    if val is not None:
        formula += f" = {val:.1f}x"
    fact_ids = [fid for fid in [ebitda_fact_id, interest_fact_id] if fid]
    return val, formula, fact_ids


def net_debt(
    total_debt: float,
    cash: float,
    debt_fact_id: Optional[str] = None,
    cash_fact_id: Optional[str] = None,
) -> tuple[float, str, list[str]]:
    val = total_debt - cash
    formula = f"Total Debt - Cash = {total_debt:,.1f} - {cash:,.1f} = {val:,.1f}"
    if val < 0:
        formula += " (Net Cash)"
    fact_ids = [fid for fid in [debt_fact_id, cash_fact_id] if fid]
    return val, formula, fact_ids


def ebitda_margin(
    ebitda: float,
    revenue: float,
    ebitda_fact_id: Optional[str] = None,
    revenue_fact_id: Optional[str] = None,
) -> tuple[Optional[float], str, list[str]]:
    val = safe_divide(ebitda, revenue)
    formula = f"EBITDA / Revenue = {ebitda:,.1f} / {revenue:,.1f}"
    if val is not None:
        formula += f" = {val * 100:.1f}%"
    fact_ids = [fid for fid in [ebitda_fact_id, revenue_fact_id] if fid]
    return val, formula, fact_ids


def net_margin(
    net_income: float,
    revenue: float,
    ni_fact_id: Optional[str] = None,
    rev_fact_id: Optional[str] = None,
) -> tuple[Optional[float], str, list[str]]:
    val = safe_divide(net_income, revenue)
    formula = f"Net Income / Revenue = {net_income:,.1f} / {revenue:,.1f}"
    if val is not None:
        formula += f" = {val * 100:.1f}%"
    fact_ids = [fid for fid in [ni_fact_id, rev_fact_id] if fid]
    return val, formula, fact_ids


def debt_to_equity(
    total_debt: float,
    total_equity: float,
    debt_fact_id: Optional[str] = None,
    equity_fact_id: Optional[str] = None,
) -> tuple[Optional[float], str, list[str]]:
    val = safe_divide(total_debt, total_equity)
    formula = f"Total Debt / Total Equity = {total_debt:,.1f} / {total_equity:,.1f}"
    if val is not None:
        formula += f" = {val:.2f}x"
    fact_ids = [fid for fid in [debt_fact_id, equity_fact_id] if fid]
    return val, formula, fact_ids

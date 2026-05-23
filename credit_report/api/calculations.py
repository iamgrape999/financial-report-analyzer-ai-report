from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.database import get_db
from credit_report.calculation_engine.models import (
    CalculationResult,
    FXRate,
    MappingRule,
    UnmappedLineItem,
)
from credit_report.calculation_engine.exchange_rate.rate_table import set_rate
from credit_report.calculation_engine.mapping.mapping_rules import (
    approve_mapping_rule,
    get_unmapped_queue,
    get_approved_rules,
    submit_mapping_rule,
)
from credit_report.models import Report
from credit_report.security.auth import get_current_user
from credit_report.security.models import User

logger = logging.getLogger(__name__)


async def _assert_calc_access(db: AsyncSession, report_id: str, current_user: User) -> None:
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if not report or report.is_deleted:
        raise HTTPException(status_code=404, detail="Report not found")
    if current_user.role != "admin" and report.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")


async def get_calc_results_for_prompt(
    db: AsyncSession, report_id: str
) -> list[dict]:
    """Return non-stale CalculationResults formatted for prompt injection into §7."""
    result = await db.execute(
        select(CalculationResult)
        .where(
            CalculationResult.report_id == report_id,
            CalculationResult.is_stale == False,  # noqa: E712
        )
        .order_by(CalculationResult.entity, CalculationResult.period, CalculationResult.metric_name)
    )
    rows = result.scalars().all()
    return [
        {
            "metric": r.metric_name,
            "entity": r.entity,
            "period": r.period,
            "value": round(r.value, 4) if r.value is not None else None,
            "formula": r.formula,
        }
        for r in rows
        if r.value is not None
    ]

router = APIRouter(prefix="/reports/{report_id}", tags=["calculations"])


async def _upsert_calculation(
    db: AsyncSession,
    report_id: str,
    entity: str,
    period: str,
    metric_name: str,
    value: Optional[float],
    formula: str,
    input_fact_ids: list[str],
) -> None:
    """Insert or update a CalculationResult, bumping version on update."""
    existing = await db.execute(
        select(CalculationResult).where(
            CalculationResult.report_id == report_id,
            CalculationResult.entity == entity,
            CalculationResult.period == period,
            CalculationResult.metric_name == metric_name,
        )
    )
    row = existing.scalars().first()
    if row:
        row.value = value
        row.formula = formula
        row.input_fact_ids = json.dumps(input_fact_ids)
        row.is_stale = False
        row.version += 1
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(CalculationResult(
            id=str(uuid.uuid4()),
            report_id=report_id,
            metric_name=metric_name,
            entity=entity,
            period=period,
            value=value,
            value_text=None,
            formula=formula,
            input_fact_ids=json.dumps(input_fact_ids),
            is_stale=False,
            version=1,
        ))


# ── FX Rates ────────────────────────────────────────────────────────────────────────────────

class FXRateIn(BaseModel):
    from_currency: str
    to_currency: str
    rate: float
    rate_date: str  # "YYYY-MM-DD"
    source: str = "internal_bank_rate_table"


class FXRateOut(BaseModel):
    id: str
    from_currency: str
    to_currency: str
    rate: float
    rate_date: str
    source: str
    is_stale: bool

    model_config = {"from_attributes": True}


@router.get("/fx-rates", response_model=list[FXRateOut])
async def list_fx_rates(
    report_id: str,
    include_stale: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_calc_access(db, report_id, current_user)
    q = select(FXRate).where(FXRate.report_id == report_id)
    if not include_stale:
        q = q.where(FXRate.is_stale == False)  # noqa: E712
    result = await db.execute(q.order_by(FXRate.created_at.desc()))
    return list(result.scalars().all())


@router.put("/fx-rates", response_model=FXRateOut)
async def upsert_fx_rate(
    report_id: str,
    payload: FXRateIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_calc_access(db, report_id, current_user)
    rate = await set_rate(
        db, report_id, payload.from_currency, payload.to_currency,
        payload.rate, payload.rate_date, payload.source,
    )
    await db.commit()
    await db.refresh(rate)
    return rate


# ── Calculations ──────────────────────────────────────────────────────────────────────────────

class CalcOut(BaseModel):
    id: str
    metric_name: str
    entity: str
    period: str
    value: Optional[float]
    value_text: Optional[str]
    formula: Optional[str]
    input_fact_ids: Optional[str]
    is_stale: bool
    version: int

    model_config = {"from_attributes": True}


@router.get("/calculations", response_model=list[CalcOut])
async def list_calculations(
    report_id: str,
    stale_only: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_calc_access(db, report_id, current_user)
    q = select(CalculationResult).where(CalculationResult.report_id == report_id)
    if stale_only:
        q = q.where(CalculationResult.is_stale == True)
    result = await db.execute(q)
    return list(result.scalars().all())


async def _run_recalculate_core(db: AsyncSession, report_id: str) -> tuple[int, int]:
    """Core recalculation logic. No commit — caller must commit.

    Returns (computed, ep_pairs) counts.
    """
    from credit_report.fact_store.repository import get_facts_for_report
    from credit_report.calculation_engine.financial_ratios import (
        debt_to_ebitda,
        interest_coverage,
        net_debt,
        ebitda_margin,
        net_margin,
        debt_to_equity,
    )
    from credit_report.calculation_engine.dscr import calculate_dscr

    facts = await get_facts_for_report(db, report_id)
    fact_index: dict[tuple[str, str, str], object] = {}
    for f in facts:
        if f.value is not None and f.entity and f.period:
            fact_index[(f.entity, f.period, f.metric_name)] = f

    ep_pairs = {(f.entity, f.period) for f in facts if f.value is not None and f.entity and f.period}
    computed = 0

    for entity, period in ep_pairs:
        def g(metric, _e=entity, _p=period):
            return fact_index.get((_e, _p, metric))

        calcs: list[tuple] = []
        ebitda_f = g("ebitda")
        debt_f = g("total_debt")
        rev_f = g("revenue")
        eq_f = g("total_equity")
        cash_f = g("cash_and_equivalents")
        ni_f = g("net_income")
        int_f = g("interest_expense")
        cfo_f = g("cash_flow_from_operations")

        if debt_f and ebitda_f:
            val, formula, fids = debt_to_ebitda(debt_f.value, ebitda_f.value, debt_f.id, ebitda_f.id)
            calcs.append(("net_debt_ebitda", val, formula, fids))
        if ebitda_f and rev_f:
            val, formula, fids = ebitda_margin(ebitda_f.value, rev_f.value, ebitda_f.id, rev_f.id)
            calcs.append(("ebitda_margin_pct", val, formula, fids))
        if ni_f and rev_f:
            val, formula, fids = net_margin(ni_f.value, rev_f.value, ni_f.id, rev_f.id)
            calcs.append(("net_margin_pct", val, formula, fids))
        if debt_f and eq_f:
            val, formula, fids = debt_to_equity(debt_f.value, eq_f.value, debt_f.id, eq_f.id)
            calcs.append(("debt_to_equity", val, formula, fids))
        if debt_f and cash_f:
            val, formula, fids = net_debt(debt_f.value, cash_f.value, debt_f.id, cash_f.id)
            calcs.append(("net_debt", val, formula, fids))
        if ebitda_f and int_f:
            val, formula, fids = interest_coverage(ebitda_f.value, int_f.value, ebitda_f.id, int_f.id)
            calcs.append(("interest_coverage", val, formula, fids))
        if cfo_f and int_f:
            val, formula, fids = calculate_dscr(
                cfo_f.value, 0.0, int_f.value, cfo_f.id, None, int_f.id
            )
            calcs.append(("dscr_cfo_based", val, formula, fids))

        for metric_name, val, formula, fids in calcs:
            await _upsert_calculation(db, report_id, entity, period, metric_name, val, formula, fids)
            computed += 1

    return computed, len(ep_pairs)


@router.post("/recalculate")
async def recalculate(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Auto-compute all derivable financial ratios from the report's CanonicalFacts."""
    await _assert_calc_access(db, report_id, current_user)
    computed, ep_pairs = await _run_recalculate_core(db, report_id)
    await db.commit()
    logger.info("recalculate: report=%s ep_pairs=%d computed=%d", report_id, ep_pairs, computed)
    return {"calculations_computed": computed, "entity_period_pairs": ep_pairs}


# ── LTV / ACR table ───────────────────────────────────────────────────────────────────────

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class LTVScheduleEntry(BaseModel):
    year: _FiniteFloat
    outstanding_pct: _FiniteFloat


class LTVACRIn(BaseModel):
    facility_amount: _FiniteFloat
    initial_asset_value: _FiniteFloat
    amortization_schedule: list[LTVScheduleEntry]
    balloon_amount: Optional[_FiniteFloat] = None
    useful_life_25yr: _FiniteFloat = Field(default=25.0, gt=0)
    useful_life_20yr: _FiniteFloat = Field(default=20.0, gt=0)
    residual_pct: _FiniteFloat = 5.0


class LTVRowOut(BaseModel):
    year: float
    loan_outstanding: float
    loan_outstanding_pct: float
    asset_value_25yr: float
    ltv_25yr_pct: float
    asset_value_20yr: float
    ltv_20yr_pct: float


@router.post("/calculations/ltv-acr")
async def compute_ltv_acr(
    report_id: str,
    payload: LTVACRIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Compute LTV / ACR table from facility + depreciation parameters."""
    await _assert_calc_access(db, report_id, current_user)
    from credit_report.calculation_engine.ltv_acr import (
        build_ltv_table,
        balloon_ltv_summary,
    )

    schedule = [{"year": e.year, "outstanding_pct": e.outstanding_pct} for e in payload.amortization_schedule]
    rows = build_ltv_table(
        facility_amount=payload.facility_amount,
        initial_asset_value=payload.initial_asset_value,
        amortization_schedule=schedule,
        useful_life_25yr=payload.useful_life_25yr,
        useful_life_20yr=payload.useful_life_20yr,
        residual_pct=payload.residual_pct,
    )

    rows_out = [
        LTVRowOut(
            year=r.year,
            loan_outstanding=r.loan_outstanding,
            loan_outstanding_pct=r.loan_outstanding_pct,
            asset_value_25yr=r.asset_value_25yr,
            ltv_25yr_pct=r.ltv_25yr_pct,
            asset_value_20yr=r.asset_value_20yr,
            ltv_20yr_pct=r.ltv_20yr_pct,
        )
        for r in rows
    ]

    # Guard against arithmetic overflow producing inf/nan — reject before DB write.
    for row in rows_out:
        for v in (row.loan_outstanding, row.asset_value_25yr, row.ltv_25yr_pct,
                  row.asset_value_20yr, row.ltv_20yr_pct):
            if not math.isfinite(v):
                raise HTTPException(
                    status_code=422,
                    detail="Calculation overflow: input magnitudes produce non-finite LTV values.",
                )

    balloon = None
    if payload.balloon_amount is not None and rows:
        last = rows[-1]
        balloon = balloon_ltv_summary(
            payload.balloon_amount,
            last.asset_value_25yr,
            last.asset_value_20yr,
        )
        if balloon and not all(
            math.isfinite(balloon.get(k, 0.0))
            for k in ("ltv_25yr_pct", "ltv_20yr_pct", "acr_25yr_pct", "acr_20yr_pct")
        ):
            raise HTTPException(
                status_code=422,
                detail="Calculation overflow: balloon inputs produce non-finite values.",
            )

    # ── Persist LTV rows to CalculationResult ────────────────────────────────
    for raw_row in rows:
        try:
            yr_period = f"YR{int(raw_row.year)}"
        except (ValueError, OverflowError):
            raise HTTPException(status_code=422, detail="year value is too large to store.")
        formula_base = (
            f"loan={raw_row.loan_outstanding:.2f} / asset25={raw_row.asset_value_25yr:.2f}"
        )
        await _upsert_calculation(
            db, report_id, "facility", yr_period,
            "ltv_25yr_pct", raw_row.ltv_25yr_pct,
            formula_base, [],
        )
        await _upsert_calculation(
            db, report_id, "facility", yr_period,
            "ltv_20yr_pct", raw_row.ltv_20yr_pct,
            formula_base, [],
        )
    if balloon:
        await _upsert_calculation(
            db, report_id, "facility", "balloon",
            "balloon_ltv_25yr_pct", balloon.get("ltv_25yr_pct"),
            f"balloon={payload.balloon_amount}", [],
        )
        await _upsert_calculation(
            db, report_id, "facility", "balloon",
            "balloon_ltv_20yr_pct", balloon.get("ltv_20yr_pct"),
            f"balloon={payload.balloon_amount}", [],
        )
    await db.commit()
    logger.info(
        "compute_ltv_acr: persisted %d LTV rows%s report=%s",
        len(rows), " + balloon" if balloon else "", report_id,
    )
    # ─────────────────────────────────────────────────────────────────────────

    return {
        "facility_amount": payload.facility_amount,
        "initial_asset_value": payload.initial_asset_value,
        "ltv_table": [r.model_dump() for r in rows_out],
        "balloon_summary": balloon,
    }


# ── Mapping: Unmapped queue ───────────────────────────────────────────────────────────────

class UnmappedOut(BaseModel):
    id: str
    source_label: str
    source_section: Optional[int]
    sample_value: Optional[float]
    status: str

    model_config = {"from_attributes": True}


class MappingRuleIn(BaseModel):
    source_label: str
    canonical_metric: str
    category: Optional[str] = None
    notes: Optional[str] = None


class MappingRuleOut(BaseModel):
    id: str
    source_label: str
    canonical_metric: str
    category: Optional[str]
    status: str
    approved_by: Optional[str]
    notes: Optional[str]

    model_config = {"from_attributes": True}


@router.get("/mapping/unmapped", response_model=list[UnmappedOut])
async def get_unmapped(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_calc_access(db, report_id, current_user)
    return await get_unmapped_queue(db, report_id)


@router.post("/mapping/rules", response_model=MappingRuleOut, status_code=201)
async def create_mapping_rule(
    report_id: str,
    payload: MappingRuleIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_calc_access(db, report_id, current_user)
    rule = await submit_mapping_rule(
        db, report_id, payload.source_label, payload.canonical_metric,
        payload.category, current_user.id, payload.notes,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A mapping rule for this source_label already exists in this report.",
        )
    await db.refresh(rule)
    return rule


@router.get("/mapping/rules", response_model=list[MappingRuleOut])
async def list_mapping_rules(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_calc_access(db, report_id, current_user)
    return await get_approved_rules(db, report_id)


@router.post("/mapping/rules/{rule_id}/approve", response_model=MappingRuleOut)
async def approve_rule(
    report_id: str,
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_calc_access(db, report_id, current_user)
    try:
        rule = await approve_mapping_rule(db, rule_id, current_user.id)
        await db.commit()
        await db.refresh(rule)
        return rule
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

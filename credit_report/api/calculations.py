from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
from credit_report.security.auth import get_current_user

router = APIRouter(prefix="/reports/{report_id}", tags=["calculations"])


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
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    result = await db.execute(
        select(FXRate).where(FXRate.report_id == report_id).order_by(FXRate.created_at.desc())
    )
    return list(result.scalars().all())


@router.put("/fx-rates", response_model=FXRateOut)
async def upsert_fx_rate(
    report_id: str,
    payload: FXRateIn,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
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
    current_user=Depends(get_current_user),
):
    q = select(CalculationResult).where(CalculationResult.report_id == report_id)
    if stale_only:
        q = q.where(CalculationResult.is_stale == True)
    result = await db.execute(q)
    return list(result.scalars().all())


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
    current_user=Depends(get_current_user),
):
    return await get_unmapped_queue(db, report_id)


@router.post("/mapping/rules", response_model=MappingRuleOut, status_code=201)
async def create_mapping_rule(
    report_id: str,
    payload: MappingRuleIn,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    rule = await submit_mapping_rule(
        db, report_id, payload.source_label, payload.canonical_metric,
        payload.category, current_user.id, payload.notes,
    )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/mapping/rules", response_model=list[MappingRuleOut])
async def list_mapping_rules(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return await get_approved_rules(db, report_id)


@router.post("/mapping/rules/{rule_id}/approve", response_model=MappingRuleOut)
async def approve_rule(
    report_id: str,
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        rule = await approve_mapping_rule(db, rule_id, current_user.id)
        await db.commit()
        await db.refresh(rule)
        return rule
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

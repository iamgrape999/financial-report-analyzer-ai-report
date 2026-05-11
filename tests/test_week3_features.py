"""
Week 3 acceptance tests: recalculate engine, LTV/ACR, section YAML configs (§8-10).
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base

import credit_report.calculation_engine.models  # noqa: F401
import credit_report.fact_store.models  # noqa: F401
import credit_report.block_ast.models  # noqa: F401
import credit_report.security.models  # noqa: F401
import credit_report.audit.events  # noqa: F401
import credit_report.models  # noqa: F401

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
REPORT_ID = str(uuid.uuid4())


@pytest_asyncio.fixture(scope="function")
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Section YAML configs §8-10 ────────────────────────────────────────────────

def test_section_8_yaml_loads():
    import yaml
    from pathlib import Path
    path = Path("credit_report/fact_store/fact_mapping_config/marine/section_8.yaml")
    cfg = yaml.safe_load(path.read_text())
    ids = [f["id_template"] for f in cfg["facts"]]
    assert "ACRA-NEW-CHARGES-{entity}-CURRENT" in ids
    assert "BANKING-CUB-TOTAL-USD-{entity}-CURRENT" in ids
    assert len(cfg["facts"]) == 5


def test_section_9_yaml_loads():
    import yaml
    from pathlib import Path
    path = Path("credit_report/fact_store/fact_mapping_config/marine/section_9.yaml")
    cfg = yaml.safe_load(path.read_text())
    ids = [f["id_template"] for f in cfg["facts"]]
    assert "COMPLIANCE-RECOMMENDATION-{entity}-CURRENT" in ids
    assert "COMPLIANCE-OUTSTANDING-ITEMS-{entity}-CURRENT" in ids
    assert len(cfg["facts"]) == 5


def test_section_10_yaml_loads():
    import yaml
    from pathlib import Path
    path = Path("credit_report/fact_store/fact_mapping_config/marine/section_10.yaml")
    cfg = yaml.safe_load(path.read_text())
    ids = [f["id_template"] for f in cfg["facts"]]
    assert "EXPOSURE-APPROVED-GROUP-LIMIT-{entity}-CURRENT" in ids
    assert "STRESS-DSCR-BASE-{entity}-FY2025F" in ids
    assert "STRESS-DSCR-WORSE-{entity}-FY2025F" in ids
    assert len(cfg["facts"]) == 9


def test_section_8_extraction_from_input():
    from credit_report.fact_store.input_extractor import InputFactExtractor
    extractor = InputFactExtractor(8)
    input_json = {
        "8A_acra_banking_charges": {
            "new_charges": False,
            "comments": "No new charges imposed since last review.",
        },
        "8B_banking_changes": {
            "total_facilities": 12,
            "cub_total_usd_m": 213.84,
            "cub_share_pct": 28.5,
        },
    }
    facts = extractor.extract(REPORT_ID, input_json)
    metrics = {f["metric_name"] for f in facts}
    assert "new_banking_charges" in metrics
    assert "cub_total_exposure_usd_m" in metrics
    assert "total_banking_facilities" in metrics
    assert len(facts) >= 3


def test_section_10_extraction_from_input():
    from credit_report.fact_store.input_extractor import InputFactExtractor
    extractor = InputFactExtractor(10)
    input_json = {
        "10A_group_exposure": {
            "approved_group_limit_usd_m": 750,
            "proposed_exposure_usd_m": 213.84,
            "existing_exposure_usd_m": 170.0,
            "total_fleet_teu": 280000,
        },
        "10C_financial_projections": {
            "base_dscr_fy2025": 12.5,
            "worse_dscr_fy2025": 10.0,
            "base_revenue_fy2025": 8500,
            "worse_revenue_fy2025": 6800,
            "reporting_currency": "USD",
            "unit": "million",
            "freight_rate_drop_pct": 30.0,
        },
    }
    facts = extractor.extract(REPORT_ID, input_json)
    metrics = {f["metric_name"] for f in facts}
    assert "approved_group_limit_usd_m" in metrics
    assert "dscr_base_case" in metrics
    assert "dscr_stress_case" in metrics
    # Periods for stress facts must be FY2025F
    base_dscr = next(f for f in facts if f["metric_name"] == "dscr_base_case")
    assert base_dscr["period"] == "FY2025F"


# ── _run_recalculate_core ─────────────────────────────────────────────────────

async def _seed_facts(db, report_id):
    """Insert a minimal set of CanonicalFacts so recalculate has something to compute."""
    from credit_report.fact_store.repository import upsert_facts
    facts = [
        {
            "id": f"{report_id[:8]}-FIN-EBITDA-BORROWER-FY2024",
            "report_id": report_id,
            "metric_name": "ebitda",
            "entity": "BORROWER",
            "period": "FY2024",
            "value": 3878.0,
            "value_text": None,
            "currency": "USD",
            "unit": "million",
            "state": "validated",
            "source_type": "analyst_input_json",
            "source_priority": 1,
            "source_section_no": 7,
        },
        {
            "id": f"{report_id[:8]}-FIN-REVENUE-BORROWER-FY2024",
            "report_id": report_id,
            "metric_name": "revenue",
            "entity": "BORROWER",
            "period": "FY2024",
            "value": 16200.0,
            "value_text": None,
            "currency": "USD",
            "unit": "million",
            "state": "validated",
            "source_type": "analyst_input_json",
            "source_priority": 1,
            "source_section_no": 7,
        },
        {
            "id": f"{report_id[:8]}-FIN-TOTAL-DEBT-BORROWER-FY2024",
            "report_id": report_id,
            "metric_name": "total_debt",
            "entity": "BORROWER",
            "period": "FY2024",
            "value": 2488.0,
            "value_text": None,
            "currency": "USD",
            "unit": "million",
            "state": "validated",
            "source_type": "analyst_input_json",
            "source_priority": 1,
            "source_section_no": 7,
        },
        {
            "id": f"{report_id[:8]}-FIN-NET-INCOME-BORROWER-FY2024",
            "report_id": report_id,
            "metric_name": "net_income",
            "entity": "BORROWER",
            "period": "FY2024",
            "value": 2791.0,
            "value_text": None,
            "currency": "USD",
            "unit": "million",
            "state": "validated",
            "source_type": "analyst_input_json",
            "source_priority": 1,
            "source_section_no": 7,
        },
    ]
    await upsert_facts(db, facts)
    await db.flush()


@pytest.mark.asyncio
async def test_recalculate_core_computes_ratios(db: AsyncSession):
    from credit_report.api.calculations import _run_recalculate_core

    rid = str(uuid.uuid4())
    await _seed_facts(db, rid)

    computed, ep_pairs = await _run_recalculate_core(db, rid)
    assert ep_pairs == 1  # one (BORROWER, FY2024) pair
    assert computed >= 2  # at least ebitda_margin_pct + net_margin_pct


@pytest.mark.asyncio
async def test_recalculate_idempotent(db: AsyncSession):
    """Running recalculate twice should not double-count."""
    from credit_report.api.calculations import _run_recalculate_core
    from credit_report.calculation_engine.models import CalculationResult
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    await _seed_facts(db, rid)

    await _run_recalculate_core(db, rid)
    await db.flush()
    await _run_recalculate_core(db, rid)
    await db.flush()

    result = await db.execute(
        select(CalculationResult).where(CalculationResult.report_id == rid)
    )
    calcs = result.scalars().all()
    # No duplicates — same (entity, period, metric) should yield exactly one row
    keys = [(c.entity, c.period, c.metric_name) for c in calcs]
    assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_recalculate_ebitda_margin_value(db: AsyncSession):
    """ebitda_margin_pct = EBITDA / Revenue = 3878 / 16200 ≈ 0.2394."""
    from credit_report.api.calculations import _run_recalculate_core
    from credit_report.calculation_engine.models import CalculationResult
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    await _seed_facts(db, rid)
    await _run_recalculate_core(db, rid)
    await db.flush()

    result = await db.execute(
        select(CalculationResult).where(
            CalculationResult.report_id == rid,
            CalculationResult.metric_name == "ebitda_margin_pct",
        )
    )
    row = result.scalar_one_or_none()
    assert row is not None
    assert abs(row.value - 3878.0 / 16200.0) < 0.001


@pytest.mark.asyncio
async def test_recalculate_version_bumps_on_update(db: AsyncSession):
    """Second recalculate must bump version on existing CalculationResult rows."""
    from credit_report.api.calculations import _run_recalculate_core
    from credit_report.calculation_engine.models import CalculationResult
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    await _seed_facts(db, rid)
    await _run_recalculate_core(db, rid)
    await db.flush()
    await _run_recalculate_core(db, rid)
    await db.flush()

    result = await db.execute(
        select(CalculationResult).where(
            CalculationResult.report_id == rid,
            CalculationResult.metric_name == "ebitda_margin_pct",
        )
    )
    row = result.scalar_one_or_none()
    assert row is not None
    assert row.version == 2


@pytest.mark.asyncio
async def test_recalculate_empty_report_returns_zero(db: AsyncSession):
    from credit_report.api.calculations import _run_recalculate_core

    computed, ep_pairs = await _run_recalculate_core(db, str(uuid.uuid4()))
    assert computed == 0
    assert ep_pairs == 0


# ── LTV/ACR pure computation ──────────────────────────────────────────────────

def test_ltv_acr_table_first_row_100pct():
    from credit_report.calculation_engine.ltv_acr import build_ltv_table
    schedule = [{"year": 0, "outstanding_pct": 100}]
    rows = build_ltv_table(213.84, 267.30, schedule)
    assert rows[0].loan_outstanding_pct == 100.0
    assert rows[0].loan_outstanding == 213.84


def test_ltv_acr_balloon_summary_values():
    from credit_report.calculation_engine.ltv_acr import balloon_ltv_summary, acr_from_ltv
    summary = balloon_ltv_summary(balloon_amount=74.84, asset_value_25yr=195.7, asset_value_20yr=178.5)
    assert summary["ltv_25yr_pct"] == round(74.84 / 195.7 * 100, 1)
    # acr_from_ltv is applied to the unrounded ltv, not the display-rounded value
    raw_ltv_25 = 74.84 / 195.7 * 100
    assert summary["acr_25yr_pct"] == acr_from_ltv(raw_ltv_25)


def test_ltv_acr_ltv_decreases_as_loan_amortizes():
    from credit_report.calculation_engine.ltv_acr import build_ltv_table
    schedule = [
        {"year": 0, "outstanding_pct": 100},
        {"year": 5, "outstanding_pct": 50},
        {"year": 10, "outstanding_pct": 10},
    ]
    rows = build_ltv_table(200.0, 300.0, schedule)
    ltvs_25 = [r.ltv_25yr_pct for r in rows]
    # LTV should fall over time as loan amortizes faster than asset depreciates
    assert ltvs_25[2] < ltvs_25[0]

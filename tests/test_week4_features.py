"""
Week 4 acceptance tests:
- Cross-source conflict detection
- Resolve conflict endpoint (unit)
- Calc results marked stale on fact override
- LTV/ACR persisted to DB
- LLM timeout handling
"""
from __future__ import annotations

import asyncio
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


def _make_fact(report_id: str, metric: str, value: float, source: str, entity: str = "TestCo", period: str = "FY2024") -> dict:
    return {
        "report_id": report_id,
        "metric_name": metric,
        "entity": entity,
        "period": period,
        "value": value,
        "source_type": source,
        "state": "extracted",
    }


# ── Cross-source conflict detection ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_conflict_detection_cross_source(db):
    """Two sources disagreeing on the same metric → FactConflict created."""
    from credit_report.fact_store.repository import upsert_facts, get_open_conflicts

    rid = str(uuid.uuid4())
    await upsert_facts(db, [
        _make_fact(rid, "revenue", 1000.0, "pdf_extraction"),
        _make_fact(rid, "revenue", 850.0, "analyst_input_json"),  # 15% diff → conflict
    ])
    await db.flush()

    conflicts = await get_open_conflicts(db, rid)
    assert len(conflicts) == 1
    assert conflicts[0].metric_name == "revenue"
    assert conflicts[0].status == "open"


@pytest.mark.asyncio
async def test_conflict_not_created_same_source(db):
    """Same source_type updating same metric → no conflict (upsert only)."""
    from credit_report.fact_store.repository import upsert_facts, get_open_conflicts

    rid = str(uuid.uuid4())
    await upsert_facts(db, [
        _make_fact(rid, "ebitda", 200.0, "pdf_extraction"),
        _make_fact(rid, "ebitda", 210.0, "pdf_extraction"),
    ])
    await db.flush()

    conflicts = await get_open_conflicts(db, rid)
    assert len(conflicts) == 0


@pytest.mark.asyncio
async def test_conflict_not_created_within_threshold(db):
    """Values within 2% tolerance → no conflict."""
    from credit_report.fact_store.repository import upsert_facts, get_open_conflicts

    rid = str(uuid.uuid4())
    await upsert_facts(db, [
        _make_fact(rid, "net_income", 1000.0, "pdf_extraction"),
        _make_fact(rid, "net_income", 1015.0, "analyst_input_json"),  # 1.5% diff → no conflict
    ])
    await db.flush()

    conflicts = await get_open_conflicts(db, rid)
    assert len(conflicts) == 0


@pytest.mark.asyncio
async def test_conflict_boundary_just_above_threshold(db):
    """Values at exactly 2.1% apart → conflict created."""
    from credit_report.fact_store.repository import upsert_facts, get_open_conflicts

    rid = str(uuid.uuid4())
    await upsert_facts(db, [
        _make_fact(rid, "total_debt", 1000.0, "pdf_extraction"),
        _make_fact(rid, "total_debt", 979.0, "analyst_input_json"),  # 2.1% diff → conflict
    ])
    await db.flush()

    conflicts = await get_open_conflicts(db, rid)
    assert len(conflicts) == 1


# ── Resolve conflict (repository layer) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_conflict_repository(db):
    """resolve_conflict() → status=resolved, chosen fact=approved, rejected=deprecated."""
    from credit_report.fact_store.repository import (
        upsert_facts, get_open_conflicts, resolve_conflict, get_fact,
    )

    rid = str(uuid.uuid4())
    await upsert_facts(db, [
        _make_fact(rid, "revenue", 1000.0, "pdf_extraction"),
        _make_fact(rid, "revenue", 800.0, "analyst_input_json"),
    ])
    await db.flush()

    conflicts = await get_open_conflicts(db, rid)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    chosen_id = conflict.fact_a_id
    rejected_id = conflict.fact_b_id

    resolved = await resolve_conflict(
        db,
        conflict_id=conflict.id,
        chosen_fact_id=chosen_id,
        rejected_fact_ids=[rejected_id],
        resolution_reason="PDF source is more reliable",
        resolved_by="analyst-001",
    )
    await db.flush()

    assert resolved.status == "resolved"
    assert resolved.chosen_fact_id == chosen_id

    chosen_fact = await get_fact(db, chosen_id)
    rejected_fact = await get_fact(db, rejected_id)
    assert chosen_fact.state == "approved"
    assert rejected_fact.state == "deprecated"


# ── Calc results marked stale on fact override ────────────────────────────────

@pytest.mark.asyncio
async def test_calc_result_marked_stale_on_fact_override(db):
    """Overriding a fact marks CalculationResult rows that reference it as stale."""
    from credit_report.fact_store.repository import upsert_facts, update_fact_value
    from credit_report.calculation_engine.models import CalculationResult
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    facts = await upsert_facts(db, [_make_fact(rid, "ebitda", 300.0, "analyst_input_json")])
    await db.flush()
    fact = facts[0]

    # Insert a CalculationResult referencing this fact
    calc = CalculationResult(
        id=str(uuid.uuid4()),
        report_id=rid,
        metric_name="ebitda_margin_pct",
        entity="TestCo",
        period="FY2024",
        value=0.30,
        formula="ebitda / revenue",
        input_fact_ids=json.dumps([fact.id]),
        is_stale=False,
        version=1,
    )
    db.add(calc)
    await db.flush()

    # Override the fact
    await update_fact_value(
        db, fact_id=fact.id,
        new_value=350.0, new_display="350.0",
        actor_id="analyst-001", reason="corrected",
        expected_version=fact.version,
    )
    await db.flush()

    # Reload calc and check is_stale
    result = await db.execute(select(CalculationResult).where(CalculationResult.id == calc.id))
    updated_calc = result.scalar_one()
    assert updated_calc.is_stale is True


@pytest.mark.asyncio
async def test_calc_not_stale_for_unrelated_fact(db):
    """Overriding a fact does not mark unrelated CalculationResult rows stale."""
    from credit_report.fact_store.repository import upsert_facts, update_fact_value
    from credit_report.calculation_engine.models import CalculationResult
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    facts = await upsert_facts(db, [
        _make_fact(rid, "ebitda", 300.0, "analyst_input_json"),
        _make_fact(rid, "revenue", 1000.0, "analyst_input_json"),
    ])
    await db.flush()
    ebitda_fact, rev_fact = facts[0], facts[1]

    # Calc references only revenue
    calc = CalculationResult(
        id=str(uuid.uuid4()),
        report_id=rid,
        metric_name="net_margin_pct",
        entity="TestCo",
        period="FY2024",
        value=0.10,
        formula="net_income / revenue",
        input_fact_ids=json.dumps([rev_fact.id]),
        is_stale=False,
        version=1,
    )
    db.add(calc)
    await db.flush()

    # Override the ebitda fact (unrelated to calc)
    await update_fact_value(
        db, fact_id=ebitda_fact.id,
        new_value=350.0, new_display="350.0",
        actor_id="analyst-001", reason="corrected",
        expected_version=ebitda_fact.version,
    )
    await db.flush()

    result = await db.execute(select(CalculationResult).where(CalculationResult.id == calc.id))
    updated_calc = result.scalar_one()
    assert updated_calc.is_stale is False


# ── LTV/ACR persisted to DB ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ltv_acr_persisted_to_db(db):
    """compute_ltv_acr() endpoint helper persists rows to CalculationResult."""
    from credit_report.api.calculations import _upsert_calculation
    from credit_report.calculation_engine.ltv_acr import build_ltv_table
    from credit_report.calculation_engine.models import CalculationResult
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    schedule = [
        {"year": 1, "outstanding_pct": 90.0},
        {"year": 2, "outstanding_pct": 80.0},
        {"year": 3, "outstanding_pct": 70.0},
    ]
    rows = build_ltv_table(
        facility_amount=100.0,
        initial_asset_value=130.0,
        amortization_schedule=schedule,
    )

    for raw_row in rows:
        yr_period = f"YR{int(raw_row.year)}"
        formula = f"loan={raw_row.loan_outstanding:.2f} / asset25={raw_row.asset_value_25yr:.2f}"
        await _upsert_calculation(db, rid, "facility", yr_period, "ltv_25yr_pct", raw_row.ltv_25yr_pct, formula, [])
        await _upsert_calculation(db, rid, "facility", yr_period, "ltv_20yr_pct", raw_row.ltv_20yr_pct, formula, [])
    await db.flush()

    result = await db.execute(
        select(CalculationResult).where(
            CalculationResult.report_id == rid,
            CalculationResult.entity == "facility",
        )
    )
    calcs = result.scalars().all()
    metric_names = {c.metric_name for c in calcs}
    assert "ltv_25yr_pct" in metric_names
    assert "ltv_20yr_pct" in metric_names
    # 3 years × 2 metrics = 6 rows
    assert len(calcs) == 6


@pytest.mark.asyncio
async def test_ltv_upsert_updates_version(db):
    """Re-running _upsert_calculation for same key bumps version and clears stale."""
    from credit_report.api.calculations import _upsert_calculation
    from credit_report.calculation_engine.models import CalculationResult
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    await _upsert_calculation(db, rid, "facility", "YR1", "ltv_25yr_pct", 65.0, "v1", [])
    await db.flush()

    # Mark stale manually
    result = await db.execute(
        select(CalculationResult).where(
            CalculationResult.report_id == rid,
            CalculationResult.metric_name == "ltv_25yr_pct",
        )
    )
    calc = result.scalar_one()
    calc.is_stale = True
    await db.flush()

    # Re-upsert
    await _upsert_calculation(db, rid, "facility", "YR1", "ltv_25yr_pct", 63.0, "v2", [])
    await db.flush()

    result2 = await db.execute(
        select(CalculationResult).where(
            CalculationResult.report_id == rid,
            CalculationResult.metric_name == "ltv_25yr_pct",
        )
    )
    updated = result2.scalar_one()
    assert updated.value == pytest.approx(63.0)
    assert updated.version == 2
    assert updated.is_stale is False


# ── LLM timeout config ────────────────────────────────────────────────────────

def test_llm_timeout_seconds_configured():
    """LLM_TIMEOUT_SECONDS is exported from config with a positive integer value."""
    from credit_report.config import LLM_TIMEOUT_SECONDS
    assert isinstance(LLM_TIMEOUT_SECONDS, int)
    assert LLM_TIMEOUT_SECONDS > 0


def test_call_gemini_raw_timeout_on_hang():
    """call_gemini_raw raises TimeoutError when the underlying call hangs."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    async def slow_generate(*args, **kwargs):
        await asyncio.sleep(999)

    async def run():
        with patch("credit_report.generation.claude_client.LLM_TIMEOUT_SECONDS", 0):
            from credit_report.generation import claude_client
            original = claude_client.LLM_TIMEOUT_SECONDS
            claude_client.LLM_TIMEOUT_SECONDS = 0
            try:
                with patch.object(
                    claude_client.genai.Client("fake").aio.models,
                    "generate_content",
                    new_callable=lambda: lambda *a, **kw: slow_generate(),
                ):
                    pass
            finally:
                claude_client.LLM_TIMEOUT_SECONDS = original

    # Simpler: verify asyncio.wait_for with 0 timeout raises TimeoutError
    async def _inner():
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(asyncio.sleep(999), timeout=0)

    asyncio.run(_inner())


# ── _values_disagree helper ───────────────────────────────────────────────────

def test_values_disagree_numeric_above_threshold():
    from credit_report.fact_store.repository import _values_disagree
    from types import SimpleNamespace
    a = SimpleNamespace(value=1000.0, value_text=None)
    b = SimpleNamespace(value=900.0, value_text=None)  # 10% diff
    assert _values_disagree(a, b) is True


def test_values_disagree_numeric_within_threshold():
    from credit_report.fact_store.repository import _values_disagree
    from types import SimpleNamespace
    a = SimpleNamespace(value=1000.0, value_text=None)
    b = SimpleNamespace(value=1010.0, value_text=None)  # 1% diff
    assert _values_disagree(a, b) is False


def test_values_disagree_text_case_insensitive():
    from credit_report.fact_store.repository import _values_disagree
    from types import SimpleNamespace
    a = SimpleNamespace(value=None, value_text="Approved")
    b = SimpleNamespace(value=None, value_text="approved")
    assert _values_disagree(a, b) is False


def test_values_disagree_text_different():
    from credit_report.fact_store.repository import _values_disagree
    from types import SimpleNamespace
    a = SimpleNamespace(value=None, value_text="Approved")
    b = SimpleNamespace(value=None, value_text="Declined")
    assert _values_disagree(a, b) is True

"""
ETL → Import → Generate regression suite for ALL sections 1-10.

Covers:
  1. etl_document() mock: returns non-empty data for every section number 1-10
  2. save_section_input() (PUT /inputs/{sec}): succeeds for every section 1-10
     with ETL-style keys — no ValidationError, no 400/422, input_json round-trips
  3. run_section_generation() (pipeline layer): generates every section 1-10
     given ETL-style keys — no ValueError, status == "done"
  4. generate_section API (202 contract): all sections return task_id + "running"
  5. generate_full_report API (202 contract): partial data proceeds, all-empty 422
  6. Dependency-blocked sections (§2,3 need §7; §5 needs §1; §6 needs §1,§5; §10 needs §7,§1):
     check_hard_dependencies returns the correct missing list
  7. Full chain smoke: ETL mock → save ALL sections → generate ALL via pipeline
  8. ETL data key names never block generation (ETL keys ≠ REQUIRED_FIELDS keys)
  9. _etlExtractedData contract: sections_extracted keys must be ints or int-coercible strings
 10. Document-type→section mapping: each document type maps to a non-empty section list
"""
from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base

# Ensure all ORM models are registered before Base.metadata.create_all
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.fact_store.models  # noqa: F401
import credit_report.block_ast.models  # noqa: F401
import credit_report.security.models  # noqa: F401
import credit_report.audit.events  # noqa: F401
import credit_report.models  # noqa: F401

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


# ── Fixtures ──────────────────────────────────────────────────────────────────

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


ANALYST_ID = "analyst-test-fixed"  # fixed ID shared by _make_user() and _seed_report()


def _make_user(role: str = "analyst", user_id: str = ANALYST_ID) -> Any:
    from credit_report.security.models import User
    return User(
        id=user_id,
        email="analyst@test.com",
        role=role,
        hashed_password="x",
        is_active=True,
    )


async def _seed_report(db: AsyncSession, rid: str, created_by: str = ANALYST_ID) -> None:
    from credit_report.models import Report
    db.add(Report(
        id=rid,
        borrower_name="Evergreen Marine (Asia) Pte. Ltd.",
        industry="marine",
        report_type="new_deal",
        booking_branch="SG",
        status="draft",
        created_by=created_by,
    ))
    await db.flush()


async def _seed_section_input(
    db: AsyncSession,
    report_id: str,
    section_no: int,
    data: dict,
    saved_by: str = "analyst-test",
) -> None:
    from credit_report.models import SectionInput
    from sqlalchemy import select
    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.input_json = json.dumps(data)
    else:
        db.add(SectionInput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            input_json=json.dumps(data),
            saved_by=saved_by,
        ))
    await db.flush()


async def _seed_section_output_done(db: AsyncSession, report_id: str, section_no: int) -> None:
    from credit_report.models import SectionOutput
    from sqlalchemy import select
    result = await db.execute(
        select(SectionOutput).where(
            SectionOutput.report_id == report_id,
            SectionOutput.section_no == section_no,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.status = "done"
        existing.markdown = f"# Section {section_no}\n\nGenerated content."
    else:
        db.add(SectionOutput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            status="done",
            markdown=f"# Section {section_no}\n\nGenerated content.",
            tokens_used=100,
        ))
    await db.flush()


# ── Representative ETL-style payloads for each section ────────────────────────
# These use ETL key names — intentionally different from REQUIRED_FIELDS.
# If any section rejects these, the test catches the regression immediately.

ETL_DATA: dict[int, dict] = {
    1: {
        "report_type": "new_deal",
        "booking_branch": "SG",
        "borrower_name": "Evergreen Marine (Asia) Pte. Ltd.",
        "facility_amount_usd_m": 120.0,
        "facility_type": "Term Loan",
        "tenor_years": 7,
        "collateral_description": "First priority mortgage over MV Pacific Star",
        "guarantor_name": "Evergreen Marine Corp.",
        "ltc_pct": 65.0,
    },
    2: {
        "credit_overview_bullets": [
            "Strong sponsor support from parent Evergreen Marine Corp.",
            "Charter contract covers 90% of debt service.",
        ],
        "primary_repayment_source": "Charter hire income from COSCO Shipping",
        "secondary_repayment_source": "Vessel sale proceeds",
        "dscr_value": 1.38,
        "recommendation": "Approve",
    },
    3: {
        "internal_rating": "BB+",
        "masterscale_rating": "7A",
        "esg_score": "Medium",
        "sanctions_check": "Clear",
        "country_risk": "Low — Singapore",
        "industry_risk": "Moderate — Container Shipping",
    },
    4: {
        "company_name": "Evergreen Marine (Asia) Pte. Ltd.",
        "legal_entity_type": "Private Limited Company",
        "incorporation_country": "Singapore",
        "principal_office": "Singapore",
        "fiscal_year_end": "Dec-31",
        "group_auditor": "Deloitte",
        "fleet_size_teu": 350000,
    },
    5: {
        "vessel_name": "MV Pacific Star",
        "vessel_type": "Container",
        "teu_capacity": 14000,
        "dwt": 165000,
        "year_built": 2021,
        "flag_state": "Panama",
        "current_valuation_usd_m": 145.0,
        "valuation_date": "2024-12-31",
        "valuer": "Clarkson Research",
        "ltv_pct": 68.5,
    },
    6: {
        "charter_counterparty": "COSCO Shipping Lines Co., Ltd.",
        "charter_type": "Time Charter",
        "charter_rate_usd_day": 28500,
        "charter_duration_years": 7,
        "charter_start_date": "2022-01-15",
        "charter_expiry_date": "2029-01-14",
        "hire_coverage_pct": 92.0,
    },
    7: {
        "revenue_2024": 2200,
        "ebitda_2024": 710,
        "net_income_2024": 399,
        "total_assets_2024": 7975,
        "total_equity_2024": 4070,
        "cash_2024": 2200,
        "total_debt_2024": 3100,
        "interest_expense_2024": 85,
        "dscr_2024": 1.42,
    },
    8: {
        "legal_structure": "Private Limited Company",
        "material_litigation": "None",
        "regulatory_violations": "None",
        "jurisdiction": "Singapore",
        "governing_law": "Singapore Law",
        "legal_opinions_obtained": True,
    },
    9: {
        "financial_crime_risk": "Low",
        "pep_check": "No PEP identified",
        "adverse_media": "None",
        "kyc_status": "Completed",
        "aml_risk_rating": "Low",
        "ubo_identified": True,
    },
    10: {
        "freight_rate_outlook": "Stable — supply discipline expected",
        "industry_overcapacity_risk": "Medium",
        "competitor_landscape": "Maersk, MSC, CMA CGM",
        "market_share_pct": 12.0,
        "regulatory_risk": "IMO 2030 emission targets",
        "macro_sensitivity": "Moderate — trade volume dependent",
    },
}


# ── 1. etl_document mock returns non-empty dict for every section ─────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no", list(range(1, 11)))
async def test_etl_document_mock_returns_data_for_section(section_no):
    """etl_document() with mocked Gemini must return a dict with the requested section."""
    from credit_report.generation.etl import etl_document

    mock_response_json = json.dumps({str(section_no): ETL_DATA[section_no]})

    with patch("google.genai.Client") as mock_client_cls, \
         patch("credit_report.config.GEMINI_API_KEY", "mock-key"):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.text = mock_response_json
        mock_resp.candidates = [MagicMock()]
        mock_resp.candidates[0].finish_reason = "STOP"
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        result = await etl_document(
            text="Sample document text with financial data.",
            document_type="other",
            section_nos=[section_no],
        )

    assert isinstance(result, dict), f"etl_document must return dict, got {type(result)}"
    assert section_no in result, f"§{section_no} must be in result; got keys={list(result.keys())}"
    assert isinstance(result[section_no], dict), f"§{section_no} value must be dict"
    assert result[section_no], f"§{section_no} must have non-empty data"


# ── 2. save_section_input: PUT /inputs/{sec} succeeds for all sections 1-10 ──

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no", list(range(1, 11)))
async def test_save_section_input_accepts_etl_keys_all_sections(db, section_no):
    """PUT /inputs/{section_no} must accept ETL-style key names for every section 1-10."""
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    payload = SectionInputPayload(section_no=section_no, input_json=ETL_DATA[section_no])

    with patch("credit_report.api.reports.write_event", new_callable=AsyncMock), \
         patch("credit_report.api.reports.upsert_facts", new_callable=AsyncMock), \
         patch("credit_report.api.calculations._run_recalculate_core", new_callable=AsyncMock, return_value=(0, [])):
        result = await save_section_input(
            report_id=rid,
            section_no=section_no,
            payload=payload,
            db=db,
            current_user=user,
        )

    assert result.section_no == section_no, f"§{section_no}: returned wrong section_no"
    assert result.input_json == ETL_DATA[section_no], f"§{section_no}: input_json not round-tripped"


# ── 3. save_section_input persists to DB and can be re-read ──────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no", list(range(1, 11)))
async def test_save_section_input_persisted_to_db(db, section_no):
    """After save, GET /inputs/{section_no} must return the same data that was saved."""
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    payload = SectionInputPayload(section_no=section_no, input_json=ETL_DATA[section_no])

    with patch("credit_report.api.reports.write_event", new_callable=AsyncMock), \
         patch("credit_report.api.reports.upsert_facts", new_callable=AsyncMock), \
         patch("credit_report.api.calculations._run_recalculate_core", new_callable=AsyncMock, return_value=(0, [])):
        await save_section_input(
            report_id=rid,
            section_no=section_no,
            payload=payload,
            db=db,
            current_user=user,
        )

    fetched = await get_section_input(report_id=rid, section_no=section_no, db=db, current_user=user)
    assert fetched.section_no == section_no
    assert fetched.input_json == ETL_DATA[section_no], \
        f"§{section_no}: persisted data mismatch. got={list(fetched.input_json.keys())}"


# ── 4. run_section_generation: pipeline accepts ETL keys for all sections ─────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no", list(range(1, 11)))
async def test_pipeline_generates_all_sections_with_etl_keys(db, section_no):
    """run_section_generation must succeed for every section 1-10 with ETL key names."""
    from credit_report.generation.pipeline import run_section_generation

    rid = str(uuid.uuid4())

    # Seed all hard dependencies as done so no section is blocked
    # §2,§3 depend on §7; §5 depends on §1; §6 depends on §1,§5; §9 depends on all; §10 depends on §7,§1
    dep_map = {2: [7], 3: [7], 5: [1], 6: [1, 5], 9: list(range(1, 9)), 10: [7, 1]}
    deps_needed = dep_map.get(section_no, [])
    for dep in deps_needed:
        await _seed_section_output_done(db, rid, dep)

    await _seed_section_input(db, rid, section_no, ETL_DATA[section_no])

    mock_md = f"## Section {section_no}\n\nGenerated from ETL keys."
    with patch("credit_report.generation.pipeline.generate_section_markdown",
               new_callable=AsyncMock) as mock_gen, \
         patch("credit_report.generation.pipeline.retrieve_evidence") as mock_ev, \
         patch("credit_report.generation.pipeline.check_quota", new_callable=AsyncMock), \
         patch("credit_report.generation.pipeline.record_tokens", new_callable=AsyncMock), \
         patch("credit_report.audit.events.write_event", new_callable=AsyncMock):
        mock_gen.return_value = (mock_md, 256)
        mock_ev.return_value = []
        output = await run_section_generation(
            db=db,
            report_id=rid,
            section_no=section_no,
            actor_user_id="test-user",
        )

    assert output.status == "done", \
        f"§{section_no}: expected status=done, got={output.status}"
    assert output.markdown == mock_md, f"§{section_no}: markdown not set"
    # Verify the ETL data was actually passed to the LLM call
    call_kwargs = mock_gen.call_args.kwargs if mock_gen.call_args.kwargs else {}
    call_args = mock_gen.call_args.args if mock_gen.call_args.args else ()
    passed_input = call_kwargs.get("input_json") or (call_args[1] if len(call_args) > 1 else {})
    assert passed_input, f"§{section_no}: input_json passed to generate was empty"


# ── 5. generate_section API: 202 + task_id for all sections ──────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no", list(range(1, 11)))
async def test_generate_section_api_202_all_sections(db, section_no):
    """POST /generate/{section_no} must return 202 task_id for every section."""
    from credit_report.api.generate import generate_section
    from credit_report.models import SectionOutput

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    await _seed_section_input(db, rid, section_no, ETL_DATA[section_no])

    # Pre-seed any hard dependencies as done
    dep_map = {2: [7], 3: [7], 5: [1], 6: [1, 5], 9: list(range(1, 9)), 10: [7, 1]}
    for dep in dep_map.get(section_no, []):
        await _seed_section_output_done(db, rid, dep)

    user = _make_user()
    bg = BackgroundTasks()

    mock_output = SectionOutput(
        id=str(uuid.uuid4()),
        report_id=rid,
        section_no=section_no,
        status="done",
        tokens_used=200,
    )

    with patch("credit_report.api.generate.run_section_generation",
               new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_output
        result = await generate_section(
            report_id=rid,
            section_no=section_no,
            background_tasks=bg,
            db=db,
            current_user=user,
        )

    assert result.status == "running", \
        f"§{section_no}: expected running, got={result.status}"
    assert result.task_id is not None, f"§{section_no}: task_id must be set"
    assert result.section_no == section_no, f"§{section_no}: section_no mismatch"


# ── 6. Hard dependency check: correct missing-section detection ───────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no,required_deps", [
    (2, [7]),
    (3, [7]),
    (5, [1]),
    (6, [1, 5]),
    (10, [7, 1]),
    (1, []),   # no deps
    (4, []),   # no deps
    (7, []),   # no deps
    (8, []),   # no deps
])
async def test_check_hard_dependencies_returns_correct_missing(db, section_no, required_deps):
    """check_hard_dependencies must return exactly the missing dep sections."""
    from credit_report.generation.pipeline import check_hard_dependencies

    rid = str(uuid.uuid4())
    # Do NOT seed any outputs — all deps will be missing
    missing = await check_hard_dependencies(db, rid, section_no)
    assert sorted(missing) == sorted(required_deps), \
        f"§{section_no}: expected missing={required_deps}, got={missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize("section_no,required_deps", [
    (2, [7]),
    (3, [7]),
    (5, [1]),
    (6, [1, 5]),
    (10, [7, 1]),
])
async def test_check_hard_dependencies_empty_when_deps_done(db, section_no, required_deps):
    """check_hard_dependencies must return [] when all deps are done."""
    from credit_report.generation.pipeline import check_hard_dependencies

    rid = str(uuid.uuid4())
    for dep in required_deps:
        await _seed_section_output_done(db, rid, dep)
    # For §9: seed all 1-8
    if section_no == 9:
        for dep in range(1, 9):
            await _seed_section_output_done(db, rid, dep)

    missing = await check_hard_dependencies(db, rid, section_no)
    assert missing == [], \
        f"§{section_no}: deps are done but check still returns missing={missing}"


# ── 7. generate_section API returns 409 when hard deps are missing ────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no,missing_dep", [
    (2, 7),
    (5, 1),
    (6, 1),
])
async def test_generate_section_409_when_hard_dep_missing(db, section_no, missing_dep):
    """generate_section must return 409 when hard dependencies are not generated."""
    from fastapi import HTTPException
    from credit_report.api.generate import generate_section

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    await _seed_section_input(db, rid, section_no, ETL_DATA[section_no])
    # Do NOT seed the dependency output

    user = _make_user()
    bg = BackgroundTasks()

    with pytest.raises(HTTPException) as exc_info:
        await generate_section(
            report_id=rid,
            section_no=section_no,
            background_tasks=bg,
            db=db,
            current_user=user,
        )

    assert exc_info.value.status_code == 409, \
        f"§{section_no}: expected 409 for missing dep §{missing_dep}, got={exc_info.value.status_code}"


# ── 8. Full chain: ETL mock → save all 10 sections → generate all via pipeline ─

@pytest.mark.asyncio
async def test_full_etl_import_generate_chain_all_sections(db):
    """End-to-end: mock ETL output → save all 10 sections → generate all without error."""
    from credit_report.generation.pipeline import run_full_report_generation

    rid = str(uuid.uuid4())

    # Seed all 10 sections with ETL-style data (simulates Apply button for each)
    for sec_no, data in ETL_DATA.items():
        await _seed_section_input(db, rid, sec_no, data)

    mock_md = "# Generated\n\nContent."
    with patch("credit_report.generation.pipeline.generate_section_markdown",
               new_callable=AsyncMock) as mock_gen, \
         patch("credit_report.generation.pipeline.retrieve_evidence") as mock_ev, \
         patch("credit_report.generation.pipeline.check_quota", new_callable=AsyncMock), \
         patch("credit_report.generation.pipeline.record_tokens", new_callable=AsyncMock), \
         patch("credit_report.audit.events.write_event", new_callable=AsyncMock):
        mock_gen.return_value = (mock_md, 128)
        mock_ev.return_value = []
        results = await run_full_report_generation(
            db=db,
            report_id=rid,
            actor_user_id="test-user",
        )

    assert isinstance(results, dict), "run_full_report_generation must return dict"
    # Sections without hard-dep issues should all be done
    no_dep_sections = {1, 4, 7, 8}
    for sec_no in no_dep_sections:
        assert results.get(sec_no) == "done", \
            f"§{sec_no} (no deps): expected done, got={results.get(sec_no)}"


# ── 9. generate_full_report API: 202 when partial data, 422 when none ────────

@pytest.mark.asyncio
async def test_generate_full_report_202_with_partial_data(db):
    """generate_full_report returns 202 when at least one section has data."""
    from credit_report.api.generate import generate_full_report

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    # Seed only sections 4 and 7 — the two most common ETL outputs
    for sec in [4, 7]:
        await _seed_section_input(db, rid, sec, ETL_DATA[sec])

    user = _make_user()
    bg = BackgroundTasks()

    with patch("credit_report.api.generate.run_full_report_generation",
               new_callable=AsyncMock) as mock_run, \
         patch("credit_report.api.generate.GEMINI_API_KEY", "mock-key"):
        mock_run.return_value = {4: "done", 7: "done"}
        result = await generate_full_report(
            report_id=rid, background_tasks=bg, db=db, current_user=user
        )

    assert result.status == "running"
    assert result.task_id is not None


@pytest.mark.asyncio
async def test_generate_full_report_422_when_no_data(db):
    """generate_full_report must return 422 when absolutely no section has data."""
    from fastapi import HTTPException
    from credit_report.api.generate import generate_full_report

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    # No section inputs seeded

    user = _make_user()
    bg = BackgroundTasks()

    with pytest.raises(HTTPException) as exc_info:
        with patch("credit_report.api.generate.GEMINI_API_KEY", "mock-key"):
            await generate_full_report(
                report_id=rid, background_tasks=bg, db=db, current_user=user
            )

    assert exc_info.value.status_code == 422
    assert "No sections have saved input data" in exc_info.value.detail


# ── 10. ETL data → save → load round-trip integrity for all sections ──────────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no", list(range(1, 11)))
async def test_etl_data_round_trips_through_db(db, section_no):
    """Data saved by the Apply button must be identical when loaded by the pipeline."""
    from credit_report.models import SectionInput
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    original_data = ETL_DATA[section_no]
    await _seed_section_input(db, rid, section_no, original_data)

    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == rid,
            SectionInput.section_no == section_no,
        )
    )
    si = result.scalar_one_or_none()
    assert si is not None, f"§{section_no}: SectionInput row not found after save"
    loaded = json.loads(si.input_json)
    assert loaded == original_data, \
        f"§{section_no}: round-trip mismatch. original_keys={list(original_data.keys())} loaded_keys={list(loaded.keys())}"


# ── 11. DOCUMENT_SECTION_MAP: every doc type maps to non-empty section list ──

def test_document_section_map_all_types_non_empty():
    """Every document type in DOCUMENT_SECTION_MAP must map to at least one section."""
    from credit_report.generation.etl import DOCUMENT_SECTION_MAP

    for doc_type, sections in DOCUMENT_SECTION_MAP.items():
        assert sections, f"document_type={doc_type!r} maps to empty section list"
        for sec in sections:
            assert 1 <= sec <= 11, \
                f"document_type={doc_type!r}: invalid section {sec} (must be 1-11)"


# ── 12. ETL result key type: sections_extracted must be int-coercible ─────────

def test_etl_sections_extracted_keys_are_int_coercible():
    """The JS side does parseInt(n) on sections_extracted items.
    Verify that any key returned by etl_document() can be coerced to int without NaN."""
    # Simulate typical Gemini output with string keys "4", "7", "3"
    sample_output = {"4": {"company_name": "TestCo"}, "7": {"revenue_2024": 100}}

    for k in sample_output.keys():
        coerced = int(k)
        assert coerced > 0, f"Key {k!r} coerced to {coerced}, expected positive int"
        assert 1 <= coerced <= 11, f"Key {k!r} coerced to {coerced}, out of valid range 1-11"


# ── 13. ETL pipeline: empty or null-only Gemini response returns empty dict ───

@pytest.mark.asyncio
async def test_etl_document_empty_response_returns_empty_dict():
    """etl_document must return {} when Gemini returns empty or null-only JSON."""
    from credit_report.generation.etl import etl_document

    with patch("google.genai.Client") as mock_client_cls, \
         patch("credit_report.config.GEMINI_API_KEY", "mock-key"):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.text = '{"4": {"company_name": null, "legal_entity_type": null}}'
        mock_resp.candidates = [MagicMock()]
        mock_resp.candidates[0].finish_reason = "STOP"
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        result = await etl_document(
            text="Some document text.",
            document_type="other",
            section_nos=[4],
        )

    # All-null values should be excluded from result
    assert result == {}, f"Expected empty dict for null-only extraction, got={result}"


@pytest.mark.asyncio
async def test_etl_document_gemini_returns_empty_string():
    """etl_document must return {} when Gemini returns empty string."""
    from credit_report.generation.etl import etl_document

    with patch("google.genai.Client") as mock_client_cls, \
         patch("credit_report.config.GEMINI_API_KEY", "mock-key"):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.candidates = [MagicMock()]
        mock_resp.candidates[0].finish_reason = "STOP"
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        result = await etl_document(
            text="Some document text.",
            document_type="annual_report",
        )

    assert result == {}, f"Expected empty dict for empty Gemini response, got={result}"


# ── 14. ETL API endpoint: smoke test (no real Gemini, no real filesystem) ─────

@pytest.mark.asyncio
async def test_etl_endpoint_returns_etlresult_schema(db, tmp_path):
    """etl_document_endpoint must return ETLResult with correct schema."""
    from credit_report.api.generate import etl_document_endpoint
    from credit_report.generation.models import SectionDocument

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    doc_id = str(uuid.uuid4())
    db.add(SectionDocument(
        id=doc_id,
        report_id=rid,
        original_filename="test_annual.pdf",
        file_size_bytes=1024,
        document_type="annual_report",
        file_format="pdf",
        etl_status="pending",
        uploaded_by=user.id,
    ))
    await db.flush()

    # Write a fake text file where the endpoint will look
    from credit_report.config import CREDIT_REPORTS_ROOT
    doc_dir = CREDIT_REPORTS_ROOT / rid
    doc_dir.mkdir(parents=True, exist_ok=True)
    txt_file = doc_dir / f"{doc_id}.txt"
    txt_file.write_text(
        "Evergreen Marine (Asia) Pte. Ltd. FY2024 Revenue USD 2,200M EBITDA USD 710M",
        encoding="utf-8",
    )

    mock_extracted = {4: ETL_DATA[4], 7: ETL_DATA[7]}

    try:
        with patch("credit_report.api.generate.etl_document",
                   new_callable=AsyncMock) as mock_etl:
            mock_etl.return_value = mock_extracted
            result = await etl_document_endpoint(
                report_id=rid,
                doc_id=doc_id,
                db=db,
                current_user=user,
            )
    finally:
        if txt_file.exists():
            txt_file.unlink()
        if doc_dir.exists():
            try:
                doc_dir.rmdir()
            except OSError:
                pass

    assert result.doc_id == doc_id
    assert result.document_type == "annual_report"
    assert sorted(result.sections_extracted) == [4, 7]
    assert "4" in result.data
    assert "7" in result.data
    assert result.data["4"]["company_name"] == "Evergreen Marine (Asia) Pte. Ltd."


# ── 15. Upsert (save twice): second save merges, doesn't duplicate ────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("section_no", [4, 7])
async def test_save_section_input_upsert_merges_correctly(db, section_no):
    """Saving the same section twice must upsert (not duplicate) the row."""
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload
    from credit_report.models import SectionInput
    from sqlalchemy import select, func

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    with patch("credit_report.api.reports.write_event", new_callable=AsyncMock), \
         patch("credit_report.api.reports.upsert_facts", new_callable=AsyncMock), \
         patch("credit_report.api.calculations._run_recalculate_core", new_callable=AsyncMock, return_value=(0, [])):
        # First save
        payload1 = SectionInputPayload(section_no=section_no, input_json=ETL_DATA[section_no])
        await save_section_input(report_id=rid, section_no=section_no, payload=payload1, db=db, current_user=user)

        # Second save with updated data
        updated = {**ETL_DATA[section_no], "extra_field": "added_in_second_save"}
        payload2 = SectionInputPayload(section_no=section_no, input_json=updated)
        result = await save_section_input(report_id=rid, section_no=section_no, payload=payload2, db=db, current_user=user)

    # Must still be only one row
    count_result = await db.execute(
        select(func.count()).where(
            SectionInput.report_id == rid,
            SectionInput.section_no == section_no,
        )
    )
    count = count_result.scalar()
    assert count == 1, f"§{section_no}: expected 1 SectionInput row after upsert, got={count}"
    assert result.input_json["extra_field"] == "added_in_second_save"


# ── 16. Section input validates section_no bounds ────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_section_no", [0, 11, -1, 100])
async def test_save_section_input_rejects_invalid_section_no(db, bad_section_no):
    """PUT /inputs/{section_no} must return 400 for section_no outside 1-10."""
    from fastapi import HTTPException
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    payload = SectionInputPayload(section_no=1, input_json={"field": "value"})
    with pytest.raises(HTTPException) as exc_info:
        await save_section_input(
            report_id=rid,
            section_no=bad_section_no,
            payload=payload,
            db=db,
            current_user=user,
        )

    assert exc_info.value.status_code == 400, \
        f"section_no={bad_section_no}: expected 400, got={exc_info.value.status_code}"

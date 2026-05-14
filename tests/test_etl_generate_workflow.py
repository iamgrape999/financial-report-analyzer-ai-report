"""
Critical workflow tests: ETL → Apply → Generate.

These tests replicate the real user journey that was completely untested before:
  1. ETL extracts data with its own key names (not REQUIRED_FIELDS names)
  2. User saves ETL data via PUT /inputs/{sec}
  3. User triggers generation — must NOT be blocked

Previously, all three layers rejected ETL-style data:
  - pipeline.py raised ValueError when input_json was empty/different
  - generate.py raised 422 when input_data was empty
  - generate_full_report blocked if any section lacked data

This suite regression-tests those fixes so the bugs can never silently return.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base

import credit_report.calculation_engine.models  # noqa: F401
import credit_report.fact_store.models          # noqa: F401
import credit_report.block_ast.models           # noqa: F401
import credit_report.security.models            # noqa: F401
import credit_report.audit.events               # noqa: F401
import credit_report.models                     # noqa: F401

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


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


# ── ETL-style input key names (as produced by the AI ETL pipeline) ─────────────
# These intentionally differ from REQUIRED_FIELDS paths (e.g. "4A_borrower") to
# replicate real ETL output that was causing the 0%-completeness block.
ETL_STYLE_SECTION_4 = {
    "company_name": "Evergreen Marine (Asia) Pte. Ltd.",
    "legal_entity_type": "Private Limited Company",
    "incorporation_country": "Singapore",
    "principal_office": "Singapore",
    "fiscal_year_end": "Dec-31",
    "group_auditor": "Deloitte",
}

ETL_STYLE_SECTION_7 = {
    "revenue_2024": 2200,
    "ebitda_2024": 710,
    "net_income_2024": 399,
    "total_assets_2024": 7975,
    "total_equity_2024": 4070,
    "cash_2024": 2200,
}


def _make_report_id() -> str:
    return str(uuid.uuid4())


def _make_user_id() -> str:
    return "analyst-" + str(uuid.uuid4())[:8]


async def _seed_section_input(db: AsyncSession, report_id: str, section_no: int, data: dict) -> None:
    """Insert a SectionInput row directly (replicates what PUT /inputs/{sec} does)."""
    from credit_report.models import SectionInput
    db.add(SectionInput(
        id=str(uuid.uuid4()),
        report_id=report_id,
        section_no=section_no,
        input_json=json.dumps(data),
        saved_by="analyst-test",
    ))
    await db.flush()


# ── 1. pipeline.py: generate with ETL-style keys (non-empty, non-REQUIRED_FIELDS) ─

@pytest.mark.asyncio
async def test_pipeline_accepts_etl_style_input(db):
    """run_section_generation proceeds when input uses ETL key names, not REQUIRED_FIELDS names."""
    from credit_report.generation.pipeline import run_section_generation

    rid = _make_report_id()
    await _seed_section_input(db, rid, 4, ETL_STYLE_SECTION_4)

    mock_md = "## Company Profile\n\nEvergreen Marine (Asia) Pte. Ltd."
    with patch("credit_report.generation.pipeline.generate_section_markdown",
               new_callable=AsyncMock) as mock_gen, \
         patch("credit_report.generation.pipeline.retrieve_evidence") as mock_ev:
        mock_gen.return_value = (mock_md, 256)
        mock_ev.return_value = []
        output = await run_section_generation(db, rid, section_no=4, actor_user_id=_make_user_id())

    assert output.status == "done"
    assert output.markdown == mock_md


# ── 2. pipeline.py: generate with completely empty input (evidence-only mode) ────

@pytest.mark.asyncio
async def test_pipeline_accepts_empty_input_evidence_only(db):
    """run_section_generation must NOT raise ValueError when there is no SectionInput row.
    This is the evidence-only generation mode where the AI uses uploaded documents."""
    from credit_report.generation.pipeline import run_section_generation

    rid = _make_report_id()
    # Deliberately do NOT seed any SectionInput — simulates a freshly created report

    mock_md = "## Section 4\n\n(Generated from evidence)"
    with patch("credit_report.generation.pipeline.generate_section_markdown",
               new_callable=AsyncMock) as mock_gen, \
         patch("credit_report.generation.pipeline.retrieve_evidence") as mock_ev:
        mock_gen.return_value = (mock_md, 128)
        mock_ev.return_value = ["Revenue for FY2024 was USD 2.2bn"]
        output = await run_section_generation(db, rid, section_no=4, actor_user_id=_make_user_id())

    assert output.status == "done"


# ── 3. pipeline.py: full report skips empty sections, generates those with data ──

@pytest.mark.asyncio
async def test_full_pipeline_partial_data_skips_empty_sections(db):
    """run_full_report_generation generates sections that have data,
    and produces a result for sections without data (skip or dep-blocked), never raises."""
    from credit_report.generation.pipeline import run_full_report_generation

    rid = _make_report_id()
    # Only seed sections 4 and 7 — others have no data
    await _seed_section_input(db, rid, 4, ETL_STYLE_SECTION_4)
    await _seed_section_input(db, rid, 7, ETL_STYLE_SECTION_7)

    with patch("credit_report.generation.pipeline.generate_section_markdown",
               new_callable=AsyncMock) as mock_gen, \
         patch("credit_report.generation.pipeline.retrieve_evidence") as mock_ev:
        mock_gen.return_value = ("# Generated", 100)
        mock_ev.return_value = []
        results = await run_full_report_generation(db, rid, actor_user_id=_make_user_id())

    # Must return a result dict for all sections, never raise
    assert isinstance(results, dict)
    # Sections 4 and 7 have no hard deps and have data → should be done
    assert results.get(4) == "done", f"§4 result: {results.get(4)}"
    assert results.get(7) == "done", f"§7 result: {results.get(7)}"


# ── 4. generate.py API: POST /generate/{sec} must not 422 with ETL data ──────────

@pytest.mark.asyncio
async def test_api_generate_section_etl_keys_no_422(db):
    """The API endpoint generate_section must not raise 422 when section
    input exists but uses ETL-style key names (0% REQUIRED_FIELDS completeness)."""
    from credit_report.api.generate import generate_section
    from credit_report.security.models import User

    rid = _make_report_id()
    await _seed_section_input(db, rid, 4, ETL_STYLE_SECTION_4)

    # Seed a report row
    from credit_report.models import Report
    db.add(Report(
        id=rid,
        borrower_name="Test Co",
        industry="marine",
        report_type="new_deal",
        booking_branch="SG",
        status="draft",
        created_by="analyst-test",
    ))
    await db.flush()

    mock_user = User(id="analyst-test", email="a@test.com", role="analyst",
                     hashed_password="x", is_active=True)

    from credit_report.models import SectionOutput
    mock_output = SectionOutput(
        id=str(uuid.uuid4()),
        report_id=rid,
        section_no=4,
        status="done",
        tokens_used=300,
    )

    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    with patch("credit_report.api.generate.run_section_generation",
               new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_output
        result = await generate_section(
            report_id=rid,
            section_no=4,
            background_tasks=bg,
            db=db,
            current_user=mock_user,
        )

    # 202: must return running task immediately — not raise 422
    assert result.section_no == 4
    assert result.status == "running"
    assert result.task_id is not None


# ── 5. generate.py API: POST /generate/{sec} proceeds even with no SectionInput ──

@pytest.mark.asyncio
async def test_api_generate_section_no_input_no_422(db):
    """generate_section must NOT raise 422 when no SectionInput exists at all.
    Previously this was the main blocker preventing any ETL→Generate flow."""
    from credit_report.api.generate import generate_section
    from credit_report.security.models import User

    rid = _make_report_id()
    # NO SectionInput seeded

    from credit_report.models import Report
    db.add(Report(
        id=rid,
        borrower_name="Empty Input Co",
        industry="marine",
        report_type="new_deal",
        booking_branch="SG",
        status="draft",
        created_by="analyst-test",
    ))
    await db.flush()

    mock_user = User(id="analyst-test", email="a@test.com", role="analyst",
                     hashed_password="x", is_active=True)

    from credit_report.models import SectionOutput
    mock_output = SectionOutput(
        id=str(uuid.uuid4()),
        report_id=rid,
        section_no=1,
        status="done",
        tokens_used=150,
    )

    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    with patch("credit_report.api.generate.run_section_generation",
               new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_output
        result = await generate_section(
            report_id=rid,
            section_no=1,
            background_tasks=bg,
            db=db,
            current_user=mock_user,
        )

    # 202: returns running task immediately — not raise 422
    assert result.status == "running"
    assert result.task_id is not None


# ── 6. generate.py API: generate_full_report skips missing, doesn't block all ────

@pytest.mark.asyncio
async def test_api_generate_full_report_partial_data_proceeds(db):
    """generate_full_report must NOT raise 422 when only SOME sections have data.
    Previously it blocked the entire report if any single section was missing input."""
    from credit_report.api.generate import generate_full_report
    from credit_report.security.models import User

    rid = _make_report_id()
    # Only seed 3 of 10 sections
    for sec in [4, 7, 1]:
        await _seed_section_input(db, rid, sec, {"stub": f"data_for_sec_{sec}"})

    from credit_report.models import Report
    db.add(Report(
        id=rid,
        borrower_name="Partial Data Co",
        industry="marine",
        report_type="new_deal",
        booking_branch="SG",
        status="draft",
        created_by="analyst-test",
    ))
    await db.flush()

    mock_user = User(id="analyst-test", email="a@test.com", role="analyst",
                     hashed_password="x", is_active=True)

    mock_results = {n: "done" for n in range(1, 11)}

    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    with patch("credit_report.api.generate.run_full_report_generation",
               new_callable=AsyncMock) as mock_run, \
         patch("credit_report.api.generate.GEMINI_API_KEY", "mock-key"):
        mock_run.return_value = mock_results
        result = await generate_full_report(
            report_id=rid, background_tasks=bg, db=db, current_user=mock_user
        )

    # 202: must return running task immediately — not raise 422
    assert result is not None
    assert result.status == "running"
    assert result.task_id is not None


# ── 7. generate.py API: generate_full_report still 422 if ALL sections empty ─────

@pytest.mark.asyncio
async def test_api_generate_full_report_all_empty_still_202(db):
    """generate_full_report must return 202 even when no sections have input data.
    The pipeline generates from uploaded evidence (evidence-only mode)."""
    from credit_report.api.generate import generate_full_report
    from credit_report.security.models import User

    rid = _make_report_id()
    # NO SectionInput for any section

    from credit_report.models import Report
    db.add(Report(
        id=rid,
        borrower_name="Totally Empty Co",
        industry="marine",
        report_type="new_deal",
        booking_branch="SG",
        status="draft",
        created_by="analyst-test",
    ))
    await db.flush()

    mock_user = User(id="analyst-test", email="a@test.com", role="analyst",
                     hashed_password="x", is_active=True)

    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    with patch("credit_report.api.generate.GEMINI_API_KEY", "mock-key"):
        result = await generate_full_report(
            report_id=rid, background_tasks=bg, db=db, current_user=mock_user
        )

    assert result.status == "running"


# ── 8. Full end-to-end workflow simulation: ETL → save → generate ────────────────

@pytest.mark.asyncio
async def test_etl_apply_then_generate_full_workflow(db):
    """Regression test for the complete ETL→Apply→Generate workflow.

    Simulates:
      1. ETL extracts data for §4 with ETL key names (not REQUIRED_FIELDS format)
      2. Frontend calls PUT /inputs/4 to save the ETL data
      3. User triggers POST /generate/4
      4. Generation succeeds (status=done) without any 422 or ValueError
    """
    from credit_report.generation.pipeline import run_section_generation
    from credit_report.models import SectionInput

    rid = _make_report_id()

    # Step 1+2: ETL data saved with ETL key names (not 4A_borrower etc)
    etl_extracted = {
        "company_name": "Evergreen Marine (Asia) Pte. Ltd.",
        "revenue_2024_usd_m": 2200,
        "ebitda_2024_usd_m": 710,
        "net_income_2024_usd_m": 399,
        "total_assets_2024_usd_m": 7975,
        "incorporation_country": "Singapore",
        "group_auditor": "Deloitte",
        "fleet_size_teu": 350000,
    }
    # None of these keys appear in REQUIRED_FIELDS — completeness would be 0%
    # Under the old code, this would block generation
    db.add(SectionInput(
        id=str(uuid.uuid4()),
        report_id=rid,
        section_no=4,
        input_json=json.dumps(etl_extracted),
        saved_by="analyst-test",
    ))
    await db.flush()

    # Step 3+4: trigger generation
    mock_md = "## §4 Company Profile\n\nEvergreen Marine (Asia) Pte. Ltd. is a Singapore-incorporated..."
    with patch("credit_report.generation.pipeline.generate_section_markdown",
               new_callable=AsyncMock) as mock_gen, \
         patch("credit_report.generation.pipeline.retrieve_evidence") as mock_ev:
        mock_gen.return_value = (mock_md, 512)
        mock_ev.return_value = ["Evergreen Marine Asia incorporated Singapore 2021"]

        # This MUST succeed — no ValueError, no 422
        output = await run_section_generation(
            db, rid, section_no=4, actor_user_id=_make_user_id()
        )

    assert output.status == "done", f"Expected done, got: {output.status}"
    assert output.markdown == mock_md
    # Verify the prompt builder received the ETL data
    call_args = mock_gen.call_args
    passed_input = call_args.kwargs.get("input_json") or call_args.args[1]
    assert passed_input.get("company_name") == "Evergreen Marine (Asia) Pte. Ltd."

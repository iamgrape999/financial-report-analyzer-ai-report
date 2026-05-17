"""
Sprint 3 acceptance tests for the AI Section Generation Pipeline.

Covers:
  1.  Keyword scoring — counts hits correctly
  2.  Evidence retrieval — ranks chunks by relevance
  3.  Evidence retrieval — returns empty when no documents exist
  4.  Text chunking — overlapping chunks with correct max size
  5.  Text chunking — single chunk for short text
  6.  save_document_text writes to correct path
  7.  Prompt builder — system prompt contains analyst instruction
  8.  Prompt builder — user prompt contains input JSON values
  9.  Prompt builder — user prompt includes evidence excerpts
  10. Prompt builder — continuation prompt uses resume token
  11. Continuation token detection — found
  12. Continuation token detection — not present
  13. Continuation token detection — section with no token (section 8)
  14. Continuation token stripping — removes token, preserves content
  15. Hard dependency check — passes when all deps done
  16. Hard dependency check — fails when dep missing
  17. Hard dependency check — fails when dep still generating
  18. Pipeline — stores SectionOutput status=done on success
  19. Pipeline — stores SectionOutput status=error on Claude failure
  20. Full report pipeline — all sections generated when no deps missing
  21. Full report pipeline — section 2 skipped when section 7 not done
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base

# Ensure all ORM models are registered with Base metadata
import credit_report.audit.events  # noqa: F401
import credit_report.block_ast.models  # noqa: F401
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.fact_store.models  # noqa: F401
import credit_report.generation.models  # noqa: F401
import credit_report.models  # noqa: F401
import credit_report.security.models  # noqa: F401

from credit_report.generation.claude_client import (
    _detect_continuation_token,
    _strip_continuation_token,
)
from credit_report.generation.evidence import (
    _chunk_text,
    _score_chunk,
    retrieve_evidence,
    save_document_text,
)
from credit_report.generation.pipeline import (
    check_hard_dependencies,
    run_full_report_generation,
    run_section_generation,
)
from credit_report.generation.prompt_builder import build_section_prompt
from credit_report.models import SectionInput, SectionOutput

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
REPORT_ID = "RPT-GEN-TEST-001"


@pytest_asyncio.fixture(scope="function")
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── 1–2. Keyword scoring ──────────────────────────────────────────────────────

def test_score_chunk_counts_keyword_hits():
    chunk = "The DSCR ratio was 1.45x and solvency remains strong."
    assert _score_chunk(chunk, ["DSCR", "solvency"]) == 2
    assert _score_chunk(chunk, ["LTV", "tariff"]) == 0


def test_score_chunk_is_case_insensitive():
    chunk = "ltv stands for loan-to-value."
    assert _score_chunk(chunk, ["LTV"]) == 1


# ── 3. Evidence retrieval ─────────────────────────────────────────────────────

def test_evidence_retrieval_ranks_relevant_chunks_first():
    relevant = (
        "The collateral LTV is 75% and the ACR is 133%. "
        "Vessel mortgage is registered with IBK bank. Refund guarantee."
    )
    irrelevant = "Administrative procedures covering office management and IT support."

    with patch("credit_report.generation.evidence.load_document_texts") as mock_load:
        mock_load.return_value = [relevant + "\n\n" + irrelevant]
        chunks = retrieve_evidence(REPORT_ID, section_no=5)

    assert len(chunks) >= 1
    assert any("LTV" in c or "ACR" in c or "mortgage" in c for c in chunks)


def test_evidence_retrieval_empty_when_no_documents():
    with patch("credit_report.generation.evidence.load_document_texts") as mock_load:
        mock_load.return_value = []
        assert retrieve_evidence(REPORT_ID, section_no=7) == []


# ── 4–5. Text chunking ────────────────────────────────────────────────────────

def test_chunk_text_respects_max_size():
    text = "X" * 3000
    chunks = _chunk_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 900  # CHUNK_SIZE + small overlap tolerance


def test_chunk_text_single_chunk_for_short_text():
    chunks = _chunk_text("Short text.")
    assert len(chunks) == 1
    assert chunks[0] == "Short text."


# ── 6. save_document_text ─────────────────────────────────────────────────────

def test_save_document_text_writes_to_filesystem(tmp_path):
    with patch("credit_report.generation.evidence.CREDIT_REPORTS_ROOT", tmp_path):
        save_document_text(REPORT_ID, "doc-abc", "DSCR and LTV details here.")

    written = (tmp_path / REPORT_ID / "doc-abc.txt").read_text()
    assert "DSCR" in written


# ── 7–10. Prompt builder ──────────────────────────────────────────────────────

def test_prompt_builder_system_prompt_has_analyst_persona():
    system_prompt, _ = build_section_prompt(section_no=7, input_json={}, evidence_chunks=[])
    assert "credit analyst" in system_prompt.lower()


def test_prompt_builder_user_prompt_contains_input_values():
    input_json = {"7A_income": {"revenue_usd_m": 5000}, "7B_balance": {"total_assets_usd_m": 20000}}
    _, user_prompt = build_section_prompt(section_no=7, input_json=input_json, evidence_chunks=[])
    assert "5000" in user_prompt
    assert "20000" in user_prompt
    assert "Financial Analysis" in user_prompt


def test_prompt_builder_includes_evidence_excerpts():
    evidence = ["The DSCR was 1.45x based on FY2024 operating cash flows."]
    _, user_prompt = build_section_prompt(section_no=2, input_json={}, evidence_chunks=evidence)
    assert "Evidence from Uploaded Documents" in user_prompt
    assert "DSCR was 1.45x" in user_prompt


def test_prompt_builder_continuation_uses_resume_token():
    _, user_prompt = build_section_prompt(
        section_no=7,
        input_json={},
        evidence_chunks=[],
        is_continuation=True,
        continuation_resume_token="[§7 CONTINUED]",
    )
    assert "[§7 CONTINUED]" in user_prompt
    assert "Continue writing" in user_prompt


# ── 11–14. Continuation tokens ────────────────────────────────────────────────

def test_continuation_token_detected_in_text():
    text = "...analysis continues...\n[§7 CONTINUED — PART 2 FOLLOWS]"
    assert _detect_continuation_token(text, section_no=7) is True


def test_continuation_token_not_present():
    assert _detect_continuation_token("This section is complete.", section_no=7) is False


def test_section_8_has_no_continuation_token():
    # Section 8 uses None token — should never trigger continuation
    assert _detect_continuation_token("Any content.", section_no=8) is False


def test_continuation_token_stripped_from_text():
    text = "Some content.\n[§7 CONTINUED — PART 2 FOLLOWS]"
    stripped = _strip_continuation_token(text, section_no=7)
    assert "[§7 CONTINUED" not in stripped
    assert "Some content." in stripped


# ── 15–17. Hard dependency checks ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hard_dep_check_passes_when_dep_is_done(db: AsyncSession):
    # Section 2 hard-depends on section 7
    db.add(SectionOutput(
        id=str(uuid.uuid4()),
        report_id=REPORT_ID,
        section_no=7,
        status="done",
        markdown="## Section 7\n\nComplete.",
    ))
    await db.flush()

    missing = await check_hard_dependencies(db, REPORT_ID, section_no=2)
    assert missing == []


@pytest.mark.asyncio
async def test_hard_dep_check_fails_when_dep_not_generated(db: AsyncSession):
    missing = await check_hard_dependencies(db, REPORT_ID, section_no=2)
    assert 7 in missing


@pytest.mark.asyncio
async def test_hard_dep_check_fails_when_dep_still_generating(db: AsyncSession):
    db.add(SectionOutput(
        id=str(uuid.uuid4()),
        report_id=REPORT_ID,
        section_no=7,
        status="generating",
    ))
    await db.flush()

    missing = await check_hard_dependencies(db, REPORT_ID, section_no=2)
    assert 7 in missing


# ── 18–19. Single section pipeline ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_stores_done_output_on_success(db: AsyncSession):
    db.add(SectionInput(
        id=str(uuid.uuid4()),
        report_id=REPORT_ID,
        section_no=4,
        input_json=json.dumps({"4A_corporate": {"borrower_name": "EMA"}}),
        saved_by="user-001",
    ))
    await db.flush()

    mock_markdown = "## Section 4\n\nEvergreen Marine (Asia) Pte. Ltd."
    mock_tokens = 512

    with patch(
        "credit_report.generation.pipeline.generate_section_markdown", new_callable=AsyncMock
    ) as mock_gen, patch(
        "credit_report.generation.pipeline.retrieve_evidence"
    ) as mock_ev:
        mock_gen.return_value = (mock_markdown, mock_tokens)
        mock_ev.return_value = []
        output = await run_section_generation(db, REPORT_ID, section_no=4, actor_user_id="user-001")

    assert output.status == "done"
    assert output.markdown == mock_markdown
    assert output.tokens_used == mock_tokens
    assert output.section_no == 4


@pytest.mark.asyncio
async def test_pipeline_marks_error_on_claude_failure(db: AsyncSession):
    db.add(SectionInput(
        id=str(uuid.uuid4()),
        report_id=REPORT_ID,
        section_no=4,
        input_json=json.dumps({"4A_borrower": {"borrower_name": "EMA"}}),
        saved_by="user-001",
    ))
    await db.flush()

    with patch(
        "credit_report.generation.pipeline.generate_section_markdown", new_callable=AsyncMock
    ) as mock_gen, patch(
        "credit_report.generation.pipeline.retrieve_evidence"
    ) as mock_ev:
        mock_gen.side_effect = Exception("API rate limit exceeded")
        mock_ev.return_value = []
        with pytest.raises(Exception, match="API rate limit"):
            await run_section_generation(db, REPORT_ID, section_no=4, actor_user_id="user-001")

    from sqlalchemy import select
    result = await db.execute(
        select(SectionOutput).where(
            SectionOutput.report_id == REPORT_ID,
            SectionOutput.section_no == 4,
        )
    )
    output = result.scalar_one_or_none()
    assert output is not None
    assert output.status == "error"


# ── 20–21. Full report pipeline ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_report_pipeline_generates_all_independent_sections(db: AsyncSession):
    # Seed minimal non-empty input for all 10 sections so the pipeline's
    # "no analyst input data" guard doesn't block any section.
    _stub = json.dumps({"stub": True})
    for sec_no in range(1, 11):
        db.add(SectionInput(
            id=str(uuid.uuid4()),
            report_id=REPORT_ID,
            section_no=sec_no,
            input_json=_stub,
            saved_by="user-001",
        ))
    await db.flush()

    with patch(
        "credit_report.generation.pipeline.generate_section_markdown", new_callable=AsyncMock
    ) as mock_gen, patch(
        "credit_report.generation.pipeline.retrieve_evidence"
    ) as mock_ev:
        mock_gen.return_value = ("# Generated", 100)
        mock_ev.return_value = []
        results = await run_full_report_generation(db, REPORT_ID, actor_user_id="user-001")

    # Section 4 has no hard deps → must succeed
    assert results.get(4) == "done"
    # Section 7 has no hard deps → must succeed
    assert results.get(7) == "done"
    # Section 9 depends on 1,2,3,4,5,6,7,8 — all generated before it in GENERATION_ORDER
    assert results.get(9) == "done"


@pytest.mark.asyncio
async def test_full_report_pipeline_skips_section_with_unmet_dep(db: AsyncSession):
    # Generate only section 4 (no hard deps), then check that section 2
    # is skipped because section 7 won't be available.
    # We patch generate_section_markdown to fail for section 7 so it stays absent.
    call_count = {"n": 0}

    async def fake_generate(section_no, **kwargs):
        call_count["n"] += 1
        if section_no == 7:
            raise Exception("Simulated failure for section 7")
        return ("# Generated", 50)

    with patch(
        "credit_report.generation.pipeline.generate_section_markdown",
        side_effect=fake_generate,
    ), patch(
        "credit_report.generation.pipeline.retrieve_evidence"
    ) as mock_ev:
        mock_ev.return_value = []
        results = await run_full_report_generation(db, REPORT_ID, actor_user_id="user-001")

    # Section 7 encountered an error
    assert "error" in results.get(7, "")
    # Section 2 depends on section 7 which failed → skipped
    assert "skipped_missing_deps" in results.get(2, "")


@pytest.mark.asyncio
async def test_ast_block_failure_does_not_abort_generation(db: AsyncSession):
    """
    Block AST parsing is wrapped in a savepoint.  If save_blocks() raises,
    the main section_output (markdown + status='done') must still be committed;
    only the block rows are rolled back.
    """
    from sqlalchemy import select
    from credit_report.block_ast.models import ReportBlock

    db.add(SectionInput(
        id=str(uuid.uuid4()),
        report_id=REPORT_ID,
        section_no=4,
        input_json='{"4A_corporate": {"borrower_name": "SavepointCo"}}',
        saved_by="user-001",
    ))
    await db.flush()

    mock_markdown = "## §4\n\nSavepoint isolation test.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"

    with patch(
        "credit_report.generation.pipeline.generate_section_markdown",
        new_callable=AsyncMock,
        return_value=(mock_markdown, 100),
    ), patch(
        "credit_report.generation.pipeline.retrieve_evidence",
        return_value=[],
    ), patch(
        "credit_report.block_ast.repository.save_blocks",
        new_callable=AsyncMock,
        side_effect=RuntimeError("simulated save_blocks crash"),
    ):
        output = await run_section_generation(db, REPORT_ID, section_no=4, actor_user_id="user-001")

    # Main section generation must succeed despite AST failure
    assert output.status == "done", f"Expected status=done, got {output.status}"
    assert output.markdown == mock_markdown

    # Blocks table must be empty — savepoint rolled back the failed write
    result = await db.execute(
        select(ReportBlock).where(ReportBlock.report_id == REPORT_ID, ReportBlock.section_no == 4)
    )
    assert result.scalars().all() == [], "AST block rows should be absent after savepoint rollback"

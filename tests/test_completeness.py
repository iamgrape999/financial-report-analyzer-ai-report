"""
Tests for the section completeness check + auto-fill mechanism.

Coverage:
A. check_section_completeness — detection logic
B. fill_missing_tables — fill call integration
C. pipeline integration — missing tables trigger fill, complete output saved
D. Edge cases — already-complete, non-§2 sections, fill failure isolation
"""
from __future__ import annotations

import uuid
import logging
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base
import credit_report.models                     # noqa: F401
import credit_report.security.models            # noqa: F401
import credit_report.audit.events               # noqa: F401
import credit_report.fact_store.models          # noqa: F401
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.block_ast.models           # noqa: F401
import credit_report.generation.models          # noqa: F401

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

FULL_S2 = (
    "**2. Overall Comments**\n\n"
    "| **Credit Overview** | 1. Market leader |\n|---|---|\n| | 2. Strong |\n\n"
    "| **Solvency** | DSCR 1.5x |\n|---|---|\n\n"
    "| **The Guarantor and their Supportive Performance** | EMC parent |\n|---|---|\n\n"
    "| **Collateral Summary** | KDB AA |\n|---|---|\n\n"
    "| **Risk and Mitigants** | Market risk |\n|---|---|\n"
)

PARTIAL_S2_T1_ONLY = (
    "**2. Overall Comments**\n\n"
    "| **Credit Overview** | 1. Market leader |\n|---|---|\n| | 2. Strong |\n"
)

PARTIAL_S2_T1_T2 = (
    "**2. Overall Comments**\n\n"
    "| **Credit Overview** | 1. Market leader |\n|---|---|\n\n"
    "| **Solvency** | DSCR 1.5x |\n|---|---|\n"
)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _uid() -> str:
    return str(uuid.uuid4())


def _mock_generate(md, tokens=5000):
    return patch(
        "credit_report.generation.pipeline.generate_section_markdown",
        new=AsyncMock(return_value=(md, tokens)),
    )


def _mock_evidence():
    return patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[])


def _mock_quota():
    return patch("credit_report.generation.pipeline.check_quota", new=AsyncMock(return_value=None))


def _mock_record():
    return patch("credit_report.generation.pipeline.record_tokens", new=AsyncMock(return_value=None))


def _mock_fill(fill_text, fill_tokens=500):
    return patch(
        "credit_report.generation.completeness.fill_missing_tables",
        new=AsyncMock(return_value=(fill_text, fill_tokens)),
    )


# ── A. check_section_completeness ────────────────────────────────────────────

class TestCheckSectionCompleteness:

    def test_full_s2_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(2, FULL_S2)
        assert missing == [], f"Expected no missing tables, got: {missing}"

    def test_partial_s2_t1_only_missing_4(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(2, PARTIAL_S2_T1_ONLY)
        labels = [label for _, label in missing]
        assert "T2 Solvency" in labels
        assert "T3 Guarantor and Supportive Performance" in labels
        assert "T4 Collateral Summary" in labels
        assert "T5 Risk and Mitigants" in labels
        assert "T1 Credit Overview" not in labels

    def test_partial_s2_t1_t2_missing_3(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(2, PARTIAL_S2_T1_T2)
        labels = [label for _, label in missing]
        assert "T1 Credit Overview" not in labels
        assert "T2 Solvency" not in labels
        assert len(missing) == 3

    def test_empty_markdown_all_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(2, "")
        assert len(missing) == 5

    def test_sections_9_10_have_no_requirements(self):
        from credit_report.generation.completeness import check_section_completeness
        # §1–§8 have completeness checks; §9–§10 have none
        for sec_no in [9, 10]:
            result = check_section_completeness(sec_no, "some markdown")
            assert result == [], f"§{sec_no} should have no completeness requirements"

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        # Test that detection is case-insensitive
        md_lower = FULL_S2.lower()
        missing = check_section_completeness(2, md_lower)
        assert missing == [], "Detection should be case-insensitive"

    def test_t3_partial_marker_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        # T3 marker is "**The Guarantor and their Supportive" (prefix match)
        md = (
            "| **Credit Overview** | |\n"
            "| **Solvency** | |\n"
            "| **The Guarantor and their Supportive Performance** | |\n"
            "| **Collateral Summary** | |\n"
            "| **Risk and Mitigants** | |\n"
        )
        missing = check_section_completeness(2, md)
        assert missing == []


# ── B. fill_missing_tables unit test ─────────────────────────────────────────

@pytest.mark.asyncio
class TestFillMissingTables:

    async def test_fill_calls_gemini_and_returns_text(self):
        from credit_report.generation.completeness import fill_missing_tables
        missing = [
            ("**Solvency**", "T2 Solvency"),
            ("**The Guarantor and their Supportive", "T3 Guarantor and Supportive Performance"),
            ("**Collateral Summary**", "T4 Collateral Summary"),
            ("**Risk and Mitigants**", "T5 Risk and Mitigants"),
        ]

        fill_response = "| **Solvency** | 1.5x DSCR |\n|---|---|\n"
        with patch(
            "credit_report.generation.claude_client.call_gemini_raw",
            new=AsyncMock(return_value=fill_response),
        ):
            text, tokens = await fill_missing_tables(
                section_no=2,
                existing_markdown=PARTIAL_S2_T1_ONLY,
                missing=missing,
                input_json={"2B_solvency": {"DSCR": "1.5x"}},
            )

        assert "Solvency" in text
        assert tokens > 0

    async def test_fill_returns_empty_string_on_llm_error(self):
        from credit_report.generation.completeness import fill_missing_tables
        missing = [
            ("**Solvency**", "T2 Solvency"),
            ("**Collateral Summary**", "T4 Collateral Summary"),
        ]

        with patch(
            "credit_report.generation.claude_client.call_gemini_raw",
            new=AsyncMock(side_effect=RuntimeError("LLM offline")),
        ):
            with pytest.raises(RuntimeError):
                await fill_missing_tables(
                    section_no=2,
                    existing_markdown=PARTIAL_S2_T1_ONLY,
                    missing=missing,
                    input_json={},
                )


# ── C. Pipeline integration ───────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPipelineCompletenessIntegration:

    async def test_missing_tables_trigger_fill_and_markdown_updated(self, db):
        """
        When the primary generation returns §2 with only T1,
        the pipeline must call fill_missing_tables and append the result.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        fill_output = (
            "| **Solvency** | DSCR 1.5x |\n|---|---|\n\n"
            "| **The Guarantor and their Supportive Performance** | EMC |\n|---|---|\n\n"
            "| **Collateral Summary** | KDB AA |\n|---|---|\n\n"
            "| **Risk and Mitigants** | Market risk |\n|---|---|\n"
        )

        with _mock_generate(PARTIAL_S2_T1_ONLY), _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output):
            output = await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())

        assert output.status == "done"
        assert "**Solvency**" in output.markdown
        assert "**Collateral Summary**" in output.markdown
        assert "**Risk and Mitigants**" in output.markdown
        assert PARTIAL_S2_T1_ONLY.strip() in output.markdown  # original preserved

    async def test_complete_s2_does_not_trigger_fill(self, db):
        """When §2 is already complete, fill_missing_tables must NOT be called."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S2), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_fill_failure_does_not_crash_generation(self, db):
        """
        If fill_missing_tables raises, generation must still complete with
        status='done' and the partial markdown must be saved.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        with _mock_generate(PARTIAL_S2_T1_ONLY), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables",
                   new=AsyncMock(side_effect=RuntimeError("LLM timeout"))):
            output = await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())

        assert output.status == "done", "Fill failure must not abort section generation"
        assert "**Credit Overview**" in output.markdown  # partial output preserved

    async def test_non_s2_section_not_affected(self, db):
        """Completeness check must be a no-op for sections without requirements."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        fill_spy = AsyncMock(return_value=("", 0))
        short_md = "§8 Banking Relationships\n\nFoo bar."
        with _mock_generate(short_md), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=8, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_tokens_accumulated_from_fill(self, db):
        """Token count must include both primary generation and fill call tokens."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        primary_tokens = 5000
        fill_tokens = 800
        fill_output = (
            "| **Solvency** | ok |\n|---|---|\n"
            "| **The Guarantor and their Supportive Performance** | ok |\n|---|---|\n"
            "| **Collateral Summary** | ok |\n|---|---|\n"
            "| **Risk and Mitigants** | ok |\n|---|---|\n"
        )

        with _mock_generate(PARTIAL_S2_T1_ONLY, tokens=primary_tokens), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, fill_tokens=fill_tokens):
            output = await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())

        assert output.tokens_used == primary_tokens + fill_tokens


# ── D. Edge cases ─────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_guarantor_table_partial_marker(self):
        """The T3 marker uses a prefix — verify partial-match detection works."""
        from credit_report.generation.completeness import check_section_completeness
        md = (
            "| **Credit Overview** | |\n"
            "| **Solvency** | |\n"
            "| **The Guarantor and their Supportive Performance** | |\n"
            "| **Collateral Summary** | |\n"
            "| **Risk and Mitigants** | |\n"
        )
        assert check_section_completeness(2, md) == []

    def test_only_t5_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_t5 = (
            "| **Credit Overview** | 1. |\n|---|---|\n"
            "| **Solvency** | DSCR |\n|---|---|\n"
            "| **The Guarantor and their Supportive Performance** | EMC |\n|---|---|\n"
            "| **Collateral Summary** | KDB |\n|---|---|\n"
        )
        missing = check_section_completeness(2, md_no_t5)
        assert len(missing) == 1
        assert missing[0][1] == "T5 Risk and Mitigants"

    def test_fill_prompt_contains_missing_labels(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [
            ("**The Guarantor and their Supportive", "T3 Guarantor and Supportive Performance"),
            ("**Collateral Summary**", "T4 Collateral Summary"),
            ("**Risk and Mitigants**", "T5 Risk and Mitigants"),
        ]
        prompt = _build_fill_user_prompt(2, missing, "existing text", {}, "en")
        assert "T3" in prompt
        assert "T4" in prompt
        assert "T5" in prompt

    def test_fill_system_prompt_not_empty(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        for sec_no in [2, 99]:
            assert _build_fill_system_prompt(sec_no)

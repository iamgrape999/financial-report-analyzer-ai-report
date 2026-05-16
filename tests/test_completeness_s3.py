"""
§3 Completeness check tests — Credit Ratings.

§3 has exactly 4 unconditional mandatory sub-sections (all always required,
unlike §1's conditional logic):
  ① External Ratings        → **External ratings:**
  ② Internal Ratings (MSR)  → **Internal ratings:**
  ③ MAS 612 Loan Grading    → **MAS 612 Loan Grading:**
  ④ ESG Rating              → **ESG ratings:**

Primary token budget raised from 8 192 → 12 288 (multi-entity MSR override
remarks can be verbose; MAS 612 requires 4 separate paragraphs).
Fill budget is 6 144 tokens.

Coverage:
A. Detection — full, partial, empty markdown
B. Conditional boundary — §3 only, not other sections
C. Pipeline integration — fill triggered, failure isolated, tokens accumulated
D. Fill prompt content — correct rules for MSR 6-col, MAS 612 4-para, ESG 4-line
E. Config — §3 primary token budget
"""
from __future__ import annotations

import uuid
import json
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


# ── Fixtures ──────────────────────────────────────────────────────────────────

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


def _uid():
    return str(uuid.uuid4())


def _mock_generate(md, tokens=6000):
    return patch(
        "credit_report.generation.pipeline.generate_section_markdown",
        new=AsyncMock(return_value=(md, tokens)),
    )


def _mock_evidence():
    return patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[])


def _mock_quota():
    return patch("credit_report.generation.pipeline.check_quota", new=AsyncMock(return_value=None))


def _mock_record():
    return patch("credit_report.generation.pipeline.reserve_and_record_tokens", new=AsyncMock(return_value=None))


def _mock_fill(text, tokens=800):
    return patch(
        "credit_report.generation.completeness.fill_missing_tables",
        new=AsyncMock(return_value=(text, tokens)),
    )


# ── Shared markdown stubs ─────────────────────────────────────────────────────

FULL_S3 = (
    "**3. Credit Ratings**\n\n"
    "**External ratings:** NIL. Evergreen Marine (Asia) Pte. Ltd. and "
    "Evergreen Marine Corporation (Taiwan) Ltd. are not externally rated.\n\n"
    "**Internal ratings:**\n\n"
    "| **Entity** | **2022/23** | **2024** | **Jul 2025** | **Nov 2025** | **Remarks** |\n"
    "|---|---|---|---|---|---|\n"
    "| (blank) | (blank) | (blank) | Generated | Generated | Proposed |\n"
    "| **Evergreen Marine (Asia) Pte. Ltd. (EMA) — Borrower** | 5+ | 5 | MSR 5 | MSR 5 (Override) | "
    "Generated MSR of 5 based on FY2024 consolidated financials. "
    "Ernst & Young with unqualified opinion. "
    "Proposed to manual override to MSR 5. "
    "Previous approved Final MSR was 5+, Current Proposed Final MSR of 5 is a decrease of 1 notch. |\n"
    "| **Evergreen Marine Corporation (Taiwan) Ltd. (EMC) — Guarantor** | 3+ | 3+ | MSR 3+ | MSR 3+ | — |\n\n"
    "**MAS 612 Loan Grading:**\n\n"
    "Borrower is internally rated as MSR 5, which is mapped to **\"PASS\"** under the "
    "\"MSR – MAS 612 Loan Classification Mapping\" matrix. "
    "We recommend the MAS Notice 612 loan grading for the Borrower to be **\"PASS\"**, "
    "in view that the Borrower does not exhibit potential weakness in repayment capability.\n\n"
    "The account conduct has been satisfactory throughout the review period.\n\n"
    "EMA had a net cash position of TWD 98.2 billion (equivalent to USD 3.0 billion) as at 9M2025, "
    "broadly in line with the FY2024 net cash level (See Section 7: Financial Analysis).\n\n"
    "Financial Projections of Borrower EMA (See Section 7) demonstrates capability to meet debt "
    "and lease liability obligations throughout.\n\n"
    "**ESG ratings:**\n\nEMA:\nESG Rating Date: 15 Nov 2025\n[System-generated ESG rating image]\n"
)

PARTIAL_S3_MISSING_MAS_ESG = (
    "**3. Credit Ratings**\n\n"
    "**External ratings:** NIL. EMA and EMC are not externally rated.\n\n"
    "**Internal ratings:**\n\n"
    "| **Entity** | **2022/23** | **2024** | **Jul 2025** | **Nov 2025** | **Remarks** |\n"
    "|---|---|---|---|---|---|\n"
    "| (blank) | (blank) | (blank) | Generated | Generated | Proposed |\n"
    "| **EMA — Borrower** | 5+ | 5 | MSR 5 | MSR 5 (Override) | Generated MSR of 5. |\n"
    "| **EMC — Guarantor** | 3+ | 3+ | MSR 3+ | MSR 3+ | — |\n"
    # MAS 612 and ESG missing
)

PARTIAL_S3_ONLY_EXTERNAL = (
    "**3. Credit Ratings**\n\n"
    "**External ratings:** NIL. EMA and EMC are not externally rated.\n"
    # Internal ratings, MAS 612, ESG all missing
)

S3_INPUT = {
    "3A_external_ratings": {"all_nil": True, "entities": ["EMA", "EMC"]},
    "3B_internal_ratings": {
        "rows": [
            {
                "entity_full_name": "Evergreen Marine (Asia) Pte. Ltd.",
                "entity_abbrev": "EMA",
                "role": "Borrower",
                "fy2022_23": "5+",
                "fy2024": "5",
                "interim": "MSR 5",
                "current": "MSR 5",
                "override_flag": True,
                "override_remarks": "Generated MSR of 5 based on FY2024 financials.",
            },
        ],
        "period_display_labels": {
            "fy2022_23": "2022/23",
            "fy2024": "2024",
            "interim": "Jul 2025",
            "current": "Nov 2025",
        },
    },
    "3C_mas_612": {
        "msr": "5",
        "account_conduct": "satisfactory",
        "net_cash_twd": "98.2 billion",
        "net_cash_usd": "3.0 billion",
    },
    "3D_esg_rating": {
        "entity_abbrev": "EMA",
        "date": "15 Nov 2025",
        "image_ref": "[System-generated ESG rating image]",
    },
}


# ── A. Detection ──────────────────────────────────────────────────────────────

class TestSection3Detection:

    def test_full_s3_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(3, FULL_S3)
        assert missing == [], f"Full §3 should have no gaps, got: {[l for _, l in missing]}"

    def test_missing_mas612_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(3, PARTIAL_S3_MISSING_MAS_ESG)
        labels = [l for _, l in missing]
        assert "MAS 612 Loan Grading (4 paragraphs)" in labels

    def test_missing_esg_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(3, PARTIAL_S3_MISSING_MAS_ESG)
        labels = [l for _, l in missing]
        assert "ESG Rating" in labels

    def test_missing_internal_ratings_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(3, PARTIAL_S3_ONLY_EXTERNAL)
        labels = [l for _, l in missing]
        assert any("Internal Ratings" in l for l in labels)
        assert "MAS 612 Loan Grading (4 paragraphs)" in labels
        assert "ESG Rating" in labels

    def test_empty_markdown_all_four_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(3, "")
        assert len(missing) == 4

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(3, FULL_S3.lower())
        assert missing == [], "Detection must be case-insensitive"

    def test_only_external_ratings_present(self):
        from credit_report.generation.completeness import check_section_completeness
        md = "**External ratings:** NIL. EMA is not externally rated.\n"
        missing = check_section_completeness(3, md)
        labels = [l for _, l in missing]
        assert "External Ratings" not in labels
        assert len(missing) == 3  # Internal, MAS 612, ESG missing

    def test_all_four_present_counted_correctly(self):
        from credit_report.generation.completeness import check_section_completeness
        # Minimal markdown with all 4 markers — MSR table must include at least one entity row
        md = (
            "**External ratings:** NIL.\n\n"
            "**Internal ratings:**\n"
            "| Entity | 2024 | Remarks |\n"
            "|---|---|---|\n"
            "| EMA — Borrower | MSR 5 | Generated. |\n\n"
            "**MAS 612 Loan Grading:**\n\nPara 1.\n\nPara 2.\n\n"
            "**ESG ratings:**\nEMA:\nESG Rating Date: 1 Jan 2025\n[image]\n"
        )
        missing = check_section_completeness(3, md)
        assert missing == []

    def test_msr_table_header_only_flagged(self):
        """MSR header + separator row but NO entity data rows → flagged as missing."""
        from credit_report.generation.completeness import check_section_completeness
        # This is the exact "weird format" bug from production: table header rendered
        # but no rows below it
        md = (
            "**External ratings:** NIL.\n\n"
            "**Internal ratings:**\n"
            "| Entity | FY2023 | FY2024 | Aug 2025 | Dec 2025 |\n"
            "|--------|--------|--------|----------|----------|\n\n"
            "**MAS 612 Loan Grading:**\n\nPara 1.\n\n"
            "**ESG ratings:**\nEMA:\nESG Rating Date: 1 Jan 2025\n[image]\n"
        )
        missing = check_section_completeness(3, md)
        labels = [l for _, l in missing]
        assert any("Internal Ratings" in l for l in labels), (
            "MSR table with header-only (no entity data rows) must be detected as incomplete"
        )

    def test_msr_header_only_no_separator_flagged(self):
        """MSR header row only (no separator, no data) → flagged as missing."""
        from credit_report.generation.completeness import check_section_completeness
        md = (
            "**External ratings:** NIL.\n\n"
            "**Internal ratings:**\n"
            "| Entity | FY2023 | FY2024 | Aug 2025 | Dec 2025 |\n\n"
            "**MAS 612 Loan Grading:**\n\nPara 1.\n\n"
            "**ESG ratings:**\nEMA:\nESG Rating Date: 1 Jan 2025\n[image]\n"
        )
        missing = check_section_completeness(3, md)
        labels = [l for _, l in missing]
        assert any("Internal Ratings" in l for l in labels)


# ── B. §3 only, not other sections ────────────────────────────────────────────

class TestSection3Isolation:

    def test_s3_check_does_not_affect_s1(self):
        from credit_report.generation.completeness import check_section_completeness
        # §1 check requires input_json; with empty json only Facility Table is checked
        result = check_section_completeness(1, FULL_S3, {})
        # FULL_S3 has no "Proposed Facility" so facility table would be flagged,
        # but that's §1 logic — here we just confirm no crash
        assert isinstance(result, list)

    def test_s3_check_does_not_affect_s2(self):
        from credit_report.generation.completeness import check_section_completeness
        # §2 checks for T1-T5 bold headers — FULL_S3 doesn't have them
        result = check_section_completeness(2, FULL_S3)
        labels = [l for _, l in result]
        # §2 markers absent from §3 content → all flagged as missing
        # Confirms §3 detection is independent of §2
        assert "T1 Credit Overview" in labels

    def test_sections_5_to_10_unaffected(self):
        from credit_report.generation.completeness import check_section_completeness
        # §4–§7 now have their own completeness checks; verify §8-§10 have none
        # §9 now has its own completeness check; only §8 and §10 have none
        for sec in [8, 10]:
            result = check_section_completeness(sec, "any content")
            assert result == [], f"§{sec} should have no completeness requirements"


# ── C. Pipeline integration ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_with_s3_input(db):
    """DB fixture pre-seeded with a report and §3 SectionInput."""
    from credit_report.models import Report, SectionInput
    rid = _uid()
    db.add(Report(id=rid, industry="marine", created_by=_uid()))
    db.add(SectionInput(
        report_id=rid,
        section_no=3,
        input_json=json.dumps(S3_INPUT),
        saved_by=_uid(),
    ))
    await db.flush()
    return db, rid


@pytest.mark.asyncio
class TestSection3PipelineIntegration:

    async def test_missing_mas612_and_esg_triggers_fill(self, db_with_s3_input):
        db, rid = db_with_s3_input
        from credit_report.generation.pipeline import run_section_generation

        fill_output = (
            "**MAS 612 Loan Grading:**\n\n"
            "Borrower is internally rated as MSR 5, which is mapped to **\"PASS\"** "
            "under the \"MSR – MAS 612 Loan Classification Mapping\" matrix. "
            "We recommend the MAS Notice 612 loan grading for the Borrower to be **\"PASS\"**, "
            "in view that the Borrower does not exhibit potential weakness in repayment capability.\n\n"
            "The account conduct has been satisfactory.\n\n"
            "EMA had a net cash position of TWD 98.2 billion (See Section 7: Financial Analysis).\n\n"
            "Financial Projections of Borrower EMA (See Section 7) demonstrates capability to meet "
            "debt and lease liability obligations throughout.\n\n"
            "**ESG ratings:**\n\nEMA:\nESG Rating Date: 15 Nov 2025\n[System-generated ESG rating image]\n"
        )

        with _mock_generate(PARTIAL_S3_MISSING_MAS_ESG, tokens=5000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=700):
            output = await run_section_generation(db, rid, section_no=3, actor_user_id=_uid())

        assert output.status == "done"
        assert "**MAS 612 Loan Grading:**" in output.markdown
        assert "**ESG ratings:**" in output.markdown
        assert PARTIAL_S3_MISSING_MAS_ESG.strip() in output.markdown

    async def test_complete_s3_does_not_trigger_fill(self, db_with_s3_input):
        db, rid = db_with_s3_input
        from credit_report.generation.pipeline import run_section_generation

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S3, tokens=8000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=3, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_fill_failure_does_not_crash_s3(self, db_with_s3_input):
        db, rid = db_with_s3_input
        from credit_report.generation.pipeline import run_section_generation

        with _mock_generate(PARTIAL_S3_MISSING_MAS_ESG, tokens=5000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables",
                   new=AsyncMock(side_effect=RuntimeError("LLM error"))):
            output = await run_section_generation(db, rid, section_no=3, actor_user_id=_uid())

        assert output.status == "done"
        assert "**Internal ratings:**" in output.markdown  # partial preserved

    async def test_tokens_accumulated_from_s3_fill(self, db_with_s3_input):
        db, rid = db_with_s3_input
        from credit_report.generation.pipeline import run_section_generation

        primary_tokens = 5000
        fill_tokens = 700
        fill_output = (
            "**MAS 612 Loan Grading:**\n\nPara 1 PASS.\n\nPara 2.\n\nPara 3 (See Section 7: Financial Analysis).\n\n"
            "Para 4 capability.\n\n**ESG ratings:**\n\nEMA:\nESG Rating Date: 15 Nov 2025\n[image]\n"
        )
        with _mock_generate(PARTIAL_S3_MISSING_MAS_ESG, tokens=primary_tokens), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=fill_tokens):
            output = await run_section_generation(db, rid, section_no=3, actor_user_id=_uid())

        assert output.tokens_used == primary_tokens + fill_tokens

    async def test_s3_completeness_warning_logged(self, db_with_s3_input, caplog):
        db, rid = db_with_s3_input
        from credit_report.generation.pipeline import run_section_generation

        fill_output = (
            "**MAS 612 Loan Grading:**\n\nPass.\n\n**ESG ratings:**\n\nEMA:\nDate: Nov 2025\n[image]\n"
        )
        with _mock_generate(PARTIAL_S3_MISSING_MAS_ESG, tokens=5000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output), \
             caplog.at_level(logging.WARNING, logger="credit_report.generation.pipeline"):
            await run_section_generation(db, rid, section_no=3, actor_user_id=_uid())

        warns = [r for r in caplog.records if "[Completeness]" in r.message and r.levelno >= logging.WARNING]
        assert warns, "Completeness gap must be logged as WARNING before fill is triggered"


# ── D. Fill prompt content ────────────────────────────────────────────────────

class TestSection3FillPrompts:

    def test_system_prompt_mentions_6_columns(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(3)
        assert "6" in prompt
        assert "6-column" in prompt or "6 columns" in prompt

    def test_system_prompt_mentions_4_paragraphs(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(3)
        assert "4" in prompt
        assert "paragraph" in prompt.lower()

    def test_system_prompt_mentions_esg_4_lines(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(3)
        assert "4 lines" in prompt or "ESG" in prompt

    def test_user_prompt_contains_missing_labels(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [
            ("**MAS 612 Loan Grading:**", "MAS 612 Loan Grading (4 paragraphs)"),
            ("**ESG ratings:**", "ESG Rating"),
        ]
        prompt = _build_fill_user_prompt(3, missing, PARTIAL_S3_MISSING_MAS_ESG, S3_INPUT, "en")
        assert "MAS 612" in prompt
        assert "ESG" in prompt

    def test_user_prompt_contains_critical_rules(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**MAS 612 Loan Grading:**", "MAS 612 Loan Grading (4 paragraphs)")]
        prompt = _build_fill_user_prompt(3, missing, "existing text", S3_INPUT, "en")
        assert "4 SEPARATE paragraphs" in prompt or "4 separate paragraphs" in prompt
        assert "not bullets" in prompt.lower() or "NOT bullets" in prompt

    def test_user_prompt_includes_input_json(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**ESG ratings:**", "ESG Rating")]
        prompt = _build_fill_user_prompt(3, missing, "existing", S3_INPUT, "en")
        assert "3D_esg_rating" in prompt or "EMA" in prompt

    def test_fill_budget_is_6144(self):
        import inspect
        from credit_report.generation import completeness
        src = inspect.getsource(completeness.fill_missing_tables)
        assert "6144" in src, "§3 fill budget must be 6 144 tokens"


# ── E. Config token budget ────────────────────────────────────────────────────

class TestSection3Config:

    def test_s3_primary_budget_is_12288(self):
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        assert SECTION_MAX_OUTPUT_TOKENS.get(3) == 12288, (
            "§3 primary token budget must be 12 288 (multi-entity MSR + MAS 612 4 paragraphs + ESG)"
        )

    def test_s3_budget_higher_than_default(self):
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        default = SECTION_MAX_OUTPUT_TOKENS.get("default", 8192)
        s3 = SECTION_MAX_OUTPUT_TOKENS.get(3, default)
        assert s3 > default, "§3 budget must exceed the 8 192 default"

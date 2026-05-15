"""
§4 Completeness check tests — Corporate History and Overview.

§4 has 10 unconditional mandatory sub-sections (all always required):
  ① C-1 Corporate Identity          → **C-1.
  ② C-2 Ownership & Group Structure → **C-2.
  ③ C-3 Key Management              → **C-3.
  ④ C-4 Business Overview           → **C-4.
  ⑤ C-5 Revenue & Fin. Highlights   → **C-5.
  ⑥ C-6 Fleet Profile               → **C-6.
  ⑦ C-7 Debt Profile                → **C-7.
  ⑧ C-8 Market Analysis             → **C-8.
  ⑨ C-9 Peer Comparison             → **C-9.
  ⑩ Banking Relationships (§E)      → banking relationships

Truncation risk is highest at C-8, C-9, and Banking Relationships (end of
section). The primary token budget is already 12 288. Fill budget is 8 192.

Coverage:
A. Detection — full, partial, empty markdown
B. Conditional boundary — §4 only, not other sections
C. Pipeline integration — fill triggered, failure isolated, tokens accumulated
D. Fill prompt content — correct labels and rules
E. Config — §4 primary token budget
"""
from __future__ import annotations

import uuid
import json
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


def _mock_generate(md, tokens=8000):
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


def _mock_fill(text, tokens=800):
    return patch(
        "credit_report.generation.completeness.fill_missing_tables",
        new=AsyncMock(return_value=(text, tokens)),
    )


# ── Shared markdown stubs ─────────────────────────────────────────────────────

FULL_S4 = (
    "**4. Corporate History and Overview**\n\n"
    "**C-1. Corporate Identity**\n"
    "| Item | Detail |\n|---|---|\n"
    "| English Name | Evergreen Marine (Asia) Pte. Ltd. |\n"
    "| UBN / Registration No. | 202045678A |\n\n"
    "**C-2. Ownership & Group Structure**\n"
    "| Name | Stake % | Country | Notes |\n|---|---|---|---|\n"
    "| Evergreen Marine Corporation | 100.0% | Taiwan | Listed on TWSE |\n"
    "The ultimate beneficial owner is Chang Yung-fa Foundation.\n\n"
    "**C-3. Key Management**\n"
    "| Name | Title | Experience (years) | Background |\n|---|---|---|---|\n"
    "| John Chen | CEO | 25 | Former VP at COSCO |\n"
    "Management team has demonstrated stability.\n\n"
    "**C-4. Business Overview**\n"
    "Evergreen Marine (Asia) Pte. Ltd. operates as a leading container shipping company.\n\n"
    "**C-5. Revenue & Financial Highlights**\n"
    "| Segment | FY Revenue | % of Total |\n|---|---|---|\n"
    "| Container Shipping | USD 5,000m | 100.0% |\n\n"
    "**C-6. Fleet Profile**\n"
    "| Category | No. of Vessels | Total TEU | Total DWT | Notes |\n|---|---|---|---|---|\n"
    "| Owned | 50 | 200,000 | 3,000,000 | — |\n"
    "| Total | 50 | 200,000 | 3,000,000 | |\n\n"
    "**C-7. Debt Profile**\n"
    "| Lender | Facility Type | CCY | Amount | Maturity | Secured/Unsecured |\n"
    "|---|---|---|---|---|---|\n"
    "| CUB | Term Loan | USD | 50m | 2029 | Secured |\n\n"
    "**C-8. Market Analysis**\n"
    "Container shipping rates (CCFI/SCFI) declined in 2024 due to capacity oversupply.\n"
    "Order book stands at 24% of existing fleet capacity.\n\n"
    "**C-9. Peer Comparison**\n"
    "| Company | Fleet TEU | Market Share % | Alliance | Listed |\n|---|---|---|---|---|\n"
    "| **Evergreen Marine (Asia) Pte. Ltd.** | **200,000** | **5.0%** | **Ocean Alliance** | **Y** |\n"
    "| MSC | 4,500,000 | 20.0% | None | N |\n"
    "Borrower ranks among the top-5 global container lines.\n\n"
    "**Banking Relationships**\n"
    "| Bank | Product | Limit (USD m) | Since |\n|---|---|---|---|\n"
    "| Cathay United Bank | Term Loan | 50 | 2022 |\n"
)

PARTIAL_S4_C1_TO_C7 = (
    "**4. Corporate History and Overview**\n\n"
    "**C-1. Corporate Identity**\n"
    "| Item | Detail |\n|---|---|\n"
    "| English Name | Evergreen Marine (Asia) Pte. Ltd. |\n\n"
    "**C-2. Ownership & Group Structure**\n"
    "| Name | Stake % | Country | Notes |\n|---|---|---|---|\n"
    "| Evergreen Marine Corporation | 100.0% | Taiwan | Listed |\n\n"
    "**C-3. Key Management**\n"
    "| Name | Title | Experience (years) | Background |\n|---|---|---|---|\n"
    "| John Chen | CEO | 25 | Former VP |\n\n"
    "**C-4. Business Overview**\n"
    "EMA operates as a leading container shipping company.\n\n"
    "**C-5. Revenue & Financial Highlights**\n"
    "Revenue for FY2024 was USD 5,000m.\n\n"
    "**C-6. Fleet Profile**\n"
    "| Category | No. of Vessels | Total TEU | Total DWT | Notes |\n|---|---|---|---|---|\n"
    "| Owned | 50 | 200,000 | 3,000,000 | — |\n\n"
    "**C-7. Debt Profile**\n"
    "| Lender | Facility Type | CCY | Amount | Maturity | Secured/Unsecured |\n"
    "|---|---|---|---|---|---|\n"
    "| CUB | Term Loan | USD | 50m | 2029 | Secured |\n"
    # C-8, C-9, Banking Relationships missing
)

PARTIAL_S4_C1_ONLY = (
    "**4. Corporate History and Overview**\n\n"
    "**C-1. Corporate Identity**\n"
    "| Item | Detail |\n|---|---|\n"
    "| English Name | Evergreen Marine (Asia) Pte. Ltd. |\n"
    # C-2 through C-9 and Banking Relationships missing
)

S4_INPUT = {
    "4A_corporate_identity": {
        "english_name": "Evergreen Marine (Asia) Pte. Ltd.",
        "ubn": "202045678A",
    },
    "4B_ownership": {
        "shareholders": [{"name": "Evergreen Marine Corporation", "stake_pct": 100.0}],
    },
    "4C_key_management": [
        {"name": "John Chen", "title": "CEO", "experience_years": 25},
    ],
    "4E_banking_relationships": [
        {"bank": "Cathay United Bank", "product": "Term Loan", "limit_usd_m": 50},
    ],
}


# ── A. Detection ──────────────────────────────────────────────────────────────

class TestSection4Detection:

    def test_full_s4_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(4, FULL_S4)
        assert missing == [], f"Full §4 should have no gaps, got: {[l for _, l in missing]}"

    def test_empty_markdown_all_ten_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(4, "")
        assert len(missing) == 10, f"Expected 10 missing, got {len(missing)}: {[l for _, l in missing]}"

    def test_partial_c1_to_c7_missing_3(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(4, PARTIAL_S4_C1_TO_C7)
        labels = [l for _, l in missing]
        assert "C-8 Market Analysis" in labels
        assert "C-9 Peer Comparison" in labels
        assert "Banking Relationships Table (Section E)" in labels
        assert len(missing) == 3

    def test_partial_c1_only_missing_9(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(4, PARTIAL_S4_C1_ONLY)
        labels = [l for _, l in missing]
        assert "C-1 Corporate Identity" not in labels
        assert "C-2 Ownership & Group Structure" in labels
        assert "C-9 Peer Comparison" in labels
        assert "Banking Relationships Table (Section E)" in labels
        assert len(missing) == 9

    def test_only_c8_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_c8 = FULL_S4.replace("**C-8. Market Analysis**\n", "")
        missing = check_section_completeness(4, md_no_c8)
        labels = [l for _, l in missing]
        assert len(missing) == 1
        assert missing[0][1] == "C-8 Market Analysis"

    def test_only_c9_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_c9 = FULL_S4.replace("**C-9. Peer Comparison**\n", "")
        missing = check_section_completeness(4, md_no_c9)
        labels = [l for _, l in missing]
        assert len(missing) == 1
        assert missing[0][1] == "C-9 Peer Comparison"

    def test_only_banking_relationships_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_banking = FULL_S4.replace("**Banking Relationships**\n", "")
        missing = check_section_completeness(4, md_no_banking)
        labels = [l for _, l in missing]
        assert len(missing) == 1
        assert missing[0][1] == "Banking Relationships Table (Section E)"

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(4, FULL_S4.lower())
        assert missing == [], "Detection must be case-insensitive"

    def test_banking_relationships_detected_by_lowercase_marker(self):
        from credit_report.generation.completeness import check_section_completeness
        # Marker is lowercase 'banking relationships' — test explicit detection
        md = FULL_S4.replace("**Banking Relationships**", "banking relationships")
        missing = check_section_completeness(4, md)
        assert missing == []

    def test_all_c_markers_detected_with_descriptions(self):
        from credit_report.generation.completeness import check_section_completeness
        # Minimal markdown with all 10 markers present
        md = (
            "**C-1. Corporate Identity**\n"
            "**C-2. Ownership & Group Structure**\n"
            "**C-3. Key Management**\n"
            "**C-4. Business Overview**\n"
            "**C-5. Revenue & Financial Highlights**\n"
            "**C-6. Fleet Profile**\n"
            "**C-7. Debt Profile**\n"
            "**C-8. Market Analysis**\n"
            "**C-9. Peer Comparison**\n"
            "Banking Relationships\n"
        )
        missing = check_section_completeness(4, md)
        assert missing == []


# ── B. §4 only, not other sections ────────────────────────────────────────────

class TestSection4Isolation:

    def test_s4_check_does_not_affect_s2(self):
        from credit_report.generation.completeness import check_section_completeness
        # §2 looks for T1-T5 bold headers — FULL_S4 doesn't have them
        result = check_section_completeness(2, FULL_S4)
        labels = [l for _, l in result]
        assert "T1 Credit Overview" in labels

    def test_s4_check_does_not_affect_s3(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(3, FULL_S4)
        labels = [l for _, l in result]
        assert "External Ratings" in labels

    def test_sections_5_to_10_unaffected(self):
        from credit_report.generation.completeness import check_section_completeness
        for sec in [5, 6, 7, 8, 9, 10]:
            result = check_section_completeness(sec, FULL_S4)
            assert result == [], f"§{sec} should have no completeness requirements"

    def test_s4_check_not_triggered_for_s3_content(self):
        from credit_report.generation.completeness import check_section_completeness
        s3_md = "**External ratings:** NIL.\n**Internal ratings:**\n**MAS 612 Loan Grading:**\n**ESG ratings:**\n"
        result = check_section_completeness(4, s3_md)
        # §4 check on §3 content → all C-1..C-9 + banking missing
        assert len(result) == 10


# ── C. Pipeline integration ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_with_s4_input(db):
    """DB fixture pre-seeded with a report and §4 SectionInput."""
    from credit_report.models import Report, SectionInput
    rid = _uid()
    db.add(Report(id=rid, industry="marine", created_by=_uid()))
    db.add(SectionInput(
        report_id=rid,
        section_no=4,
        input_json=json.dumps(S4_INPUT),
        saved_by=_uid(),
    ))
    await db.flush()
    return db, rid


@pytest.mark.asyncio
class TestSection4PipelineIntegration:

    async def test_missing_c8_c9_banking_triggers_fill(self, db_with_s4_input):
        """When §4 is missing C-8, C-9, and Banking Relationships, fill must be called."""
        db, rid = db_with_s4_input
        from credit_report.generation.pipeline import run_section_generation

        fill_output = (
            "**C-8. Market Analysis**\n"
            "Container shipping rates declined in 2024. Order book at 24% of fleet.\n\n"
            "**C-9. Peer Comparison**\n"
            "| Company | Fleet TEU | Market Share % | Alliance | Listed |\n|---|---|---|---|---|\n"
            "| **Evergreen Marine (Asia) Pte. Ltd.** | **200,000** | **5.0%** | **Ocean Alliance** | **Y** |\n"
            "| MSC | 4,500,000 | 20.0% | None | N |\n\n"
            "**Banking Relationships**\n"
            "| Bank | Product | Limit (USD m) | Since |\n|---|---|---|---|\n"
            "| Cathay United Bank | Term Loan | 50 | 2022 |\n"
        )

        with _mock_generate(PARTIAL_S4_C1_TO_C7, tokens=9000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=600):
            output = await run_section_generation(db, rid, section_no=4, actor_user_id=_uid())

        assert output.status == "done"
        assert "**C-8." in output.markdown
        assert "**C-9." in output.markdown
        assert "Banking Relationships" in output.markdown
        assert PARTIAL_S4_C1_TO_C7.strip() in output.markdown

    async def test_complete_s4_does_not_trigger_fill(self, db_with_s4_input):
        """When §4 is already complete, fill must NOT be called."""
        db, rid = db_with_s4_input
        from credit_report.generation.pipeline import run_section_generation

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S4, tokens=10000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=4, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_fill_failure_does_not_crash_generation(self, db_with_s4_input):
        """If fill_missing_tables raises, generation completes with status='done'."""
        db, rid = db_with_s4_input
        from credit_report.generation.pipeline import run_section_generation

        with _mock_generate(PARTIAL_S4_C1_TO_C7, tokens=9000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables",
                   new=AsyncMock(side_effect=RuntimeError("LLM timeout"))):
            output = await run_section_generation(db, rid, section_no=4, actor_user_id=_uid())

        assert output.status == "done", "Fill failure must not abort section generation"
        assert "**C-1." in output.markdown  # partial output preserved

    async def test_tokens_accumulated_from_fill(self, db_with_s4_input):
        """Token count must include both primary generation and fill call tokens."""
        db, rid = db_with_s4_input
        from credit_report.generation.pipeline import run_section_generation

        primary_tokens = 9000
        fill_tokens = 700
        fill_output = (
            "**C-8. Market Analysis**\nContainer rates declined.\n\n"
            "**C-9. Peer Comparison**\n"
            "| Company | Fleet TEU | Market Share % | Alliance | Listed |\n|---|---|---|---|---|\n"
            "| **EMA** | **200,000** | **5.0%** | **Ocean Alliance** | **Y** |\n\n"
            "**Banking Relationships**\n"
            "| Bank | Product | Limit (USD m) | Since |\n|---|---|---|---|\n"
            "| CUB | Term Loan | 50 | 2022 |\n"
        )

        with _mock_generate(PARTIAL_S4_C1_TO_C7, tokens=primary_tokens), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=fill_tokens):
            output = await run_section_generation(db, rid, section_no=4, actor_user_id=_uid())

        assert output.tokens_used == primary_tokens + fill_tokens

    async def test_non_s4_section_not_affected(self, db):
        """Completeness check must be a no-op for sections without §4 content."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        fill_spy = AsyncMock(return_value=("", 0))
        short_md = "§5 Collateral Analysis\n\nFoo bar."
        with _mock_generate(short_md), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=5, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()


# ── D. Fill prompt content ─────────────────────────────────────────────────────

class TestSection4FillPrompts:

    def test_system_prompt_not_empty(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(4)
        assert prompt, "§4 system prompt must not be empty"

    def test_system_prompt_mentions_c9_peer(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(4)
        assert "C-9" in prompt or "peer" in prompt.lower()

    def test_system_prompt_mentions_banking_relationships(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(4)
        assert "banking relationships" in prompt.lower() or "Banking Relationships" in prompt

    def test_user_prompt_contains_missing_labels(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [
            ("**C-8.", "C-8 Market Analysis"),
            ("**C-9.", "C-9 Peer Comparison"),
            ("banking relationships", "Banking Relationships Table (Section E)"),
        ]
        prompt = _build_fill_user_prompt(4, missing, PARTIAL_S4_C1_TO_C7, S4_INPUT, "en")
        assert "C-8 Market Analysis" in prompt
        assert "C-9 Peer Comparison" in prompt
        assert "Banking Relationships" in prompt

    def test_user_prompt_includes_existing_tail(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**C-9.", "C-9 Peer Comparison")]
        prompt = _build_fill_user_prompt(4, missing, PARTIAL_S4_C1_TO_C7, S4_INPUT, "en")
        assert "do NOT repeat" in prompt or "context only" in prompt

    def test_user_prompt_includes_input_json(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("banking relationships", "Banking Relationships Table (Section E)")]
        prompt = _build_fill_user_prompt(4, missing, PARTIAL_S4_C1_TO_C7, S4_INPUT, "en")
        assert "4A_corporate_identity" in prompt or "4E_banking_relationships" in prompt


# ── E. Config — §4 primary token budget ───────────────────────────────────────

class TestSection4Config:

    def test_s4_primary_token_budget_is_12288(self):
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        assert SECTION_MAX_OUTPUT_TOKENS.get(4, SECTION_MAX_OUTPUT_TOKENS["default"]) >= 12288, (
            "§4 primary token budget must be ≥12 288 to accommodate all 9 sub-sections "
            "plus Banking Relationships."
        )

    @pytest.mark.asyncio
    async def test_s4_fill_budget_independent_from_s3(self):
        """§4 fill budget (8192) must not be reduced by §3 path (6144)."""
        from credit_report.generation.completeness import fill_missing_tables
        missing = [("**C-9.", "C-9 Peer Comparison")]
        with patch(
            "credit_report.generation.claude_client.call_gemini_raw",
            new=AsyncMock(return_value="| Company | Fleet TEU |\n|---|---|\n| EMA | 200,000 |"),
        ) as mock_call:
            await fill_missing_tables(
                section_no=4,
                existing_markdown=PARTIAL_S4_C1_TO_C7,
                missing=missing,
                input_json=S4_INPUT,
            )
            _, kwargs = mock_call.call_args
            max_tok = kwargs.get("max_tokens", mock_call.call_args[0][2] if mock_call.call_args[0] else None)
            # §4 fill must use 8192, not the §3 value of 6144
            assert max_tok == 8192, f"§4 fill budget should be 8192, got {max_tok}"

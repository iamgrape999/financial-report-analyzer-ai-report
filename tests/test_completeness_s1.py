"""
§1 Completeness check tests.

§1 differs fundamentally from §2:
- Sub-sections are CONDITIONAL — presence depends on report_type and input keys.
- Markers are heterogeneous (11-col table, prose paragraphs, 21-field T&C table, etc.)
- The fill budget is 10 240 tokens (Deal Comparison + Account Strategy are non-compressible).

Coverage:
A. Detection — new_deal, annual_review, partial markdown cases
B. Pipeline integration — fill triggered, tokens accumulated, failure isolated
C. Input-conditional logic — sub-section skipped when input key absent
D. check_section_completeness signature backward-compat (input_json=None)
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


def _mock_fill(text, tokens=1000):
    return patch(
        "credit_report.generation.completeness.fill_missing_tables",
        new=AsyncMock(return_value=(text, tokens)),
    )


# ── Shared input stubs ────────────────────────────────────────────────────────

NEW_DEAL_INPUT = {
    "metadata": {"report_type": "new_deal"},
    "regulatory_compliance": {
        "banking_act_33_3": {"requirement": "25%", "compliant": "Y"},
        "unsecured_exposure_table": [{"item": "EMA", "credit_limit": 213.84}],
    },
    "purpose_and_recommendation": {"purpose_text": "Finance vessel construction.", "vessel_specs": "24000 TEU"},
    "terms_and_conditions": {
        "tc_rows": [{"field": "Borrower", "content": "EMA"}],
        "deal_comparison_rows": [{"guarantor": "EMC", "facility_amount": "213.84"}],
    },
    "account_strategy": {
        "wallet": {"bank_market": "10", "capital_market": "5", "treasury": "2", "deposit": "1"},
        "immediate_opportunities": "FX hedging.",
    },
}

ANNUAL_REVIEW_INPUT = {
    "metadata": {"report_type": "annual_review"},
    "regulatory_compliance": {
        "banking_act_33_3": {"requirement": "25%", "compliant": "Y"},
    },
    "purpose_and_recommendation": {"purpose_text": "Annual review of existing facility."},
    "account_strategy": {"wallet": {"bank_market": "8"}},
}

FULL_S1 = (
    "## 1. Credit Facility and Case Details\n\n"
    "| Item | Borrower | Booking | Current | Proposed Facility | Outstanding (As at end Oct 2025) | "
    "CCY | Tenor | Facility Type | Collateral | Guarantor |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| 1 | EMA | SG | - | **[NEW] 213.84** | 0 | USD | 7 years | SLL | KDB RG | EMC |\n\n"
    "Banking Act 33-3 Compliance:\n| Requirement | Borrower | Compliant |\n|---|---|---|\n"
    "| 25% | EMA | Y |\n\n"
    "Unsecured Exposure:\n| USD'm | Credit Limit | Unsecured | Secured |\n"
    "| EMA | 213.84 | 0 | 213.84 |\n\n"
    "Purpose of Report: Finance vessel construction.\n\n"
    "| Field | Content |\n|---|---|\n"
    "| Value Maintenance | 110% FMV |\n"
    "| Conditions Precedent | Legal opinions. |\n\n"
    "Deal Comparison:\n| Guarantor | Facility Amount | Purpose | Vessel Type | Tenor | "
    "Margin | Upfront Fee | SLL Ratchet | Drawdowns | Availability Period | Security | FMV Maintenance |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| EMC | 213.84 | Pre-delivery | 24kTEU | 11yr | 1.35% | 0.50% | ±5bps | 3 | 4yr | KDB RG | 110% |\n\n"
    "Account Strategy:\n"
    "**Wallet Overview**: Bank Market USD 10m | Capital Market USD 5m | "
    "Treasury USD 2m | Deposit USD 1m\n"
    "**Immediate Opportunities**: FX hedging.\n"
    "**Future Opportunities**: Bond issuance.\n"
    "**Other Opportunities**: NIL\n"
)

PARTIAL_S1_NO_DEAL_COMP_NO_ACCT = (
    "## 1. Credit Facility and Case Details\n\n"
    "| Item | Borrower | Booking | Current | Proposed Facility | Outstanding (As at end Oct 2025) | "
    "CCY | Tenor | Facility Type | Collateral | Guarantor |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| 1 | EMA | SG | - | **[NEW] 213.84** | 0 | USD | 7 years | SLL | KDB RG | EMC |\n\n"
    "Banking Act 33-3 Compliance:\n| Requirement | Borrower | Compliant |\n|---|---|---|\n"
    "| 25% | EMA | Y |\n\n"
    "Unsecured Exposure:\n| USD'm | Credit Limit | Unsecured | Secured |\n"
    "| EMA | 213.84 | 0 | 213.84 |\n\n"
    "Purpose of Report: Finance vessel construction.\n\n"
    "| Field | Content |\n|---|---|\n"
    "| Value Maintenance | 110% FMV |\n"
    "| Conditions Precedent | Legal opinions. |\n"
    # Deal Comparison and Account Strategy missing
)

PARTIAL_S1_NO_FACILITY_TABLE = (
    "## 1. Credit Facility and Case Details\n\n"
    "Purpose of Report: Finance vessel construction.\n\n"
    "Account Strategy:\n**Wallet Overview**: Bank Market USD 10m\n"
    "**Immediate Opportunities**: FX hedging.\n"
)


# ── A. Detection logic ────────────────────────────────────────────────────────

class TestSection1Detection:

    def test_full_s1_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(1, FULL_S1, NEW_DEAL_INPUT)
        assert missing == [], f"Full §1 should have no gaps, got: {missing}"

    def test_partial_s1_detects_deal_comparison_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(1, PARTIAL_S1_NO_DEAL_COMP_NO_ACCT, NEW_DEAL_INPUT)
        labels = [l for _, l in missing]
        assert "Deal Comparison Table (≥11 columns)" in labels

    def test_partial_s1_detects_account_strategy_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(1, PARTIAL_S1_NO_DEAL_COMP_NO_ACCT, NEW_DEAL_INPUT)
        labels = [l for _, l in missing]
        assert "Account Strategy (5 sub-sections)" in labels

    def test_partial_s1_detects_facility_table_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(1, PARTIAL_S1_NO_FACILITY_TABLE, NEW_DEAL_INPUT)
        labels = [l for _, l in missing]
        assert "Facility Summary Table (11 columns)" in labels

    def test_full_s1_present_after_fill(self):
        from credit_report.generation.completeness import check_section_completeness
        fill_addon = (
            "Deal Comparison:\n| Guarantor | Facility Amount | Purpose | Vessel Type | Tenor | "
            "Margin | Upfront Fee | SLL Ratchet | Drawdowns | Availability Period | Security | FMV |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| EMC | 213.84 | Pre-delivery | 24kTEU | 11yr | 1.35% | 0.50% | ±5bps | 3 | 4yr | KDB | 110% |\n\n"
            "Account Strategy:\n**Wallet Overview**: Bank Market USD 10m\n"
            "**Immediate Opportunities**: FX hedging.\n"
            "**Other Opportunities**: NIL\n"
        )
        combined = PARTIAL_S1_NO_DEAL_COMP_NO_ACCT + "\n\n" + fill_addon
        still_missing = check_section_completeness(1, combined, NEW_DEAL_INPUT)
        assert still_missing == [], f"After fill should be complete, still missing: {still_missing}"


class TestSection1ConditionalLogic:

    def test_deal_comparison_not_required_for_annual_review(self):
        from credit_report.generation.completeness import check_section_completeness
        # annual_review: Deal Comparison is NOT required
        md = (
            "| Item | Borrower | Current | Proposed Facility | Outstanding (As at end Oct 2025) | "
            "CCY | Tenor | Facility Type | Collateral | Guarantor |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| 1 | EMA | 155.12 | 155.12 | 0 | USD | 7 years | SLL | KDB RG | EMC |\n\n"
            "Banking Act 33-3 Compliance:\n| Requirement | Borrower | Compliant |\n|---|---|---|\n"
            "| 25% | EMA | Y |\n\n"
            "Purpose of Report: Annual review.\n\n"
            "Account Strategy:\n**Wallet Overview**: Bank Market USD 10m\n"
            "**Immediate Opportunities**: Refinancing.\n"
        )
        missing = check_section_completeness(1, md, ANNUAL_REVIEW_INPUT)
        labels = [l for _, l in missing]
        assert "Deal Comparison Table (≥11 columns)" not in labels

    def test_tcs_not_required_when_input_absent(self):
        from credit_report.generation.completeness import check_section_completeness
        input_no_tc = {
            "metadata": {"report_type": "new_deal"},
            "account_strategy": {"wallet": {"bank_market": "10"}},
        }
        md = (
            "| Item | Borrower | Proposed Facility | Outstanding (As at Oct 2025) | CCY | Tenor | "
            "Facility Type | Collateral | Guarantor |\n|---|---|---|---|---|---|---|---|---|\n"
            "| 1 | EMA | **[NEW] 213.84** | 0 | USD | 7yr | SLL | KDB RG | EMC |\n\n"
            "Account Strategy:\n**Wallet Overview**: Bank Market USD 10m\n"
            "**Immediate Opportunities**: FX.\n"
        )
        missing = check_section_completeness(1, md, input_no_tc)
        labels = [l for _, l in missing]
        assert "Terms & Conditions Table (21 fields)" not in labels

    def test_regulatory_not_required_when_input_absent(self):
        from credit_report.generation.completeness import check_section_completeness
        input_no_reg = {
            "metadata": {"report_type": "new_deal"},
            "account_strategy": {"wallet": {"bank_market": "10"}},
        }
        md = (
            "| Item | Proposed Facility | Outstanding (As at Oct 2025) | CCY |\n"
            "|---|---|---|---|\n| 1 | **[NEW] 100** | 0 | USD |\n\n"
            "Account Strategy:\n**Wallet Overview**: Bank Market 10m\n"
            "**Immediate Opportunities**: NIL\n"
        )
        missing = check_section_completeness(1, md, input_no_reg)
        labels = [l for _, l in missing]
        assert "Regulatory Compliance (Banking Act 33-3)" not in labels

    def test_account_strategy_not_checked_when_input_absent(self):
        from credit_report.generation.completeness import check_section_completeness
        # No account_strategy in input → check should not flag it as missing
        input_no_acct = {"metadata": {"report_type": "new_deal"}}
        md = (
            "| Item | Proposed Facility | Outstanding (As at Oct 2025) | CCY |\n"
            "|---|---|---|---|\n| 1 | **[NEW] 100** | 0 | USD |\n"
        )
        missing = check_section_completeness(1, md, input_no_acct)
        labels = [l for _, l in missing]
        assert "Account Strategy (5 sub-sections)" not in labels

    def test_input_json_none_falls_back_gracefully(self):
        from credit_report.generation.completeness import check_section_completeness
        # input_json=None → only unconditional checks run (no crash)
        md = "Some partial output without facility table."
        result = check_section_completeness(1, md, None)
        # Should detect missing facility table
        labels = [l for _, l in result]
        assert "Facility Summary Table (11 columns)" in labels

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        md_lower = FULL_S1.lower()
        missing = check_section_completeness(1, md_lower, NEW_DEAL_INPUT)
        assert missing == [], "Detection must be case-insensitive"

    def test_alternative_outstanding_marker(self):
        from credit_report.generation.completeness import check_section_completeness
        # "Outstanding as at" (without parentheses) should also match
        md = (
            "| Item | Proposed Facility | Outstanding as at Oct 2025 | CCY |\n"
            "|---|---|---|---|\n| 1 | **[NEW] 100** | 0 | USD |\n\n"
            "Account Strategy:\n**Wallet Overview**: Bank 10m\n**Immediate Opportunities**: NIL\n"
        )
        input_minimal = {"metadata": {"report_type": "new_deal"}, "account_strategy": {"wallet": {}}}
        missing = check_section_completeness(1, md, input_minimal)
        labels = [l for _, l in missing]
        assert "Facility Summary Table (11 columns)" not in labels


# ── B. Pipeline integration ───────────────────────────────────────────────────

async def _seed_section_input(db, report_id: str, section_no: int, input_json: dict) -> None:
    """Insert a SectionInput row so pipeline.run_section_generation can load input_json."""
    import json as _json
    from credit_report.models import SectionInput
    db.add(SectionInput(
        report_id=report_id,
        section_no=section_no,
        input_json=_json.dumps(input_json),
        saved_by=_uid(),
    ))
    await db.flush()


@pytest.mark.asyncio
class TestSection1PipelineIntegration:

    async def test_missing_deal_comparison_triggers_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()
        await _seed_section_input(db, rid, 1, NEW_DEAL_INPUT)

        fill_output = (
            "Deal Comparison:\n| Guarantor | Facility Amount | Purpose | Vessel Type | Tenor | "
            "Margin | Upfront Fee | SLL Ratchet | Drawdowns | Availability Period | Security | FMV |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| EMC | 213.84 | Pre-delivery | 24kTEU | 11yr | 1.35% | 0.50% | ±5bps | 3 | 4yr | KDB | 110% |\n\n"
            "Account Strategy:\n**Wallet Overview**: Bank Market USD 10m\n"
            "**Immediate Opportunities**: FX hedging.\n"
            "**Other Opportunities**: NIL\n"
        )

        with _mock_generate(PARTIAL_S1_NO_DEAL_COMP_NO_ACCT, tokens=7000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=1200):
            output = await run_section_generation(
                db, rid, section_no=1, actor_user_id=_uid()
            )

        assert output.status == "done"
        assert "deal comparison" in output.markdown.lower()
        assert "account strategy" in output.markdown.lower()

    async def test_complete_s1_does_not_trigger_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()
        await _seed_section_input(db, rid, 1, NEW_DEAL_INPUT)

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S1, tokens=12000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(
                db, rid, section_no=1, actor_user_id=_uid()
            )

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_fill_failure_does_not_crash_s1_generation(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()
        await _seed_section_input(db, rid, 1, NEW_DEAL_INPUT)

        with _mock_generate(PARTIAL_S1_NO_DEAL_COMP_NO_ACCT, tokens=7000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables",
                   new=AsyncMock(side_effect=TimeoutError("LLM timeout"))):
            output = await run_section_generation(db, rid, section_no=1, actor_user_id=_uid())

        assert output.status == "done", "Fill failure must not abort §1 generation"
        assert "Proposed Facility" in output.markdown  # partial output preserved

    async def test_tokens_accumulated_from_s1_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()
        await _seed_section_input(db, rid, 1, NEW_DEAL_INPUT)

        primary_tokens = 7000
        fill_tokens = 1500
        fill_output = (
            "Deal Comparison:\n| Guarantor | Facility Amount | Purpose | Vessel Type | Tenor | "
            "Margin | Upfront Fee | SLL Ratchet | Drawdowns | Availability Period | Security | FMV |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| EMC | 213.84 | Pre-delivery | 24kTEU | 11yr | 1.35% | 0.50% | ±5bps | 3 | 4yr | KDB | 110% |\n\n"
            "Account Strategy:\n**Wallet Overview**: Bank Market USD 10m\n"
            "**Immediate Opportunities**: FX hedging.\n"
        )
        with _mock_generate(PARTIAL_S1_NO_DEAL_COMP_NO_ACCT, tokens=primary_tokens), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=fill_tokens):
            output = await run_section_generation(db, rid, section_no=1, actor_user_id=_uid())

        assert output.tokens_used == primary_tokens + fill_tokens

    async def test_non_s1_section_not_affected_by_s1_logic(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()
        # §5 has no completeness requirements — no SectionInput needed

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate("§5 Collateral Analysis text.", tokens=3000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=5, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()


# ── C. Fill prompt content ────────────────────────────────────────────────────

class TestSection1FillPrompts:

    def test_system_prompt_mentions_11_columns(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(1)
        assert "11" in prompt
        assert "Deal Comparison" in prompt
        assert "Account Strategy" in prompt
        assert "5 sub-sections" in prompt

    def test_user_prompt_contains_missing_labels(self):
        from credit_report.generation.completeness import _build_fill_user_prompt, _check_section1
        missing = _check_section1(PARTIAL_S1_NO_DEAL_COMP_NO_ACCT, NEW_DEAL_INPUT)
        prompt = _build_fill_user_prompt(1, missing, PARTIAL_S1_NO_DEAL_COMP_NO_ACCT, NEW_DEAL_INPUT, "en")
        assert "Deal Comparison" in prompt
        assert "Account Strategy" in prompt

    def test_user_prompt_includes_input_json(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("Deal Comparison", "Deal Comparison Table (≥11 columns)")]
        prompt = _build_fill_user_prompt(1, missing, "existing text", NEW_DEAL_INPUT, "en")
        assert "new_deal" in prompt
        assert "EMA" in prompt or "EMC" in prompt

    def test_fill_max_tokens_is_10240_for_s1(self):
        # §1 fill allows 10 240 tokens (Deal Comparison + Account Strategy are non-compressible)
        # Verify the constant is set correctly in fill_missing_tables
        import inspect
        from credit_report.generation import completeness
        src = inspect.getsource(completeness.fill_missing_tables)
        assert "10240" in src, "§1 fill budget must be 10 240 tokens"


# ── D. Backward compatibility ─────────────────────────────────────────────────

class TestBackwardCompatibility:

    def test_s2_still_works_without_input_json(self):
        from credit_report.generation.completeness import check_section_completeness
        md = (
            "| **Credit Overview** | 1. |\n|---|---|\n"
            "| **Solvency** | DSCR |\n|---|---|\n"
            "| **The Guarantor and their Supportive Performance** | EMC |\n|---|---|\n"
            "| **Collateral Summary** | KDB |\n|---|---|\n"
            "| **Risk and Mitigants** | Market |\n|---|---|\n"
        )
        # §2 does not need input_json — should still return empty (all present)
        missing = check_section_completeness(2, md)
        assert missing == []

    def test_s2_still_detects_missing_without_input_json(self):
        from credit_report.generation.completeness import check_section_completeness
        md = "| **Credit Overview** | 1. |\n|---|---|\n"
        missing = check_section_completeness(2, md)
        assert len(missing) == 4  # T2-T5 all missing

    def test_other_sections_return_empty(self):
        from credit_report.generation.completeness import check_section_completeness
        # §3 and §4 now have their own completeness checks; verify §5-§10 have none
        for sec in [5, 6, 7, 8, 9, 10]:
            result = check_section_completeness(sec, "any markdown")
            assert result == [], f"§{sec} should have no requirements"

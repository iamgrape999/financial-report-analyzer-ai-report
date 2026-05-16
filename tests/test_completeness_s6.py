"""
§6 Completeness check tests — Project Analysis.

§6 is CONDITIONAL on facility type:
- If NOT applicable (non-shipbuilding), the AI emits one sentence containing
  "not applicable" and stops — completeness check returns [] immediately.
- If applicable, 8 bold-header sub-sections are required:

  ① **Project Overview**          → always (C-1)
  ② **Builder Assessment**        → always (C-2)
  ③ **Contract Structure**        → always (C-3)
  ④ **Payment & Delivery Schedule** → always (C-4)
  ⑤ **RG Mechanism**              → only when 6E_rg_mechanism.applicable (C-5)
  ⑥ **Construction Progress**     → always (C-6)
  ⑦ **Force Majeure**             → only when 6G_force_majeure data present (C-7)
  ⑧ **Project Economics**         → always (one cross-ref sentence) (C-8)

NOTE: §6 output uses bold topic headers WITHOUT C-N. prefix (unlike §4/§5).
Detection uses the `**` prefix to distinguish headers from in-body text.

Primary token budget raised 8 192 → 12 288 (11-column payment table + multiple
risk blocks each with 3-5 mitigant bullets).
Fill budget is 10 240 tokens (C-4 and C-6 can both be verbose).

Coverage:
A. Detection — applicable full, partial, not-applicable, empty markdown
B. Conditional boundary — no-project-data early exit; RG/FM conditional
C. Pipeline integration — fill triggered, failure isolated, tokens accumulated
D. Fill prompt content — 11-col table, mitigants, force majeure, cross-ref
E. Config — §6 primary token budget
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


def _mock_generate(md, tokens=9000):
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

NOT_APPLICABLE = "**6. Project Analysis**\n\nNot applicable — this is a post-delivery refinancing."

# Full applicable §6 with RG and Force Majeure
FULL_S6_WITH_RG_FM = (
    "**6. Project Analysis**\n\n"
    "**Project Overview**\n"
    "The facility finances the construction of a 24,000 TEU LNG dual-fuel containership "
    "(Hull No. 1234) at Hyundai Heavy Industries (HHI), Ulsan, South Korea. "
    "Contract price: USD 267.30m. Expected delivery: 15 Dec 2025 (grace: 90 days; "
    "latest delivery: 15 Mar 2026). CUB facility: USD 213.84m = LTC 80.0%. "
    "(See Section 4 for fleet context and orderbook.)\n\n"
    "**Builder Assessment**\n"
    "| Field | Detail |\n|---|---|\n"
    "| Formerly | Hyundai Heavy Industries |\n"
    "| Founded | 1972 |\n"
    "| HQ | Ulsan, South Korea |\n"
    "HHI delivered the world's first 23,000 TEU vessel in 2020.\n\n"
    "**Contract Structure**\n"
    "| Term | Detail |\n|---|---|\n"
    "| Contract Type | Shipbuilding Contract |\n"
    "| Price | USD 267,300,000 |\n"
    "| Late Delivery Penalty | each day of delay (standard Korean shipbuilding contract terms) |\n\n"
    "**Payment & Delivery Schedule**\n"
    "| # | Milestone | Expected Date | Actual Date | Status | % of Contract | "
    "Amount (USD m) | Cumulative Paid (USD m) | CUB Drawdown | RG In Force | RG Amount (USD m) |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| 1 | Steel Cutting | 15 Mar 2024 | 15 Mar 2024 | ✅ Completed | 5.0% | 13.37 | 13.37 | — | ✅ | 13.37 |\n"
    "| 2 | Keel Laying | 20 Jun 2024 | 20 Jun 2024 | ✅ Completed | 5.0% | 13.37 | 26.73 | — | ✅ | 26.73 |\n"
    "| 3 | Launch | 15 Sep 2024 | — | ⏳ Pending | 10.0% | 26.73 | 53.46 | — | ✅ | 53.46 |\n"
    "| 4 | Delivery | 15 Dec 2025 | — | ⏳ Pending | 80.0% | 213.84 | 267.30 | ≤ cap* | ❌** | — |\n"
    "\\* PAM and SAM will jointly control drawdown; pre-delivery drawdown of USD 50m is within "
    "Banking Act s.33-3 single-borrower unsecured limit, agreed with HQ Risk.\n"
    "\\** RG expires at delivery; security transitions to first priority mortgage. "
    "(See Section 5 for lag time analysis.)\n"
    "Commentary: 2 of 4 milestones completed (25% by value). First drawdown Q2 2026, "
    "reimbursement basis, ~USD 50m. RG coverage 100% at max exposure.\n\n"
    "**RG Mechanism**\n"
    "**RG Issuer:** Korea Development Bank — rated AA (S&P) / AA- (Fitch)\n"
    "**Beneficiary:** Evergreen Marine (Asia) Pte. Ltd., assigned to CUB SG\n"
    "**Trigger Events:**\n"
    "1. Builder insolvency\n"
    "2. Failure to deliver within latest delivery date\n"
    "RG Coverage: min 13.0% at steel cutting; max 100.0% at delivery. Cross-ref §5.\n\n"
    "**Construction Progress & Risk**\n"
    "**Status (as of 31 Oct 2024):** Milestones 2/4 | Completion 25% | On schedule | "
    "Next: Launch (15 Sep 2024)\n"
    "**Builder Insolvency Risk** (Likelihood: Low)\n"
    "HHI is the world's largest shipyard by capacity.\n"
    "Mitigant:\n"
    "- KDB Refund Guarantee rated AA (S&P) / AA- (Fitch) provides full coverage.\n"
    "- KDB is state-owned with AAA sovereign backing.\n"
    "- HHI order book covers operations through 2027.\n\n"
    "**Force Majeure**\n"
    "The shipbuilding contract covers force majeure events including natural disasters, "
    "war, and pandemic. COVID-19 (2020-2022) caused average delays of 3-6 months. "
    "Current supply chain conditions are normalised post-pandemic.\n\n"
    "**Project Economics**\n"
    "Vessel earnings projections, breakeven freight rate analysis, and detailed cash flow "
    "projections are covered in Section 7: Financial Analysis.\n"
)

# Partial §6 — C-1 through C-5 present, C-6 through C-8 missing
PARTIAL_S6_MISSING_C6_C7_C8 = (
    "**6. Project Analysis**\n\n"
    "**Project Overview**\n"
    "Hull No. 1234, 24,000 TEU LNG dual-fuel. Contract price USD 267.30m. "
    "CUB facility USD 213.84m = LTC 80.0%. (See Section 4.)\n\n"
    "**Builder Assessment**\n"
    "| Field | Detail |\n|---|---|\n"
    "| Formerly | Hyundai Heavy Industries |\n\n"
    "**Contract Structure**\n"
    "| Term | Detail |\n|---|---|\n"
    "| Price | USD 267,300,000 |\n\n"
    "**Payment & Delivery Schedule**\n"
    "| # | Milestone | Expected Date | Actual Date | Status | % of Contract | "
    "Amount (USD m) | Cumulative Paid (USD m) | CUB Drawdown | RG In Force | RG Amount (USD m) |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| 1 | Steel Cutting | 15 Mar 2024 | 15 Mar 2024 | ✅ Completed | 5.0% | 13.37 | 13.37 | — | ✅ | 13.37 |\n\n"
    "**RG Mechanism**\n"
    "KDB rated AA (S&P) / AA- (Fitch). Trigger: builder insolvency.\n"
    # C-6 Construction Progress, C-7 Force Majeure, C-8 Project Economics missing
)

S6_FULL_INPUT = {
    "6A_project": {
        "hull_number": "1234",
        "teu": 24000,
        "fuel_type": "LNG",
        "contract_price_usd_m": 267.30,
        "loan_amount_usd_m": 213.84,
        "delivery_date": "2025-12-15",
    },
    "6B_builder": {
        "name": "Hyundai Heavy Industries",
        "formerly": "Hyundai Heavy Industries",
        "founded": 1972,
    },
    "6C_contract": {"contract_type": "Shipbuilding Contract", "price_verbatim": "USD 267,300,000"},
    "6D_milestones": {
        "milestones": [{"no": 1, "milestone": "Steel Cutting", "status": "Completed"}],
    },
    "6E_rg_mechanism": {
        "applicable": True,
        "issuer_full_name": "Korea Development Bank",
        "issuer_rating_verbatim": "AA (S&P) / AA- (Fitch)",
    },
    "6F_construction_progress": {
        "status_date": "2024-10-31",
        "milestones_completed": 2,
        "milestones_total": 4,
        "risks": [{"title": "Builder Insolvency Risk", "likelihood": "Low", "mitigant_bullets": ["KDB RG"]}],
    },
    "6G_force_majeure": {
        "applicable": True,
        "covered_events": ["natural disasters", "war", "pandemic"],
        "historical_context_verbatim": "COVID-19 (2020-2022) caused 3-6 month delays.",
        "current_supply_chain_status": "Normalised post-pandemic.",
    },
}

S6_INPUT_NO_RG_NO_FM = {
    "6A_project": {
        "hull_number": "5678",
        "teu": 15000,
        "contract_price_usd_m": 150.0,
    },
    "6B_builder": {"name": "Samsung Heavy Industries"},
    "6C_contract": {"contract_type": "Shipbuilding Contract"},
    "6D_milestones": {"milestones": [{"no": 1, "milestone": "Steel Cutting"}]},
    "6E_rg_mechanism": {"applicable": False},
    "6F_construction_progress": {
        "status_date": "2024-10-31",
        "risks": [{"title": "Delay Risk", "likelihood": "Medium", "mitigant_bullets": ["Penalty clauses"]}],
    },
    "6G_force_majeure": {"applicable": False, "covered_events": []},
}


# ── A. Detection ──────────────────────────────────────────────────────────────

class TestSection6Detection:

    def test_full_s6_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(6, FULL_S6_WITH_RG_FM, S6_FULL_INPUT)
        assert missing == [], f"Full §6 should have no gaps, got: {[l for _, l in missing]}"

    def test_not_applicable_returns_empty(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(6, NOT_APPLICABLE, S6_FULL_INPUT)
        assert missing == [], "Not-applicable §6 must return empty (no fill needed)"

    def test_not_applicable_short_text_also_returns_empty(self):
        from credit_report.generation.completeness import check_section_completeness
        md = "6. Project Analysis: Not applicable — existing fleet loan."
        missing = check_section_completeness(6, md, {})
        assert missing == []

    def test_no_project_data_returns_empty(self):
        from credit_report.generation.completeness import check_section_completeness
        # No 6A_project data → section is not applicable
        missing = check_section_completeness(6, "§6 Some random text", {})
        assert missing == [], "No project data should result in empty (not applicable)"

    def test_partial_missing_c6_c7_c8_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(6, PARTIAL_S6_MISSING_C6_C7_C8, S6_FULL_INPUT)
        labels = [l for _, l in missing]
        assert "C-6 Construction Progress & Risk" in labels
        assert "C-7 Force Majeure" in labels
        assert "C-8 Project Economics" in labels
        assert len(missing) == 3

    def test_empty_markdown_with_project_data_all_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(6, "", S6_FULL_INPUT)
        labels = [l for _, l in missing]
        # All 8 items expected
        assert "C-1 Project Overview" in labels
        assert "C-2 Builder Assessment" in labels
        assert "C-3 Contract Structure" in labels
        assert "C-4 Payment & Delivery Schedule" in labels
        assert "C-5 RG Mechanism" in labels
        assert "C-6 Construction Progress & Risk" in labels
        assert "C-7 Force Majeure" in labels
        assert "C-8 Project Economics" in labels
        assert len(missing) == 8

    def test_c5_rg_skipped_when_not_applicable(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(6, "", S6_INPUT_NO_RG_NO_FM)
        labels = [l for _, l in missing]
        assert "C-5 RG Mechanism" not in labels
        assert "C-7 Force Majeure" not in labels
        # All other 6 items present
        assert len(missing) == 6

    def test_c7_fm_skipped_when_no_fm_data(self):
        from credit_report.generation.completeness import check_section_completeness
        input_no_fm = {**S6_FULL_INPUT, "6G_force_majeure": {"applicable": False}}
        missing = check_section_completeness(6, "", input_no_fm)
        labels = [l for _, l in missing]
        assert "C-7 Force Majeure" not in labels

    def test_c5_rg_detected_by_issuer_name(self):
        from credit_report.generation.completeness import check_section_completeness
        # RG applicable when issuer_full_name present even if applicable=None
        input_rg_by_name = {**S6_INPUT_NO_RG_NO_FM,
                            "6E_rg_mechanism": {"issuer_full_name": "Korea Development Bank"}}
        missing = check_section_completeness(6, "", input_rg_by_name)
        labels = [l for _, l in missing]
        assert "C-5 RG Mechanism" in labels

    def test_c7_fm_detected_by_events_list(self):
        from credit_report.generation.completeness import check_section_completeness
        # FM applicable when covered_events is non-empty even if applicable=None
        input_fm_by_events = {**S6_INPUT_NO_RG_NO_FM,
                              "6G_force_majeure": {"covered_events": ["war", "pandemic"]}}
        missing = check_section_completeness(6, "", input_fm_by_events)
        labels = [l for _, l in missing]
        assert "C-7 Force Majeure" in labels

    def test_only_c8_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_c8 = FULL_S6_WITH_RG_FM.replace("**Project Economics**\n", "")
        md_no_c8 = md_no_c8[:md_no_c8.rfind("Vessel earnings")]
        missing = check_section_completeness(6, md_no_c8, S6_FULL_INPUT)
        labels = [l for _, l in missing]
        assert len(missing) == 1
        assert missing[0][1] == "C-8 Project Economics"

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(6, FULL_S6_WITH_RG_FM.lower(), S6_FULL_INPUT)
        assert missing == [], "Detection must be case-insensitive"

    def test_force_majeure_in_body_text_not_detected_as_header(self):
        from credit_report.generation.completeness import check_section_completeness
        # "force majeure" appears in contract body text but NOT as bold header
        md_with_fm_in_body = (
            "**Project Overview**\nHull 1234.\n\n"
            "**Builder Assessment**\nHHI.\n\n"
            "**Contract Structure**\nThe contract covers force majeure events.\n\n"
            "**Payment & Delivery Schedule**\n| # | Milestone |\n|---|---|\n| 1 | Steel Cutting |\n\n"
            "**RG Mechanism**\nKDB rated AA.\n\n"
            "**Construction Progress & Risk**\nOn schedule.\n\n"
            "**Force Majeure**\n"  # Bold header — correctly present
            "Covered events include war.\n\n"
            "**Project Economics**\nVessel earnings in Section 7.\n"
        )
        missing = check_section_completeness(6, md_with_fm_in_body, S6_FULL_INPUT)
        assert missing == []

    def test_full_s6_no_rg_no_fm_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_rg_fm = (
            "**Project Overview**\nHull 5678, 15000 TEU.\n\n"
            "**Builder Assessment**\nSHI.\n\n"
            "**Contract Structure**\nShipbuilding Contract.\n\n"
            "**Payment & Delivery Schedule**\n| # | Milestone |\n|---|---|\n| 1 | Steel Cutting |\n\n"
            "**Construction Progress & Risk**\nOn schedule.\n\n"
            "**Project Economics**\nVessel earnings in Section 7.\n"
        )
        missing = check_section_completeness(6, md_no_rg_fm, S6_INPUT_NO_RG_NO_FM)
        assert missing == [], f"Complete §6 with no RG/FM should have no gaps: {[l for _, l in missing]}"


# ── B. §6 isolation ───────────────────────────────────────────────────────────

class TestSection6Isolation:

    def test_s6_check_does_not_affect_s2(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(2, FULL_S6_WITH_RG_FM)
        labels = [l for _, l in result]
        assert "T1 Credit Overview" in labels

    def test_s6_check_does_not_affect_s3(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(3, FULL_S6_WITH_RG_FM)
        labels = [l for _, l in result]
        assert "External Ratings" in labels

    def test_sections_8_to_10_unaffected(self):
        from credit_report.generation.completeness import check_section_completeness
        # §7 now has its own completeness check; verify §8-§10 have none
        # §9 now has its own completeness check; only §8 and §10 have none
        for sec in [8, 10]:
            result = check_section_completeness(sec, FULL_S6_WITH_RG_FM)
            assert result == [], f"§{sec} should have no completeness requirements"

    def test_s6_not_applicable_never_triggers_check_for_other_sections(self):
        from credit_report.generation.completeness import check_section_completeness
        # Not-applicable §6 text is short — confirm no phantom matches for §4/§5
        result4 = check_section_completeness(4, NOT_APPLICABLE)
        labels4 = [l for _, l in result4]
        assert "C-1 Corporate Identity" in labels4  # §4 markers absent → all flagged


# ── C. Pipeline integration ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_with_s6_input(db):
    """DB fixture pre-seeded with a report and full §6 SectionInput."""
    from credit_report.models import Report, SectionInput
    rid = _uid()
    db.add(Report(id=rid, industry="marine", created_by=_uid()))
    db.add(SectionInput(
        report_id=rid,
        section_no=6,
        input_json=json.dumps(S6_FULL_INPUT),
        saved_by=_uid(),
    ))
    await db.flush()
    return db, rid


@pytest.mark.asyncio
class TestSection6PipelineIntegration:

    async def test_missing_c6_c7_c8_triggers_fill(self, db_with_s6_input):
        """When §6 is missing C-6, C-7, C-8, fill must be called."""
        db, rid = db_with_s6_input
        from credit_report.generation.pipeline import run_section_generation

        fill_output = (
            "**Construction Progress & Risk**\n"
            "**Status (as of 31 Oct 2024):** Milestones 2/4 | Completion 25% | On schedule\n"
            "**Builder Insolvency Risk** (Likelihood: Low)\n"
            "HHI is the world's largest shipyard.\n"
            "Mitigant:\n"
            "- KDB RG provides full coverage.\n"
            "- KDB is state-owned.\n"
            "- HHI order book covers 2027.\n\n"
            "**Force Majeure**\n"
            "The contract covers force majeure events. COVID-19 (2020-2022) caused "
            "average delays of 3-6 months. Current supply chain conditions are normalised.\n\n"
            "**Project Economics**\n"
            "Vessel earnings projections, breakeven freight rate analysis, and detailed "
            "cash flow projections are covered in Section 7: Financial Analysis.\n"
        )

        with _mock_generate(PARTIAL_S6_MISSING_C6_C7_C8, tokens=8000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=700):
            output = await run_section_generation(db, rid, section_no=6, actor_user_id=_uid())

        assert output.status == "done"
        assert "**Construction Progress" in output.markdown
        assert "**Force Majeure**" in output.markdown
        assert "**Project Economics**" in output.markdown
        assert PARTIAL_S6_MISSING_C6_C7_C8.strip() in output.markdown

    async def test_complete_s6_does_not_trigger_fill(self, db_with_s6_input):
        """When §6 is already complete, fill must NOT be called."""
        db, rid = db_with_s6_input
        from credit_report.generation.pipeline import run_section_generation

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S6_WITH_RG_FM, tokens=11000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=6, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_not_applicable_does_not_trigger_fill(self, db_with_s6_input):
        """When AI outputs 'Not applicable', fill must NOT be called."""
        db, rid = db_with_s6_input
        from credit_report.generation.pipeline import run_section_generation

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(NOT_APPLICABLE, tokens=50), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=6, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_fill_failure_does_not_crash_generation(self, db_with_s6_input):
        """If fill raises, generation must still complete with status='done'."""
        db, rid = db_with_s6_input
        from credit_report.generation.pipeline import run_section_generation

        with _mock_generate(PARTIAL_S6_MISSING_C6_C7_C8, tokens=8000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables",
                   new=AsyncMock(side_effect=RuntimeError("LLM timeout"))):
            output = await run_section_generation(db, rid, section_no=6, actor_user_id=_uid())

        assert output.status == "done"
        assert "**Project Overview**" in output.markdown  # partial preserved

    async def test_tokens_accumulated_from_fill(self, db_with_s6_input):
        """Token count must include both primary generation and fill call tokens."""
        db, rid = db_with_s6_input
        from credit_report.generation.pipeline import run_section_generation

        primary_tokens = 8000
        fill_tokens = 1500
        fill_output = (
            "**Construction Progress & Risk**\nOn schedule.\nMitigant:\n- KDB RG.\n\n"
            "**Force Majeure**\nCOVID-19 delays resolved.\n\n"
            "**Project Economics**\nVessel earnings in Section 7.\n"
        )

        with _mock_generate(PARTIAL_S6_MISSING_C6_C7_C8, tokens=primary_tokens), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=fill_tokens):
            output = await run_section_generation(db, rid, section_no=6, actor_user_id=_uid())

        assert output.tokens_used == primary_tokens + fill_tokens

    async def test_non_s6_section_not_affected(self, db):
        """§8 has no completeness requirements — fill must not be called."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate("§8 Banking Relationships\n\nCUB relationship since 2022."), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=8, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()


# ── D. Fill prompt content ─────────────────────────────────────────────────────

class TestSection6FillPrompts:

    def test_system_prompt_not_empty(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(6)
        assert prompt, "§6 system prompt must not be empty"

    def test_system_prompt_mentions_11_column_payment_table(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(6)
        assert "11" in prompt and ("column" in prompt or "col" in prompt)

    def test_system_prompt_mentions_mitigant_bullets(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(6)
        assert "mitigant" in prompt.lower() or "bullet" in prompt.lower()

    def test_system_prompt_forbids_credit_judgments(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(6)
        assert "satisfactory" in prompt.lower() or "ZERO" in prompt

    def test_system_prompt_mentions_project_economics_cross_ref(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(6)
        assert "Section 7" in prompt or "cross-reference" in prompt.lower()

    def test_user_prompt_contains_missing_labels(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [
            ("**Construction Progress", "C-6 Construction Progress & Risk"),
            ("**Force Majeure**", "C-7 Force Majeure"),
            ("**Project Economics**", "C-8 Project Economics"),
        ]
        prompt = _build_fill_user_prompt(6, missing, PARTIAL_S6_MISSING_C6_C7_C8, S6_FULL_INPUT, "en")
        assert "C-6 Construction Progress & Risk" in prompt
        assert "C-7 Force Majeure" in prompt
        assert "C-8 Project Economics" in prompt

    def test_user_prompt_includes_existing_tail(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**Project Economics**", "C-8 Project Economics")]
        prompt = _build_fill_user_prompt(6, missing, PARTIAL_S6_MISSING_C6_C7_C8, S6_FULL_INPUT, "en")
        assert "do NOT repeat" in prompt or "context only" in prompt

    def test_user_prompt_includes_input_json(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**Force Majeure**", "C-7 Force Majeure")]
        prompt = _build_fill_user_prompt(6, missing, PARTIAL_S6_MISSING_C6_C7_C8, S6_FULL_INPUT, "en")
        assert "6A_project" in prompt or "6G_force_majeure" in prompt or "6F_construction" in prompt

    def test_user_prompt_credit_judgment_prohibition(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**Construction Progress", "C-6 Construction Progress & Risk")]
        prompt = _build_fill_user_prompt(6, missing, PARTIAL_S6_MISSING_C6_C7_C8, S6_FULL_INPUT, "en")
        assert "satisfactory" in prompt.lower() or "FORBIDDEN" in prompt or "ZERO" in prompt


# ── E. Config — §6 primary token budget ───────────────────────────────────────

class TestSection6Config:

    def test_s6_primary_token_budget_is_12288(self):
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        budget = SECTION_MAX_OUTPUT_TOKENS.get(6, SECTION_MAX_OUTPUT_TOKENS["default"])
        assert budget >= 12288, (
            f"§6 primary token budget must be ≥12 288 to accommodate 11-column payment table "
            f"and construction risk blocks (3-5 mitigant bullets each). Got {budget}."
        )

    @pytest.mark.asyncio
    async def test_s6_fill_budget_is_10240(self):
        """§6 fill budget must be 10 240 (same group as §4 and §5)."""
        from credit_report.generation.completeness import fill_missing_tables
        missing = [("**Project Economics**", "C-8 Project Economics")]
        with patch(
            "credit_report.generation.claude_client.call_gemini_raw",
            new=AsyncMock(return_value="Vessel earnings in Section 7."),
        ) as mock_call:
            await fill_missing_tables(
                section_no=6,
                existing_markdown=PARTIAL_S6_MISSING_C6_C7_C8,
                missing=missing,
                input_json=S6_FULL_INPUT,
            )
            _, kwargs = mock_call.call_args
            max_tok = kwargs.get("max_tokens")
            assert max_tok == 10240, f"§6 fill budget should be 10240, got {max_tok}"

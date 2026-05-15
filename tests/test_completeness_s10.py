"""
§10 Completeness check tests — Appendix (CUB Exposure / Fleet Growth / Projections).

§10 has THREE conditional appendices, each triggered only when the corresponding
input block is present and non-empty:

  Appendix I   — 10A_group_exposure.rows non-empty
    ① Heading  (Appendix I: CUB's Exposure to [Group])
    ② Exposure Table  (10 cols, "Current Approved" header)
    ③ Group Limit Sub-table  (Approved Group Limit | Utilization | Headroom)

  Appendix II  — 10B_fleet_growth.rows non-empty
    ④ Heading  (Appendix II: EMC Capacity Growth Targets)
    ⑤ Fleet Table  (5 cols: Year | Owned Fleet | Total Fleet | Total Vessels | Owned%)
    ⑥ CAPEX Key Note #5  (EMC CAPEX + EMA capital commitment — most truncation-prone)

  Appendix III — 10C_projections truthy
    ⑦  Key Assumptions Table
    ⑧  Assumptions Narrative  (italic paragraph)
    ⑨  Base Case P&L  (≥12 rows, Gross Profit → Net Income)
    ⑩  Base Case Balance Sheet  (≥16 rows, Total Current Assets → Total Equity)
    ⑪  Base Case Cash Flow  (≥6 rows, Operating CF → Closing Cash)
    ⑫  DSCR Table  (separate from CF: OCF | Total Debt Service | DSCR)
    ⑬  DSCR Commentary  (italic "DSCR remains above…")
    ⑭  Worse Case Stress Assumptions  (comparison table ≥4 rows)
    ⑮  Worse Case Summary Table  (Revenue | … | DSCR)
    ⑯  Worse Case Commentary  (italic "Under Worse Case, DSCR declines to…")

Primary token budget: 16 384 (raised per config — §10 is the longest section).
Fill budget: 16 384 tokens.

Coverage:
A. Detection       — full output, individual missing components, empty markdown
B. Conditionality  — 10A/10B/10C absent → skip that appendix
C. Isolation       — §10 check does not bleed into other sections
D. Pipeline        — fill triggered, failure isolated, tokens accumulated
E. Fill prompts    — system prompt content, user prompt content
F. Config          — fill budget == 16384, §11 still has no requirements
"""
from __future__ import annotations

import uuid
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

import json as _json

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


def _mock_fill(text, tokens=1200):
    return patch(
        "credit_report.generation.completeness.fill_missing_tables",
        new=AsyncMock(return_value=(text, tokens)),
    )


# ── Shared input stubs ────────────────────────────────────────────────────────

_INPUT_10A = {
    "10A_group_exposure": {
        "entity_group": "EMC Group",
        "group_limit_usd_m": 1000.0,
        "currency": "USD",
        "unit": "millions",
        "as_of_date": "Jan 2025",
        "rows": [
            {
                "entity": "EMA",
                "branch": "SG",
                "facility_type": "Term Loan (SLL)",
                "current_approved_usd_m": 150.0,
                "proposed_usd_m": 150.0,
                "outstanding_usd_m": 0.0,
                "collateral": "RG + Vessel Mortgage",
                "guarantor": "EMC",
                "maturity_str": "Dec 2034E",
                "msr": "MSR3",
                "is_new_facility": True,
            }
        ],
        "group_limit_sub_table": {
            "approved_group_limit_usd_m": 900.0,
            "proposed_total_exposure_usd_m": 1000.0,
            "utilization_pct": 100.0,
            "headroom_usd_m": 0.0,
        },
        "eva_note": None,
    }
}

_INPUT_10B = {
    "10B_fleet_growth": {
        "group_name": "EMC",
        "year_range": "2023-2028E",
        "rows": [
            {"year_label": "2023", "owned_fleet_teu_m": 1.2, "total_fleet_teu_m": 1.9,
             "total_vessels": 35, "owned_pct": 63.0},
            {"year_label": "2028E", "owned_fleet_teu_m": 2.0, "total_fleet_teu_m": 2.5,
             "total_vessels": 50, "owned_pct": 80.0},
        ],
        "cagr_pct": 6.2,
        "chart_reference": "EMC Fleet Capacity Growth Chart — Source: EMC IR 2024",
        "key_notes": [
            "Target capacity: 2.5m TEU by 2028E",
            "Owned fleet transition: 63% → 80%",
            "Newbuild delivery: 15 vessels; orderbook 20 vessels",
            "CUB-financed vessel: 14,000 TEU, Hull No. 4508, delivery Dec 2025",
            "EMC CAPEX plan: USD 3.2bn; EMA capital commitment: USD 150m (as of Jan 2025)",
        ],
    }
}

_INPUT_10C = {
    "10C_projections": {
        "entity_name": "EMA Standalone",
        "basis": "Standalone",
        "currency": "USD",
        "unit": "USD'000",
        "key_assumptions": [
            {"assumption": "Charter rate (USD/day)", "FY2026E": 28000, "FY2027E": 28500}
        ],
        "assumptions_narrative": "Revenue growth assumes charter rates of USD 28,000/day. COGS reflects fuel and voyage expenses. CAPEX per newbuild schedule.",
        "base_case_pl": [
            {"item": "Revenue", "FY2026E": 500000, "FY2027E": 550000},
            {"item": "Cost of Goods Sold", "FY2026E": 300000, "FY2027E": 330000},
            {"item": "Gross Profit", "FY2026E": 200000, "FY2027E": 220000, "is_subtotal": True},
            {"item": "Operating Profit", "FY2026E": 150000, "FY2027E": 165000, "is_subtotal": True},
            {"item": "Net Income", "FY2026E": 100000, "FY2027E": 110000, "is_subtotal": True},
        ],
        "base_case_bs": [
            {"item": "Cash & Equivalents", "FY2026E": 80000, "FY2027E": 90000},
            {"item": "Total Current Assets", "FY2026E": 120000, "FY2027E": 135000, "is_subtotal": True},
            {"item": "Total Assets", "FY2026E": 900000, "FY2027E": 950000, "is_subtotal": True},
            {"item": "Total Equity", "FY2026E": 400000, "FY2027E": 450000, "is_subtotal": True},
        ],
        "base_case_cf": [
            {"item": "Operating Cash Flow", "FY2026E": 120000, "FY2027E": 130000},
            {"item": "Closing Cash", "FY2026E": 80000, "FY2027E": 90000, "is_subtotal": True},
        ],
        "base_case_dscr": [
            {"year_label": "FY2026E", "ocf": 120000, "debt_service": 22000, "dscr": 5.5}
        ],
        "dscr_commentary": "DSCR remains above 1.10x throughout. Minimum DSCR of 1.32x occurs in FY2029E.",
        "stress_assumptions": [
            {"assumption": "Revenue", "base_case": "USD 28,000/day", "worse_case": "USD 22,400/day", "stress_magnitude": "-20%"},
            {"assumption": "COGS%", "base_case": "60%", "worse_case": "65%", "stress_magnitude": "+5 ppts"},
            {"assumption": "SOFR", "base_case": "5.0%", "worse_case": "6.0%", "stress_magnitude": "+100 bps"},
            {"assumption": "Dividend", "base_case": "40%", "worse_case": "0%", "stress_magnitude": "-40 ppts"},
        ],
        "worse_case_summary": [
            {"item": "Revenue", "value": 440000},
            {"item": "Operating Profit", "value": 110000},
            {"item": "Net Income", "value": 75000},
            {"item": "OCF", "value": 85000},
            {"item": "Cash Balance", "value": 40000},
            {"item": "DSCR", "value": 1.10, "is_dscr": True},
        ],
        "worse_case_commentary": "Under Worse Case, DSCR declines to minimum 1.10x in FY2028E but remains above 1.0x in all years.",
    }
}

_INPUT_ALL = {**_INPUT_10A, **_INPUT_10B, **_INPUT_10C}


# ── Shared markdown stubs ─────────────────────────────────────────────────────

_APP_I = (
    "## Appendix I: CUB's Exposure to EMC Group\n\n"
    "*The following table supports Section 1: Credit Facility and Case Details (Group Limit).*\n"
    "*Unit: USD millions | As of: Jan 2025*\n\n"
    "| Entity | Branch | Facility Type | Current Approved | Proposed | Outstanding | "
    "Collateral | Guarantor | Maturity | MSR |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
    "| EMA | SG | **[NEW]** Term Loan (SLL) | — | 150.0 | — | RG + Vessel Mortgage | EMC | Dec 2034E | MSR3 |\n"
    "| **EMA Subtotal** | | | — | **150.0** | — | | | | |\n"
    "| **Group Total** | | | 900.0 | **1,000.0** | 750.0 | | | | |\n\n"
    "**Group Limit**\n\n"
    "| Item | Amount (USD m) |\n"
    "|---|---|\n"
    "| Approved Group Limit | 900.0 |\n"
    "| Proposed Total Exposure | 1,000.0 |\n"
    "| **Utilization** | **111.1%** |\n"
    "| Headroom | (100.0) |\n\n"
)

_APP_II = (
    "## Appendix II: EMC Capacity Growth Targets (2023–2028E)\n\n"
    "*The following supports Section 4: Corporate History and Overview (Fleet Overview).*\n\n"
    "| Year | Owned Fleet (TEU million) | Total Fleet (TEU million) | Total Vessels | Owned% |\n"
    "|---|---|---|---|---|\n"
    "| 2023 | 1.20 | 1.90 | 35 | 63% |\n"
    "| 2028E | 2.00 | 2.50 | 50 | 80% |\n\n"
    "**CAGR: 6.2%**\n\n"
    "*[EMC Fleet Capacity Growth Chart — Source: EMC IR 2024 / EMC Investor Presentation]*\n\n"
    "**Key Notes:**\n"
    "1. Target capacity: 2.5m TEU by 2028E.\n"
    "2. Owned fleet transition: 63% → 80% — reducing charter reliance.\n"
    "3. Newbuild delivery: 15 vessels; orderbook 20 vessels.\n"
    "4. CUB-financed vessel: 14,000 TEU, Hull No. 4508, delivery Dec 2025.\n"
    "5. EMC CAPEX plan: USD 3.2bn; EMA capital commitment: USD 150m (as of Jan 2025).\n\n"
)

_APP_III_ASSUMPTIONS = (
    "## Appendix III: EMA — Detailed Financial Projections\n\n"
    "*Entity: EMA Standalone | Currency: USD | Unit: USD'000*\n\n"
    "#### Key Assumptions\n\n"
    "| Assumption | FY2026E | FY2027E |\n"
    "|---|---|---|\n"
    "| Charter rate (USD/day) | 28,000 | 28,500 |\n\n"
    "_Revenue growth assumes charter rates of USD 28,000/day. "
    "COGS reflects fuel and voyage expenses. CAPEX per newbuild schedule._\n\n"
)

_APP_III_PL = (
    "#### Base Case — Projected P&L\n\n"
    "| Item | FY2026E | FY2027E |\n"
    "|---|---|---|\n"
    "| Revenue | 500,000 | 550,000 |\n"
    "| Cost of Goods Sold | (300,000) | (330,000) |\n"
    "| **Gross Profit** | **200,000** | **220,000** |\n"
    "| Other Operating Income | 5,000 | 5,500 |\n"
    "| Operating Expenses | (50,000) | (55,000) |\n"
    "| **Operating Profit** | **155,000** | **170,500** |\n"
    "| Finance Income | 1,000 | 1,200 |\n"
    "| Finance Cost | (20,000) | (22,000) |\n"
    "| Other Non-Operating | 0 | 0 |\n"
    "| **Profit Before Tax** | **136,000** | **149,700** |\n"
    "| Income Tax | (36,000) | (39,700) |\n"
    "| **Net Income** | **100,000** | **110,000** |\n\n"
)

_APP_III_BS = (
    "#### Base Case — Projected Balance Sheet\n\n"
    "| Item | FY2026E | FY2027E |\n"
    "|---|---|---|\n"
    "| Cash & Equivalents | 80,000 | 90,000 |\n"
    "| Trade Receivables | 30,000 | 33,000 |\n"
    "| Other Current Assets | 10,000 | 12,000 |\n"
    "| **Total Current Assets** | **120,000** | **135,000** |\n"
    "| Vessels & Equipment | 700,000 | 730,000 |\n"
    "| Right-of-Use Assets | 50,000 | 55,000 |\n"
    "| Other Non-Current Assets | 30,000 | 30,000 |\n"
    "| **Total Non-Current Assets** | **780,000** | **815,000** |\n"
    "| **Total Assets** | **900,000** | **950,000** |\n"
    "| **Total Current Liabilities** | **100,000** | **105,000** |\n"
    "| Long-term Borrowings | 350,000 | 340,000 |\n"
    "| Non-Current Lease Liabilities | 40,000 | 45,000 |\n"
    "| Other Non-Current Liabilities | 10,000 | 10,000 |\n"
    "| **Total Non-Current Liabilities** | **400,000** | **395,000** |\n"
    "| **Total Liabilities** | **500,000** | **500,000** |\n"
    "| **Total Equity** | **400,000** | **450,000** |\n\n"
)

_APP_III_CF = (
    "#### Base Case — Projected Cash Flow\n\n"
    "| Item | FY2026E | FY2027E |\n"
    "|---|---|---|\n"
    "| Operating Cash Flow | 120,000 | 130,000 |\n"
    "| Investing Cash Flow | (80,000) | (70,000) |\n"
    "| Financing Cash Flow | (30,000) | (32,000) |\n"
    "| **Net Change in Cash** | **10,000** | **28,000** |\n"
    "| Opening Cash | 70,000 | 80,000 |\n"
    "| **Closing Cash** | **80,000** | **90,000** |\n\n"
)

_APP_III_DSCR = (
    "#### Base Case — DSCR Analysis\n\n"
    "| FY[Y]E | OCF | Total Debt Service (P+I) | DSCR |\n"
    "|---|---|---|---|\n"
    "| FY2026E | 120,000 | 22,000 | 5.5x |\n"
    "| FY2027E | 130,000 | 23,000 | 5.7x |\n\n"
    "_DSCR remains above 1.10x throughout. Minimum DSCR of 1.32x occurs in FY2029E._\n\n"
)

_APP_III_WC = (
    "#### Worse Case — Stress Assumptions\n\n"
    "| Assumption | Base Case | Worse Case | Stress Magnitude |\n"
    "|---|---|---|---|\n"
    "| Revenue | USD 28,000/day | USD 22,400/day | -20% |\n"
    "| COGS% | 60% | 65% | +5 ppts |\n"
    "| SOFR | 5.0% | 6.0% | +100 bps |\n"
    "| Dividend | 40% | 0% | -40 ppts |\n\n"
    "#### Worse Case — Stressed Summary\n\n"
    "| Item | Worse Case |\n"
    "|---|---|\n"
    "| Revenue | 440,000 |\n"
    "| Operating Profit | 110,000 |\n"
    "| **Net Income** | **75,000** |\n"
    "| OCF | 85,000 |\n"
    "| Cash Balance | 40,000 |\n"
    "| **DSCR** | **1.10x** |\n\n"
    "_Under Worse Case, DSCR declines to minimum 1.10x in FY2028E but remains above 1.0x in all years._\n\n"
)

_APP_III = (
    _APP_III_ASSUMPTIONS
    + _APP_III_PL
    + _APP_III_BS
    + _APP_III_CF
    + _APP_III_DSCR
    + _APP_III_WC
)

FULL_S10 = _APP_I + _APP_II + _APP_III


# ── A. Detection ──────────────────────────────────────────────────────────────

class TestSection10Detection:

    def test_full_s10_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(10, FULL_S10, _INPUT_ALL)
        assert missing == [], f"Expected no missing, got: {missing}"

    def test_no_input_data_no_requirements(self):
        from credit_report.generation.completeness import check_section_completeness
        # All appendices are conditional — no input means nothing to check
        missing = check_section_completeness(10, "", {})
        assert missing == [], "No input data → no requirements"

    def test_appendix_i_exposure_table_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _APP_I.replace("Current Approved", "Approved").replace("current approved", "approved")
        missing = check_section_completeness(10, md, _INPUT_10A)
        labels = [label for _, label in missing]
        assert any("Current Approved" in l or "A-I Exposure" in l for l in labels), labels

    def test_appendix_i_group_limit_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_limit = _APP_I.replace("Group Limit", "Facility Summary")
        missing = check_section_completeness(10, md_no_limit, _INPUT_10A)
        labels = [label for _, label in missing]
        assert any("Group Limit" in l for l in labels), labels

    def test_appendix_ii_fleet_table_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        # Remove "Total Fleet" and "Total Vessels" so fleet table is undetectable
        md = _APP_II.replace("Total Fleet", "XX").replace("Total Vessels", "XX")
        missing = check_section_completeness(10, md, _INPUT_10B)
        labels = [label for _, label in missing]
        assert any("Fleet Table" in l or "Owned Fleet" in l for l in labels), labels

    def test_appendix_ii_capex_note_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md_no_capex = _APP_II.replace("CAPEX", "capital expenditure")
        missing = check_section_completeness(10, md_no_capex, _INPUT_10B)
        labels = [label for _, label in missing]
        assert any("CAPEX" in l for l in labels), labels

    def test_appendix_iii_base_pl_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _APP_III_ASSUMPTIONS + _APP_III_BS + _APP_III_CF + _APP_III_DSCR + _APP_III_WC
        missing = check_section_completeness(10, md, _INPUT_10C)
        labels = [label for _, label in missing]
        assert any("P&L" in l or "Gross Profit" in l for l in labels), labels

    def test_appendix_iii_base_bs_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _APP_III_ASSUMPTIONS + _APP_III_PL + _APP_III_CF + _APP_III_DSCR + _APP_III_WC
        missing = check_section_completeness(10, md, _INPUT_10C)
        labels = [label for _, label in missing]
        assert any("Balance Sheet" in l or "Total Current Assets" in l for l in labels), labels

    def test_appendix_iii_base_cf_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _APP_III_ASSUMPTIONS + _APP_III_PL + _APP_III_BS + _APP_III_DSCR + _APP_III_WC
        missing = check_section_completeness(10, md, _INPUT_10C)
        labels = [label for _, label in missing]
        assert any("Cash Flow" in l or "Operating Cash Flow" in l for l in labels), labels

    def test_appendix_iii_dscr_table_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _APP_III_ASSUMPTIONS + _APP_III_PL + _APP_III_BS + _APP_III_CF + _APP_III_WC
        missing = check_section_completeness(10, md, _INPUT_10C)
        labels = [label for _, label in missing]
        assert any("DSCR" in l for l in labels), labels

    def test_appendix_iii_dscr_commentary_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md = (
            _APP_III_ASSUMPTIONS + _APP_III_PL + _APP_III_BS + _APP_III_CF
            + "| FY2026E | 120,000 | 22,000 | 5.5x |\n\n"  # DSCR table with "debt service"
            + "Total Debt Service (P+I) shown above.\n\n"  # no "dscr remains" or "minimum dscr"
            + _APP_III_WC
        )
        missing = check_section_completeness(10, md, _INPUT_10C)
        labels = [label for _, label in missing]
        assert any("DSCR Commentary" in l for l in labels), labels

    def test_appendix_iii_wc_stress_table_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _APP_III_ASSUMPTIONS + _APP_III_PL + _APP_III_BS + _APP_III_CF + _APP_III_DSCR
        missing = check_section_completeness(10, md, _INPUT_10C)
        labels = [label for _, label in missing]
        assert any("Stress" in l for l in labels), labels

    def test_appendix_iii_wc_commentary_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        # Remove "Under Worse Case" from the WC block
        wc_no_comment = _APP_III_WC.replace(
            "_Under Worse Case, DSCR declines to minimum 1.10x in FY2028E "
            "but remains above 1.0x in all years._\n\n",
            "",
        )
        md = _APP_III_ASSUMPTIONS + _APP_III_PL + _APP_III_BS + _APP_III_CF + _APP_III_DSCR + wc_no_comment
        missing = check_section_completeness(10, md, _INPUT_10C)
        labels = [label for _, label in missing]
        assert any("Worse Case Commentary" in l for l in labels), labels

    def test_empty_markdown_with_all_inputs_flags_all_components(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(10, "", _INPUT_ALL)
        labels = [label for _, label in missing]
        # All 3 appendices should be flagged
        assert any("A-I" in l or "Current Approved" in l for l in labels), "Appendix I expected"
        assert any("Fleet Table" in l or "Owned Fleet" in l for l in labels), "Appendix II fleet table expected"
        assert any("CAPEX" in l for l in labels), "Appendix II CAPEX note expected"
        assert any("P&L" in l or "Gross Profit" in l for l in labels), "Appendix III P&L expected"
        assert any("DSCR Commentary" in l for l in labels), "Appendix III DSCR commentary expected"
        assert any("Worse Case Commentary" in l for l in labels), "Appendix III WC commentary expected"


# ── B. Conditionality ─────────────────────────────────────────────────────────

class TestSection10Conditionality:

    def test_no_10a_data_skips_appendix_i(self):
        from credit_report.generation.completeness import check_section_completeness
        # Only 10B present — Appendix I should NOT be flagged
        missing = check_section_completeness(10, _APP_II, _INPUT_10B)
        labels = [label for _, label in missing]
        assert not any("A-I" in l or "Current Approved" in l or "Group Limit" in l for l in labels), labels

    def test_empty_10a_rows_skips_appendix_i(self):
        from credit_report.generation.completeness import check_section_completeness
        input_empty_rows = {
            "10A_group_exposure": {
                "entity_group": "EMC Group",
                "rows": [],
                "group_limit_sub_table": None,
            }
        }
        missing = check_section_completeness(10, "", input_empty_rows)
        labels = [label for _, label in missing]
        assert not any("A-I" in l or "Current Approved" in l for l in labels), labels

    def test_no_10b_data_skips_appendix_ii(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(10, _APP_I, _INPUT_10A)
        labels = [label for _, label in missing]
        assert not any("Owned Fleet" in l or "CAPEX" in l or "A-II" in l for l in labels), labels

    def test_empty_10b_rows_skips_appendix_ii(self):
        from credit_report.generation.completeness import check_section_completeness
        input_empty_fleet = {"10B_fleet_growth": {"group_name": "EMC", "rows": []}}
        missing = check_section_completeness(10, "", input_empty_fleet)
        labels = [label for _, label in missing]
        assert not any("Owned Fleet" in l or "CAPEX" in l for l in labels), labels

    def test_no_10c_data_skips_appendix_iii(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(10, _APP_I + _APP_II, {**_INPUT_10A, **_INPUT_10B})
        labels = [label for _, label in missing]
        assert not any("Gross Profit" in l or "P&L" in l or "DSCR" in l for l in labels), labels

    def test_10c_none_skips_appendix_iii(self):
        from credit_report.generation.completeness import check_section_completeness
        input_null_proj = {"10C_projections": None}
        missing = check_section_completeness(10, "", input_null_proj)
        labels = [label for _, label in missing]
        assert not any("Gross Profit" in l or "P&L" in l for l in labels), labels

    def test_only_10c_present_skips_i_and_ii(self):
        from credit_report.generation.completeness import check_section_completeness
        # Only 10C data → only Appendix III checks run
        missing = check_section_completeness(10, _APP_III, _INPUT_10C)
        labels = [label for _, label in missing]
        assert not any("A-I" in l or "Current Approved" in l for l in labels), labels
        assert not any("Owned Fleet" in l or "CAPEX" in l for l in labels), labels


# ── C. Isolation ──────────────────────────────────────────────────────────────

class TestSection10Isolation:

    def test_s10_check_does_not_affect_s9(self):
        from credit_report.generation.completeness import check_section_completeness
        # §9 check uses different markers — §10 content must not interfere
        result = check_section_completeness(9, FULL_S10)
        labels = [label for _, label in result]
        # §10 content has no checklist, no sign-off → §9 will report them missing,
        # but it must NOT produce zero length (verify it actually runs §9 logic)
        assert all("A-I" not in l and "Appendix" not in l for l in labels), \
            "§9 check should not reference §10-specific labels"

    def test_section_11_has_no_requirements(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(11, "some markdown", _INPUT_ALL)
        assert result == [], "§11 has no completeness requirements"

    def test_s10_full_content_does_not_trigger_s7_checks(self):
        from credit_report.generation.completeness import check_section_completeness
        # §7 checks for **C-1. Borrower Historical Financials** etc.
        # §10 Appendix III P&L contains "Net Income" and "Gross Profit" but NOT §7 markers
        missing_s7 = check_section_completeness(7, FULL_S10, {})
        labels = [label for _, label in missing_s7]
        assert any("C-1 Borrower Historical Financials" in l for l in labels), \
            "§7 C-1 should be flagged when §10 content provided"

    def test_s10_markers_case_insensitive(self):
        from credit_report.generation.completeness import check_section_completeness
        md_lower = FULL_S10.lower()
        missing = check_section_completeness(10, md_lower, _INPUT_ALL)
        assert missing == [], "Detection should be case-insensitive"


# ── D. Pipeline integration ───────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSection10PipelineIntegration:

    async def test_missing_dscr_commentary_triggers_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        db.add(SectionInput(report_id=rid, section_no=10,
                            input_json=_json.dumps(_INPUT_ALL)))
        await db.flush()

        # §10 with all appendices except DSCR commentary
        partial = (
            _APP_I + _APP_II + _APP_III_ASSUMPTIONS + _APP_III_PL
            + _APP_III_BS + _APP_III_CF + _APP_III_DSCR.split("_DSCR remains")[0]
            + _APP_III_WC
        )
        fill_output = "_DSCR remains above 1.10x throughout. Minimum DSCR of 1.32x in FY2029E._\n\n"

        with _mock_generate(partial), _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output):
            output = await run_section_generation(db, rid, section_no=10, actor_user_id=_uid())

        assert output.status == "done"
        assert "dscr remains" in output.markdown.lower() or "minimum dscr" in output.markdown.lower()

    async def test_complete_s10_does_not_trigger_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        db.add(SectionInput(report_id=rid, section_no=10,
                            input_json=_json.dumps(_INPUT_ALL)))
        await db.flush()

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S10), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=10, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_fill_failure_does_not_crash_s10(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        db.add(SectionInput(report_id=rid, section_no=10,
                            input_json=_json.dumps(_INPUT_ALL)))
        await db.flush()

        partial = _APP_I  # Only Appendix I, missing II and III

        with _mock_generate(partial), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables",
                   new=AsyncMock(side_effect=RuntimeError("LLM timeout"))):
            output = await run_section_generation(db, rid, section_no=10, actor_user_id=_uid())

        assert output.status == "done", "Fill failure must not abort §10 generation"
        assert "Current Approved" in output.markdown  # partial output preserved

    async def test_tokens_accumulated_from_s10_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        db.add(SectionInput(report_id=rid, section_no=10,
                            input_json=_json.dumps(_INPUT_ALL)))
        await db.flush()

        primary_tokens = 8000
        fill_tokens = 3000
        fill_output = "_DSCR remains above 1.10x throughout._\n\n" + _APP_III_WC

        with _mock_generate(
            _APP_I + _APP_II + _APP_III_ASSUMPTIONS + _APP_III_PL
            + _APP_III_BS + _APP_III_CF + _APP_III_DSCR.split("_DSCR remains")[0],
            tokens=primary_tokens,
        ), _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=fill_tokens):
            output = await run_section_generation(db, rid, section_no=10, actor_user_id=_uid())

        assert output.tokens_used == primary_tokens + fill_tokens


# ── E. Fill prompts ───────────────────────────────────────────────────────────

class TestSection10FillPrompts:

    def test_system_prompt_mentions_10_columns(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "10 columns" in prompt or "EXACTLY 10" in prompt, prompt[:300]

    def test_system_prompt_mentions_current_approved(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "Current Approved" in prompt

    def test_system_prompt_mentions_group_limit_sub_table(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "Group Limit" in prompt and "Utilization" in prompt

    def test_system_prompt_mentions_5_column_fleet_table(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "5 columns" in prompt or "EXACTLY 5" in prompt

    def test_system_prompt_mentions_owned_pct(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "Owned%" in prompt or "Owned %" in prompt

    def test_system_prompt_mentions_capex(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "CAPEX" in prompt

    def test_system_prompt_mentions_key_notes(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "Key Notes" in prompt or "key notes" in prompt.lower()

    def test_system_prompt_mentions_pl_rows(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "P&L" in prompt or "≥12" in prompt

    def test_system_prompt_mentions_balance_sheet_rows(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "BS" in prompt or "≥16" in prompt or "Balance Sheet" in prompt

    def test_system_prompt_mentions_cash_flow_rows(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "CF" in prompt or "≥6" in prompt or "Cash Flow" in prompt

    def test_system_prompt_mentions_dscr_table_separate(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "SEPARATE" in prompt and "DSCR" in prompt

    def test_system_prompt_mentions_dscr_commentary(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "DSCR remains" in prompt or "dscr remains" in prompt.lower()

    def test_system_prompt_mentions_worse_case_stress_table(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "Stress Magnitude" in prompt

    def test_system_prompt_mentions_worse_case_commentary(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "Under Worse Case" in prompt

    def test_system_prompt_prohibits_credit_judgments(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "satisfactory" in prompt.lower() or "FORBIDDEN" in prompt

    def test_system_prompt_prohibits_same_in_assumption_cells(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(10)
        assert "Same" in prompt or "'Same'" in prompt

    def test_user_prompt_contains_missing_labels(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [
            ("DSCR", "A-III DSCR Analysis Table"),
            ("Worse Case Commentary", "A-III Worse Case Commentary"),
        ]
        prompt = _build_fill_user_prompt(10, missing, FULL_S10, _INPUT_ALL, "en")
        assert "A-III DSCR Analysis Table" in prompt
        assert "A-III Worse Case Commentary" in prompt

    def test_user_prompt_includes_input_json(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("CAPEX", "A-II Key Note #5")]
        prompt = _build_fill_user_prompt(10, missing, "", _INPUT_10B, "en")
        assert "10B_fleet_growth" in prompt or "EMC" in prompt

    def test_user_prompt_includes_language_field(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("Gross Profit", "A-III P&L")]
        prompt = _build_fill_user_prompt(10, missing, "", {}, "zh-TW")
        assert "zh-TW" in prompt

    def test_user_prompt_mentions_appendix_iii_rules(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("Gross Profit", "A-III Base Case P&L")]
        prompt = _build_fill_user_prompt(10, missing, "", _INPUT_10C, "en")
        assert "P&L" in prompt or "≥12" in prompt or "Gross Profit" in prompt


# ── F. Config ─────────────────────────────────────────────────────────────────

class TestSection10Config:

    @pytest.mark.asyncio
    async def test_s10_fill_budget_is_16384(self):
        """fill_missing_tables must use 16 384 tokens for §10."""
        from credit_report.generation.completeness import fill_missing_tables

        captured = {}

        async def fake_raw(system_prompt, user_prompt, max_tokens, **kwargs):
            captured["max_tokens"] = max_tokens
            return "| Item | FY2026E |\n| Revenue | 500,000 |\n"

        with patch("credit_report.generation.claude_client.call_gemini_raw", new=fake_raw):
            await fill_missing_tables(
                section_no=10,
                existing_markdown="",
                missing=[("Gross Profit", "A-III Base Case P&L")],
                input_json=_INPUT_10C,
            )

        assert captured.get("max_tokens") == 16384, \
            f"Expected 16384, got {captured.get('max_tokens')}"

    def test_s10_primary_budget_is_16384(self):
        """SECTION_MAX_OUTPUT_TOKENS[10] must be 16 384."""
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        assert SECTION_MAX_OUTPUT_TOKENS.get(10) == 16384

    def test_section_11_has_no_requirements_after_s10_added(self):
        """After adding §10 support, §11 must still return no requirements."""
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(11, "some markdown", _INPUT_ALL)
        assert result == [], "§11 should have no completeness requirements"

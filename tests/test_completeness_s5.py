"""
§5 Completeness check tests — Collateral / Responsible Person / Guarantor / Support.

§5 is HIGHLY CONDITIONAL — unlike §2/§3/§4:
  ① C-0 Security Package Overview      → **C-0.   (ALWAYS)
  ② C-1 Pre-Delivery Security (RG)     → **C-1.   (only when 5B_refund_guarantee provided)
  ③ C-2 Post-Delivery Mortgage         → **C-2.   (only when secured)
  ④ C-3 Amortisation Profile           → **C-3.   (only when secured + schedule data)
  ⑤ C-4 Insurance                      → **C-4.   (only when 5D_insurance provided)
  ⑥ C-5 Value Maintenance Clause       → **C-5.   (only when VMC data provided)
  ⑦ C-6 Corporate Guarantee            → **C-6.   (only when 5F_corporate_guarantee applicable)
  ⑧ C-7 Responsible Person Guarantee   → **C-7.   (ALWAYS — even if "none")
  ⑨ C-8 Collateral Adequacy Conclusion → **C-8.   (ALWAYS, QA F-7)

Truncation risk: C-6, C-7, C-8 are at the end of a verbose section.
Primary token budget raised from 8 192 → 12 288 (amortisation table up to 24 rows,
dual-currency guarantor table, 8-column RG milestone table).
Fill budget is 10 240 tokens (C-3 amortisation can be very large).

Coverage:
A. Detection — full secured, partial, unsecured, empty markdown
B. Conditional boundary — §5 only; skip logic for unsecured / no-guarantor
C. Pipeline integration — fill triggered, failure isolated, tokens accumulated
D. Fill prompt content — correct column rules for RG, VMC, guarantor
E. Config — §5 primary token budget
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

# Full secured facility with RG + mortgage + amortisation + insurance + VMC + guarantor
FULL_S5_SECURED = (
    "**5. Collateral / Responsible Person / Guarantor / Support**\n\n"
    "**C-0. Security Package Overview**\n"
    "This is a secured facility. The following security instruments are taken:\n"
    "1. Pre-delivery: Refund Guarantee issued by Korea Development Bank.\n"
    "2. Post-delivery: First priority mortgage over the vessel.\n\n"
    "**C-1. Pre-Delivery Security — Refund Guarantee**\n"
    "Korea Development Bank (KDB) issued a Refund Guarantee covering all pre-delivery "
    "instalments. KDB is rated AA by S&P and AA- by Fitch.\n"
    "| Milestone | Sched. Date | RG Amount (USD m) | Max Loan O/S (USD m) | "
    "Coverage % | Drawdown (USD m) | Cum. Drawdown (USD m) | Status |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| Steel Cutting | 15 Mar 2024 | 10.00 | 10.00 | 100.0% | 10.00 | 10.00 | ✅ Completed |\n"
    "| Keel Laying | 20 Jun 2024 | 10.00 | 20.00 | 100.0% | 10.00 | 20.00 | ✅ Completed |\n"
    "| Launch | 15 Sep 2024 | 20.00 | 40.00 | 100.0% | 20.00 | 40.00 | Pending |\n"
    "| Delivery | 15 Dec 2024 | 173.84 | 213.84 | 100.0% | 173.84 | 213.84 | Pending |\n"
    "[RG = Refund Guarantee; O/S = Outstanding]\n\n"
    "**C-2. Post-Delivery Security — First Priority Mortgage**\n"
    "| Vessel | TEU | DWT | Year Built | Valuer | Valuation Date | "
    "Market Value (USD m) | Distressed Value (USD m) |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| M/V EMC Vessel | 15,000 | 180,000 | 2025 | Clarksons | 15 Jan 2025 | 280.00 | 224.00 |\n"
    "LTC = 213.84 / 267.30 = 80.0% (limit: ≤80.0%)\n"
    "ACR at delivery = 280.00 / 213.84 = 130.9% (floor: ≥125.0%)\n"
    "LTV at maturity = 42.77 / 224.00 = 19.1% (cap: ≤100.0%)\n\n"
    "**C-3. Amortisation Profile (Loan Repayment Schedule)**\n"
    "| Period | Date | Principal (USD m) | Interest (USD m) | "
    "Total Debt Service (USD m) | Outstanding Balance (USD m) | LTV % |\n"
    "|---|---|---|---|---|---|---|\n"
    "| 1 | 15 Jun 2025 | 7.71 | 2.50 | 10.21 | 206.13 | 73.6% |\n"
    "| 14 | 15 Dec 2031 | 7.71 | 0.50 | 8.21 | 42.77 | 19.1% |\n\n"
    "**C-4. Insurance**\n"
    "| Type | Insurer / P&I Club | Insured Value (USD m) | Notes |\n"
    "|---|---|---|---|\n"
    "| Hull & Machinery | Skuld P&I | 280.00 | CUB named as co-insured |\n"
    "| Protection & Indemnity | UK P&I Club | — | CUB named as loss payee |\n\n"
    "**C-5. Value Maintenance Clause**\n"
    "ACR Covenant: ACR ≥ 125.0% where ACR = Fair Market Value / Loan Outstanding\n"
    "LTV Covenant: LTV ≤ 100.0% where LTV = Loan Outstanding / Distressed Value\n"
    "Testing: every 2 years OR upon each drawdown (whichever earlier)\n"
    "Cure Period: 30 Banking Days\n"
    "Upon breach of the value maintenance clause, the Borrower shall within 30 Banking Days "
    "of receipt of written notice from the Bank either (i) prepay such portion of the Loan as "
    "will restore compliance, or (ii) provide additional security satisfactory to the Bank.\n\n"
    "**C-6. Corporate Guarantee & Guarantor Financial Capacity**\n"
    "Evergreen Marine Corporation (Taiwan) Ltd. (EMC) provides a full corporate guarantee.\n"
    "| Metric | FY2023 TWD bn | FY2023 USD bn | FY2024 TWD bn | FY2024 USD bn |\n"
    "|---|---|---|---|---|\n"
    "| Cash & Equivalents | 120.0 | 3.7 | 135.0 | 4.2 |\n"
    "FX rate used: USD/TWD = 32.5\n\n"
    "**C-7. Responsible Person Guarantee**\n"
    "No responsible person guarantee is required for this facility.\n\n"
    "**C-8. Collateral Adequacy Conclusion**\n"
    "The collateral package is assessed as adequate. LTC of 80.0% is within policy limits. "
    "ACR at delivery of 130.9% exceeds the 125.0% floor. "
    "LTV at maturity of 19.1% is well within the 100.0% cap. "
    "The KDB refund guarantee provides full coverage during the pre-delivery phase. "
    "Overall, the collateral position is satisfactory.\n"
)

# Unsecured facility — C-2 through C-5 are skipped by design
FULL_S5_UNSECURED = (
    "**5. Collateral / Responsible Person / Guarantor / Support**\n\n"
    "**C-0. Security Package Overview**\n"
    "This is a clean/unsecured facility. No collateral is taken.\n\n"
    "**C-7. Responsible Person Guarantee**\n"
    "No responsible person guarantee is required for this facility.\n\n"
    "**C-8. Collateral Adequacy Conclusion**\n"
    "This is an unsecured facility; no collateral adequacy assessment is applicable. "
    "The facility is approved on the basis of the borrower's standalone creditworthiness.\n"
)

# Partial — C-0 through C-6 present, C-7 and C-8 missing (most likely truncation point)
PARTIAL_S5_MISSING_C7_C8 = (
    "**5. Collateral / Responsible Person / Guarantor / Support**\n\n"
    "**C-0. Security Package Overview**\n"
    "This is a secured facility.\n\n"
    "**C-1. Pre-Delivery Security — Refund Guarantee**\n"
    "KDB issued a Refund Guarantee.\n"
    "| Milestone | Sched. Date | RG Amount (USD m) | Max Loan O/S (USD m) | "
    "Coverage % | Drawdown (USD m) | Cum. Drawdown (USD m) | Status |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| Steel Cutting | 15 Mar 2024 | 10.00 | 10.00 | 100.0% | 10.00 | 10.00 | ✅ Completed |\n"
    "[RG = Refund Guarantee; O/S = Outstanding]\n\n"
    "**C-2. Post-Delivery Security — First Priority Mortgage**\n"
    "LTC = 213.84 / 267.30 = 80.0%\n\n"
    "**C-3. Amortisation Profile (Loan Repayment Schedule)**\n"
    "| Period | Date | Principal (USD m) | Interest (USD m) | "
    "Total Debt Service (USD m) | Outstanding Balance (USD m) | LTV % |\n"
    "|---|---|---|---|---|---|---|\n"
    "| 1 | 15 Jun 2025 | 7.71 | 2.50 | 10.21 | 206.13 | 73.6% |\n\n"
    "**C-4. Insurance**\n"
    "| Type | Insurer / P&I Club | Insured Value (USD m) | Notes |\n"
    "|---|---|---|---|\n"
    "| Hull & Machinery | Skuld P&I | 280.00 | CUB named as co-insured |\n\n"
    "**C-5. Value Maintenance Clause**\n"
    "ACR Covenant: ACR ≥ 125.0%; Cure Period: 30 Banking Days\n\n"
    "**C-6. Corporate Guarantee & Guarantor Financial Capacity**\n"
    "EMC provides a full corporate guarantee.\n"
    # C-7 and C-8 missing
)

S5_SECURED_INPUT = {
    "5A_security_overview": {"is_secured": True, "security_instruments": [{"rank": 1, "instrument": "RG"}]},
    "5B_refund_guarantee": {
        "applicable": True,
        "issuer_full_name": "Korea Development Bank",
        "issuer_rating": "AA",
        "milestones": [{"milestone": "Steel Cutting", "rg_amount_usd_m": 10.0}],
    },
    "5C_vessel_mortgage": {
        "applicable": True,
        "vessel_valuations": [{"vessel": "M/V EMC Vessel", "market_value_usd_m": 280.0}],
        "loan_amount_usd_m": 213.84,
        "amortisation_schedule": [{"period": 1, "date": "2025-06-15"}],
    },
    "5D_insurance": [{"type": "Hull & Machinery", "insurer_or_club": "Skuld P&I", "insured_value_usd_m": 280.0}],
    "5E_value_maintenance_clause": {
        "acr_covenant_pct": 125.0,
        "ltv_covenant_pct": 100.0,
        "cure_period_banking_days": 30,
        "cure_mechanism_verbatim": "prepay or additional security",
    },
    "5F_corporate_guarantee": {
        "applicable": True,
        "guarantor_full_name": "Evergreen Marine Corporation (Taiwan) Ltd.",
    },
    "5G_responsible_person": {"provided": False},
}

S5_UNSECURED_INPUT = {
    "5A_security_overview": {"is_secured": False, "unsecured_reason": "Clean facility"},
    "5B_refund_guarantee": {"applicable": False},
    "5C_vessel_mortgage": {"applicable": False},
    "5D_insurance": [],
    "5E_value_maintenance_clause": {},
    "5F_corporate_guarantee": {"applicable": False},
    "5G_responsible_person": {"provided": False},
}


# ── A. Detection ──────────────────────────────────────────────────────────────

class TestSection5Detection:

    def test_full_secured_s5_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(5, FULL_S5_SECURED, S5_SECURED_INPUT)
        assert missing == [], f"Full §5 secured should have no gaps, got: {[l for _, l in missing]}"

    def test_full_unsecured_s5_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(5, FULL_S5_UNSECURED, S5_UNSECURED_INPUT)
        assert missing == [], f"Full §5 unsecured should have no gaps, got: {[l for _, l in missing]}"

    def test_empty_markdown_secured_all_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(5, "", S5_SECURED_INPUT)
        labels = [l for _, l in missing]
        # All 9 items expected for a full secured facility
        assert "C-0 Security Package Overview" in labels
        assert "C-1 Pre-Delivery Security — Refund Guarantee" in labels
        assert "C-2 Post-Delivery Security — First Priority Mortgage" in labels
        assert "C-3 Amortisation Profile (Loan Repayment Schedule)" in labels
        assert "C-4 Insurance" in labels
        assert "C-5 Value Maintenance Clause" in labels
        assert "C-6 Corporate Guarantee & Guarantor Financial Capacity" in labels
        assert "C-7 Responsible Person Guarantee" in labels
        assert "C-8 Collateral Adequacy Conclusion" in labels
        assert len(missing) == 9

    def test_empty_markdown_unsecured_missing_c0_c7_c8_only(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(5, "", S5_UNSECURED_INPUT)
        labels = [l for _, l in missing]
        assert "C-0 Security Package Overview" in labels
        assert "C-7 Responsible Person Guarantee" in labels
        assert "C-8 Collateral Adequacy Conclusion" in labels
        # C-2 through C-5 skipped for unsecured; C-1 skipped (no RG); C-6 skipped (no guarantor)
        assert "C-1 Pre-Delivery Security — Refund Guarantee" not in labels
        assert "C-2 Post-Delivery Security — First Priority Mortgage" not in labels
        assert "C-3 Amortisation Profile (Loan Repayment Schedule)" not in labels
        assert "C-4 Insurance" not in labels
        assert "C-5 Value Maintenance Clause" not in labels
        assert "C-6 Corporate Guarantee & Guarantor Financial Capacity" not in labels
        assert len(missing) == 3

    def test_partial_missing_c7_c8_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(5, PARTIAL_S5_MISSING_C7_C8, S5_SECURED_INPUT)
        labels = [l for _, l in missing]
        assert "C-7 Responsible Person Guarantee" in labels
        assert "C-8 Collateral Adequacy Conclusion" in labels
        assert len(missing) == 2

    def test_c7_always_required_even_without_guarantor(self):
        from credit_report.generation.completeness import check_section_completeness
        md = PARTIAL_S5_MISSING_C7_C8
        input_no_guarantor = {**S5_SECURED_INPUT, "5F_corporate_guarantee": {"applicable": False}}
        missing = check_section_completeness(5, md, input_no_guarantor)
        labels = [l for _, l in missing]
        assert "C-7 Responsible Person Guarantee" in labels
        assert "C-6 Corporate Guarantee & Guarantor Financial Capacity" not in labels

    def test_c8_always_required(self):
        from credit_report.generation.completeness import check_section_completeness
        # Markdown with C-0 through C-7 but no C-8
        md = FULL_S5_SECURED.replace("**C-8. Collateral Adequacy Conclusion**\n", "")
        md = md[:md.rfind("The collateral package")]
        missing = check_section_completeness(5, md, S5_SECURED_INPUT)
        labels = [l for _, l in missing]
        assert "C-8 Collateral Adequacy Conclusion" in labels

    def test_c1_skipped_when_no_rg_data(self):
        from credit_report.generation.completeness import check_section_completeness
        input_no_rg = {**S5_SECURED_INPUT, "5B_refund_guarantee": {"applicable": False}}
        missing = check_section_completeness(5, "", input_no_rg)
        labels = [l for _, l in missing]
        assert "C-1 Pre-Delivery Security — Refund Guarantee" not in labels

    def test_c3_skipped_when_no_amortisation_data(self):
        from credit_report.generation.completeness import check_section_completeness
        input_no_amort = {
            **S5_SECURED_INPUT,
            "5C_vessel_mortgage": {"applicable": True, "vessel_valuations": [{"vessel": "Test"}]},
        }
        missing = check_section_completeness(5, "", input_no_amort)
        labels = [l for _, l in missing]
        assert "C-3 Amortisation Profile (Loan Repayment Schedule)" not in labels

    def test_c4_skipped_when_no_insurance_data(self):
        from credit_report.generation.completeness import check_section_completeness
        input_no_ins = {**S5_SECURED_INPUT, "5D_insurance": []}
        missing = check_section_completeness(5, "", input_no_ins)
        labels = [l for _, l in missing]
        assert "C-4 Insurance" not in labels

    def test_c5_skipped_when_no_vmc_data(self):
        from credit_report.generation.completeness import check_section_completeness
        input_no_vmc = {**S5_SECURED_INPUT, "5E_value_maintenance_clause": {}}
        missing = check_section_completeness(5, "", input_no_vmc)
        labels = [l for _, l in missing]
        assert "C-5 Value Maintenance Clause" not in labels

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(5, FULL_S5_SECURED.lower(), S5_SECURED_INPUT)
        assert missing == [], "Detection must be case-insensitive"

    def test_fallback_marker_refund_guarantee_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        # C-1 can also be detected by "refund guarantee" prose
        md = "Refund Guarantee issued by KDB.\n**C-0. Security Package Overview**\nSecured.\n"
        missing = check_section_completeness(5, md, S5_SECURED_INPUT)
        labels = [l for _, l in missing]
        assert "C-1 Pre-Delivery Security — Refund Guarantee" not in labels

    def test_fallback_marker_value_maintenance_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        # C-5 can also be detected by "value maintenance" text
        md = "value maintenance clause\n**C-0. Security Package Overview**\nSecured.\n"
        missing = check_section_completeness(5, md, S5_SECURED_INPUT)
        labels = [l for _, l in missing]
        assert "C-5 Value Maintenance Clause" not in labels

    def test_no_input_json_checks_only_unconditional(self):
        from credit_report.generation.completeness import check_section_completeness
        # Without input_json, only unconditional items (C-0, C-7, C-8) should be checked
        missing = check_section_completeness(5, "")
        labels = [l for _, l in missing]
        assert "C-0 Security Package Overview" in labels
        assert "C-7 Responsible Person Guarantee" in labels
        assert "C-8 Collateral Adequacy Conclusion" in labels
        # Conditional items absent since no input_json
        assert "C-1 Pre-Delivery Security — Refund Guarantee" not in labels
        assert "C-6 Corporate Guarantee & Guarantor Financial Capacity" not in labels


# ── B. §5 isolation ───────────────────────────────────────────────────────────

class TestSection5Isolation:

    def test_s5_check_does_not_affect_s2(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(2, FULL_S5_SECURED)
        labels = [l for _, l in result]
        assert "T1 Credit Overview" in labels

    def test_s5_check_does_not_affect_s4(self):
        from credit_report.generation.completeness import check_section_completeness
        # §4 checks for C-1..C-9 + Banking Relationships.
        # FULL_S5_SECURED contains **C-1. through **C-8. (§5 sub-headers) so those
        # match §4's prefix markers, but C-9 and Banking Relationships are absent.
        result = check_section_completeness(4, FULL_S5_SECURED)
        labels = [l for _, l in result]
        assert "C-9 Peer Comparison" in labels
        assert "Banking Relationships Table (Section E)" in labels

    def test_sections_6_to_10_unaffected(self):
        from credit_report.generation.completeness import check_section_completeness
        for sec in [6, 7, 8, 9, 10]:
            result = check_section_completeness(sec, FULL_S5_SECURED)
            assert result == [], f"§{sec} should have no completeness requirements"


# ── C. Pipeline integration ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_with_s5_secured_input(db):
    """DB fixture pre-seeded with a report and full-secured §5 SectionInput."""
    from credit_report.models import Report, SectionInput
    rid = _uid()
    db.add(Report(id=rid, industry="marine", created_by=_uid()))
    db.add(SectionInput(
        report_id=rid,
        section_no=5,
        input_json=json.dumps(S5_SECURED_INPUT),
        saved_by=_uid(),
    ))
    await db.flush()
    return db, rid


@pytest_asyncio.fixture
async def db_with_s5_unsecured_input(db):
    """DB fixture pre-seeded with an unsecured §5 SectionInput."""
    from credit_report.models import Report, SectionInput
    rid = _uid()
    db.add(Report(id=rid, industry="marine", created_by=_uid()))
    db.add(SectionInput(
        report_id=rid,
        section_no=5,
        input_json=json.dumps(S5_UNSECURED_INPUT),
        saved_by=_uid(),
    ))
    await db.flush()
    return db, rid


@pytest.mark.asyncio
class TestSection5PipelineIntegration:

    async def test_missing_c7_c8_triggers_fill(self, db_with_s5_secured_input):
        """When §5 is missing C-7 and C-8, fill must be called."""
        db, rid = db_with_s5_secured_input
        from credit_report.generation.pipeline import run_section_generation

        fill_output = (
            "**C-7. Responsible Person Guarantee**\n"
            "No responsible person guarantee is required for this facility.\n\n"
            "**C-8. Collateral Adequacy Conclusion**\n"
            "The collateral package is assessed as adequate. LTC of 80.0% is within policy limits. "
            "ACR at delivery of 130.9% exceeds the 125.0% floor. Overall, collateral is satisfactory.\n"
        )

        with _mock_generate(PARTIAL_S5_MISSING_C7_C8, tokens=9000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=600):
            output = await run_section_generation(db, rid, section_no=5, actor_user_id=_uid())

        assert output.status == "done"
        assert "**C-7." in output.markdown
        assert "**C-8." in output.markdown
        assert PARTIAL_S5_MISSING_C7_C8.strip() in output.markdown

    async def test_complete_secured_s5_does_not_trigger_fill(self, db_with_s5_secured_input):
        """When §5 is already complete (secured), fill must NOT be called."""
        db, rid = db_with_s5_secured_input
        from credit_report.generation.pipeline import run_section_generation

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S5_SECURED, tokens=10000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=5, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_complete_unsecured_s5_does_not_trigger_fill(self, db_with_s5_unsecured_input):
        """Unsecured facility with C-0 + C-7 + C-8 must not trigger fill."""
        db, rid = db_with_s5_unsecured_input
        from credit_report.generation.pipeline import run_section_generation

        fill_spy = AsyncMock(return_value=("", 0))
        with _mock_generate(FULL_S5_UNSECURED, tokens=3000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=5, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()

    async def test_fill_failure_does_not_crash_generation(self, db_with_s5_secured_input):
        """If fill raises, generation must still complete with status='done'."""
        db, rid = db_with_s5_secured_input
        from credit_report.generation.pipeline import run_section_generation

        with _mock_generate(PARTIAL_S5_MISSING_C7_C8, tokens=9000), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables",
                   new=AsyncMock(side_effect=RuntimeError("LLM timeout"))):
            output = await run_section_generation(db, rid, section_no=5, actor_user_id=_uid())

        assert output.status == "done"
        assert "**C-0." in output.markdown  # partial output preserved

    async def test_tokens_accumulated_from_fill(self, db_with_s5_secured_input):
        """Token count must include both primary generation and fill call tokens."""
        db, rid = db_with_s5_secured_input
        from credit_report.generation.pipeline import run_section_generation

        primary_tokens = 9000
        fill_tokens = 1200
        fill_output = (
            "**C-7. Responsible Person Guarantee**\n"
            "No responsible person guarantee is required for this facility.\n\n"
            "**C-8. Collateral Adequacy Conclusion**\n"
            "The collateral package is satisfactory.\n"
        )

        with _mock_generate(PARTIAL_S5_MISSING_C7_C8, tokens=primary_tokens), \
             _mock_evidence(), _mock_quota(), _mock_record(), \
             _mock_fill(fill_output, tokens=fill_tokens):
            output = await run_section_generation(db, rid, section_no=5, actor_user_id=_uid())

        assert output.tokens_used == primary_tokens + fill_tokens

    async def test_non_s5_section_not_affected(self, db):
        """Completeness check must be a no-op for §6 (no requirements)."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        fill_spy = AsyncMock(return_value=("", 0))
        short_md = "§6 Project Analysis\n\nConstruction on schedule."
        with _mock_generate(short_md), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.generation.completeness.fill_missing_tables", new=fill_spy):
            output = await run_section_generation(db, rid, section_no=6, actor_user_id=_uid())

        assert output.status == "done"
        fill_spy.assert_not_called()


# ── D. Fill prompt content ─────────────────────────────────────────────────────

class TestSection5FillPrompts:

    def test_system_prompt_not_empty(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(5)
        assert prompt, "§5 system prompt must not be empty"

    def test_system_prompt_mentions_banking_days(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(5)
        assert "Banking Days" in prompt, "System prompt must enforce 'Banking Days' (capitalised)"

    def test_system_prompt_mentions_rg_8_columns(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(5)
        assert "8 columns" in prompt or "8-column" in prompt or "Milestone" in prompt

    def test_system_prompt_mentions_c8_conclusion(self):
        from credit_report.generation.completeness import _build_fill_system_prompt
        prompt = _build_fill_system_prompt(5)
        assert "C-8" in prompt or "adequacy" in prompt.lower()

    def test_user_prompt_contains_missing_labels(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [
            ("**C-7.", "C-7 Responsible Person Guarantee"),
            ("**C-8.", "C-8 Collateral Adequacy Conclusion"),
        ]
        prompt = _build_fill_user_prompt(5, missing, PARTIAL_S5_MISSING_C7_C8, S5_SECURED_INPUT, "en")
        assert "C-7 Responsible Person Guarantee" in prompt
        assert "C-8 Collateral Adequacy Conclusion" in prompt

    def test_user_prompt_includes_input_json(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**C-8.", "C-8 Collateral Adequacy Conclusion")]
        prompt = _build_fill_user_prompt(5, missing, PARTIAL_S5_MISSING_C7_C8, S5_SECURED_INPUT, "en")
        assert "5A_security_overview" in prompt or "5B_refund_guarantee" in prompt or "5C_vessel_mortgage" in prompt

    def test_user_prompt_mentions_banking_days_rule(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**C-5.", "C-5 Value Maintenance Clause")]
        prompt = _build_fill_user_prompt(5, missing, PARTIAL_S5_MISSING_C7_C8, S5_SECURED_INPUT, "en")
        assert "Banking Days" in prompt

    def test_user_prompt_includes_existing_tail(self):
        from credit_report.generation.completeness import _build_fill_user_prompt
        missing = [("**C-7.", "C-7 Responsible Person Guarantee")]
        prompt = _build_fill_user_prompt(5, missing, PARTIAL_S5_MISSING_C7_C8, S5_SECURED_INPUT, "en")
        assert "do NOT repeat" in prompt or "context only" in prompt


# ── E. Config — §5 primary token budget ───────────────────────────────────────

class TestSection5Config:

    def test_s5_primary_token_budget_is_12288(self):
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        budget = SECTION_MAX_OUTPUT_TOKENS.get(5, SECTION_MAX_OUTPUT_TOKENS["default"])
        assert budget >= 12288, (
            f"§5 primary token budget must be ≥12 288 to accommodate RG 8-column table, "
            f"up-to-24-row amortisation schedule, dual-currency guarantor table, and VMC. Got {budget}."
        )

    @pytest.mark.asyncio
    async def test_s5_fill_budget_is_10240(self):
        """§5 fill budget must be 10 240 for the large amortisation schedule."""
        from credit_report.generation.completeness import fill_missing_tables
        missing = [("**C-7.", "C-7 Responsible Person Guarantee")]
        with patch(
            "credit_report.generation.claude_client.call_gemini_raw",
            new=AsyncMock(return_value="No responsible person guarantee is required."),
        ) as mock_call:
            await fill_missing_tables(
                section_no=5,
                existing_markdown=PARTIAL_S5_MISSING_C7_C8,
                missing=missing,
                input_json=S5_SECURED_INPUT,
            )
            _, kwargs = mock_call.call_args
            max_tok = kwargs.get("max_tokens")
            assert max_tok == 10240, f"§5 fill budget should be 10240, got {max_tok}"

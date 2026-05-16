"""
§9 Completeness check tests — Credit Analysis Checklist & Recommendation.

§9 is ALWAYS required (no conditionality). Four components must be present:

  C-1  23-item, 5-column checklist table + 2 mandatory footnotes:
         Footnote 15 — Banking Act s.33-3 + exemption basis
         Footnote 16 — ACRA charge search + UEN + CUB charge cross-reference
  C-2a Conditions Precedent table (No. | Description | Testing)
  C-2b Ongoing Covenants table (Description | Threshold/Requirement | Testing)
  C-3  RECOMMENDATION block (7 exact bold-label fields)
  C-4  Sign-Off block (Prepared by / Reviewed by / Date)

Truncation risk is highest at C-3 Recommendation + C-4 Sign-Off (end of a
dense, information-packed section) and at checklist items 15–23 (if the first
14 items + table header exhaust the token budget).

Primary token budget: 12 288 (raised per config).
Fill budget: 10 240 tokens.

Coverage:
A. Detection — full output, partial, missing individual components, empty
B. Checklist completeness — item 23 presence, footnote markers
C. Recommendation sub-field verification — Balloon LTV, Risk Level
D. Pipeline integration — fill triggered, failure isolated, tokens accumulated
E. Fill prompt content — 23-item table rules, footnote format, recommendation
F. Config — fill budget == 10240, §10 still has no requirements
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


def _mock_generate(md, tokens=4000):
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

_CHECKLIST_TABLE = (
    "| # | Category | Checklist Item | Response | Remarks |\n"
    "|---|---|---|---|---|\n"
    "| 1 | KYC & Compliance | CDD completed | **Yes** | Tier 2 |\n"
    "| 2 | Sanctions & AML | OFAC/MAS clear | **Yes** | Screened 15 Jan 2025 |\n"
    "| 3 | KYC & Compliance | PEP check | **Yes** | No PEP identified |\n"
    "| 4 | Credit Risk | MSR rating generated | **Yes** | MSR3 |\n"
    "| 5 | Credit Risk | MSR vs external rating | **Yes** | Aligned |\n"
    "| 6 | Credit Risk | Country risk | **Yes** | SG — Low |\n"
    "| 7 | Credit Risk | Industry risk | **Yes** | Marine — Moderate |\n"
    "| 8 | Financial | Audited financials reviewed | **Yes** | FY2022-2024 |\n"
    "| 9 | Financial | Base case DSCR | **Yes** | 1.32x (threshold: 1.10x) |\n"
    "| 10 | Financial | Worse case DSCR | **Yes** | 1.05x |\n"
    "| 11 | Collateral | Vessel valuation | **Yes** | BV, 10 Jan 2025 |\n"
    "| 12 | Collateral | ACR at delivery | **Yes** | 80.5% (floor: 80%) |\n"
    "| 13 | Collateral | VMC included | **Yes** | 30 Banking Days cure |\n"
    "| 14 | Collateral | Insurance | **Yes** | CUB named loss payee |\n"
    "| 15 | Legal & Documentation | Banking Act s.33-3 | **Yes** | Pre-delivery USD 50m |\n"
    "| 16 | Legal & Documentation | ACRA charges | **Yes** | 3 charges; 1 CUB |\n"
    "| 17 | Legal & Documentation | Legal opinions | **Yes** | SG and Marshall Islands |\n"
    "| 18 | Legal & Documentation | Security documents | **Yes** | Before drawdown |\n"
    "| 19 | ESG & Environmental | ESG rating reviewed | **Yes** | MSCI: BBB, score 5.2 |\n"
    "| 20 | ESG & Environmental | Poseidon Principles | **Yes** | CII rating: B |\n"
    "| 21 | ESG & Environmental | EU ETS | **Yes** | Scope 1; in compliance |\n"
    "| 22 | ESG & Environmental | IMO GHG | **Yes** | Rating C at delivery |\n"
    "| 23 | Regulatory (MAS) | MAS 612 classification | **Yes** | PASS |\n\n"
)

_FOOTNOTE_15 = (
    "\\* Item 15: Pre-delivery unsecured drawdown of USD 50.0m is within the "
    "Banking Act s.33-3 single-borrower unsecured limit. "
    "Exemption basis: item (d). CUB internal approval reference: CA-2025-001.\n\n"
)

_FOOTNOTE_16 = (
    "\\* Item 16: ACRA charge search conducted on 15 Jan 2025 for Example Shipping Pte Ltd "
    "(UEN: 201900001Z). CUB charge(s): Item 1, §1 (USD 50.0m, M/V Example Star).\n\n"
)

_CONDITIONS_PRECEDENT = (
    "**Conditions Precedent**\n\n"
    "| No. | Description | Testing |\n"
    "|---|---|---|\n"
    "| 1 | Execution of facility agreement | Before first drawdown |\n"
    "| 2 | Vessel mortgage registration | Before vessel delivery |\n\n"
)

_ONGOING_COVENANTS = (
    "**Ongoing Covenants**\n\n"
    "| Description | Threshold/Requirement | Testing |\n"
    "|---|---|---|\n"
    "| ACR | ≥80% | Semi-annual |\n"
    "| DSCR | ≥1.10x | Annual |\n\n"
    "**Financial Covenants: NIL**\n\n"
)

_RECOMMENDATION = (
    "**RECOMMENDATION:**\n\n"
    "**Decision:** APPROVE\n"
    "**Facility Amount:** USD 150.0m\n"
    "**Tenor:** 7 years from first drawdown\n"
    "**Security Structure:** First priority mortgage on M/V Example Star + VMC.\n"
    "**Key Conditions:**\n"
    "1. Drawdown before 31 Dec 2025.\n"
    "2. DSCR ≥1.10x maintained.\n"
    "**Balloon LTV:** 65.0% (cap: 70.0%) — Compliant\n"
    "**Risk Level vs. Prior Review:** No change — financial profile stable.\n\n"
)

_SIGN_OFF = (
    "Prepared by: [Prepared by], Credit Analyst, Credit Management Department, CUB SG Branch\n"
    "Reviewed by: [Reviewed by], Senior Credit Officer, Credit Management Department, CUB SG Branch\n"
    "Date: 15 Jan 2025\n"
)

FULL_S9 = (
    _CHECKLIST_TABLE
    + _FOOTNOTE_15
    + _FOOTNOTE_16
    + _CONDITIONS_PRECEDENT
    + _ONGOING_COVENANTS
    + _RECOMMENDATION
    + _SIGN_OFF
)


# ── A. Detection ──────────────────────────────────────────────────────────────

class TestSection9Detection:

    def test_full_s9_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(9, FULL_S9)
        assert missing == [], f"Expected no missing, got: {missing}"

    def test_missing_checklist_table_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Checklist Table" in l or "C-1 23-Item" in l for l in labels), labels

    def test_incomplete_checklist_item_23_missing(self):
        """Checklist present but item 23 absent (truncated at item 22)."""
        from credit_report.generation.completeness import check_section_completeness
        # Build checklist with only items 1-22
        partial_checklist = "\n".join(_CHECKLIST_TABLE.splitlines()[:-2]) + "\n\n"  # drop row 23
        md = partial_checklist + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Item 23" in l or "truncated" in l.lower() for l in labels), labels

    def test_missing_footnote_15_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _CHECKLIST_TABLE + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Footnote" in l and "15" in l or "Item 15" in l for l in labels), labels

    def test_missing_footnote_16_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Footnote" in l and "16" in l or "Item 16" in l for l in labels), labels

    def test_missing_conditions_precedent_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Conditions Precedent" in l for l in labels), labels

    def test_missing_ongoing_covenants_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _RECOMMENDATION + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Ongoing Covenants" in l for l in labels), labels

    def test_missing_recommendation_block_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Recommendation" in l or "RECOMMENDATION" in l for l in labels), labels

    def test_missing_sign_off_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Sign-Off" in l or "Prepared by" in l for l in labels), labels

    def test_empty_markdown_flags_all_components(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(9, "")
        assert len(missing) >= 5, f"Expected ≥5 missing from empty markdown, got: {missing}"

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(9, FULL_S9.lower())
        assert missing == [], "Detection should be case-insensitive"


# ── B. Checklist completeness and footnote specificity ────────────────────────

class TestSection9ChecklistAndFootnotes:

    def test_item_23_row_confirms_completeness(self):
        """'| 23 |' in markdown confirms all 23 items are present."""
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(9, FULL_S9)
        labels = [label for _, label in missing]
        assert not any("Item 23" in l for l in labels), labels

    def test_exemption_basis_alternative_for_footnote_15(self):
        """'exemption basis' in text satisfies footnote 15 check."""
        from credit_report.generation.completeness import check_section_completeness
        md_without_explicit_item15 = (
            _CHECKLIST_TABLE
            + "Exemption basis: item (d). Pre-delivery unsecured drawdown noted.\n\n"
            + _FOOTNOTE_16
            + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        )
        missing = check_section_completeness(9, md_without_explicit_item15)
        labels = [label for _, label in missing]
        assert not any("Item 15" in l or "Footnote" in l and "15" in l for l in labels), labels

    def test_pre_delivery_unsecured_drawdown_satisfies_footnote_15(self):
        from credit_report.generation.completeness import check_section_completeness
        md = (
            _CHECKLIST_TABLE
            + "Pre-delivery unsecured drawdown of USD 50m is within the Banking Act s.33-3 limit.\n\n"
            + _FOOTNOTE_16
            + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        )
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert not any("Item 15" in l for l in labels), labels

    def test_acra_charge_search_conducted_satisfies_footnote_16(self):
        from credit_report.generation.completeness import check_section_completeness
        md = (
            _CHECKLIST_TABLE
            + _FOOTNOTE_15
            + "ACRA charge search conducted on 15 Jan 2025 for Example Co (UEN: 12345). "
            "CUB charge(s): Item 1 (§1).\n\n"
            + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        )
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert not any("Item 16" in l for l in labels), labels

    def test_item_16_uen_cub_charge_combo_satisfies_footnote_16(self):
        """'item 16' + 'uen' + 'cub charge' pattern satisfies footnote 16."""
        from credit_report.generation.completeness import check_section_completeness
        md = (
            _CHECKLIST_TABLE
            + _FOOTNOTE_15
            + "* Item 16: Search completed. UEN: 201900001Z. CUB charge confirmed at Item 1, §1.\n\n"
            + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _RECOMMENDATION + _SIGN_OFF
        )
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert not any("Item 16" in l for l in labels), labels

    def test_covenants_threshold_column_alternative_marker(self):
        """'threshold' in markdown satisfies the Ongoing Covenants check."""
        from credit_report.generation.completeness import check_section_completeness
        md = (
            _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT
            + "| Covenant | Threshold | Testing |\n|---|---|---|\n| ACR | ≥80% | Annual |\n\n"
            + _RECOMMENDATION + _SIGN_OFF
        )
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert not any("Ongoing Covenants" in l for l in labels), labels


# ── C. Recommendation sub-field verification ──────────────────────────────────

class TestSection9RecommendationSubFields:

    def test_recommendation_present_but_balloon_ltv_missing_flagged(self):
        """RECOMMENDATION block exists but **Balloon LTV** is absent → flagged."""
        from credit_report.generation.completeness import check_section_completeness
        rec_without_balloon = (
            "**RECOMMENDATION:**\n\n"
            "**Decision:** APPROVE\n"
            "**Facility Amount:** USD 150.0m\n"
            "**Tenor:** 7 years\n"
            "**Security Structure:** First priority mortgage.\n"
            "**Key Conditions:**\n1. Drawdown condition.\n\n"
            # Missing: Balloon LTV and Risk Level vs. Prior Review
        )
        md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + rec_without_balloon + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Balloon LTV" in l or "Risk Level" in l for l in labels), labels

    def test_recommendation_present_but_risk_level_missing_flagged(self):
        from credit_report.generation.completeness import check_section_completeness
        rec_without_risk = (
            "**RECOMMENDATION:**\n\n"
            "**Decision:** APPROVE\n"
            "**Facility Amount:** USD 150.0m\n"
            "**Tenor:** 7 years\n"
            "**Security Structure:** First priority mortgage.\n"
            "**Key Conditions:**\n1. Drawdown condition.\n"
            "**Balloon LTV:** 65.0% (cap: 70.0%) — Compliant\n"
            # Missing: Risk Level vs. Prior Review
        )
        md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + rec_without_risk + _SIGN_OFF
        missing = check_section_completeness(9, md)
        labels = [label for _, label in missing]
        assert any("Risk Level" in l or "Prior Review" in l for l in labels), labels

    def test_recommendation_all_7_fields_no_flag(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(9, FULL_S9)
        labels = [label for _, label in missing]
        assert not any("Balloon LTV" in l or "Risk Level" in l for l in labels), labels

    def test_decision_alternative_marker_for_recommendation(self):
        """**Decision:** in markdown satisfies the RECOMMENDATION block check."""
        from credit_report.generation.completeness import check_section_completeness
        md_with_decision_only = (
            _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16
            + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS
            + "**Decision:** APPROVE WITH CONDITIONS\n"
            "**Balloon LTV:** 68% (cap: 70%) — Compliant\n"
            "**Risk Level vs. Prior Review:** Improved — DSCR strengthened.\n"
            + _SIGN_OFF
        )
        missing = check_section_completeness(9, md_with_decision_only)
        labels = [label for _, label in missing]
        assert not any("RECOMMENDATION" in l or "Recommendation Block" in l for l in labels), labels


# ── D. Cross-section isolation ────────────────────────────────────────────────

class TestSection9Isolation:

    def test_s9_check_does_not_affect_s2(self):
        from credit_report.generation.completeness import check_section_completeness
        s2_md = (
            "| **Credit Overview** | Details |\n"
            "| **Solvency** | Details |\n"
            "| **The Guarantor and their Supportive** | Details |\n"
            "| **Collateral Summary** | Details |\n"
            "| **Risk and Mitigants** | Details |\n"
        )
        missing = check_section_completeness(2, s2_md)
        assert missing == [], f"§2 check should not be affected by §9 detection: {missing}"

    def test_section_10_still_has_no_requirements(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(10, FULL_S9)
        assert result == [], "§10 should have no completeness requirements"

    def test_s9_content_does_not_match_s7_markers(self):
        """§9 content should not accidentally satisfy §7 C-1/C-2 sub-section markers."""
        from credit_report.generation.completeness import check_section_completeness
        # §9 content should still flag §7 as incomplete (no financials)
        missing_s7 = check_section_completeness(7, FULL_S9, {})
        labels = [label for _, label in missing_s7]
        assert any("Borrower Historical Financials" in l for l in labels)
        assert any("Borrower Summary Statistics" in l for l in labels)


# ── E. Pipeline integration ───────────────────────────────────────────────────

class TestSection9PipelineIntegration:

    async def test_missing_recommendation_triggers_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=9,
            input_json=__import__("json").dumps({}),
        ))
        await db.flush()

        incomplete_md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS + _SIGN_OFF
        fill_md = _RECOMMENDATION

        with _mock_generate(incomplete_md), _mock_evidence(), _mock_quota(), _mock_record():
            with _mock_fill(fill_md, tokens=600):
                output = await run_section_generation(
                    db, rid, section_no=9, actor_user_id=_uid()
                )

        assert output.status == "done"
        assert "**RECOMMENDATION:**" in output.markdown or "**Decision:**" in output.markdown

    async def test_complete_s9_does_not_trigger_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=9,
            input_json=__import__("json").dumps({}),
        ))
        await db.flush()

        with _mock_generate(FULL_S9), _mock_evidence(), _mock_quota(), _mock_record():
            mock_fill = AsyncMock(return_value=("", 0))
            with patch("credit_report.generation.completeness.fill_missing_tables", mock_fill):
                output = await run_section_generation(
                    db, rid, section_no=9, actor_user_id=_uid()
                )

        mock_fill.assert_not_called()
        assert output.status == "done"

    async def test_fill_failure_does_not_crash_s9(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=9,
            input_json=__import__("json").dumps({}),
        ))
        await db.flush()

        # Missing recommendation + sign-off
        incomplete_md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS

        with _mock_generate(incomplete_md), _mock_evidence(), _mock_quota(), _mock_record():
            with patch(
                "credit_report.generation.completeness.fill_missing_tables",
                new=AsyncMock(side_effect=RuntimeError("LLM timeout")),
            ):
                output = await run_section_generation(
                    db, rid, section_no=9, actor_user_id=_uid()
                )

        assert output.status == "done"
        assert output.markdown

    async def test_tokens_accumulated_from_s9_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=9,
            input_json=__import__("json").dumps({}),
        ))
        await db.flush()

        incomplete_md = _CHECKLIST_TABLE + _FOOTNOTE_15 + _FOOTNOTE_16 + _CONDITIONS_PRECEDENT + _ONGOING_COVENANTS
        fill_tokens = 900

        with _mock_generate(incomplete_md, tokens=3000), _mock_evidence(), _mock_quota(), _mock_record():
            with _mock_fill(_RECOMMENDATION + _SIGN_OFF, tokens=fill_tokens):
                output = await run_section_generation(
                    db, rid, section_no=9, actor_user_id=_uid()
                )

        assert output.tokens_used >= 3000 + fill_tokens


# ── F. Fill prompt content ────────────────────────────────────────────────────

class TestSection9FillPrompts:

    def _system(self) -> str:
        from credit_report.generation.completeness import _build_fill_system_prompt
        return _build_fill_system_prompt(9)

    def _user(self, missing=None, existing="...", inp=None) -> str:
        from credit_report.generation.completeness import _build_fill_user_prompt
        if missing is None:
            missing = [("**RECOMMENDATION:**", "C-3 Recommendation Block")]
        return _build_fill_user_prompt(9, missing, existing, inp or {}, "en")

    def test_system_prompt_mentions_23_items(self):
        assert "23" in self._system()

    def test_system_prompt_mentions_5_columns(self):
        assert "5 column" in self._system().lower() or "5-column" in self._system().lower()

    def test_system_prompt_mentions_bold_yes_no_na(self):
        prompt = self._system()
        assert "**Yes**" in prompt and "**No" in prompt and "**N/A**" in prompt

    def test_system_prompt_prohibits_checkmark_symbols(self):
        prompt = self._system()
        assert "✓" in prompt or "✗" in prompt or "symbol" in prompt.lower()

    def test_system_prompt_mentions_footnote_15_format(self):
        prompt = self._system()
        assert "s.33-3" in prompt or "33-3" in prompt
        assert "exemption basis" in prompt.lower()

    def test_system_prompt_mentions_footnote_16_format(self):
        prompt = self._system()
        assert "ACRA charge search conducted" in prompt or "acra charge search" in prompt.lower()
        assert "UEN" in prompt

    def test_system_prompt_mentions_conditions_precedent(self):
        assert "Conditions Precedent" in self._system()

    def test_system_prompt_mentions_ongoing_covenants(self):
        assert "Ongoing Covenants" in self._system() or "ongoing covenants" in self._system().lower()

    def test_system_prompt_mentions_testing_column_header(self):
        prompt = self._system()
        assert "'Testing'" in prompt or '"Testing"' in prompt

    def test_system_prompt_mentions_recommendation_format(self):
        prompt = self._system()
        assert "**RECOMMENDATION:**" in prompt or "RECOMMENDATION" in prompt
        assert "**Decision:**" in prompt

    def test_system_prompt_mentions_balloon_ltv(self):
        assert "Balloon LTV" in self._system()

    def test_system_prompt_mentions_risk_level_vs_prior_review(self):
        assert "Prior Review" in self._system() or "prior review" in self._system().lower()

    def test_system_prompt_prohibits_approval_authority(self):
        prompt = self._system()
        assert "Approval Authority" in prompt or "approval authority" in prompt.lower()

    def test_system_prompt_mentions_sign_off_format(self):
        prompt = self._system()
        assert "Prepared by:" in prompt or "prepared by" in prompt.lower()

    def test_system_prompt_mentions_banking_days(self):
        assert "Banking Days" in self._system()

    def test_system_prompt_prohibits_credit_judgments(self):
        prompt = self._system()
        assert "satisfactory" in prompt.lower() or "credit judgment" in prompt.lower()

    def test_user_prompt_contains_missing_labels(self):
        prompt = self._user(missing=[("**RECOMMENDATION:**", "C-3 Recommendation Block (7 fields)")])
        assert "C-3 Recommendation Block" in prompt

    def test_user_prompt_mentions_footnote_rules(self):
        prompt = self._user()
        assert "s.33-3" in prompt or "Footnote" in prompt or "Item 15" in prompt

    def test_user_prompt_mentions_recommendation_fields(self):
        prompt = self._user()
        assert "RECOMMENDATION" in prompt or "Balloon LTV" in prompt

    def test_user_prompt_includes_input_json(self):
        prompt = self._user(inp={"9C_checklist": {"item_1": "Yes"}})
        assert "9C_checklist" in prompt or "item_1" in prompt

    def test_user_prompt_includes_language_field(self):
        assert "en" in self._user() or "LANGUAGE" in self._user()


# ── G. Config ─────────────────────────────────────────────────────────────────

class TestSection9Config:

    def test_s9_fill_budget_is_10240(self):
        import inspect
        from credit_report.generation import completeness
        src = inspect.getsource(completeness.fill_missing_tables)
        assert "(4, 5, 6, 9)" in src or "section_no == 9" in src, (
            "§9 should be in the 10240-token fill budget group"
        )
        assert "10240" in src

    def test_s9_primary_budget_is_12288(self):
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        budget = SECTION_MAX_OUTPUT_TOKENS.get(9, SECTION_MAX_OUTPUT_TOKENS.get("default"))
        assert budget == 12288, f"Expected §9 primary budget 12288, got {budget}"

    def test_section_10_has_no_requirements_after_s9_added(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(10, "some markdown")
        assert result == [], "§10 should still have no completeness requirements"

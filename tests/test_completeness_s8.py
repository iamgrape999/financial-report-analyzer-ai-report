"""
§8 Completeness check tests — Changes in Engaged Banks (ACRA Banking Charges).

§8 is DATA-DRIVEN:
- acra_data_available == false → AI emits "Not Available" only → check returns []
- acra_data_available == true  → Full C-1 content required:
  ① Search Metadata (ACRA date + entity + UEN opening sentence)
  ② Charges Table (8 cols: # | Chargee | Date Reg | Date Charge |
                   Amount (USD m) | Currency | Property Charged | Status)
  ③ Summary (4-line exact format: Total charges / Total active amount /
              CUB charges / Unique chargees)
  ④ CA Commentary (≥4 bullets: Volume+Trend, CUB Position, Satisfied Charges,
                   Red Flags + "No unusual patterns" if clean)
  ⑤ Forward-looking bullet (MANDATORY for new_deal/renewal when
     8A.has_proposed_facility or 8A.proposed_facility_amount_usd_m is set)

Primary token budget: 8 192 (default — §8 is compact).
Fill budget: 6 144 tokens.

Coverage:
A. Detection — full output, partial, not-applicable, empty markdown
B. Conditional boundary — acra_data false, no input, forward-looking trigger
C. Pipeline integration — fill triggered, failure isolated, tokens accumulated
D. Fill prompt content — 8-col table rules, exact summary format, bullet rules
E. Config — fill budget == 6144
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


def _mock_generate(md, tokens=3000):
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


def _mock_fill(text, tokens=600):
    return patch(
        "credit_report.generation.completeness.fill_missing_tables",
        new=AsyncMock(return_value=(text, tokens)),
    )


# ── Shared markdown stubs ─────────────────────────────────────────────────────

_ACRA_METADATA = (
    "Based on ACRA search dated 15 Jan 2025, Example Shipping Pte Ltd (UEN: 201900001Z), "
    "a Singapore-incorporated company, has the following registered charges:\n\n"
)

_CHARGES_TABLE = (
    "| # | Chargee | Date of Registration | Date of Charge | Amount (USD m) | "
    "Currency | Property Charged | Status |\n"
    "|---|---------|---------------------|----------------|----------------|----------|-----------------|--------|\n"
    "| 1 | CUB Singapore Branch | 01 Mar 2022 | 28 Feb 2022 | 50.0 | USD | "
    "Vessel — M/V Example Star — **CUB facility (Item 1, §1)** | Registered |\n"
    "| 2 | Bank of Tokyo-Mitsubishi UFJ | 01 Jun 2022 | 30 May 2022 | 60.0 | USD | "
    "Vessel — M/V Example Moon | Registered |\n"
    "| 3 | Standard Chartered Bank | 15 Jan 2023 | 12 Jan 2023 | 40.0 | USD | "
    "Vessel — M/V Example Star | Satisfied (20 Dec 2024) |\n\n"
)

_SUMMARY = (
    "Total charges: 3 (2 active, 1 satisfied)\n"
    "Total active amount: USD 110.0m\n"
    "CUB charges: 1 totaling USD 50.0m\n"
    "Unique chargees: CUB Singapore Branch, Bank of Tokyo-Mitsubishi UFJ, "
    "Standard Chartered Bank (3 distinct banking groups)\n\n"
)

_COMMENTARY_4_BULLETS = (
    "- **Volume & trend:** Three charges registered between Mar 2022 and Jan 2023, "
    "consistent with fleet expansion.\n"
    "- **CUB position:** CUB Singapore Branch holds 1 charge (USD 50.0m) for Item 1 (§1), "
    "matching the proposed facility amount.\n"
    "- **Satisfied charges:** Standard Chartered Bank charge (USD 40.0m) satisfied Dec 2024, "
    "consistent with vessel refinancing.\n"
    "- **Red flags:** No unusual patterns identified. All chargees are international banking groups.\n"
)

FULL_S8 = _ACRA_METADATA + _CHARGES_TABLE + _SUMMARY + _COMMENTARY_4_BULLETS

NOT_AVAILABLE_S8 = "Not Available — Borrower is not incorporated in Singapore.\n"

_INPUT_ACRA_AVAILABLE = {
    "8A_acra_banking_charges": {
        "acra_data_available": True,
        "charges": [
            {"chargee": "CUB Singapore Branch", "amount_usd_m": 50.0},
        ],
    }
}

_INPUT_ACRA_NOT_AVAILABLE = {
    "8A_acra_banking_charges": {
        "acra_data_available": False,
    }
}

_INPUT_NO_8A = {}

_INPUT_NEW_DEAL_WITH_FACILITY = {
    "metadata": {"report_type": "new_deal"},
    "8A_acra_banking_charges": {
        "acra_data_available": True,
        "has_proposed_facility": True,
        "proposed_facility_amount_usd_m": 75.0,
    },
}


# ── A. Detection ──────────────────────────────────────────────────────────────

class TestSection8Detection:

    def test_full_s8_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, FULL_S8, _INPUT_ACRA_AVAILABLE)
        assert missing == [], f"Expected no missing, got: {missing}"

    def test_missing_search_metadata_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _CHARGES_TABLE + _SUMMARY + _COMMENTARY_4_BULLETS
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert any("Search Metadata" in l or "C-1a" in l for l in labels), labels

    def test_missing_charges_table_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _ACRA_METADATA + _SUMMARY + _COMMENTARY_4_BULLETS
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert any("Charges Table" in l or "C-1b" in l for l in labels), labels

    def test_missing_summary_detected(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _ACRA_METADATA + _CHARGES_TABLE + _COMMENTARY_4_BULLETS
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert any("Summary" in l or "Total charges" in l for l in labels), labels

    def test_missing_commentary_detected_when_zero_bullets(self):
        from credit_report.generation.completeness import check_section_completeness
        md = _ACRA_METADATA + _CHARGES_TABLE + _SUMMARY
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert any("Commentary" in l or "C-1d" in l for l in labels), labels

    def test_missing_commentary_detected_when_only_3_bullets(self):
        from credit_report.generation.completeness import check_section_completeness
        three_bullets = (
            "- Volume trend bullet.\n"
            "- CUB position bullet.\n"
            "- Satisfied charges bullet.\n"
        )
        md = _ACRA_METADATA + _CHARGES_TABLE + _SUMMARY + three_bullets
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert any("Commentary" in l or "≥4 bullets" in l for l in labels), labels

    def test_four_bullets_sufficient(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, FULL_S8, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert not any("Commentary" in l or "≥4 bullets" in l for l in labels), labels

    def test_not_available_s8_no_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, NOT_AVAILABLE_S8, _INPUT_ACRA_NOT_AVAILABLE)
        assert missing == [], "Not Available section should have no missing sub-sections"

    def test_empty_markdown_with_input_flags_all_missing(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, "", _INPUT_ACRA_AVAILABLE)
        assert len(missing) >= 3, f"Expected at least 3 missing items, got: {missing}"

    def test_case_insensitive_detection(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, FULL_S8.lower(), _INPUT_ACRA_AVAILABLE)
        assert missing == [], "Detection should be case-insensitive"

    def test_uen_alternative_marker_for_metadata(self):
        """UEN: pattern (without 'ACRA search') counts as search metadata."""
        from credit_report.generation.completeness import check_section_completeness
        md = "Example Co (UEN: 201900001Z) has registered charges:\n\n" + _CHARGES_TABLE + _SUMMARY + _COMMENTARY_4_BULLETS
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert not any("C-1a" in l or "Search Metadata" in l for l in labels), labels

    def test_property_charged_column_alternative_marker_for_table(self):
        """'Property Charged' + 'chargee' together count as a valid table marker."""
        from credit_report.generation.completeness import check_section_completeness
        md = _ACRA_METADATA + "| Chargee | Property Charged |\n|---|---|\n| ABC | Vessel |\n\n" + _SUMMARY + _COMMENTARY_4_BULLETS
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert not any("C-1b" in l or "Charges Table" in l for l in labels), labels

    def test_total_active_amount_alternative_marker_for_summary(self):
        """'Total active amount:' alone is sufficient for the summary check."""
        from credit_report.generation.completeness import check_section_completeness
        md = _ACRA_METADATA + _CHARGES_TABLE + "Total active amount: USD 110.0m\n\n" + _COMMENTARY_4_BULLETS
        missing = check_section_completeness(8, md, _INPUT_ACRA_AVAILABLE)
        labels = [label for _, label in missing]
        assert not any("Summary" in l or "Total charges" in l for l in labels), labels


# ── B. Conditional boundary ───────────────────────────────────────────────────

class TestSection8ConditionalBoundary:

    def test_no_input_returns_empty(self):
        """Without 8A input, applicability is unknown → skip checks."""
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, "", _INPUT_NO_8A)
        assert missing == [], "No input should return empty (skip checks)"

    def test_acra_not_available_false_bool_returns_empty(self):
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, "", _INPUT_ACRA_NOT_AVAILABLE)
        assert missing == [], "acra_data_available=False should skip checks"

    def test_acra_not_available_string_false_returns_empty(self):
        from credit_report.generation.completeness import check_section_completeness
        inp = {"8A_acra_banking_charges": {"acra_data_available": "false"}}
        missing = check_section_completeness(8, "", inp)
        assert missing == [], "acra_data_available='false' string should skip checks"

    def test_acra_not_available_string_no_returns_empty(self):
        from credit_report.generation.completeness import check_section_completeness
        inp = {"8A_acra_banking_charges": {"acra_data_available": "no"}}
        missing = check_section_completeness(8, "", inp)
        assert missing == [], "acra_data_available='no' string should skip checks"

    def test_forward_looking_not_checked_without_proposed_facility(self):
        """new_deal report without has_proposed_facility → forward-looking not required."""
        from credit_report.generation.completeness import check_section_completeness
        inp = {
            "metadata": {"report_type": "new_deal"},
            "8A_acra_banking_charges": {"acra_data_available": True},
        }
        missing = check_section_completeness(8, FULL_S8, inp)
        labels = [label for _, label in missing]
        assert not any("Forward-looking" in l or "upon execution" in l for l in labels), labels

    def test_forward_looking_checked_for_new_deal_with_proposed_facility(self):
        """new_deal + has_proposed_facility → forward-looking must be present."""
        from credit_report.generation.completeness import check_section_completeness
        missing = check_section_completeness(8, FULL_S8, _INPUT_NEW_DEAL_WITH_FACILITY)
        labels = [label for _, label in missing]
        assert any("Forward-looking" in l or "upon execution" in l for l in labels), (
            "Forward-looking should be flagged when new_deal + has_proposed_facility, "
            f"got: {labels}"
        )

    def test_forward_looking_satisfied_when_present(self):
        """'upon execution' in markdown satisfies the forward-looking check."""
        from credit_report.generation.completeness import check_section_completeness
        forward_bullet = (
            "- Upon execution of proposed facility (Item 2, §1, USD 75.0m for M/V New Vessel), "
            "an additional charge will be registered for CUB Singapore Branch, "
            "bringing CUB total to 2 charges / USD 125.0m.\n"
        )
        md = FULL_S8 + forward_bullet
        missing = check_section_completeness(8, md, _INPUT_NEW_DEAL_WITH_FACILITY)
        labels = [label for _, label in missing]
        assert not any("Forward-looking" in l or "upon execution" in l for l in labels), labels

    def test_forward_looking_checked_for_renewal_with_proposed_facility(self):
        """renewal + proposed_facility_amount_usd_m → forward-looking required."""
        from credit_report.generation.completeness import check_section_completeness
        inp = {
            "metadata": {"report_type": "renewal"},
            "8A_acra_banking_charges": {
                "acra_data_available": True,
                "proposed_facility_amount_usd_m": 50.0,
            },
        }
        missing = check_section_completeness(8, FULL_S8, inp)
        labels = [label for _, label in missing]
        assert any("Forward-looking" in l or "upon execution" in l for l in labels), labels

    def test_annual_review_no_forward_looking_check(self):
        """annual_review report type → forward-looking is not required."""
        from credit_report.generation.completeness import check_section_completeness
        inp = {
            "metadata": {"report_type": "annual_review"},
            "8A_acra_banking_charges": {
                "acra_data_available": True,
                "has_proposed_facility": True,
            },
        }
        missing = check_section_completeness(8, FULL_S8, inp)
        labels = [label for _, label in missing]
        assert not any("Forward-looking" in l for l in labels), labels


# ── C. Cross-section isolation ────────────────────────────────────────────────

class TestSection8Isolation:

    def test_s8_check_does_not_affect_s2(self):
        from credit_report.generation.completeness import check_section_completeness
        # §2 content has no ACRA markers; check_section_completeness(2) ignores §8 input
        s2_md = (
            "| **Credit Overview** | Details |\n"
            "| **Solvency** | Details |\n"
            "| **The Guarantor and their Supportive** | Details |\n"
            "| **Collateral Summary** | Details |\n"
            "| **Risk and Mitigants** | Details |\n"
        )
        missing = check_section_completeness(2, s2_md, _INPUT_ACRA_AVAILABLE)
        assert missing == [], f"§2 check should not be affected by §8 input: {missing}"

    def test_s8_check_does_not_affect_s4(self):
        from credit_report.generation.completeness import check_section_completeness
        s4_md = (
            "**C-1.** Corporate Identity\n**C-2.** Ownership\n**C-3.** Management\n"
            "**C-4.** Business Overview\n**C-5.** Financial Highlights\n**C-6.** Fleet Profile\n"
            "**C-7.** Debt Profile\n**C-8.** Market Analysis\n**C-9.** Peer Comparison\n"
            "Banking relationships table here\n"
        )
        missing = check_section_completeness(4, s4_md, _INPUT_ACRA_AVAILABLE)
        assert missing == [], f"§4 check should ignore §8 input: {missing}"

    def test_section_10_still_unaffected(self):
        from credit_report.generation.completeness import check_section_completeness
        result = check_section_completeness(10, "some markdown", _INPUT_ACRA_AVAILABLE)
        assert result == [], f"§10 should have no requirements (got: {result})"

    def test_s8_full_content_does_not_trigger_s7_checks(self):
        """§8 ACRA content should not accidentally match §7 sub-section markers."""
        from credit_report.generation.completeness import check_section_completeness
        # §7 checks for **C-1. and "Borrower Historical Financials" etc.
        # §8 content has none of those → §7 check should flag them all
        missing_s7 = check_section_completeness(7, FULL_S8, {})
        labels = [label for _, label in missing_s7]
        # C-1 and C-2 are unconditionally mandatory for §7
        assert any("C-1 Borrower Historical Financials" in l for l in labels)
        assert any("C-2 Borrower Summary Statistics" in l for l in labels)


# ── D. Pipeline integration ───────────────────────────────────────────────────

class TestSection8PipelineIntegration:

    async def test_missing_charges_table_triggers_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        input_data = {"8A_acra_banking_charges": {"acra_data_available": True, "charges": []}}
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=8,
            input_json=__import__("json").dumps(input_data),
        ))
        await db.flush()

        # AI produces metadata + summary + bullets but no charges table
        incomplete_md = _ACRA_METADATA + _SUMMARY + _COMMENTARY_4_BULLETS
        fill_md = _CHARGES_TABLE

        with _mock_generate(incomplete_md), _mock_evidence(), _mock_quota(), _mock_record():
            with _mock_fill(fill_md, tokens=500):
                output = await run_section_generation(
                    db, rid, section_no=8, actor_user_id=_uid()
                )

        assert output.status == "done"
        assert "| Chargee |" in output.markdown or "chargee" in output.markdown.lower()

    async def test_complete_s8_does_not_trigger_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=8,
            input_json=__import__("json").dumps(_INPUT_ACRA_AVAILABLE),
        ))
        await db.flush()

        with _mock_generate(FULL_S8), _mock_evidence(), _mock_quota(), _mock_record():
            mock_fill = AsyncMock(return_value=("", 0))
            with patch("credit_report.generation.completeness.fill_missing_tables", mock_fill):
                output = await run_section_generation(
                    db, rid, section_no=8, actor_user_id=_uid()
                )

        mock_fill.assert_not_called()
        assert output.status == "done"

    async def test_fill_failure_does_not_crash_s8(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=8,
            input_json=__import__("json").dumps(_INPUT_ACRA_AVAILABLE),
        ))
        await db.flush()

        incomplete_md = _ACRA_METADATA + _CHARGES_TABLE + _SUMMARY  # missing commentary

        with _mock_generate(incomplete_md), _mock_evidence(), _mock_quota(), _mock_record():
            with patch(
                "credit_report.generation.completeness.fill_missing_tables",
                new=AsyncMock(side_effect=RuntimeError("LLM error")),
            ):
                output = await run_section_generation(
                    db, rid, section_no=8, actor_user_id=_uid()
                )

        assert output.status == "done"
        assert output.markdown  # partial content still saved

    async def test_tokens_accumulated_from_s8_fill(self, db):
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=8,
            input_json=__import__("json").dumps(_INPUT_ACRA_AVAILABLE),
        ))
        await db.flush()

        incomplete_md = _ACRA_METADATA + _CHARGES_TABLE + _SUMMARY  # no bullets
        fill_tokens = 700

        with _mock_generate(incomplete_md, tokens=1500), _mock_evidence(), _mock_quota(), _mock_record():
            with _mock_fill(_COMMENTARY_4_BULLETS, tokens=fill_tokens):
                output = await run_section_generation(
                    db, rid, section_no=8, actor_user_id=_uid()
                )

        assert output.tokens_used >= 1500 + fill_tokens

    async def test_not_available_s8_does_not_trigger_fill(self, db):
        """When acra_data_available=False, the 'Not Available' output is correct — no fill."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report, SectionInput

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=_uid()))
        db.add(SectionInput(
            id=_uid(), report_id=rid, section_no=8,
            input_json=__import__("json").dumps(_INPUT_ACRA_NOT_AVAILABLE),
        ))
        await db.flush()

        with _mock_generate(NOT_AVAILABLE_S8), _mock_evidence(), _mock_quota(), _mock_record():
            mock_fill = AsyncMock(return_value=("", 0))
            with patch("credit_report.generation.completeness.fill_missing_tables", mock_fill):
                output = await run_section_generation(
                    db, rid, section_no=8, actor_user_id=_uid()
                )

        mock_fill.assert_not_called()
        assert output.status == "done"


# ── E. Fill prompt content ────────────────────────────────────────────────────

class TestSection8FillPrompts:

    def _system(self) -> str:
        from credit_report.generation.completeness import _build_fill_system_prompt
        return _build_fill_system_prompt(8)

    def _user(self, missing=None, existing="...", inp=None) -> str:
        from credit_report.generation.completeness import _build_fill_user_prompt
        if missing is None:
            missing = [("| Chargee |", "C-1b Charges Table")]
        return _build_fill_user_prompt(8, missing, existing, inp or {}, "en")

    def test_system_prompt_mentions_8_columns(self):
        prompt = self._system()
        assert "8 columns" in prompt or "| # | Chargee |" in prompt

    def test_system_prompt_mentions_chargee(self):
        prompt = self._system()
        assert "Chargee" in prompt

    def test_system_prompt_mentions_chronological(self):
        prompt = self._system()
        assert "chronological" in prompt.lower() or "earliest first" in prompt.lower()

    def test_system_prompt_mentions_registered_or_satisfied_status(self):
        prompt = self._system()
        assert "Registered" in prompt and "Satisfied" in prompt

    def test_system_prompt_mentions_summary_4_line_format(self):
        prompt = self._system()
        assert "Total charges:" in prompt
        assert "Total active amount:" in prompt
        assert "CUB charges:" in prompt
        assert "Unique chargees:" in prompt

    def test_system_prompt_mentions_at_least_4_bullets(self):
        prompt = self._system()
        assert "4" in prompt and ("bullet" in prompt.lower() or "BULLET" in prompt)

    def test_system_prompt_mentions_forward_looking(self):
        prompt = self._system()
        assert "forward-looking" in prompt.lower() or "Upon execution" in prompt

    def test_system_prompt_prohibits_credit_judgments(self):
        prompt = self._system()
        assert "credit judgment" in prompt.lower() or "satisfactory" in prompt.lower()

    def test_system_prompt_prohibits_source_referencing(self):
        prompt = self._system()
        assert "source-referencing" in prompt.lower() or "as per" in prompt.lower()

    def test_user_prompt_contains_missing_labels(self):
        prompt = self._user(missing=[("| Chargee |", "C-1b Charges Table (8 columns)")])
        assert "C-1b Charges Table" in prompt

    def test_user_prompt_mentions_charges_table_rules(self):
        prompt = self._user()
        assert "Charges Table" in prompt or "8 columns" in prompt

    def test_user_prompt_mentions_summary_exact_format(self):
        prompt = self._user()
        assert "Total charges" in prompt or "4-line format" in prompt

    def test_user_prompt_mentions_commentary_bullets(self):
        prompt = self._user()
        assert "bullet" in prompt.lower() or "≥4" in prompt

    def test_user_prompt_mentions_forward_looking_rule(self):
        prompt = self._user()
        assert "forward-looking" in prompt.lower() or "Upon execution" in prompt

    def test_user_prompt_includes_input_json(self):
        inp = {"8A_acra_banking_charges": {"acra_data_available": True}}
        prompt = self._user(inp=inp)
        assert "acra_data_available" in prompt or "8A_acra_banking_charges" in prompt

    def test_user_prompt_includes_language_field(self):
        prompt = self._user()
        assert "en" in prompt or "LANGUAGE" in prompt


# ── F. Config ─────────────────────────────────────────────────────────────────

class TestSection8Config:

    def test_s8_fill_budget_is_6144(self):
        """§8 fill calls should use 6144 max_tokens."""
        import inspect
        from credit_report.generation import completeness
        src = inspect.getsource(completeness.fill_missing_tables)
        # The budget logic reads: elif section_no in (3, 8): max_tokens = 6144
        assert "6144" in src, "Fill budget 6144 should be present in fill_missing_tables"
        # Verify §8 maps to 6144 by checking the elif condition includes 8
        assert "(3, 8)" in src or "section_no == 8" in src, (
            "§8 should be grouped with 6144-token budget sections"
        )

    def test_s8_default_token_budget_is_8192(self):
        """§8 primary generation uses the default 8192 budget."""
        from credit_report.config import SECTION_MAX_OUTPUT_TOKENS
        budget = SECTION_MAX_OUTPUT_TOKENS.get(8, SECTION_MAX_OUTPUT_TOKENS.get("default", 8192))
        assert budget == 8192, f"Expected §8 to use default 8192, got {budget}"

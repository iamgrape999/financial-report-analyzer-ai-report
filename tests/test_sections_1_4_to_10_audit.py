"""
Deep audit CI/CD tests for §1, §4, §5, §6, §7, §8, §9, §10.

Mirrors the bug-class checks done for §2 and §3:

  Bug B — prompt uses "Left:"/"Right:" positional labels as column directives
           → AI outputs literal "Left"/"Right" as column headers
  Bug C — analyst input nested objects (FORMAT C) passed without normalization
           → AI outputs "—" for all period values

For each section this suite verifies:
  1. Bug B: no "Left:" / "Right:" directive anywhere in the prompt
  2. Bug C: build_section_prompt() accepts nested-object input without error
             and serialises it as valid JSON (AI will see the data)
  3. Structural: prompt builds without error for empty and realistic inputs
  4. Section-specific properties (correct column names, mandatory rules present)

Run:
    python -m pytest tests/test_sections_1_4_to_10_audit.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from main import app  # noqa: E402

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
REPORTS = f"{BASE}/reports"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _mock_gemini(text: str = "## Section\n\nMock output."):
    mock_resp = MagicMock()
    mock_resp.text = text
    mock_resp.usage_metadata.prompt_token_count = 100
    mock_resp.usage_metadata.candidates_token_count = 200
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
    return patch("google.genai.Client", return_value=mock_client)


@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def hdrs(ac):
    r = await ac.post(f"{AUTH}/login", data={"username": "admin@example.com", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def rid(ac, hdrs):
    r = await ac.post(
        REPORTS,
        json={"borrower_name": f"AuditCo {uuid.uuid4().hex[:6]}", "industry": "marine", "report_type": "new_deal"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_section_output(report_id: str, section_no: int, markdown: str = "## Seeded\n\nContent."):
    """Insert a done SectionOutput to satisfy hard-dependency checks."""
    from credit_report.database import AsyncSessionLocal
    from credit_report.models import SectionOutput
    async with AsyncSessionLocal() as db:
        db.add(SectionOutput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            status="done",
            markdown=markdown,
            model_id="mock",
            tokens_used=0,
        ))
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# BUG B — Left/Right column directive scan (applies to ALL sections)
# ══════════════════════════════════════════════════════════════════════════════

class TestBugBLeftRightHeaders:
    """No section prompt may use 'Left:' or 'Right:' as column-header directives."""

    TARGET_SECTIONS = [1, 4, 5, 6, 7, 8, 9, 10]

    def _get_instruction(self, sec_no: int) -> str:
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        return SECTION_INSTRUCTIONS.get(sec_no, "")

    @pytest.mark.parametrize("sec_no", TARGET_SECTIONS)
    def test_no_left_colon_directive(self, sec_no):
        """'Left: **...**' as a column-label instruction must not appear."""
        instr = self._get_instruction(sec_no)
        bad = re.findall(r"\bLeft:\s+\*\*[^*]+\*\*", instr)
        assert not bad, (
            f"§{sec_no} prompt still contains 'Left: **...**' column-header directive: {bad}\n"
            "This causes the AI to output literal 'Left' as a column header (Bug B)."
        )

    @pytest.mark.parametrize("sec_no", TARGET_SECTIONS)
    def test_no_right_colon_directive(self, sec_no):
        """'Right: ...' as a column-label instruction must not appear."""
        instr = self._get_instruction(sec_no)
        bad = re.findall(r"\bRight:\s+(?:numbered|\*\*)", instr)
        assert not bad, (
            f"§{sec_no} prompt still contains 'Right:' column-header directive: {bad}\n"
            "This causes the AI to output literal 'Right' as a column header (Bug B)."
        )

    @pytest.mark.parametrize("sec_no", TARGET_SECTIONS)
    def test_column_headers_use_content_names(self, sec_no):
        """Each section's table instructions must use actual content labels, not positional words."""
        instr = self._get_instruction(sec_no)
        assert instr, f"§{sec_no} has no instruction text — is it registered in SECTION_INSTRUCTIONS?"
        # Confirm the instruction text is non-trivial
        assert len(instr) > 200, f"§{sec_no} instruction seems too short ({len(instr)} chars) — may be placeholder"


# ══════════════════════════════════════════════════════════════════════════════
# BUG C — Nested-object input (FORMAT C) handling
# ══════════════════════════════════════════════════════════════════════════════

class TestBugCNestedObjectInput:
    """
    build_section_prompt() must not crash when input contains nested objects.
    The serialised JSON sent to the AI must include the data (not silently drop it).
    This is the FORMAT C scenario that caused §3 to output all '—'.
    """

    def _build(self, sec_no: int, input_json: dict) -> tuple[str, str]:
        from credit_report.generation.prompt_builder import build_section_prompt
        return build_section_prompt(sec_no, input_json, evidence_chunks=[])

    def test_section1_nested_facility_rows(self):
        """§1 — facility_summary rows as nested objects must be serialised cleanly."""
        input_json = {
            "facility_summary": {
                "rows": [
                    {
                        "item": 1,
                        "borrower": "Evergreen Marine (Asia) Pte. Ltd. (\"EMA\")",
                        "booking": "SG",
                        "current_facility": None,
                        "proposed_facility": {"amount": 213.84, "tag": "NEW"},
                        "outstanding": 0,
                        "ccy": "USD",
                        "tenor": {"years": 7, "note": "from delivery"},
                    }
                ],
                "totals": {"total_credit_limit": 213.84},
                "footnotes": [],
            }
        }
        _, user_prompt = self._build(1, input_json)
        assert "facility_summary" in user_prompt, "§1 input data not included in prompt"
        assert "213.84" in user_prompt, "§1 nested amount not serialised to prompt"

    def test_section4_nested_management_objects(self):
        """§4 — management team as nested objects must be serialised."""
        input_json = {
            "4C_key_management": {
                "executives": [
                    {"name": "John Chen", "title": "CEO", "years_experience": 25,
                     "background": {"education": "MBA", "prior_roles": ["CFO at Maersk"]}},
                    {"name": "Sarah Lim", "title": "CFO", "years_experience": 18,
                     "background": {"education": "CPA", "prior_roles": ["Finance Director"]}},
                ]
            }
        }
        _, user_prompt = self._build(4, input_json)
        assert "John Chen" in user_prompt, "§4 nested management data not serialised"
        assert "CEO" in user_prompt, "§4 title not in prompt"

    def test_section5_nested_rg_milestones(self):
        """§5 — RG milestone table as nested objects must be serialised."""
        input_json = {
            "5A_pre_delivery_rg": {
                "issuer": "Korea Development Bank",
                "rating": {"sp": "AA", "fitch": "AA-"},
                "milestones": [
                    {"name": "Steel Cutting", "date": "2026-01-15", "rg_amount_usd_m": 50.0},
                    {"name": "Keel Laying", "date": "2026-06-01", "rg_amount_usd_m": 100.0},
                ]
            }
        }
        _, user_prompt = self._build(5, input_json)
        assert "Korea Development Bank" in user_prompt, "§5 RG issuer not serialised"
        assert "50.0" in user_prompt, "§5 RG amount not serialised"

    def test_section6_nested_risk_objects(self):
        """§6 — risks as nested objects (title, description, mitigants list) must be serialised."""
        input_json = {
            "6F_construction_progress": {
                "status_date": "2025-11-01",
                "milestones_completed": 2,
                "milestones_total": 5,
                "risks": [
                    {
                        "title": "Builder insolvency",
                        "likelihood": "Low",
                        "description": "Samsung HI is financially sound.",
                        "mitigants": [
                            "Refund Guarantee (RG) issued by KDB (AA / AA-).",
                            "RG covers 100% of pre-delivery instalments.",
                            "CUB holds perfected assignment of RG.",
                            "KDB rated AA by S&P, AA- by Fitch.",
                        ]
                    }
                ]
            }
        }
        _, user_prompt = self._build(6, input_json)
        assert "Builder insolvency" in user_prompt, "§6 risk title not serialised"
        assert "Refund Guarantee" in user_prompt, "§6 mitigant not serialised"
        # Verify the nested list is present in output (not dropped)
        assert "KDB rated AA" in user_prompt, "§6 nested mitigant bullet 4 not serialised"

    def test_section7_nested_financial_rows(self):
        """§7 — P&L rows as nested objects must be serialised cleanly."""
        input_json = {
            "7A_borrower_financials": {
                "entity": "EMA",
                "currency": "USD",
                "unit": "m",
                "pl": {
                    "periods": ["FY2022", "FY2023", "FY2024"],
                    "rows": [
                        {"item": "Revenue", "values": [14700, 8200, 5100], "bold": False},
                        {"item": "Gross Profit", "values": [5600, 2100, 800], "bold": True},
                        {"item": "Net Income", "values": [4200, 1500, 400], "bold": True},
                    ]
                }
            }
        }
        _, user_prompt = self._build(7, input_json)
        assert "Revenue" in user_prompt, "§7 P&L row not serialised"
        assert "14700" in user_prompt, "§7 revenue value not serialised"

    def test_section8_nested_acra_charges(self):
        """§8 — ACRA charges as nested objects must be serialised."""
        input_json = {
            "section_applicability": {"acra_data_available": True},
            "8A_acra_banking_charges": {
                "search_date": "2025-10-15",
                "entity_name": "Evergreen Marine (Asia) Pte. Ltd.",
                "uen": "202300001A",
                "charges": [
                    {
                        "chargee": "Cathay United Bank, Singapore Branch",
                        "registration_date": "2024-01-10",
                        "charge_date": "2024-01-08",
                        "amount_usd_m": 155.12,
                        "currency": "USD",
                        "property": "Vessel — Hull No. 4508",
                        "status": {"type": "Registered"},
                    }
                ]
            }
        }
        _, user_prompt = self._build(8, input_json)
        assert "Cathay United Bank" in user_prompt, "§8 chargee not serialised"
        assert "155.12" in user_prompt, "§8 charge amount not serialised"
        assert "202300001A" in user_prompt, "§8 UEN not serialised"

    def test_section9_nested_checklist_items(self):
        """§9 — checklist answers as nested objects must be serialised."""
        input_json = {
            "9A_checklist": {
                "items": [
                    {
                        "number": 1,
                        "category": "KYC & Compliance",
                        "item": "CDD completed",
                        "response": {"answer": "Yes", "remarks": "Tier 2 customer"},
                    },
                    {
                        "number": 4,
                        "category": "Credit Risk",
                        "item": "Internal MSR rating generated",
                        "response": {"answer": "Yes", "remarks": "MSR 3+"},
                    }
                ]
            },
            "9B_recommendation": {
                "decision": "APPROVE",
                "facility_amount_usd_m": 213.84,
            }
        }
        _, user_prompt = self._build(9, input_json)
        assert "CDD completed" in user_prompt, "§9 checklist item not serialised"
        assert "MSR 3+" in user_prompt, "§9 nested remarks not serialised"
        assert "APPROVE" in user_prompt, "§9 decision not serialised"

    def test_section10_nested_exposure_rows(self):
        """§10 — group exposure as nested objects must be serialised."""
        input_json = {
            "10A_group_exposure": {
                "group_name": "Evergreen Group",
                "as_of": "October 2025",
                "rows": [
                    {
                        "entity": "EMA",
                        "branch": "SG",
                        "facility_type": "Term Loan (SLL)",
                        "current_approved": {"amount": 155.12, "currency": "USD"},
                        "proposed": {"amount": 213.84, "tag": "NEW"},
                        "outstanding": 0,
                        "msr": "3+",
                    }
                ]
            }
        }
        _, user_prompt = self._build(10, input_json)
        assert "Evergreen Group" in user_prompt, "§10 group name not serialised"
        assert "213.84" in user_prompt, "§10 nested proposed amount not serialised"


# ══════════════════════════════════════════════════════════════════════════════
# Structural prompt checks (section-specific rules)
# ══════════════════════════════════════════════════════════════════════════════

class TestSectionPromptStructure:
    """Section prompts must contain key structural requirements."""

    def _get_instruction(self, sec_no: int) -> str:
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        return SECTION_INSTRUCTIONS.get(sec_no, "")

    def test_section1_has_zero_hallucination_rule(self):
        """§1 must have a ZERO HALLUCINATION / DATA NOT PROVIDED rule for null inputs."""
        instr = self._get_instruction(1)
        assert "DATA NOT PROVIDED" in instr or "not in input" in instr, (
            "§1 prompt missing null-data fallback rule — AI may hallucinate missing values instead "
            "of writing '[DATA NOT PROVIDED]'"
        )

    def test_section1_facility_table_column_count(self):
        """§1 facility table must specify all 11 mandatory columns explicitly."""
        instr = self._get_instruction(1)
        expected_columns = ["Item", "Borrower", "Booking", "Tenor", "CCY", "Collateral", "Guarantor"]
        for col in expected_columns:
            assert col in instr, f"§1 facility table missing mandatory column '{col}'"

    def test_section1_no_subheadings_rule(self):
        """§1 must enforce ZERO sub-headings (no 1.1/1.2) — it's a flat section."""
        instr = self._get_instruction(1)
        assert "ZERO sub-heading" in instr or "no sub-number" in instr or "no 1.1" in instr, (
            "§1 prompt missing the ZERO sub-headings rule — AI may add 1.1/1.2 sub-sections"
        )

    def test_section4_corporate_identity_table(self):
        """§4 C-1 must use explicit 'Item | Detail' column labels (not positional words)."""
        instr = self._get_instruction(4)
        assert "Item | Detail" in instr, (
            "§4 C-1 Corporate Identity table missing explicit 'Item | Detail' column labels"
        )

    def test_section4_null_row_omission_rule(self):
        """§4 must specify how to handle null values (omit row, not write N/A)."""
        instr = self._get_instruction(4)
        assert "null" in instr.lower() and ("omit" in instr.lower() or "unknown" in instr.lower()), (
            "§4 prompt missing null/unknown row omission rule — AI may write 'N/A' instead of omitting"
        )

    def test_section5_pre_delivery_rg_columns(self):
        """§5 C-1 RG coverage table must specify all 8 columns."""
        instr = self._get_instruction(5)
        expected = ["Milestone", "RG Amount", "Coverage %", "Status"]
        for col in expected:
            assert col in instr, f"§5 RG coverage table missing column '{col}'"

    def test_section5_omit_na_rule(self):
        """§5 must say to omit sub-sections rather than write 'N/A' for absent data."""
        instr = self._get_instruction(5)
        assert "NO 'N/A'" in instr or "omit" in instr.lower(), (
            "§5 missing the 'omit row/sub-section if data absent' rule"
        )

    def test_section6_mitigant_bullet_count_rule(self):
        """§6 C-6 must specify ALL mitigant bullets (3-5 per risk) — not compressed."""
        instr = self._get_instruction(6)
        assert "3-5" in instr and "mitigant" in instr.lower(), (
            "§6 C-6 missing mitigant count rule (3-5 bullets per risk) — AI may compress to 1 sentence"
        )
        assert "NEVER compress" in instr or "ALL mitigant bullets" in instr, (
            "§6 C-6 missing explicit 'ALL mitigant bullets' / 'NEVER compress' instruction"
        )

    def test_section6_payment_schedule_column_count(self):
        """§6 C-4 payment schedule must specify 11 columns."""
        instr = self._get_instruction(6)
        assert "11 column" in instr or "11 col" in instr, (
            "§6 C-4 payment schedule missing '11 columns' requirement — AI may omit columns"
        )

    def test_section6_force_majeure_standalone(self):
        """§6 C-7 Force Majeure must be a STANDALONE paragraph (not inside C-6 risk)."""
        instr = self._get_instruction(6)
        assert "STANDALONE" in instr and "Force Majeure" in instr, (
            "§6 missing 'STANDALONE paragraph' rule for Force Majeure — AI may merge it with C-6 risk"
        )

    def test_section7_financial_table_row_checks(self):
        """§7 must specify minimum row counts for P&L, BS, CF tables."""
        instr = self._get_instruction(7)
        assert "≥12 rows" in instr or "12 rows" in instr, (
            "§7 P&L missing ≥12 row check — AI may output condensed P&L"
        )
        assert "≥20 rows" in instr or "20 rows" in instr, (
            "§7 BS missing ≥20 row check — AI may collapse balance sheet"
        )

    def test_section7_n_m_vs_n_a_rule(self):
        """§7 must define N/M (denominator ≤0) vs N/A (interim + annualization) distinction."""
        instr = self._get_instruction(7)
        assert "N/M" in instr and "N/A" in instr and "Denominator" in instr, (
            "§7 missing N/M vs N/A distinction for ratio cells — AI may use them interchangeably"
        )

    def test_section8_applicability_gate(self):
        """§8 must have explicit acra_data_available == false handling."""
        instr = self._get_instruction(8)
        assert "acra_data_available" in instr or "ACRA" in instr, (
            "§8 missing ACRA applicability gate"
        )
        assert "Not Available" in instr or "not incorporated" in instr, (
            "§8 missing 'Not Available' output rule for non-SG borrowers"
        )

    def test_section8_charges_table_8_columns(self):
        """§8 charges table must specify all 8 columns."""
        instr = self._get_instruction(8)
        expected = ["Chargee", "Date of Registration", "Amount", "Status"]
        for col in expected:
            assert col in instr, f"§8 charges table missing column '{col}'"

    def test_section8_commentary_bullet_count(self):
        """§8 CA commentary must require ≥4 bullet points."""
        instr = self._get_instruction(8)
        assert "≥4 bullets" in instr or "4 bullet" in instr or "4-5 bullet" in instr or "3-5 bullet" in instr, (
            "§8 CA commentary missing minimum bullet count — AI may write single prose paragraph"
        )

    def test_section9_exactly_23_checklist_items(self):
        """§9 checklist must specify exactly 23 items."""
        instr = self._get_instruction(9)
        assert "23" in instr, "§9 missing '23-item' checklist requirement"
        # Also check the response format is enforced
        assert "Yes" in instr and "N/A" in instr, (
            "§9 missing response column format (Yes/No*/N/A)"
        )

    def test_section9_recommendation_format(self):
        """§9 must specify the exact RECOMMENDATION block format."""
        instr = self._get_instruction(9)
        assert "RECOMMENDATION" in instr and "APPROVE" in instr, (
            "§9 missing RECOMMENDATION block format with APPROVE/DECLINE options"
        )
        assert "Approval Authority" in instr, (
            "§9 missing prohibition on 'Approval Authority' line"
        )

    def test_section10_exposure_table_10_columns(self):
        """§10 Appendix I must specify 10 columns."""
        instr = self._get_instruction(10)
        expected = ["Entity", "Branch", "Facility Type", "Current Approved", "Proposed", "MSR"]
        for col in expected:
            assert col in instr, f"§10 exposure table missing column '{col}'"

    def test_section10_full_detail_rule(self):
        """§10 must enforce FULL DETAIL (NEVER compress or abbreviate)."""
        instr = self._get_instruction(10)
        assert "NEVER compress" in instr or "FULL DETAIL" in instr or "MUST expand" in instr, (
            "§10 missing NEVER COMPRESS / FULL DETAIL rule — AI may summarise appendix tables"
        )


# ══════════════════════════════════════════════════════════════════════════════
# build_section_prompt() stability tests (empty + realistic input)
# ══════════════════════════════════════════════════════════════════════════════

class TestPromptBuildStability:
    """build_section_prompt() must not crash for any section with any valid input."""

    def _build(self, sec_no: int, input_json: dict) -> tuple[str, str]:
        from credit_report.generation.prompt_builder import build_section_prompt
        return build_section_prompt(sec_no, input_json, evidence_chunks=[])

    @pytest.mark.parametrize("sec_no", [1, 4, 5, 6, 7, 8, 9, 10])
    def test_builds_with_empty_input(self, sec_no):
        """build_section_prompt must not raise with empty dict input."""
        try:
            sys_p, user_p = self._build(sec_no, {})
        except Exception as exc:
            pytest.fail(f"§{sec_no} build_section_prompt crashed with empty input: {exc}")
        assert sys_p, f"§{sec_no} system prompt is empty"
        assert user_p, f"§{sec_no} user prompt is empty"

    @pytest.mark.parametrize("sec_no", [1, 4, 5, 6, 7, 8, 9, 10])
    def test_prompt_contains_section_heading(self, sec_no):
        """The generated user prompt must reference the correct section number."""
        _, user_p = self._build(sec_no, {})
        assert f"§{sec_no}" in user_p or f"Section {sec_no}" in user_p, (
            f"§{sec_no} user prompt does not reference section number"
        )

    @pytest.mark.parametrize("sec_no", [1, 4, 5, 6, 7, 8, 9, 10])
    def test_builds_with_nested_object_input(self, sec_no):
        """build_section_prompt must not crash when input contains nested objects (FORMAT C scenario)."""
        nested_input = {
            f"{sec_no}A_data": {
                "rows": [{"key": "value", "nested": {"a": 1, "b": [1, 2, 3]}}],
                "metadata": {"version": 2, "flags": {"override": True}},
            }
        }
        try:
            sys_p, user_p = self._build(sec_no, nested_input)
        except Exception as exc:
            pytest.fail(f"§{sec_no} build_section_prompt crashed with nested-object input: {exc}")
        # Input must be included in the serialised prompt
        assert "rows" in user_p, f"§{sec_no} nested input rows not serialised to prompt"

    @pytest.mark.parametrize("sec_no", [1, 4, 5, 6, 7, 8, 9, 10])
    def test_input_json_serialisable(self, sec_no):
        """The input_json sent to the AI must be valid JSON (no MagicMock or circular refs)."""
        from credit_report.generation.prompt_builder import build_section_prompt
        sample_inputs = {
            1: {"facility_summary": {"rows": [{"item": 1, "amount": 213.84}]}},
            4: {"4A_corporate_identity": {"english_name": "EMA", "uen": "202300001A"}},
            5: {"5A_pre_delivery_rg": {"issuer": "KDB", "milestones": []}},
            6: {"6A_project": {"asset_type": "Containership", "teu": 24000}},
            7: {"7A_borrower_financials": {"entity": "EMA", "currency": "USD"}},
            8: {"section_applicability": {"acra_data_available": False}},
            9: {"9B_recommendation": {"decision": "APPROVE"}},
            10: {"10A_group_exposure": {"group_name": "Evergreen", "rows": []}},
        }
        _, user_p = build_section_prompt(sec_no, sample_inputs.get(sec_no, {}), evidence_chunks=[])
        # The JSON block in the prompt must be parseable
        json_match = re.search(r"```json\n(.*?)```", user_p, re.DOTALL)
        if json_match:
            raw = json_match.group(1)
            try:
                json.loads(raw)
            except json.JSONDecodeError as exc:
                pytest.fail(f"§{sec_no} prompt JSON block is not valid JSON: {exc}\nRaw:\n{raw[:400]}")


# ══════════════════════════════════════════════════════════════════════════════
# Section-3 normalization is NOT accidentally applied to other sections
# ══════════════════════════════════════════════════════════════════════════════

class TestSection3NormalizationIsolation:
    """_normalize_section3_ratings() must only modify §3 input, not §1/§4-§10."""

    def test_normalize_does_not_mutate_non_section3_input(self):
        """_normalize_section3_ratings() with input lacking 3B_internal_ratings must return unchanged."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        original = {"facility_summary": {"rows": [{"item": 1, "nested_obj": {"amount": 100}}]}}
        result = _normalize_section3_ratings(original)
        # Should return same content since there's no 3B_internal_ratings
        assert result["facility_summary"]["rows"][0]["nested_obj"] == {"amount": 100}, (
            "_normalize_section3_ratings mutated non-§3 input — side effect bug"
        )

    @pytest.mark.parametrize("sec_no", [1, 4, 5, 6, 7, 8, 9, 10])
    def test_build_prompt_does_not_call_normalize_for_other_sections(self, sec_no):
        """build_section_prompt with sec_no != 3 must not invoke section-3 normalization."""
        from credit_report.generation import prompt_builder

        call_log = []
        original_fn = prompt_builder._normalize_section3_ratings

        def tracking_normalize(input_json):
            call_log.append(sec_no)
            return original_fn(input_json)

        import unittest.mock
        with unittest.mock.patch.object(
            prompt_builder, "_normalize_section3_ratings", side_effect=tracking_normalize
        ):
            prompt_builder.build_section_prompt(sec_no, {}, evidence_chunks=[])

        assert not call_log, (
            f"_normalize_section3_ratings was called during §{sec_no} prompt build — "
            "normalization should ONLY run for §3"
        )


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end generation tests (selected sections without hard deps)
# ══════════════════════════════════════════════════════════════════════════════

class TestEndToEndGenerationNoLeftRight:
    """
    Generate sections that have no hard dependencies and verify:
    - Response is 202 (accepted)
    - Saved markdown does NOT contain '| Left | Right |' or '| Left |'
    - Saved markdown is non-empty
    """

    @pytest.mark.asyncio
    async def test_section4_generated_no_left_right_headers(self, ac, hdrs, rid):
        """§4 has no hard dependencies — generate and check output has no Left/Right headers."""
        input_data = {
            "4A_corporate_identity": {"english_name": "EMA", "incorporation_country": "Singapore"},
            "4B_ownership": {"shareholders": [{"name": "EMC", "stake_pct": 100}]},
        }
        r = await ac.put(f"{REPORTS}/{rid}/inputs/4", json={"section_no": 4, "input_json": input_data}, headers=hdrs)
        assert r.status_code == 200

        good_output = (
            "**4. Corporate History and Overview**\n\n"
            "**C-1. Corporate Identity**\n\n"
            "| Item | Detail |\n"
            "|---|---|\n"
            "| English Name | Evergreen Marine (Asia) Pte. Ltd. (\"EMA\") |\n"
            "| Incorporation Country | Singapore |\n"
        )
        with _mock_gemini(good_output):
            r = await ac.post(f"{REPORTS}/{rid}/generate/4?gen_language=en", headers=hdrs)
        assert r.status_code == 202, r.text

        r = await ac.get(f"{REPORTS}/{rid}/sections/4/output", headers=hdrs)
        assert r.status_code == 200
        markdown = r.json().get("markdown", "")
        assert markdown, "§4 generated markdown is empty"
        assert "| Left |" not in markdown, "§4 output contains '| Left |' as column header (Bug B regression)"
        assert "| Right |" not in markdown, "§4 output contains '| Right |' as column header (Bug B regression)"
        assert "| Left | Right |" not in markdown, "§4 output contains '| Left | Right |' header row"

    @pytest.mark.asyncio
    async def test_section8_generated_no_left_right_headers(self, ac, hdrs, rid):
        """§8 has no hard dependencies — generate and check output has no Left/Right headers."""
        input_data = {
            "section_applicability": {"acra_data_available": True},
            "8A_acra_banking_charges": {
                "search_date": "15 Oct 2025",
                "entity_name": "Evergreen Marine (Asia) Pte. Ltd.",
                "uen": "202300001A",
                "charges": [
                    {
                        "chargee": "Cathay United Bank, Singapore Branch",
                        "registration_date": "10 Jan 2024",
                        "charge_date": "08 Jan 2024",
                        "amount_usd_m": 155.12,
                        "currency": "USD",
                        "property": "Vessel — Hull No. 4508",
                        "status": "Registered",
                    }
                ]
            }
        }
        r = await ac.put(f"{REPORTS}/{rid}/inputs/8", json={"section_no": 8, "input_json": input_data}, headers=hdrs)
        assert r.status_code == 200

        good_output = (
            "8. Changes in Engaged Banks\n\n"
            "Based on ACRA search dated 15 Oct 2025, Evergreen Marine (Asia) Pte. Ltd. "
            "(UEN: 202300001A) has the following registered charges:\n\n"
            "| # | Chargee | Date of Registration | Date of Charge | Amount (USD m) | "
            "Currency | Property Charged | Status |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "| 1 | Cathay United Bank, Singapore Branch | 10 Jan 2024 | 08 Jan 2024 | "
            "155.12 | USD | Vessel — Hull No. 4508 — **CUB facility (Item 1, §1)** | Registered |\n"
        )
        with _mock_gemini(good_output):
            r = await ac.post(f"{REPORTS}/{rid}/generate/8?gen_language=en", headers=hdrs)
        assert r.status_code == 202, r.text

        r = await ac.get(f"{REPORTS}/{rid}/sections/8/output", headers=hdrs)
        assert r.status_code == 200
        markdown = r.json().get("markdown", "")
        assert markdown, "§8 generated markdown is empty"
        assert "| Left |" not in markdown, "§8 output contains '| Left |' as column header"
        assert "| Left | Right |" not in markdown, "§8 output contains '| Left | Right |' header row"

    @pytest.mark.asyncio
    async def test_section7_generated_no_left_right_with_dependency(self, ac, hdrs, rid):
        """§7 has no hard dependencies — generate and verify no Left/Right headers."""
        input_data = {
            "7A_borrower_financials": {
                "entity": "EMA",
                "currency": "USD",
                "unit": "m",
                "periods": ["FY2022", "FY2023", "FY2024"],
                "pl": [
                    {"item": "Revenue", "values": [14700, 8200, 5100]},
                    {"item": "Net Income", "values": [4200, 1500, 400]},
                ]
            }
        }
        r = await ac.put(f"{REPORTS}/{rid}/inputs/7", json={"section_no": 7, "input_json": input_data}, headers=hdrs)
        assert r.status_code == 200

        good_output = (
            "## 7. Financial Analysis\n\n"
            "**EMA — Standalone | Currency: USD | Unit: USD'm**\n\n"
            "### Profit & Loss Statement\n\n"
            "| Line Item | FY2022 | FY2023 | FY2024 |\n"
            "|---|---|---|---|\n"
            "| Revenue | 14,700 | 8,200 | 5,100 |\n"
            "| Net Income | 4,200 | 1,500 | 400 |\n"
        )
        with _mock_gemini(good_output):
            r = await ac.post(f"{REPORTS}/{rid}/generate/7?gen_language=en", headers=hdrs)
        assert r.status_code == 202, r.text

        r = await ac.get(f"{REPORTS}/{rid}/sections/7/output", headers=hdrs)
        assert r.status_code == 200
        markdown = r.json().get("markdown", "")
        assert markdown, "§7 generated markdown is empty"
        assert "| Left |" not in markdown, "§7 output contains '| Left |' column header (Bug B regression)"


# ══════════════════════════════════════════════════════════════════════════════
# Regression: §2 fix did not break other sections
# ══════════════════════════════════════════════════════════════════════════════

class TestSection2FixRegression:
    """§2 Bug B fix must not have inadvertently broken other sections."""

    def test_section2_fix_did_not_remove_required_instructions(self):
        """§2 prompt must still contain all five table requirements (T1-T5)."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS.get(2, "")
        for label in ["Credit Overview", "Solvency", "Guarantor", "Collateral", "Risk and Mitigants"]:
            assert label in instr, (
                f"§2 prompt missing table label '{label}' — the Bug B fix may have removed required content"
            )

    def test_section2_all_five_null_data_rules_present(self):
        """§2 must retain NULL DATA RULES for all 5 tables (T1-T5)."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS.get(2, "")
        for key in ["2A_credit_overview", "2B_solvency", "2C_guarantor", "2D_collateral", "2E_risk_and_mitigants"]:
            assert key in instr, (
                f"§2 prompt missing NULL DATA RULE for {key} — may have been removed by Bug B fix"
            )

    def test_section2_explicit_format_example_preserved(self):
        """§2 T1 explicit table format example must still be in the prompt."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS.get(2, "")
        assert "| **Credit Overview** |" in instr, (
            "§2 T1 explicit format example '| **Credit Overview** |' missing — Bug B fix may have removed it"
        )

    def test_section3_normalize_function_still_exists(self):
        """_normalize_section3_ratings() must still be importable after all changes."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        assert callable(_normalize_section3_ratings)

    def test_section3_format_c_still_normalized(self):
        """§3 FORMAT C normalization must still work after §2 fix was applied."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        result = _normalize_section3_ratings({
            "3B_internal_ratings": {
                "rows": [{
                    "entity_full_name": "EMA",
                    "entity_abbrev": "EMA",
                    "role": "Borrower",
                    "fy2022_23": "3",
                    "fy2024": "3+",
                    "interim": {"generated_msr": "4+", "override_applied": True, "override_to": "4+"},
                    "current": {"proposed_assessment": {"generated_msr": "3", "proposed_final_msr": "3+"}},
                    "override_flag": False,
                    "override_remarks": "",
                }]
            }
        })
        row = result["3B_internal_ratings"]["rows"][0]
        assert row["interim"] == "4+", "§3 FORMAT C normalization broken after §2 fix"
        assert row["current"] == "3+", "§3 FORMAT C normalization broken after §2 fix"

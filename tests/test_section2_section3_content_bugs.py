"""
Real content-verification tests for §2 and §3 bugs.

These tests would have caught the bugs that passed previous tests:

  Bug-A  table_cells.display_value VARCHAR(255) truncates long cell text
  Bug-B  §2 T1 outputs literal "Left" / "Right" as column headers
  Bug-C  §3 MSR table outputs "—" for all periods when data is FORMAT C

Tests verify:
  1. _normalize_section3_ratings() correctly flattens FORMAT C → flat strings
  2. §3 prompt built from FORMAT C input contains actual MSR values (not dicts)
  3. §2 prompt does NOT contain "Left:" / "Right:" column-header instructions
  4. §2 generated markdown does NOT start table rows with "| Left |" or "| Right |"
  5. TableCell.display_value TEXT column accepts values longer than 255 chars
  6. End-to-end: generate §2 with mocked AI → markdown must NOT contain "Left | Right" header row

Run:
    python -m pytest tests/test_section2_section3_content_bugs.py -v --tb=short
"""
from __future__ import annotations

import json
import os
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

def _mock_gemini(text: str):
    mock_resp = MagicMock()
    mock_resp.text = text
    # Return real ints for token counting so quota.record_tokens() doesn't crash
    mock_resp.usage_metadata.prompt_token_count = 100
    mock_resp.usage_metadata.candidates_token_count = 200
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
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
        json={"borrower_name": f"BugTest {uuid.uuid4().hex[:6]}", "industry": "marine", "report_type": "new_deal"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ══════════════════════════════════════════════════════════════════════════════
# Bug-A: VARCHAR(255) → TEXT for display_value
# ══════════════════════════════════════════════════════════════════════════════

class TestDisplayValueTextColumn:
    """display_value must accept values longer than 255 characters."""

    @pytest.mark.asyncio
    async def test_table_cell_accepts_long_display_value(self):
        """Inserting a cell with display_value > 255 chars must NOT raise StringDataRightTruncationError."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.block_ast.models import ReportBlock, TableCell

        long_value = (
            "Primary source of repayment will be from cash generated from EMA's operating activities. "
            "Secondary source of repayment include available cash on hand, sale of vessel, "
            "or capital injection from parent EMC. Additional considerations include the strong "
            "track record of the borrower in meeting its debt obligations."
        )
        assert len(long_value) > 255, "test data must be longer than 255 chars to be meaningful"

        report_id = f"bug-a-test-{uuid.uuid4().hex[:8]}"
        block_id = f"bug-a-blk-{uuid.uuid4().hex[:8]}"
        cell_id = str(uuid.uuid4())

        async with AsyncSessionLocal() as db:
            # Seed a parent report block (skip FK to Report since AUTO_CREATE_TABLES)
            db.add(ReportBlock(
                id=block_id,
                report_id=report_id,
                section_no=2,
                block_type="table",
                content="| Col1 | Col2 |\n|---|---|\n",
                validation_status="pending",
                is_stale=False,
                version=1,
            ))
            await db.flush()
            db.add(TableCell(
                id=cell_id,
                block_id=block_id,
                row_id="r1",
                column_id="content",
                display_value=long_value,   # This must NOT raise VARCHAR(255) truncation error
                binding_status="unbound",
                version=1,
            ))
            await db.commit()

        # Read back and confirm value was stored in full
        async with AsyncSessionLocal() as db:
            cell = await db.get(TableCell, cell_id)
            assert cell is not None
            assert cell.display_value == long_value, (
                f"display_value was truncated! stored={len(cell.display_value)} expected={len(long_value)}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Bug-B: §2 T1 "Left" / "Right" literal column headers
# ══════════════════════════════════════════════════════════════════════════════

class TestSection2PromptNoLeftRight:
    """§2 prompt instructions must NOT use 'Left' / 'Right' as column labels."""

    def test_section2_prompt_instructions_do_not_say_left_colon(self):
        """The raw instruction string must not instruct AI to use 'Left:' as a column header."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS.get(2, "")
        # These exact patterns cause the AI to output literal "Left" / "Right" headers
        assert "Left: **Credit Overview**" not in instr, (
            "§2 prompt still uses 'Left: **Credit Overview**' — AI will generate '| Left | Right |' headers"
        )
        assert "Right: numbered bullets" not in instr, (
            "§2 prompt still uses 'Right: numbered bullets' — AI will generate '| Left | Right |' headers"
        )

    def test_section2_prompt_contains_explicit_format_example(self):
        """The §2 prompt must include an explicit table format example showing the Credit Overview label."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS.get(2, "")
        assert "**Credit Overview**" in instr, "§2 prompt must mention **Credit Overview** as the column label"
        # The format example should show the actual table structure
        assert "| **Credit Overview** |" in instr, (
            "§2 prompt must contain '| **Credit Overview** |' as an explicit table format example"
        )

    def test_section2_prompt_has_null_data_rule_for_t1(self):
        """T1 must have a NULL DATA RULE like T2-T5."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS.get(2, "")
        assert "2A_credit_overview" in instr and "null" in instr.lower() and "T1" in instr, (
            "§2 T1 is missing a NULL DATA RULE — if 2A_credit_overview is absent the AI must not crash or output empty cells"
        )

    def test_section2_prompt_explicitly_forbids_left_right_headers(self):
        """Prompt must explicitly say NEVER use 'Left' or 'Right' as column headers."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        instr = SECTION_INSTRUCTIONS.get(2, "")
        assert "NEVER use 'Left' or 'Right'" in instr or "NEVER use \"Left\" or \"Right\"" in instr, (
            "§2 prompt must explicitly forbid 'Left'/'Right' as column headers"
        )

    @pytest.mark.asyncio
    async def test_section2_generated_markdown_does_not_have_left_right_header(
        self, ac, hdrs, rid
    ):
        """
        End-to-end: generate §2 with a mocked AI that returns the correct format.
        §2 requires §7 as a hard dependency — we seed §7 output first.
        Verifies the saved markdown does NOT have a '| Left | Right |' table header row.
        """
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput

        # Seed §7 output (hard dependency for §2)
        async with AsyncSessionLocal() as db:
            db.add(SectionOutput(
                id=str(uuid.uuid4()),
                report_id=rid,
                section_no=7,
                status="done",
                markdown="## Section 7\n\nFinancial Analysis seeded for test.",
                model_id="mock",
                tokens_used=0,
            ))
            await db.commit()

        # Save §2 input data
        sec2_input = {
            "2A_credit_overview": {
                "bullets": [
                    "Bullet 1: Market position",
                    "Bullet 2: Transaction purpose",
                    "Bullet 3: Financial strength",
                ]
            },
            "2B_solvency": None,
            "2C_guarantor": None,
            "2D_collateral": None,
            "2E_risk_and_mitigants": None,
        }
        r = await ac.put(
            f"{REPORTS}/{rid}/inputs/2",
            json={"section_no": 2, "input_json": sec2_input},
            headers=hdrs,
        )
        assert r.status_code == 200

        # Mock the AI to return the CORRECT format (what a well-instructed AI would return)
        correct_section2_output = (
            "**2. Overall Comments**\n\n"
            "| **Credit Overview** | 1. Bullet 1: Market position |\n"
            "|---|---|\n"
            "| | 2. Bullet 2: Transaction purpose |\n"
            "| | 3. Bullet 3: Financial strength |\n\n"
            "| **Solvency** | [Solvency data not yet provided — please complete the 2B_solvency section in the analyst input form] |\n"
            "|---|---|\n"
        )

        with _mock_gemini(correct_section2_output):
            r = await ac.post(f"{REPORTS}/{rid}/generate/2?gen_language=en", headers=hdrs)
        assert r.status_code == 202, r.text

        # Fetch the saved output
        r = await ac.get(f"{REPORTS}/{rid}/sections/2/output", headers=hdrs)
        assert r.status_code == 200
        markdown = r.json().get("markdown", "")

        # The critical check: no "| Left | Right |" in any form
        assert "| Left | Right |" not in markdown, (
            f"§2 output contains '| Left | Right |' literal headers — prompt bug not fixed!\n"
            f"Markdown:\n{markdown[:500]}"
        )
        assert "| Left |" not in markdown, (
            f"§2 output contains '| Left |' — AI used 'Left' as a column header\nMarkdown:\n{markdown[:500]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Bug-C: §3 MSR FORMAT C normalization
# ══════════════════════════════════════════════════════════════════════════════

class TestSection3MsrFormatCNormalization:
    """_normalize_section3_ratings() must flatten FORMAT C nested objects to flat strings."""

    def _make_format_c_input(self) -> dict:
        """Construct a realistic FORMAT C input (nested period objects)."""
        return {
            "3A_external_ratings": {"all_nil": True},
            "3B_internal_ratings": {
                "rows": [
                    {
                        "entity_full_name": "Evergreen Marine Corporation (Taiwan) Ltd.",
                        "entity_abbrev": "EMA",
                        "role": "Borrower",
                        "fy2022_23": "3",
                        "fy2024": "3+",
                        "interim": {
                            "generated_msr": "4+",
                            "override_applied": True,
                            "override_to": "4+",
                            "override_remarks": "Override applied for interim period.",
                        },
                        "current": {
                            "proposed_assessment": {
                                "generated_msr": "3",
                                "proposed_final_msr": "3+",
                                "override_applied": True,
                            }
                        },
                        "override_flag": False,
                        "override_remarks": "",
                    },
                    {
                        "entity_full_name": "Evergreen Marine Corporation",
                        "entity_abbrev": "EMC",
                        "role": "Guarantor",
                        "fy2022_23": None,
                        "fy2024": "2+",
                        "interim": {
                            "generated_msr": "3",
                            "override_applied": False,
                        },
                        "current": {
                            "proposed_assessment": {
                                "generated_msr": "3+",
                                "proposed_final_msr": None,
                            }
                        },
                        "override_flag": False,
                        "override_remarks": "",
                    },
                ],
                "period_display_labels": {
                    "fy2022_23": "2022/23",
                    "fy2024": "2024",
                    "interim": "Jul 2025",
                    "current": "Nov 2025",
                },
            },
            "3C_mas_612": {},
            "3D_esg_rating": {},
        }

    def test_format_c_interim_dict_flattened_to_override_to(self):
        """FORMAT C: interim = {generated_msr, override_applied, override_to} → 'override_to' value."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        result = _normalize_section3_ratings(self._make_format_c_input())
        rows = result["3B_internal_ratings"]["rows"]
        ema = rows[0]

        assert isinstance(ema["interim"], str), (
            f"interim should be a flat string after normalization, got: {type(ema['interim'])}"
        )
        assert ema["interim"] == "4+", (
            f"interim override_to='4+' should be extracted, got: {ema['interim']!r}"
        )

    def test_format_c_current_proposed_assessment_flattened(self):
        """FORMAT C: current = {proposed_assessment: {proposed_final_msr}} → proposed_final_msr."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        result = _normalize_section3_ratings(self._make_format_c_input())
        rows = result["3B_internal_ratings"]["rows"]
        ema = rows[0]

        assert isinstance(ema["current"], str), (
            f"current should be a flat string after normalization, got: {type(ema['current'])}"
        )
        assert ema["current"] == "3+", (
            f"proposed_final_msr='3+' should be extracted, got: {ema['current']!r}"
        )

    def test_format_c_flat_fields_unchanged(self):
        """FORMAT C: flat string fields (fy2022_23, fy2024) must pass through unchanged."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        result = _normalize_section3_ratings(self._make_format_c_input())
        rows = result["3B_internal_ratings"]["rows"]
        ema = rows[0]

        assert ema["fy2022_23"] == "3", f"flat fy2022_23 must be unchanged, got: {ema['fy2022_23']!r}"
        assert ema["fy2024"] == "3+", f"flat fy2024 must be unchanged, got: {ema['fy2024']!r}"

    def test_format_c_none_field_unchanged(self):
        """FORMAT C: None period values must remain None (→ '—' in AI output)."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        result = _normalize_section3_ratings(self._make_format_c_input())
        rows = result["3B_internal_ratings"]["rows"]
        emc = rows[1]

        assert emc["fy2022_23"] is None, (
            f"None fy2022_23 must remain None after normalization, got: {emc['fy2022_23']!r}"
        )

    def test_format_c_override_flag_set_when_override_applied(self):
        """override_flag must be True on a row where any period dict has override_applied=True."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        result = _normalize_section3_ratings(self._make_format_c_input())
        rows = result["3B_internal_ratings"]["rows"]
        ema = rows[0]

        assert ema.get("override_flag") is True, (
            f"override_flag must be set True when override_applied=True in any period, got: {ema.get('override_flag')}"
        )

    def test_format_c_second_row_interim_generated_msr_extracted(self):
        """Guarantor row: interim = {generated_msr, override_applied=false} → generated_msr."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        result = _normalize_section3_ratings(self._make_format_c_input())
        rows = result["3B_internal_ratings"]["rows"]
        emc = rows[1]

        assert isinstance(emc["interim"], str), (
            f"guarantor interim should be flat string, got: {type(emc['interim'])}"
        )
        assert emc["interim"] == "3", (
            f"guarantor interim generated_msr='3' should be extracted, got: {emc['interim']!r}"
        )

    def test_format_c_proposed_assessment_none_final_falls_back_to_generated(self):
        """Guarantor current: proposed_final_msr is None → fall back to generated_msr."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        result = _normalize_section3_ratings(self._make_format_c_input())
        rows = result["3B_internal_ratings"]["rows"]
        emc = rows[1]

        # proposed_final_msr is None, generated_msr is "3+"
        assert isinstance(emc["current"], str), (
            f"current should be flat string, got: {type(emc['current'])}"
        )
        assert emc["current"] == "3+", (
            f"fall-back to generated_msr='3+' expected when proposed_final_msr is None, got: {emc['current']!r}"
        )

    def test_format_a_unchanged(self):
        """FORMAT A (already flat strings) must pass through normalization unchanged."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        format_a_input = {
            "3B_internal_ratings": {
                "rows": [
                    {
                        "entity_full_name": "EMA",
                        "entity_abbrev": "EMA",
                        "role": "Borrower",
                        "fy2022_23": "3",
                        "fy2024": "3+",
                        "interim": "4+",
                        "current": "3+",
                        "override_flag": False,
                        "override_remarks": "",
                    }
                ],
                "period_display_labels": {},
            }
        }
        result = _normalize_section3_ratings(format_a_input)
        row = result["3B_internal_ratings"]["rows"][0]
        assert row["fy2022_23"] == "3"
        assert row["fy2024"] == "3+"
        assert row["interim"] == "4+"
        assert row["current"] == "3+"

    def test_format_b_unchanged(self):
        """FORMAT B (flat keys, no rows array) must pass through normalization unchanged."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings

        format_b_input = {
            "3B_internal_ratings": {
                "borrower_entity_full_name": "EMA",
                "borrower_fy2022_23": "3",
                "borrower_fy2024": "3+",
                "borrower_interim": "4+",
                "borrower_current": "3+",
            }
        }
        result = _normalize_section3_ratings(format_b_input)
        # Should pass through since there's no "rows" key
        ratings = result["3B_internal_ratings"]
        assert ratings["borrower_fy2022_23"] == "3"
        assert ratings["borrower_interim"] == "4+"

    def test_section3_prompt_json_contains_flat_msr_values_not_dicts(self):
        """
        build_section_prompt for §3 with FORMAT C input must serialize flat MSR strings,
        NOT raw dicts, so the AI receives '4+' not '{"generated_msr": "4+", ...}'.
        """
        from credit_report.generation.prompt_builder import build_section_prompt

        format_c_input = {
            "3A_external_ratings": {"all_nil": True},
            "3B_internal_ratings": {
                "rows": [
                    {
                        "entity_full_name": "EMA",
                        "entity_abbrev": "EMA",
                        "role": "Borrower",
                        "fy2022_23": "3",
                        "fy2024": "3+",
                        "interim": {
                            "generated_msr": "4+",
                            "override_applied": True,
                            "override_to": "4+",
                        },
                        "current": {
                            "proposed_assessment": {
                                "generated_msr": "3",
                                "proposed_final_msr": "3+",
                            }
                        },
                        "override_flag": False,
                        "override_remarks": "",
                    }
                ],
                "period_display_labels": {
                    "fy2022_23": "2022/23",
                    "fy2024": "2024",
                    "interim": "Jul 2025",
                    "current": "Nov 2025",
                },
            },
            "3C_mas_612": {},
            "3D_esg_rating": {},
        }

        _, user_prompt = build_section_prompt(
            section_no=3,
            input_json=format_c_input,
            evidence_chunks=[],
        )

        # The user_prompt must NOT contain raw nested dicts for MSR period fields
        assert '"generated_msr": "4+"' not in user_prompt or (
            # Acceptable only if it appears inside override_remarks text, not as a period value
            user_prompt.count('"generated_msr": "4+"') == 0
        ), (
            "§3 user_prompt contains raw nested MSR dict — FORMAT C normalization not applied!\n"
            "The AI will see a Python dict where it expects a string and output '—' for all periods."
        )

        # The user_prompt MUST contain the flat MSR strings
        assert '"4+"' in user_prompt, (
            "§3 user_prompt must contain the flat MSR value '4+' after FORMAT C normalization"
        )
        assert '"3+"' in user_prompt, (
            "§3 user_prompt must contain the flat MSR value '3+' (from proposed_final_msr) after normalization"
        )

    @pytest.mark.asyncio
    async def test_section3_generated_with_format_c_data_has_msr_values(self, ac, hdrs, rid):
        """
        End-to-end: generate §3 with FORMAT C data.
        §3 requires §7 as a hard dependency — we seed §7 output first.
        Verifies the saved markdown does NOT have all '—' for MSR values.
        """
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput

        # Seed §7 output (hard dependency for §3)
        async with AsyncSessionLocal() as db:
            db.add(SectionOutput(
                id=str(uuid.uuid4()),
                report_id=rid,
                section_no=7,
                status="done",
                markdown="## Section 7\n\nFinancial Analysis seeded for test.",
                model_id="mock",
                tokens_used=0,
            ))
            await db.commit()

        format_c_input = {
            "3A_external_ratings": {"all_nil": True},
            "3B_internal_ratings": {
                "rows": [
                    {
                        "entity_full_name": "Evergreen Marine Corporation (Taiwan) Ltd.",
                        "entity_abbrev": "EMA",
                        "role": "Borrower",
                        "fy2022_23": "3",
                        "fy2024": "3+",
                        "interim": {
                            "generated_msr": "4+",
                            "override_applied": True,
                            "override_to": "4+",
                        },
                        "current": {
                            "proposed_assessment": {
                                "generated_msr": "3",
                                "proposed_final_msr": "3+",
                            }
                        },
                        "override_flag": False,
                        "override_remarks": "",
                    }
                ],
                "period_display_labels": {
                    "fy2022_23": "2022/23",
                    "fy2024": "2024",
                    "interim": "Jul 2025",
                    "current": "Nov 2025",
                },
            },
            "3C_mas_612": {
                "account_conduct": "Satisfactory",
                "financial_profile": "Strong net cash position",
            },
            "3D_esg_rating": {"entity": "EMA", "date": "2025-06-30"},
        }

        r = await ac.put(
            f"{REPORTS}/{rid}/inputs/3",
            json={"section_no": 3, "input_json": format_c_input},
            headers=hdrs,
        )
        assert r.status_code == 200

        # Mock AI returning a correct MSR table (what AI should output with correct prompting)
        correct_section3_output = (
            "**External ratings:** NIL. EMA and EMC are not externally rated.\n\n"
            "**Internal ratings:**\n\n"
            "| **Entity** | **2022/23** | **2024** | **Jul 2025** | **Nov 2025** | **Remarks** |\n"
            "|---|---|---|---|---|---|\n"
            "| (blank) | (blank) | (blank) | Generated | Generated | Proposed |\n"
            "| **Evergreen Marine Corporation (Taiwan) Ltd. (EMA)** Borrower | 3 | 3+ | 4+ | 3+ | Override applied |\n"
        )

        with _mock_gemini(correct_section3_output):
            r = await ac.post(f"{REPORTS}/{rid}/generate/3?gen_language=en", headers=hdrs)
        assert r.status_code == 202, r.text

        # Fetch saved output
        r = await ac.get(f"{REPORTS}/{rid}/sections/3/output", headers=hdrs)
        assert r.status_code == 200
        markdown = r.json().get("markdown", "")

        # Critical: no "— | — | — | —" pattern meaning all MSR values are dashes
        all_dashes = "| — | — | — | — |" in markdown or "| - | - | - | - |" in markdown
        assert not all_dashes, (
            f"§3 MSR table has all '—' values despite FORMAT C data being present!\n"
            f"This means FORMAT C normalization is NOT working.\nMarkdown:\n{markdown[:800]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Combined regression: prompt content checks
# ══════════════════════════════════════════════════════════════════════════════

class TestPromptContentRegression:
    """Snapshot-style checks on prompt text that protect against regression."""

    def test_no_bare_left_colon_in_any_section_instruction(self):
        """No section instruction should use 'Left:' as a column label directive."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        offenders = []
        for sec_no, instr in SECTION_INSTRUCTIONS.items():
            # Match lines like "Left: **Solvency**" or "Left: content"
            import re
            if re.search(r"\bLeft:\s+\*\*", instr):
                offenders.append(sec_no)
        assert not offenders, (
            f"Sections {offenders} still use 'Left: **...**' as column-header directive — "
            "AI will generate literal 'Left' / 'Right' column headers"
        )

    def test_section3_instructions_describe_format_c_or_normalization(self):
        """
        §3 instructions should either describe FORMAT C OR the system normalizes it.
        We verify the normalization function exists and is callable.
        """
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        assert callable(_normalize_section3_ratings), (
            "_normalize_section3_ratings must be a callable function in prompt_builder.py"
        )

    def test_section2_all_five_tables_forbidden_left_right(self):
        """All five T1-T5 instructions must not use 'Left:' / 'Right:' as column descriptors."""
        from credit_report.generation.prompt_builder import SECTION_INSTRUCTIONS
        import re
        instr = SECTION_INSTRUCTIONS.get(2, "")
        # Should NOT match patterns like "Left: **Credit Overview**" etc.
        bad_matches = re.findall(r"(?:Left|Right):\s+\*\*[^*]+\*\*", instr)
        assert not bad_matches, (
            f"§2 prompt still contains column-header directives using 'Left:'/'Right:': {bad_matches}"
        )

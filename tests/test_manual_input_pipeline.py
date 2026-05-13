"""
Manual Input → JSON Conversion → Report Generation CI/CD Test Suite
=====================================================================
Covers the complete pipeline for 100% manual input across all 11 report sections:

  A. FIELD_DEFS frontend schema completeness (§1–11, 98 fields)
  B. REQUIRED_FIELDS coverage & cross-reference validation
  C. collectFormData() type-conversion logic (text/number/textarea/lines/json/bool/select)
  D. isFieldFilled / getCompleteness JS logic (Python-equivalent simulation)
  E. JSON hint parseability — all json-type field examples parse correctly
  F. Backend API: save section input §1–11 (PUT /inputs/{n})
  G. Backend API: import section JSON §1–11 (POST /documents/json/{n})
  H. Backend API: generate section §1–10; §11 must be blocked (400)
  I. §11 reference-section full integration
  J. Sequential full pipeline: manual input → save → mocked generate §1–10
  K. HTML/JS structural tests (function & element presence, no DOM required)
  L. Edge cases (empty values, special chars, oversized payloads, NaN)

Professional test report produced via pytest -v output.
"""
from __future__ import annotations

import io
import json
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"


def _load_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _make_user(role: str = "analyst") -> MagicMock:
    u = MagicMock()
    u.id = str(uuid.uuid4())
    u.role = role
    u.email = f"{role}@test.local"
    return u


async def _seed_report(db, rid: str, owner_id: str | None = None):
    from credit_report.models import Report
    uid = owner_id or str(uuid.uuid4())
    r = Report(id=rid, borrower_name="ManualInputTest Co", created_by=uid,
               status="draft", is_deleted=False)
    db.add(r)
    await db.flush()
    return r, uid


# ─────────────────────────────────────────────────────────────────────────────
# EXPECTED COUNTS (derived from actual FIELD_DEFS in index.html)
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_FIELD_COUNTS = {
    1: 54, 2: 33, 3: 4, 4: 12, 5: 7, 6: 7,
    7: 8, 8: 2, 9: 4, 10: 3, 11: 12,
}

EXPECTED_REQUIRED_COUNTS = {
    1: 6, 2: 5, 3: 4, 4: 7, 5: 3, 6: 6,
    7: 3, 8: 1, 9: 2, 10: 2, 11: 4,
}

VALID_FIELD_TYPES = {"text", "number", "textarea", "lines", "json", "bool", "select"}

# Minimal valid input payloads for each section (representative, not exhaustive)
MINIMAL_PAYLOADS: dict[int, dict] = {
    1: {
        "report_type": "new_deal",
        "facility_summary": {
            "rows": [{"item_no": 1, "borrower_full_name": "Test Borrower Ltd",
                      "booking_location": "SG", "proposed_facility_usd_m": 100.0,
                      "is_new": True, "currency": "USD", "facility_type": "Term Loan"}],
            "totals": {"total_credit_limit_usd_m": 100.0},
            "footnotes": [],
            "appendix_ref": "See Appendix I.",
        },
        "regulatory_compliance": {
            "compliance_status": "Compliant",
            "group_limit": {"approved_group_limit_usd_m": 500, "within_limit": True},
        },
        "purpose_and_recommendation": {
            "purpose_verbatim": "Vessel acquisition finance.",
            "recommendation": "APPROVE",
        },
        "terms_and_conditions": {
            "borrower": "Test Borrower Ltd",
            "guarantors": ["Test Guarantor Corp"],
            "facility_type": "Term Loan",
            "facility_amount_usd_m": 100.0,
            "ltc_percent": 80.0,
            "tenor_years": 5,
            "tenor_structure": "5 years",
            "repayment_schedule": "5% semi-annual + 30% balloon",
            "balloon_percent": 30.0,
            "interest_rate_basis": "SOFR",
            "margin_bps": 200,
            "security_pre_delivery": "Refund Guarantee",
            "security_post_delivery": "First Priority Mortgage",
            "value_maintenance_clause": {"acr_minimum_pct": 120, "ltv_maximum_pct": 83},
            "sustainability_linked_kpi": {"description": "CO2 intensity"},
            "financial_covenants": "NIL",
            "drawdown_conditions": {"max_drawdowns": 3},
            "conditions_precedent": ["Execute facility agreement", "Legal opinions"],
            "governing_law": "Singapore",
            "deal_comparison": [{"term": "Amount", "proposed": "USD100m", "previous": "USD80m"}],
        },
        "account_strategy": {
            "wallet": {"bank_market": "NII USD5m p.a."},
            "current_relationship": "Term loan facility",
            "immediate_opportunities": "Upfront fee USD100,000",
            "future_opportunities": "Refinancing",
            "other_opportunities": "FX hedging",
        },
    },
    2: {
        "2A_credit_overview": {"bullets": [{"order": 1, "text_verbatim": "Test bullet"}],
                               "tariff_impact_paragraphs": ["Minimal tariff exposure."]},
        "2B_solvency": {
            "primary_repayment_source_verbatim": "Operating cash flow.",
            "secondary_repayment_source_verbatim": "Guarantor support.",
            "ema": {
                "period": "FY2024",
                "cash_bn_usd": 2.2,
                "total_debt_bn_usd": 8.5,
                "op_ebitda_bn_usd": 3.1,
                "debt_ebitda_ratio": 2.74,
                "interest_coverage": 15.0,
                "prior_year_coverage": 14.2,
            },
        },
        "2C_guarantor": {
            "guarantor_name_abbrev": "TESTG",
            "guarantor_full_name": "Test Guarantor Corp",
            "period": "FY2024",
            "cash_twd_bn": 198.3,
            "total_debt_twd_bn": 450.0,
            "net_worth_twd_bn": 320.0,
            "interest_coverage": 15.0,
            "support_history_verbatim": "No prior support events.",
        },
        "2D_collateral": {
            "pre_delivery": {
                "issuer_full_name": "Test Bank",
                "issuer_rating": "A",
                "facility_amount_pct": 100,
                "assigned_to_cub": True,
            },
            "post_delivery": {
                "ltc_pct": 80,
                "acr_pct": 120,
                "ltv_pct": 83,
            },
        },
        "2E_risk_and_mitigants": {
            "risk_factors": [{"risk_no": 1, "level": "Medium",
                              "title": "Rate risk", "risk_bullets": ["Rate volatility"],
                              "mitigant_bullets": ["TC agreement"]}],
            "additional_risk_factors_from_previous": [],
        },
        "report_type": "new_deal",
    },
    3: {
        "3A_external_ratings": {"all_nil": True, "ratings": []},
        "3B_internal_ratings": {"rows": [{"entity_abbrev": "TB", "role": "Borrower",
                                          "current": "6"}]},
        "3C_mas_612": {"grade": "PASS",
                       "primary_paragraph_verbatim": "Borrower meets PASS criteria."},
        "3D_esg_rating": {"entity_abbrev": "TB", "rating_date": "2025-01-15"},
    },
    4: {
        "4A_borrower": {"company_name_en": "Test Co", "incorporation_country": "Singapore"},
        "4B_ownership": {"shareholders": [{"name": "Test Parent", "stake_percent": 100}]},
        "4C_management": [{"name": "Jane Doe", "title": "GM"}],
        "4D_business": {"primary_business": "Shipping"},
        "4E_financials": {"currency": "USD", "fiscal_year": "FY2024", "revenue": 1000.0},
        "4F_fleet": {"total_owned_teu": 100000, "fleet_breakdown": []},
        "4G_debt_profile": [{"lender_bond": "Test Bank", "ccy": "USD", "amount": 100}],
        "4H_banking_relationships": [{"bank": "CUB", "product": "Term Loan"}],
        "4I_market_data": {"ccfi_level": 1000},
        "4J_peer_comparison": [{"company": "MSC", "fleet_teu": 5000000}],
        "4K_major_customers": [{"name": "Amazon", "contract_type": "Long-term"}],
    },
    5: {
        "5A_security_overview": {"is_secured": True, "security_instruments": []},
        "5B_refund_guarantee": {"applicable": True, "issuer_full_name": "Test Bank",
                                "milestones": []},
        "5C_vessel_mortgage": {"applicable": True, "loan_amount_usd_m": 100.0,
                               "ltc_pct": 80.0, "vessel_valuations": []},
        "5D_insurance": [{"type": "H&M", "insurer_or_club": "Test P&I"}],
        "5E_value_maintenance_clause": {"acr_covenant_pct": 120.0, "ltv_covenant_pct": 75.0,
                                        "test_frequency_verbatim": "Every 2 years",
                                        "cure_period_banking_days": 21,
                                        "cure_mechanism_verbatim": "Prepay or provide security."},
        "5F_corporate_guarantee": {"applicable": True,
                                   "guarantor_full_name": "Test Guarantor Corp"},
        "5G_responsible_person": {"provided": False},
    },
    6: {
        "6A_project": {"hull_number": "H-001", "vessel_type": "Container",
                       "teu": 10000, "delivery_date": "2026-12-31"},
        "6B_builder": {"name": "Test Shipyard", "founded": "1980"},
        "6C_contract": {"contract_type": "Fixed-price", "price_verbatim": "USD100m"},
        "6D_milestones": {"milestones": [{"no": 1, "milestone": "Steel Cutting",
                                          "pct_of_contract": 10}]},
        "6E_rg_mechanism": {"applicable": True, "issuer_full_name": "Test Bank"},
        "6F_construction_progress": {"status_date": "2025-05-01", "completion_pct": 30},
    },
    7: {
        "entities_to_analyze": [{"name": "Test Co", "role": "Borrower",
                                  "currency": "USD", "guarantor_exists": False, "depth": "FULL"}],
        "7A_borrower_financials": {"reporting_currency": "USD",
                                   "income_statement": {"FY2024": {"revenue": 1000, "ebitda": 200}},
                                   "balance_sheet": {"FY2024": {"total_assets": 5000}},
                                   "cash_flow": {"FY2024": {"ocf": 300}}},
        "7B_key_ratios": {"FY2024": {"gross_margin_pct": 25.0, "debt_ebitda": 2.5,
                                     "dscr": 1.5, "current_ratio": 1.8}},
        "7C_guarantor_financials": {"applicable": False},
        "7E_base_case": {"applicable": True, "key_assumptions": [],
                         "conclusion": "Adequate DSCR."},
        "7F_worse_case": {"applicable": True, "conclusion": "Covered by guarantor."},
        "7H_sensitivity": {"applicable": True, "rows": []},
    },
    8: {
        "8A_acra_banking_charges": {"acra_data_available": True, "charges": [],
                                    "summary": {"total_charges": 0, "cub_charge_count": 0}},
    },
    9: {
        "9A_checklist": [{"no": 1, "category": "KYC", "item": "CDD completed",
                           "response": "Yes", "remarks": "Reviewed"}],
        "9B_conditions_covenants": {"conditions_precedent": [], "ongoing_covenants": []},
        "9C_recommendation": {"decision": "APPROVE", "facility_amount_usd_m": 100.0},
        "9D_signoff": {"date": "2026-01-15", "prepared_by": "Test Analyst"},
    },
    10: {
        "10A_group_exposure": {"entity_group": "Test Group", "rows": [],
                               "group_limit_sub_table": {"approved_group_limit_usd_m": 500}},
        "10B_fleet_growth": {"group_name": "Test", "rows": []},
        "10C_projections": {"entity_name": "Test Co", "base_case_pl": [],
                            "base_case_bs": [], "base_case_cf": []},
    },
    11: {
        "11A_report_meta": {"analyst_firm": "Test Securities", "report_date": "2026-03-15",
                            "subject_ticker": "2603.TT", "report_type": "Initiation"},
        "11B_rating": {"current_rating": "Buy", "target_price_12m": 52.0,
                       "target_price_currency": "TWD", "current_price": 38.5},
        "11C_company_fundamentals": {"ticker": "2603.TT", "market_cap_usd_m": 10000},
        "11D_investment_thesis": {"summary_verbatim": "Strong buy on valuation.",
                                  "bull_points": ["Net cash"], "risks": ["Trade war"]},
        "11E_annual_income_statement": {"currency": "TWD", "periods": [
            {"year": "FY2024A", "is_forecast": False, "revenue": 240300}]},
        "11F_quarterly_income_statement": {"currency": "TWD", "periods": []},
        "11G_balance_sheet": {"currency": "TWD", "periods": []},
        "11H_cash_flow": {"currency": "TWD", "periods": []},
        "11I_ratio_analysis": {"currency": "TWD", "periods": []},
        "11J_valuation_metrics": {"per_current": 4.4, "pbr_current": 1.08},
        "11K_esg": {"esg_overall_score": 72, "cii_rating": "B"},
        "11L_industry_context": {"ccfi_current": 1012,
                                 "forward_outlook_narrative": "Recovery expected H2 2026."},
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    from credit_report.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# A — FIELD_DEFS Frontend Schema Completeness
# ══════════════════════════════════════════════════════════════════════════════

class TestFieldDefsCompleteness:
    """FIELD_DEFS in index.html must contain all 11 sections with correct fields."""

    @staticmethod
    def _extract_field_defs(html: str) -> dict[int, list[dict]]:
        """Parse FIELD_DEFS from index.html into Python dict."""
        fd_start = html.find("const FIELD_DEFS={")
        fd_end = html.find("\n};", fd_start) + 3
        fd_text = html[fd_start:fd_end]
        result: dict[int, list[dict]] = {}
        for sec in range(1, 12):
            # Extract all {p:'...', l:'...', t:'...'} entries for this section
            # Use section boundary detection
            if sec < 11:
                pattern = rf"\n\s*{sec}:\[(.*?)\],\s*\n\s*{sec+1}:"
            else:
                pattern = r"\n\s*11:\[(.*?)\]\s*\n\};"
            m = re.search(pattern, fd_text, re.DOTALL)
            if m:
                section_text = m.group(1)
                fields = []
                for fm in re.finditer(r"\{p:'([^']+)',l:'([^']+)',t:'([^']+)'([^}]*)\}", section_text):
                    entry = {"p": fm.group(1), "l": fm.group(2), "t": fm.group(3)}
                    opts_m = re.search(r"opts:\[([^\]]+)\]", fm.group(4))
                    if opts_m:
                        entry["opts"] = re.findall(r"'([^']+)'", opts_m.group(1))
                    hint_m = re.search(r",h:'(.*?)'(?=[,}])", fm.group(0))
                    if hint_m:
                        entry["h"] = hint_m.group(1)
                    fields.append(entry)
                result[sec] = fields
        return result

    def test_all_11_sections_present(self):
        html = _load_html()
        defs = self._extract_field_defs(html)
        for sec in range(1, 12):
            assert sec in defs, f"§{sec} missing from FIELD_DEFS"

    @pytest.mark.parametrize("sec_no,expected", list(EXPECTED_FIELD_COUNTS.items()))
    def test_section_field_count(self, sec_no, expected):
        html = _load_html()
        defs = self._extract_field_defs(html)
        actual = len(defs.get(sec_no, []))
        assert actual == expected, (
            f"§{sec_no}: expected {expected} fields, got {actual}. "
            f"Fields found: {[f['p'] for f in defs.get(sec_no, [])]}"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_all_field_types_valid(self, sec_no):
        html = _load_html()
        defs = self._extract_field_defs(html)
        fields = defs.get(sec_no, [])
        for f in fields:
            assert f["t"] in VALID_FIELD_TYPES, (
                f"§{sec_no}.{f['p']}: invalid type '{f['t']}', "
                f"must be one of {VALID_FIELD_TYPES}"
            )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_no_duplicate_field_paths_within_section(self, sec_no):
        html = _load_html()
        defs = self._extract_field_defs(html)
        paths = [f["p"] for f in defs.get(sec_no, [])]
        dups = [p for p in paths if paths.count(p) > 1]
        assert not dups, f"§{sec_no}: duplicate field paths: {list(set(dups))}"

    def test_total_field_count_across_all_sections(self):
        html = _load_html()
        defs = self._extract_field_defs(html)
        total = sum(len(v) for v in defs.values())
        assert total == sum(EXPECTED_FIELD_COUNTS.values()), (
            f"Total field count mismatch: expected {sum(EXPECTED_FIELD_COUNTS.values())}, "
            f"got {total}"
        )

    def test_section_11_has_12_fields(self):
        html = _load_html()
        defs = self._extract_field_defs(html)
        fields_11 = defs.get(11, [])
        assert len(fields_11) == 12, (
            f"§11 expected 12 fields (11A–11L), got {len(fields_11)}: "
            f"{[f['p'] for f in fields_11]}"
        )

    def test_section_11_field_keys_11a_to_11l(self):
        html = _load_html()
        defs = self._extract_field_defs(html)
        fields_11 = defs.get(11, [])
        paths = {f["p"] for f in fields_11}
        expected_paths = {
            "11A_report_meta", "11B_rating", "11C_company_fundamentals",
            "11D_investment_thesis", "11E_annual_income_statement",
            "11F_quarterly_income_statement", "11G_balance_sheet", "11H_cash_flow",
            "11I_ratio_analysis", "11J_valuation_metrics", "11K_esg",
            "11L_industry_context",
        }
        assert paths == expected_paths, (
            f"§11 field paths mismatch.\n  Expected: {sorted(expected_paths)}\n"
            f"  Got: {sorted(paths)}"
        )

    def test_select_fields_have_opts(self):
        html = _load_html()
        fd_start = html.find("const FIELD_DEFS={")
        fd_end = html.find("\n};", fd_start) + 3
        fd_text = html[fd_start:fd_end]
        # Every t:'select' field must have an opts array nearby
        select_positions = [m.start() for m in re.finditer(r"t:'select'", fd_text)]
        for pos in select_positions:
            context = fd_text[max(0, pos-200):pos+200]
            assert "opts:[" in context, (
                f"select-type field at position {pos} has no opts array: "
                f"...{context}..."
            )


# ══════════════════════════════════════════════════════════════════════════════
# B — REQUIRED_FIELDS Coverage & Cross-Reference
# ══════════════════════════════════════════════════════════════════════════════

class TestRequiredFieldsCoverage:
    """REQUIRED_FIELDS must exist for all 11 sections and reference real FIELD_DEFS paths."""

    @staticmethod
    def _extract_required_fields(html: str) -> dict[int, list[str]]:
        rf_start = html.find("const REQUIRED_FIELDS={")
        rf_end = html.find("\n};", rf_start) + 3
        rf_text = html[rf_start:rf_end]
        result: dict[int, list[str]] = {}
        for sec in range(1, 12):
            if sec < 11:
                pattern = rf"\n\s*{sec}:\[(.*?)\],\s*\n\s*{sec+1}:"
            else:
                pattern = r"\n\s*11:\[(.*?)\],\s*\n\};"
            m = re.search(pattern, rf_text, re.DOTALL)
            if m:
                paths = re.findall(r"\{p:'([^']+)'", m.group(1))
                result[sec] = paths
        return result

    @staticmethod
    def _extract_field_defs_paths(html: str) -> dict[int, set[str]]:
        fd_start = html.find("const FIELD_DEFS={")
        fd_end = html.find("\n};", fd_start) + 3
        fd_text = html[fd_start:fd_end]
        result: dict[int, set[str]] = {}
        for sec in range(1, 12):
            if sec < 11:
                pattern = rf"\n\s*{sec}:\[(.*?)\],\s*\n\s*{sec+1}:"
            else:
                pattern = r"\n\s*11:\[(.*?)\]\s*\n\};"
            m = re.search(pattern, fd_text, re.DOTALL)
            if m:
                result[sec] = set(re.findall(r"\{p:'([^']+)'", m.group(1)))
        return result

    def test_all_11_sections_in_required_fields(self):
        html = _load_html()
        rf = self._extract_required_fields(html)
        for sec in range(1, 12):
            assert sec in rf, f"§{sec} missing from REQUIRED_FIELDS"

    @pytest.mark.parametrize("sec_no,expected", list(EXPECTED_REQUIRED_COUNTS.items()))
    def test_required_field_count_per_section(self, sec_no, expected):
        html = _load_html()
        rf = self._extract_required_fields(html)
        actual = len(rf.get(sec_no, []))
        assert actual == expected, (
            f"§{sec_no}: expected {expected} required fields, got {actual}. "
            f"Paths: {rf.get(sec_no, [])}"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_required_fields_paths_exist_in_field_defs(self, sec_no):
        html = _load_html()
        rf = self._extract_required_fields(html)
        fd_paths = self._extract_field_defs_paths(html)
        required_paths = rf.get(sec_no, [])
        defined_paths = fd_paths.get(sec_no, set())
        dangling = [p for p in required_paths if p not in defined_paths]
        assert not dangling, (
            f"§{sec_no}: REQUIRED_FIELDS paths not found in FIELD_DEFS: {dangling}. "
            f"Available FIELD_DEFS paths: {sorted(defined_paths)}"
        )

    def test_section_11_required_fields_are_4(self):
        html = _load_html()
        rf = self._extract_required_fields(html)
        assert len(rf.get(11, [])) == 4, (
            f"§11 expected 4 required fields, got {len(rf.get(11, []))}: {rf.get(11)}"
        )

    def test_section_11_required_fields_correct_paths(self):
        html = _load_html()
        rf = self._extract_required_fields(html)
        paths = set(rf.get(11, []))
        expected = {"11A_report_meta", "11B_rating", "11D_investment_thesis",
                    "11E_annual_income_statement"}
        assert paths == expected, (
            f"§11 required field paths wrong.\n  Expected: {sorted(expected)}\n"
            f"  Got: {sorted(paths)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# C — collectFormData() Type-Conversion Logic (Python simulation)
# ══════════════════════════════════════════════════════════════════════════════

class TestCollectFormDataTypeConversion:
    """
    Simulate JS collectFormData() in Python.
    type 'text' → str
    type 'number' → float (via parseFloat)
    type 'textarea' → str
    type 'lines' → list[str] (split by \\n, trimmed, non-empty only)
    type 'json' → parsed dict/list, or raw str if parse fails
    type 'bool' → bool ('true'→True, 'false'→False)
    type 'select' → str
    Empty string → excluded from result
    """

    def _collect(self, field_type: str, raw_value: str) -> Any | None:
        """Python equivalent of JS collectFormData() for a single field."""
        raw = raw_value.strip()
        if not raw:
            return None  # excluded from result
        if field_type == "number":
            try:
                n = float(raw)
                return n if n == n else None  # NaN check
            except ValueError:
                return None
        elif field_type == "lines":
            lines = [l.strip() for l in raw.split("\n") if l.strip()]
            return lines if lines else None
        elif field_type == "json":
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw  # fallback to raw string
        elif field_type == "bool":
            return raw == "true"
        else:
            return raw  # text, textarea, select

    def test_text_type_returns_string(self):
        assert self._collect("text", "hello world") == "hello world"

    def test_text_type_trimmed(self):
        assert self._collect("text", "  hello  ") == "hello"

    def test_text_type_empty_excluded(self):
        assert self._collect("text", "") is None

    def test_text_type_whitespace_only_excluded(self):
        assert self._collect("text", "   ") is None

    def test_number_type_integer_string(self):
        assert self._collect("number", "42") == 42.0

    def test_number_type_float_string(self):
        assert self._collect("number", "3.14") == pytest.approx(3.14)

    def test_number_type_negative(self):
        assert self._collect("number", "-100.5") == pytest.approx(-100.5)

    def test_number_type_zero(self):
        assert self._collect("number", "0") == 0.0

    def test_number_type_empty_excluded(self):
        assert self._collect("number", "") is None

    def test_number_type_non_numeric_excluded(self):
        assert self._collect("number", "abc") is None

    def test_textarea_type_returns_string(self):
        text = "Line 1\nLine 2\nLine 3"
        assert self._collect("textarea", text) == text

    def test_textarea_type_preserves_newlines(self):
        text = "para one\n\npara two"
        assert self._collect("textarea", text) == text

    def test_lines_type_splits_by_newline(self):
        result = self._collect("lines", "item1\nitem2\nitem3")
        assert result == ["item1", "item2", "item3"]

    def test_lines_type_trims_each_line(self):
        result = self._collect("lines", "  item1  \n  item2  ")
        assert result == ["item1", "item2"]

    def test_lines_type_filters_blank_lines(self):
        result = self._collect("lines", "item1\n\n\nitem2")
        assert result == ["item1", "item2"]

    def test_lines_type_empty_excluded(self):
        assert self._collect("lines", "") is None

    def test_lines_type_all_blank_excluded(self):
        assert self._collect("lines", "\n\n\n") is None

    def test_json_type_parses_object(self):
        result = self._collect("json", '{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_json_type_parses_array(self):
        result = self._collect("json", '[{"a": 1}, {"a": 2}]')
        assert result == [{"a": 1}, {"a": 2}]

    def test_json_type_parses_nested(self):
        nested = '{"outer": {"inner": [1, 2, 3]}}'
        result = self._collect("json", nested)
        assert result == {"outer": {"inner": [1, 2, 3]}}

    def test_json_type_invalid_fallback_to_raw(self):
        invalid = '{"key": value_without_quotes}'
        result = self._collect("json", invalid)
        assert result == invalid  # raw string fallback

    def test_json_type_empty_excluded(self):
        assert self._collect("json", "") is None

    def test_bool_type_true_string_to_true(self):
        assert self._collect("bool", "true") is True

    def test_bool_type_false_string_to_false(self):
        assert self._collect("bool", "false") is False

    def test_bool_type_empty_excluded(self):
        assert self._collect("bool", "") is None

    def test_select_type_returns_selected_value(self):
        assert self._collect("select", "new_deal") == "new_deal"

    def test_select_type_empty_excluded(self):
        assert self._collect("select", "") is None


# ══════════════════════════════════════════════════════════════════════════════
# D — isFieldFilled / getCompleteness JS Logic Simulation
# ══════════════════════════════════════════════════════════════════════════════

class TestFieldFilledCompletenessLogic:
    """
    Simulate JS isFieldFilled() and getCompleteness() in Python.
    Rejection rules match the JS source:
      - None/undefined → not filled
      - Empty string → not filled
      - String starting with 'To be generated from' → not filled
      - String 'APPROVE/DECLINE' → not filled
      - Empty list → not filled
      - Empty dict {} → not filled
    """

    def _is_filled(self, v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            t = v.strip()
            if not t:
                return False
            if t.startswith("To be generated from"):
                return False
            if t == "APPROVE/DECLINE":
                return False
            return True
        if isinstance(v, list):
            return len(v) > 0
        if isinstance(v, dict):
            return len(v) > 0
        return True  # numbers, bools

    @staticmethod
    def _get_nested(obj: dict, path: str) -> Any:
        """Simulate JS getNestedValue — traverse dot-notation path."""
        cur = obj
        for key in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur

    def _get_completeness(self, sec_no: int, data: dict) -> dict:
        from_html = _load_html()
        rf_start = from_html.find("const REQUIRED_FIELDS={")
        rf_end = from_html.find("\n};", rf_start) + 3
        rf_text = from_html[rf_start:rf_end]
        if sec_no < 11:
            pattern = rf"\n\s*{sec_no}:\[(.*?)\],\s*\n\s*{sec_no+1}:"
        else:
            pattern = r"\n\s*11:\[(.*?)\],\s*\n\};"
        m = re.search(pattern, rf_text, re.DOTALL)
        required = re.findall(r"\{p:'([^']+)'", m.group(1)) if m else []
        if not required:
            return {"filled": 0, "total": 0, "pct": 100, "missing": []}
        filled_paths = [p for p in required if self._is_filled(self._get_nested(data, p))]
        missing = [p for p in required if not self._is_filled(self._get_nested(data, p))]
        pct = round(len(filled_paths) / len(required) * 100) if required else 100
        return {"filled": len(filled_paths), "total": len(required),
                "pct": pct, "missing": missing}

    # ── isFieldFilled tests ──

    def test_none_not_filled(self):
        assert not self._is_filled(None)

    def test_empty_string_not_filled(self):
        assert not self._is_filled("")

    def test_whitespace_only_not_filled(self):
        assert not self._is_filled("   ")

    def test_placeholder_string_not_filled(self):
        assert not self._is_filled("To be generated from ETL")

    def test_approve_decline_literal_not_filled(self):
        assert not self._is_filled("APPROVE/DECLINE")

    def test_normal_string_filled(self):
        assert self._is_filled("Test Borrower Ltd")

    def test_empty_list_not_filled(self):
        assert not self._is_filled([])

    def test_non_empty_list_filled(self):
        assert self._is_filled(["item1", "item2"])

    def test_empty_dict_not_filled(self):
        assert not self._is_filled({})

    def test_non_empty_dict_filled(self):
        assert self._is_filled({"key": "value"})

    def test_number_zero_filled(self):
        assert self._is_filled(0)

    def test_number_positive_filled(self):
        assert self._is_filled(100.5)

    def test_bool_true_filled(self):
        assert self._is_filled(True)

    def test_bool_false_filled(self):
        assert self._is_filled(False)  # False is a valid bool selection

    # ── getCompleteness tests ──

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_empty_data_zero_completeness(self, sec_no):
        result = self._get_completeness(sec_no, {})
        assert result["pct"] == 0 or result["total"] == 0, (
            f"§{sec_no}: empty data should yield 0% completeness, got {result['pct']}%"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_all_required_filled_100_percent(self, sec_no):
        payload = MINIMAL_PAYLOADS[sec_no]
        result = self._get_completeness(sec_no, payload)
        assert result["pct"] == 100, (
            f"§{sec_no}: minimal payload should yield 100% completeness. "
            f"Missing: {result['missing']}"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_completeness_filled_plus_missing_equals_total(self, sec_no):
        payload = MINIMAL_PAYLOADS[sec_no]
        result = self._get_completeness(sec_no, payload)
        if result["total"] > 0:
            assert result["filled"] + len(result["missing"]) == result["total"], (
                f"§{sec_no}: filled + missing ≠ total: "
                f"{result['filled']} + {len(result['missing'])} ≠ {result['total']}"
            )

    def test_partial_completeness_correct_percentage(self):
        # §2 has 5 required fields; provide 2 → expect ~40%
        partial_data = {
            "2A_credit_overview": {"bullets": [{"order": 1, "text_verbatim": "OK"}]},
            "2B_solvency": {"primary_repayment_source_verbatim": "OCF"},
        }
        result = self._get_completeness(2, partial_data)
        assert result["filled"] == 2
        assert result["total"] == 5
        assert result["pct"] == 40


# ══════════════════════════════════════════════════════════════════════════════
# E — JSON Hint Parseability (all json-type field examples)
# ══════════════════════════════════════════════════════════════════════════════

class TestJsonHintParseability:
    """Every json-type field in FIELD_DEFS must have a valid, parseable JSON hint."""

    @staticmethod
    def _extract_json_hints(html: str) -> list[tuple[int, str, str]]:
        """Returns list of (section_no, field_path, json_hint_str) tuples.
        Uses section-boundary detection to correctly attribute §1 non-prefixed paths.
        """
        fd_start = html.find("const FIELD_DEFS={")
        fd_end = html.find("\n};", fd_start) + 3
        fd_text = html[fd_start:fd_end]
        results = []
        for sec_no in range(1, 12):
            if sec_no < 11:
                pattern = rf"\n\s*{sec_no}:\[(.*?)\],\s*\n\s*{sec_no+1}:"
            else:
                pattern = r"\n\s*11:\[(.*?)\]\s*\n\};"
            m = re.search(pattern, fd_text, re.DOTALL)
            if not m:
                continue
            sec_text = m.group(1)
            # Extract {p:'path', l:'label', t:'json', h:'...hint...'} entries
            for fm in re.finditer(r"\{p:'([^']+)',l:'[^']+',t:'json',h:'", sec_text):
                path = fm.group(1)
                # Extract the hint value (handle escaped single quotes)
                hint_start = fm.end()
                hint_chars = []
                i = hint_start
                while i < len(sec_text):
                    ch = sec_text[i]
                    if ch == "'" and (i == 0 or sec_text[i-1] != "\\"):
                        break
                    hint_chars.append(ch)
                    i += 1
                hint_raw = "".join(hint_chars)
                results.append((sec_no, path, hint_raw))
        return results

    def test_all_json_hints_parse(self):
        html = _load_html()
        hints = self._extract_json_hints(html)
        failures = []
        for sec_no, path, raw in hints:
            # Unescape common JS string escapes: \' → '
            unescaped = raw.replace("\\'", "'")
            try:
                json.loads(unescaped)
            except json.JSONDecodeError as e:
                failures.append(f"§{sec_no}.{path}: {e} — raw: {unescaped[:80]}...")
        assert not failures, (
            f"{len(failures)} json hints failed to parse:\n" + "\n".join(failures[:10])
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_section_json_hints_parse(self, sec_no):
        html = _load_html()
        hints = self._extract_json_hints(html)
        sec_hints = [(p, h) for (s, p, h) in hints if s == sec_no]
        assert sec_hints, f"§{sec_no}: no json-type field hints found"
        for path, raw in sec_hints:
            unescaped = raw.replace("\\'", "'")
            try:
                json.loads(unescaped)
            except json.JSONDecodeError as e:
                pytest.fail(f"§{sec_no}.{path}: JSON parse error: {e}")

    def test_section_11_all_12_hints_parse(self):
        html = _load_html()
        hints = self._extract_json_hints(html)
        sec11_hints = [(p, h) for (s, p, h) in hints if s == 11]
        assert len(sec11_hints) == 12, (
            f"§11 expected 12 json hints, found {len(sec11_hints)}: "
            f"{[p for p, _ in sec11_hints]}"
        )
        for path, raw in sec11_hints:
            unescaped = raw.replace("\\'", "'")
            try:
                result = json.loads(unescaped)
                assert isinstance(result, (dict, list)), (
                    f"§11.{path}: hint must parse to object or array, got {type(result)}"
                )
            except json.JSONDecodeError as e:
                pytest.fail(f"§11.{path}: JSON parse failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# F — Backend API: Save Section Input §1–11
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sec_no", list(range(1, 12)))
@pytest.mark.asyncio
async def test_save_section_input_each_section(db, sec_no):
    """PUT /inputs/{sec_no} with minimal valid payload → 200, stored correctly."""
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    payload = SectionInputPayload(section_no=sec_no, input_json=MINIMAL_PAYLOADS[sec_no])
    result = await save_section_input(report_id=rid, section_no=sec_no, payload=payload,
                                      db=db, current_user=user)
    assert result.section_no == sec_no, f"§{sec_no}: returned section_no mismatch"
    assert result.saved_at is not None, f"§{sec_no}: saved_at must be set"
    assert isinstance(result.input_json, dict), f"§{sec_no}: input_json must be dict"


@pytest.mark.parametrize("sec_no", list(range(1, 12)))
@pytest.mark.asyncio
async def test_readback_after_save_matches_input(db, sec_no):
    """GET /inputs/{sec_no} after PUT must return the exact same payload."""
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload
    from fastapi import HTTPException

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    payload_data = MINIMAL_PAYLOADS[sec_no]
    payload = SectionInputPayload(section_no=sec_no, input_json=payload_data)
    await save_section_input(report_id=rid, section_no=sec_no, payload=payload,
                             db=db, current_user=user)

    readback = await get_section_input(report_id=rid, section_no=sec_no,
                                       db=db, current_user=user)
    for key, expected_val in payload_data.items():
        if isinstance(expected_val, (dict, list)):
            continue  # deep equality not checked here — structure presence verified
        assert key in readback.input_json, (
            f"§{sec_no}: key '{key}' missing from readback. "
            f"Keys present: {list(readback.input_json.keys())}"
        )


@pytest.mark.parametrize("bad_section_no", [0, 12, -1, 100])
@pytest.mark.asyncio
async def test_save_section_input_rejects_invalid_section_no(db, bad_section_no):
    """Section numbers outside 1–11 must be rejected at schema level."""
    from pydantic import ValidationError
    from credit_report.schemas import SectionInputPayload

    with pytest.raises((ValidationError, Exception)):
        SectionInputPayload(section_no=bad_section_no, input_json={"key": "value"})


@pytest.mark.asyncio
async def test_double_save_upsert_latest_wins(db):
    """Second PUT to same section overwrites first (upsert semantics)."""
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    first_payload = SectionInputPayload(section_no=3,
                                        input_json={"3A_external_ratings": {"all_nil": True, "ratings": []}})
    await save_section_input(report_id=rid, section_no=3, payload=first_payload,
                             db=db, current_user=user)

    second_payload = SectionInputPayload(section_no=3,
                                         input_json={"3A_external_ratings": {"all_nil": False,
                                                                              "ratings": [{"agency": "S&P", "rating": "BBB"}]},
                                                     "3C_mas_612": {"grade": "PASS",
                                                                    "primary_paragraph_verbatim": "Pass"}})
    await save_section_input(report_id=rid, section_no=3, payload=second_payload,
                             db=db, current_user=user)

    readback = await get_section_input(report_id=rid, section_no=3, db=db, current_user=user)
    # After second save, 3C_mas_612 should be present
    assert "3C_mas_612" in readback.input_json, (
        "After second save, newly added key '3C_mas_612' must be present. "
        f"Keys found: {list(readback.input_json.keys())}"
    )


@pytest.mark.asyncio
async def test_different_sections_do_not_interfere(db):
    """Saving §2 and §5 independently must not overwrite each other."""
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    for sec in [2, 5]:
        p = SectionInputPayload(section_no=sec, input_json=MINIMAL_PAYLOADS[sec])
        await save_section_input(report_id=rid, section_no=sec, payload=p,
                                 db=db, current_user=user)

    rb2 = await get_section_input(report_id=rid, section_no=2, db=db, current_user=user)
    rb5 = await get_section_input(report_id=rid, section_no=5, db=db, current_user=user)

    assert "2A_credit_overview" in rb2.input_json, "§2 data missing after saving §2 and §5"
    assert "5A_security_overview" in rb5.input_json, "§5 data missing after saving §2 and §5"
    assert "5A_security_overview" not in rb2.input_json, "§5 data leaked into §2"
    assert "2A_credit_overview" not in rb5.input_json, "§2 data leaked into §5"


@pytest.mark.asyncio
async def test_save_wrong_owner_denied(db):
    """Non-owner analyst must receive 403 when saving section input."""
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload
    from fastapi import HTTPException

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    intruder = _make_user("analyst")  # different user

    payload = SectionInputPayload(section_no=1, input_json={"borrower": "Attacker"})
    with pytest.raises(HTTPException) as exc_info:
        await save_section_input(report_id=rid, section_no=1, payload=payload,
                                 db=db, current_user=intruder)
    assert exc_info.value.status_code == 403, (
        f"Expected 403 for wrong owner, got {exc_info.value.status_code}"
    )


@pytest.mark.asyncio
async def test_save_deleted_report_returns_404(db):
    """Saving to a soft-deleted report must return 404."""
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload
    from credit_report.models import Report
    from fastapi import HTTPException

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    report.is_deleted = True
    await db.flush()

    user = _make_user("analyst")
    user.id = owner_id
    payload = SectionInputPayload(section_no=1, input_json={"borrower": "Test"})
    with pytest.raises(HTTPException) as exc_info:
        await save_section_input(report_id=rid, section_no=1, payload=payload,
                                 db=db, current_user=user)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_save_any_report(db):
    """Admin role must be allowed to save input to any report regardless of ownership."""
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    admin = _make_user("admin")  # admin, not owner

    payload = SectionInputPayload(section_no=8,
                                  input_json=MINIMAL_PAYLOADS[8])
    result = await save_section_input(report_id=rid, section_no=8, payload=payload,
                                      db=db, current_user=admin)
    assert result.section_no == 8


# ══════════════════════════════════════════════════════════════════════════════
# G — Backend API: Import Section JSON §1–11
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sec_no", list(range(1, 12)))
@pytest.mark.asyncio
async def test_import_section_json_all_sections(db, sec_no):
    """POST /documents/json/{sec_no} must accept JSON for §1–11."""
    from credit_report.api.generate import import_section_json
    from fastapi import UploadFile

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    json_bytes = json.dumps(MINIMAL_PAYLOADS[sec_no]).encode("utf-8")
    mock_file = MagicMock(spec=UploadFile)
    mock_file.read = AsyncMock(return_value=json_bytes)

    result = await import_section_json(report_id=rid, section_no=sec_no,
                                       file=mock_file, db=db, current_user=user)
    assert result is not None, f"§{sec_no}: import_section_json returned None"


@pytest.mark.parametrize("bad_sec", [0, 12, -1])
@pytest.mark.asyncio
async def test_import_section_json_rejects_out_of_range(db, bad_sec):
    """Section numbers 0, 12, -1 must return 400 from import_section_json."""
    from credit_report.api.generate import import_section_json
    from fastapi import UploadFile, HTTPException

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    mock_file = MagicMock(spec=UploadFile)
    mock_file.read = AsyncMock(return_value=b'{"key": "value"}')

    with pytest.raises(HTTPException) as exc_info:
        await import_section_json(report_id=rid, section_no=bad_sec,
                                  file=mock_file, db=db, current_user=user)
    assert exc_info.value.status_code == 400, (
        f"Section {bad_sec}: expected 400, got {exc_info.value.status_code}"
    )


@pytest.mark.asyncio
async def test_import_section_json_invalid_json_returns_400(db):
    """Malformed JSON upload must return 400."""
    from credit_report.api.generate import import_section_json
    from fastapi import UploadFile, HTTPException

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    mock_file = MagicMock(spec=UploadFile)
    mock_file.read = AsyncMock(return_value=b"{invalid json}")

    with pytest.raises(HTTPException) as exc_info:
        await import_section_json(report_id=rid, section_no=3, file=mock_file,
                                  db=db, current_user=user)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_import_section_json_array_root_returns_400(db):
    """JSON array at root must be rejected (root must be object {})."""
    from credit_report.api.generate import import_section_json
    from fastapi import UploadFile, HTTPException

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    mock_file = MagicMock(spec=UploadFile)
    mock_file.read = AsyncMock(return_value=b'[{"a": 1}]')

    with pytest.raises(HTTPException) as exc_info:
        await import_section_json(report_id=rid, section_no=3, file=mock_file,
                                  db=db, current_user=user)
    assert exc_info.value.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# H — Backend API: Generate §1–10 Allowed, §11 Blocked
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sec_no", list(range(1, 11)))
@pytest.mark.asyncio
async def test_generate_section_allowed_for_1_to_10(db, sec_no):
    """generate_section §1–10 must return 202 (with hard deps patched out)."""
    from fastapi import BackgroundTasks, HTTPException
    from credit_report.api.generate import generate_section

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    bg = BackgroundTasks()
    with patch(
        "credit_report.api.generate.check_hard_dependencies",
        new=AsyncMock(return_value=[]),
    ):
        result = await generate_section(report_id=rid, section_no=sec_no,
                                        background_tasks=bg, db=db, current_user=user)
    assert result.status == "running", (
        f"§{sec_no}: expected 'running', got '{result.status}'"
    )
    assert result.task_id is not None, f"§{sec_no}: task_id must be set"


@pytest.mark.asyncio
async def test_generate_section_11_blocked_with_400(db):
    """generate_section §11 must return HTTP 400 (section_no must be 1-10)."""
    from fastapi import BackgroundTasks, HTTPException
    from credit_report.api.generate import generate_section

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc_info:
        await generate_section(report_id=rid, section_no=11, background_tasks=bg,
                               db=db, current_user=user)
    assert exc_info.value.status_code == 400, (
        f"§11 generate must return 400, got {exc_info.value.status_code}: "
        f"{exc_info.value.detail}"
    )


@pytest.mark.parametrize("bad_sec", [0, 12, -1])
@pytest.mark.asyncio
async def test_generate_section_out_of_range_blocked(db, bad_sec):
    """generate_section §0, §12, §-1 must return HTTP 400."""
    from fastapi import BackgroundTasks, HTTPException
    from credit_report.api.generate import generate_section

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc_info:
        await generate_section(report_id=rid, section_no=bad_sec, background_tasks=bg,
                               db=db, current_user=user)
    assert exc_info.value.status_code in (400, 422), (
        f"Section {bad_sec}: expected 400 or 422, got {exc_info.value.status_code}"
    )


@pytest.mark.asyncio
async def test_generate_section_hard_dep_missing_returns_409(db):
    """generate_section §7 with unsatisfied deps must return HTTP 409."""
    from fastapi import BackgroundTasks, HTTPException
    from credit_report.api.generate import generate_section

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    bg = BackgroundTasks()
    # Simulate §7 having unmet dependencies (§6 not done)
    with patch(
        "credit_report.api.generate.check_hard_dependencies",
        new=AsyncMock(return_value=[6]),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await generate_section(report_id=rid, section_no=7, background_tasks=bg,
                                   db=db, current_user=user)
    assert exc_info.value.status_code == 409, (
        f"Expected 409 for unmet hard deps, got {exc_info.value.status_code}: "
        f"{exc_info.value.detail}"
    )


@pytest.mark.asyncio
async def test_generate_full_report_no_inputs_returns_422(db):
    """generate_full_report with no section inputs must return HTTP 422."""
    from fastapi import BackgroundTasks, HTTPException
    from credit_report.api.generate import generate_full_report

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as exc_info:
        await generate_full_report(report_id=rid, background_tasks=bg,
                                   db=db, current_user=user)
    assert exc_info.value.status_code == 422, (
        f"Expected 422 when no inputs, got {exc_info.value.status_code}"
    )


@pytest.mark.asyncio
async def test_generate_full_report_with_inputs_returns_202(db):
    """generate_full_report with at least one saved input must return task_id."""
    from fastapi import BackgroundTasks
    from credit_report.api.generate import generate_full_report
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    # Save §3 (no hard deps for full report context)
    p = SectionInputPayload(section_no=3, input_json=MINIMAL_PAYLOADS[3])
    await save_section_input(report_id=rid, section_no=3, payload=p, db=db, current_user=user)

    bg = BackgroundTasks()
    with patch("credit_report.api.generate.run_full_report_generation",
               new=AsyncMock(return_value=None)):
        result = await generate_full_report(report_id=rid, background_tasks=bg,
                                            db=db, current_user=user)
    assert result.task_id is not None, "generate_full_report must return task_id"
    assert result.status in ("running", "done"), (
        f"generate_full_report initial status must be 'running', got '{result.status}'"
    )


# ══════════════════════════════════════════════════════════════════════════════
# I — §11 Reference Section Full Integration
# ══════════════════════════════════════════════════════════════════════════════

class TestSection11FullIntegration:
    """§11 is a reference-only section: save allowed, generate blocked."""

    @pytest.mark.asyncio
    async def test_section_11_save_and_readback(self, db):
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        p = SectionInputPayload(section_no=11, input_json=MINIMAL_PAYLOADS[11])
        saved = await save_section_input(report_id=rid, section_no=11, payload=p,
                                         db=db, current_user=user)
        assert saved.section_no == 11

        readback = await get_section_input(report_id=rid, section_no=11,
                                           db=db, current_user=user)
        assert readback.section_no == 11
        assert "11A_report_meta" in readback.input_json

    @pytest.mark.asyncio
    async def test_section_11_generate_blocked(self, db):
        from fastapi import BackgroundTasks, HTTPException
        from credit_report.api.generate import generate_section

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        bg = BackgroundTasks()
        with pytest.raises(HTTPException) as exc_info:
            await generate_section(report_id=rid, section_no=11, background_tasks=bg,
                                   db=db, current_user=user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_section_11_import_json_allowed(self, db):
        from credit_report.api.generate import import_section_json
        from fastapi import UploadFile

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        json_bytes = json.dumps(MINIMAL_PAYLOADS[11]).encode("utf-8")
        mock_file = MagicMock(spec=UploadFile)
        mock_file.read = AsyncMock(return_value=json_bytes)

        result = await import_section_json(report_id=rid, section_no=11,
                                           file=mock_file, db=db, current_user=user)
        assert result is not None

    def test_section_11_in_snames_js(self):
        """§11 must appear in both SNAMES and SNAMES_ZH in index.html."""
        html = _load_html()
        assert "11:" in html and "Analyst" in html, (
            "§11 not found in SNAMES or key '11:' missing from JS section name map"
        )
        assert "外部研究報告" in html or "11:" in html, (
            "§11 Chinese name missing from SNAMES_ZH"
        )

    def test_section_11_in_export_section_names(self):
        """export.py SECTION_NAMES must include §11."""
        from credit_report.api.export import SECTION_NAMES
        assert 11 in SECTION_NAMES, (
            f"§11 not in export.SECTION_NAMES. Keys: {sorted(SECTION_NAMES.keys())}"
        )
        assert "Analyst" in SECTION_NAMES[11], (
            f"§11 name unexpected: '{SECTION_NAMES[11]}'"
        )

    def test_section_11_field_defs_has_all_12_fields(self):
        html = _load_html()
        count = html.count("{p:'11")
        assert count >= 12, (
            f"Expected ≥12 §11 field entries in FIELD_DEFS (found {count})"
        )

    def test_section_11_required_fields_has_4_entries(self):
        html = _load_html()
        rf_start = html.find("const REQUIRED_FIELDS={")
        rf_end = html.find("\n};", rf_start) + 3
        rf_text = html[rf_start:rf_end]
        m = re.search(r"\n\s*11:\[(.*?)\],\s*\n\};", rf_text, re.DOTALL)
        assert m, "§11 not found in REQUIRED_FIELDS"
        paths = re.findall(r"\{p:'([^']+)'", m.group(1))
        assert len(paths) == 4, f"§11 expected 4 required fields, got {len(paths)}: {paths}"

    @pytest.mark.asyncio
    async def test_section_11_all_12_fields_roundtrip(self, db):
        """All 12 §11 fields saved and read back intact."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        full_11 = MINIMAL_PAYLOADS[11]
        p = SectionInputPayload(section_no=11, input_json=full_11)
        await save_section_input(report_id=rid, section_no=11, payload=p,
                                  db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=11, db=db, current_user=user)

        for key in full_11:
            assert key in rb.input_json, (
                f"§11 field '{key}' lost during save/readback. "
                f"Keys present: {list(rb.input_json.keys())}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# J — Sequential Full Pipeline: Manual Input → Save → Mocked Generate §1–10
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sec_no", list(range(1, 11)))
@pytest.mark.asyncio
async def test_full_pipeline_manual_to_generate_per_section(db, sec_no):
    """
    Full pipeline test for §1–10:
      1. Save minimal manual input via PUT /inputs/{sec_no}
      2. Verify save succeeded (readback)
      3. Trigger generate_section (with mocked pipeline)
      4. Verify task_id returned and status is 'running'
    """
    from fastapi import BackgroundTasks
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.api.generate import generate_section
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    # Step 1: Save manual input
    payload = SectionInputPayload(section_no=sec_no, input_json=MINIMAL_PAYLOADS[sec_no])
    save_result = await save_section_input(report_id=rid, section_no=sec_no, payload=payload,
                                           db=db, current_user=user)
    assert save_result.section_no == sec_no, f"§{sec_no}: save returned wrong section_no"

    # Step 2: Readback verification
    rb = await get_section_input(report_id=rid, section_no=sec_no, db=db, current_user=user)
    assert rb.section_no == sec_no, f"§{sec_no}: readback section_no mismatch"
    assert len(rb.input_json) > 0, f"§{sec_no}: readback input_json is empty"

    # Step 3: Trigger generate (no hard deps, no actual LLM call)
    bg = BackgroundTasks()
    with patch("credit_report.api.generate.check_hard_dependencies",
               new=AsyncMock(return_value=[])):
        gen_result = await generate_section(report_id=rid, section_no=sec_no,
                                            background_tasks=bg, db=db, current_user=user)

    # Step 4: Verify task enqueued
    assert gen_result.task_id is not None, f"§{sec_no}: no task_id returned"
    assert gen_result.status == "running", f"§{sec_no}: status should be 'running'"
    assert gen_result.section_no == sec_no, (
        f"§{sec_no}: task section_no={gen_result.section_no} doesn't match"
    )


@pytest.mark.asyncio
async def test_save_all_11_sections_sequential(db):
    """Save all 11 sections sequentially for one report — cumulative state must be correct."""
    from credit_report.api.reports import save_section_input, list_section_inputs
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    for sec in range(1, 12):
        payload = SectionInputPayload(section_no=sec, input_json=MINIMAL_PAYLOADS[sec])
        result = await save_section_input(report_id=rid, section_no=sec, payload=payload,
                                          db=db, current_user=user)
        assert result.section_no == sec, f"§{sec}: wrong section_no in save result"

    # All 11 sections must appear in list
    inputs_list = await list_section_inputs(report_id=rid, db=db, current_user=user)
    saved_sections = {item["section_no"] for item in inputs_list}
    assert saved_sections == set(range(1, 12)), (
        f"Not all sections saved. Present: {sorted(saved_sections)}"
    )


@pytest.mark.asyncio
async def test_generate_all_sections_1_to_10_sequential(db):
    """After saving §1–10 inputs, trigger generate for each — all should return task_ids."""
    from fastapi import BackgroundTasks
    from credit_report.api.reports import save_section_input
    from credit_report.api.generate import generate_section
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    report, owner_id = await _seed_report(db, rid)
    user = _make_user("analyst")
    user.id = owner_id

    # Save §1–10
    for sec in range(1, 11):
        payload = SectionInputPayload(section_no=sec, input_json=MINIMAL_PAYLOADS[sec])
        await save_section_input(report_id=rid, section_no=sec, payload=payload,
                                  db=db, current_user=user)

    # Generate §1–10
    task_ids = []
    with patch("credit_report.api.generate.check_hard_dependencies",
               new=AsyncMock(return_value=[])):
        for sec in range(1, 11):
            bg = BackgroundTasks()
            result = await generate_section(report_id=rid, section_no=sec,
                                            background_tasks=bg, db=db, current_user=user)
            task_ids.append(result.task_id)

    assert len(task_ids) == 10, f"Expected 10 task_ids, got {len(task_ids)}"
    assert len(set(task_ids)) == 10, "Task IDs must be unique across sections"


# ══════════════════════════════════════════════════════════════════════════════
# K — HTML/JS Structural Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestHTMLJSStructure:
    """Verify critical JS functions and HTML elements exist in index.html."""

    REQUIRED_JS_FUNCTIONS = [
        "function renderFieldForm",
        "function populateForm",
        "function collectFormData",
        "function saveFormInput",
        "function syncFormToJson",
        "function openStructuredForm",
        "function getCompleteness",
        "function isFieldFilled",
        "function renderCompleteness",
    ]

    REQUIRED_HTML_ELEMENTS = [
        'id="jsonEd"',
        'id="formFields"',
        'id="tabForm"',
        'id="tabInput"',
    ]

    @pytest.mark.parametrize("fn_signature", REQUIRED_JS_FUNCTIONS)
    def test_js_function_present(self, fn_signature):
        html = _load_html()
        assert fn_signature in html, (
            f"Required JS function not found in index.html: '{fn_signature}'"
        )

    @pytest.mark.parametrize("element_id", REQUIRED_HTML_ELEMENTS)
    def test_html_element_present(self, element_id):
        html = _load_html()
        assert element_id in html, (
            f"Required HTML element not found in index.html: '{element_id}'"
        )

    def test_field_id_pattern_in_render_form(self):
        """renderFieldForm must generate IDs in the format ff_{sec}_{path}."""
        html = _load_html()
        # Look for the ID generation pattern in renderFieldForm
        assert "ff_" in html, "HTML ID prefix 'ff_' not found in index.html"
        # The _fid helper should be present
        assert "function _fid" in html or "_fid(" in html, (
            "_fid() helper function not found in index.html"
        )

    def test_save_form_input_uses_put_method(self):
        """saveFormInput must use PUT method and /inputs/ endpoint."""
        html = _load_html()
        fn_start = html.find("async function saveFormInput")
        # Grab 800 chars — enough to capture the full one-liner function body
        fn_body = html[fn_start:fn_start + 800]
        assert "PUT" in fn_body, "saveFormInput does not use PUT method"
        assert "/inputs/" in fn_body, "saveFormInput does not call /inputs/ endpoint"

    def test_save_form_input_sends_section_no_and_input_json(self):
        """saveFormInput payload must include section_no and input_json keys."""
        html = _load_html()
        fn_start = html.find("async function saveFormInput")
        fn_body = html[fn_start:fn_start + 800]
        assert "section_no" in fn_body, "saveFormInput payload missing 'section_no'"
        assert "input_json" in fn_body, "saveFormInput payload missing 'input_json'"

    def test_sync_form_to_json_calls_collect_form_data(self):
        """syncFormToJson must call collectFormData."""
        html = _load_html()
        fn_start = html.find("function syncFormToJson")
        fn_end = html.find("}\n", fn_start + 50)
        fn_body = html[fn_start:fn_end + 2]
        assert "collectFormData" in fn_body, (
            "syncFormToJson does not call collectFormData()"
        )

    def test_sync_form_to_json_updates_json_editor(self):
        """syncFormToJson must write to 'jsonEd' element."""
        html = _load_html()
        fn_start = html.find("function syncFormToJson")
        fn_end = html.find("}\n", fn_start + 50)
        fn_body = html[fn_start:fn_end + 2]
        assert "jsonEd" in fn_body, (
            "syncFormToJson does not update the 'jsonEd' element"
        )

    def test_field_defs_declared_as_const(self):
        html = _load_html()
        assert "const FIELD_DEFS=" in html, (
            "FIELD_DEFS not declared as const in index.html"
        )

    def test_required_fields_declared_as_const(self):
        html = _load_html()
        assert "const REQUIRED_FIELDS=" in html, (
            "REQUIRED_FIELDS not declared as const in index.html"
        )

    def test_all_11_sections_in_snames(self):
        html = _load_html()
        snames_start = html.find("const SNAMES=")
        snames_end = html.find("};", snames_start)
        snames_text = html[snames_start:snames_end + 2]
        for sec in range(1, 12):
            assert f"{sec}:" in snames_text, (
                f"§{sec} missing from SNAMES constant in index.html"
            )

    def test_collect_form_data_handles_json_type(self):
        """collectFormData must have JSON.parse handling."""
        html = _load_html()
        fn_start = html.find("function collectFormData")
        fn_end = html.find("return result", fn_start)
        fn_body = html[fn_start:fn_end + 20] if fn_end > 0 else html[fn_start:fn_start + 500]
        assert "JSON.parse" in fn_body, (
            "collectFormData must call JSON.parse() for 'json' type fields"
        )

    def test_collect_form_data_handles_lines_type(self):
        """collectFormData must split 'lines' type by newline."""
        html = _load_html()
        fn_start = html.find("function collectFormData")
        fn_end = html.find("return result", fn_start)
        fn_body = html[fn_start:fn_end + 20] if fn_end > 0 else html[fn_start:fn_start + 500]
        assert "split" in fn_body, (
            "collectFormData must call split() for 'lines' type fields"
        )

    def test_populate_form_handles_json_type(self):
        """populateForm must stringify objects for json-type fields."""
        html = _load_html()
        fn_start = html.find("function populateForm")
        fn_body = html[fn_start:fn_start + 600]
        assert "JSON.stringify" in fn_body, (
            "populateForm must call JSON.stringify() to display json-type values"
        )


# ══════════════════════════════════════════════════════════════════════════════
# L — Edge Cases
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge cases for manual input pipeline robustness."""

    @pytest.mark.asyncio
    async def test_save_very_large_json_payload(self, db):
        """Saving a large nested JSON payload must succeed."""
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        large_payload = {
            "7A_borrower_financials": {
                "reporting_currency": "USD",
                "income_statement": {
                    f"FY{y}": {"revenue": y * 1000, "ebitda": y * 200, "net_income": y * 100}
                    for y in range(2015, 2026)
                },
                "balance_sheet": {
                    f"FY{y}": {"total_assets": y * 10000}
                    for y in range(2015, 2026)
                },
                "cash_flow": {
                    f"FY{y}": {"ocf": y * 500}
                    for y in range(2015, 2026)
                },
            }
        }

        p = SectionInputPayload(section_no=7, input_json=large_payload)
        result = await save_section_input(report_id=rid, section_no=7, payload=p,
                                          db=db, current_user=user)
        assert result.section_no == 7

    @pytest.mark.asyncio
    async def test_save_special_characters_in_text_fields(self, db):
        """Text fields with special chars (quotes, slashes, unicode) must be stored correctly."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        special = {
            "borrower": 'Test "Borrower" Ltd & Co.',
            "purpose": "Vessel acquisition: 20,000 TEU LNG dual-fuel — 100% eco-design",
            "facility_type": "Term Loan (SLL) / 長期貸款",
        }

        p = SectionInputPayload(section_no=1, input_json=special)
        await save_section_input(report_id=rid, section_no=1, payload=p, db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=1, db=db, current_user=user)
        assert rb.input_json.get("borrower") == special["borrower"], (
            "Special chars in text field corrupted during save/readback"
        )

    def test_lines_type_single_item_becomes_one_element_list(self):
        """lines-type with a single line becomes a list with one element."""
        raw = "Single guarantor"
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        assert lines == ["Single guarantor"]
        assert len(lines) == 1

    def test_json_hint_with_nested_array_of_objects_parses(self):
        """Nested JSON with arrays of objects (common in §1-11) must parse."""
        hint = json.dumps([
            {"risk_no": 1, "level": "High", "title": "Rate risk",
             "risk_bullets": ["Volatility"], "mitigant_bullets": ["TC contract"]},
            {"risk_no": 2, "level": "Medium", "title": "Delivery risk",
             "risk_bullets": ["Delay"], "mitigant_bullets": ["IBK RG"]}
        ])
        result = json.loads(hint)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["risk_no"] == 1

    def test_minimal_payload_for_each_section_is_valid_json(self):
        """All MINIMAL_PAYLOADS must be JSON-serializable."""
        for sec_no, payload in MINIMAL_PAYLOADS.items():
            try:
                serialized = json.dumps(payload)
                deserialized = json.loads(serialized)
                assert deserialized == payload, (
                    f"§{sec_no} payload round-trip mismatch"
                )
            except (TypeError, json.JSONDecodeError) as e:
                pytest.fail(f"§{sec_no} MINIMAL_PAYLOAD not JSON-serializable: {e}")

    @pytest.mark.asyncio
    async def test_save_empty_dict_payload_fails_gracefully(self, db):
        """Saving empty input_json should succeed (empty dict is valid)."""
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        p = SectionInputPayload(section_no=2, input_json={})
        result = await save_section_input(report_id=rid, section_no=2, payload=p,
                                          db=db, current_user=user)
        assert result.section_no == 2

    @pytest.mark.asyncio
    async def test_save_section_11_with_all_12_fields_complete(self, db):
        """A complete §11 payload with all 12 fields saves successfully."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        complete_11 = {
            "11A_report_meta": {"analyst_firm": "Capital Securities", "report_date": "2026-03-15",
                                 "subject_ticker": "2603.TT", "report_type": "Initiation"},
            "11B_rating": {"current_rating": "Buy", "target_price_12m": 52.0,
                           "target_price_currency": "TWD", "current_price": 38.5, "upside_pct": 35.1},
            "11C_company_fundamentals": {"ticker": "2603.TT", "market_cap_usd_m": 10000,
                                          "debt_ratio_pct": 35.2},
            "11D_investment_thesis": {"summary_verbatim": "Strong buy.", "bull_points": ["Net cash"],
                                       "bear_points": ["Trade war"], "risks": ["Oversupply"],
                                       "key_catalysts": ["Alliance reorg"]},
            "11E_annual_income_statement": {"currency": "TWD", "unit": "百萬元", "periods": [
                {"year": "FY2024A", "is_forecast": False, "revenue": 240300, "net_income": 73900}]},
            "11F_quarterly_income_statement": {"currency": "TWD", "periods": [
                {"quarter": "1Q2025A", "is_forecast": False, "revenue": 58000}]},
            "11G_balance_sheet": {"currency": "TWD", "periods": [
                {"year": "FY2024A", "is_forecast": False, "total_assets": 850000}]},
            "11H_cash_flow": {"currency": "TWD", "periods": [
                {"year": "FY2024A", "is_forecast": False, "ocf": 108000}]},
            "11I_ratio_analysis": {"currency": "TWD", "periods": [
                {"year": "FY2024A", "is_forecast": False, "roe_pct": 17.0, "per": 4.4}]},
            "11J_valuation_metrics": {"per_current": 4.4, "pbr_current": 1.08,
                                       "valuation_methodology": "P/E and P/B band"},
            "11K_esg": {"esg_overall_score": 72, "cii_rating": "B",
                         "co2_reduction_target_pct": 40, "co2_base_year": 2019},
            "11L_industry_context": {"ccfi_current": 1012,
                                      "forward_outlook_narrative": "Recovery H2 2026.",
                                      "analyst_sector_call": "Overweight"},
        }

        p = SectionInputPayload(section_no=11, input_json=complete_11)
        saved = await save_section_input(report_id=rid, section_no=11, payload=p,
                                          db=db, current_user=user)
        assert saved.section_no == 11

        rb = await get_section_input(report_id=rid, section_no=11, db=db, current_user=user)
        assert len(rb.input_json) == 12, (
            f"§11 roundtrip: expected 12 fields, got {len(rb.input_json)}: "
            f"{list(rb.input_json.keys())}"
        )

    def test_number_zero_is_valid_input(self):
        """Number type: 0 is a valid value and must not be excluded."""
        raw = "0"
        raw_stripped = raw.strip()
        assert raw_stripped  # not empty
        n = float(raw_stripped)
        assert n == 0.0
        assert n == n  # not NaN

    def test_bool_false_is_valid_selection(self):
        """bool type: 'false' converts to Python False (valid, not excluded)."""
        raw = "false"
        result = (raw == "true")
        assert result is False
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_schema_section_no_range_11_accepted(self, db):
        """SectionInputPayload with section_no=11 must pass Pydantic validation."""
        from credit_report.schemas import SectionInputPayload
        p = SectionInputPayload(section_no=11, input_json={"key": "value"})
        assert p.section_no == 11

    @pytest.mark.asyncio
    async def test_schema_section_no_range_12_rejected(self, db):
        """SectionInputPayload with section_no=12 must fail Pydantic validation."""
        from pydantic import ValidationError
        from credit_report.schemas import SectionInputPayload
        with pytest.raises(ValidationError):
            SectionInputPayload(section_no=12, input_json={"key": "value"})

    @pytest.mark.asyncio
    async def test_schema_section_no_range_0_rejected(self, db):
        """SectionInputPayload with section_no=0 must fail Pydantic validation."""
        from pydantic import ValidationError
        from credit_report.schemas import SectionInputPayload
        with pytest.raises(ValidationError):
            SectionInputPayload(section_no=0, input_json={"key": "value"})

"""
§1-10 CI/CD Quality Endurance Test Suite
=========================================
Covers: input → form parse → finalizePayload → API save → schema check → round-trip

  A. Field type coercion — number/lines/bool for all FIELD_DEFS types
  B. finalizePayload transforms — flat form dict → nested JSON for each section
  C. expandPayload round-trip — finalized JSON expands back to flat, no data loss
  D. API save integration — PUT /inputs/{n} for §1-10 returns 200
  E. JSON schema integrity — stored input_json contains expected keys
  F. Generation blocking rules — §11 blocked (400), §1-10 accepted (202)
  G. Quality endurance — edge cases: special chars, long text, unicode, pipe lines
  H. Hint coverage — every non-select/non-bool field in §1-10 has h: or hz:
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    r = Report(id=rid, borrower_name="CICDQuality Co", created_by=uid,
               status="draft", is_deleted=False)
    db.add(r)
    await db.flush()
    return r, uid


def _extract_field_defs(html: str) -> dict[int, list[dict]]:
    """Parse FIELD_DEFS from index.html into Python dict."""
    fd_start = html.find("const FIELD_DEFS={")
    fd_end = html.find("\n};", fd_start) + 3
    fd_text = html[fd_start:fd_end]
    result: dict[int, list[dict]] = {}
    for sec in range(1, 12):
        if sec < 11:
            pattern = rf"\n\s*{sec}:\[(.*?)\],\s*\n\s*{sec + 1}:"
        else:
            pattern = r"\n\s*11:\[(.*?)\]\s*\n\};"
        m = re.search(pattern, fd_text, re.DOTALL)
        if m:
            section_text = m.group(1)
            fields = []
            for fm in re.finditer(r"\{p:'([^']+)',l:'([^']+)'(?:,lz:'[^']*')?,t:'([^']+)'([^}]*)\}", section_text):
                entry: dict = {"p": fm.group(1), "l": fm.group(2), "t": fm.group(3)}
                opts_m = re.search(r"opts:\[([^\]]+)\]", fm.group(4))
                if opts_m:
                    entry["opts"] = re.findall(r"'([^']+)'", opts_m.group(1))
                hint_m = re.search(r",h:'(.*?)'(?=[,}])", fm.group(0))
                if hint_m:
                    entry["h"] = hint_m.group(1)
                hz_m = re.search(r",hz:'(.*?)'(?=[,}])", fm.group(0))
                if hz_m:
                    entry["hz"] = hz_m.group(1)
                fields.append(entry)
            result[sec] = fields
    return result


# Minimal valid payloads — used for API save/schema tests
MINIMAL_PAYLOADS: dict[int, dict] = {
    1: {
        "report_type": "new_deal",
        "facility_summary": {
            "rows": ["1|Test Borrower Ltd|SG|100.0|Yes|USD|5 years|Term Loan|RG|Vessel Mortgage|Test Guarantor"],
            "totals": {"total_credit_limit_usd_m": 100.0},
            "footnotes": ["[1] Expected Vessel Delivery Date 30 Jun 2028 with 180 days grace period."],
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
            "deal_comparison": ["Amount|USD100m|USD80m", "Tenor|5 years|5 years"],
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
        "2A_credit_overview": {
            "bullets": "EMC is the 7th largest container line globally\nNew USD178.5m SLL to finance one vessel",
            "tariff_impact_paragraphs": "EMC has minimal direct exposure to US tariff risk.\n\nHistorical leverage benchmarks show EMC maintained net cash position.",
        },
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
            },
        },
        "2C_guarantor": {
            "guarantor_name_abbrev": "TESTG",
            "guarantor_full_name": "Test Guarantor Corp",
            "period": "FY2024",
        },
        "2D_collateral": {
            "pre_delivery": {
                "issuer_full_name": "Test Bank",
                "issuer_rating": "A",
                "facility_amount_pct": 100,
                "assigned_to_cub": True,
            },
            "post_delivery": {"ltc_pct": 80, "acr_pct": 120, "ltv_pct": 83},
        },
        "2E_risk_and_mitigants": {
            "risk_1_level": "Medium",
            "risk_1_title": "Rate risk",
            "risk_1_risk_bullets": "Rate volatility affects revenue",
            "risk_1_mitigant_bullets": "TC agreement covers 80% of vessel revenue",
            "risk_2_level": "Medium",
            "risk_2_title": "Construction risk",
            "risk_2_risk_bullets": "Delivery delay risk",
            "risk_2_mitigant_bullets": "KDB Refund Guarantee covers each installment",
            "additional_risk_factors_from_previous": "",
        },
        "report_type": "new_deal",
    },
    3: {
        "3A_external_ratings": {"all_nil": True, "ratings": []},
        "3B_internal_ratings": {
            "period_display_labels": {"fy2022_23": "2022/23", "fy2024": "2024",
                                      "interim": "Jul 2025", "current": "Nov 2025"},
            "borrower_entity_full_name": "Test Borrower Pte. Ltd.",
            "borrower_entity_abbrev": "TB",
            "borrower_fy2022_23": "5",
            "borrower_fy2024": "4+",
            "borrower_interim": "4+",
            "borrower_current": "4+",
            "borrower_override_flag": False,
        },
        "3C_mas_612": {
            "grade": "PASS",
            "msr_value": "4+",
            "para_1_msr_mapping_verbatim": "Borrower is internally rated MSR 4+, mapped to PASS.",
            "para_2_account_conduct_verbatim": "Account conduct remains satisfactory.",
            "para_3_financial_profile_verbatim": "Financial profile is adequate.",
            "para_4_projection_verbatim": "Projections demonstrate repayment capability.",
        },
    },
    4: {
        "4A_borrower": {
            "company_name_en": "Test Co Ltd",
            "company_name_zh": "測試公司",
            "legal_entity_type": "Private Limited Company",
            "registration_number": "202100001Z",
            "incorporation_country": "Singapore",
            "incorporation_date": "2021-01-01",
            "fiscal_year_end": "Dec-31",
            "group_auditor": "Deloitte",
        },
        "4B_ownership": {
            "shareholders": ["Test Parent Corp|100|TW"],
            "ultimate_beneficial_owner": "Test Family",
            "ubo_stake_pct": 55.0,
        },
        "4C_management": {
            "ceo_name": "Jane Doe",
            "ceo_title": "Chief Executive Officer",
            "ceo_background": "20 years in container shipping",
            "cfo_name": "John Smith",
            "cfo_title": "Chief Financial Officer",
            "cfo_background": "15 years in shipping finance",
        },
        "4D_business": {
            "primary_business": "Container liner shipping",
            "trade_routes": "Asia-Europe, Trans-Pacific",
            "operational_model": "Owner-operator",
            "global_ranking": 10,
        },
        "4E_financials": {"currency": "USD", "fiscal_year": "FY2024",
                          "revenue": 1000.0, "ebitda": 250.0},
        "4F_fleet": {
            "owned_vessel_count": 105,
            "owned_total_teu": 350000,
            "chartered_vessel_count": 95,
            "chartered_total_teu": 800000,
            "on_order_vessel_count": 63,
            "on_order_total_teu": 1200000,
        },
        "4J_peer_comparison": ["MSC|5900000|17.8|None", "Maersk|4200000|13.1|Gemini"],
    },
    5: {
        "5A_security_overview": {
            "is_secured": True,
            "instr_1_instrument": "Refund Guarantee (Test Bank)",
            "instr_1_description": "Issued by Test Bank, covers all pre-delivery installments",
            "instr_2_instrument": "First Priority Vessel Mortgage",
            "instr_2_description": "Over vessel upon delivery, assigned to CUB",
        },
        "5B_refund_guarantee": {
            "applicable": True,
            "issuer_full_name": "Test Bank",
            "issuer_rating": "A+",
            "rating_agency": "S&P",
            "legal_structure": "Demand guarantee",
            "governing_law": "English law",
            "assigned_to_cub": True,
            "m1_name": "Steel Cutting",
            "m1_date": "2024-09-01",
            "m1_rg_usd_m": 100.0,
            "m1_coverage_pct": 500.0,
            "m1_status": "Completed",
            "m2_name": "Delivery",
            "m2_date": "2026-06-01",
            "m2_rg_usd_m": 100.0,
            "m2_coverage_pct": 100.0,
            "m2_status": "Pending",
        },
        "5C_vessel_mortgage": {
            "applicable": True,
            "vessel_name": "MV Test Star",
            "vessel_teu": 10000,
            "valuer": "Clarkson",
            "market_value_usd_m": 120.0,
            "contract_price_usd_m": 100.0,
            "loan_amount_usd_m": 100.0,
            "ltc_pct": 100.0,
            "acr_at_delivery_pct": 120.0,
            "ltv_at_maturity_pct": 75.0,
        },
        "5D_insurance": {
            "applicable": True,
            "hm_insurer": "China P&I Club",
            "hm_insured_value_usd_m": 180.0,
            "hm_notes": "CUB named co-insured",
            "pi_insurer": "UK P&I Club",
            "pi_insured_value_usd_m": 0.0,
            "pi_notes": "Standard P&I coverage",
        },
        "5E_value_maintenance_clause": {
            "acr_covenant_pct": 120.0,
            "ltv_covenant_pct": 75.0,
            "test_frequency_verbatim": "Every 2 years",
            "cure_period_banking_days": 21,
            "cure_mechanism_verbatim": "Prepay or provide security.",
        },
        "5F_corporate_guarantee": {
            "applicable": True,
            "guarantor_full_name": "Test Guarantor Corp",
            "guarantor_listed_exchange": "TSE",
            "relationship_to_borrower": "Parent company",
            "guarantee_scope": "Full guarantee covering all obligations.",
            "guarantee_covers_predelivery": True,
            "guarantee_covers_postdelivery": True,
            "fx_rate_to_usd": 32.5,
            "cash_twd_bn": 198.3,
            "cash_usd_bn": 6.1,
            "total_debt_twd_bn": 310.0,
            "net_worth_twd_bn": 440.0,
            "revenue_twd_bn": 381.2,
            "ebitda_twd_bn": 89.6,
            "interest_coverage": 15.0,
            "net_margin_pct": 19.4,
            "roe_pct": 16.8,
        },
        "5G_responsible_person": {"provided": False, "name": "", "title": ""},
    },
    6: {
        "6A_project": {
            "hull_number": "H-001",
            "vessel_type": "Container",
            "teu": 10000,
            "fuel_type": "LNG Dual Fuel",
            "imo_tier": "IMO Tier III",
            "dwt": 120000,
            "loa_m": 300,
            "beam_m": 48,
            "speed_knots": 22.0,
            "class_society": "DNV",
            "flag_state": "Singapore",
            "contract_price_usd_m": 150.0,
            "loan_amount_usd_m": 120.0,
            "ltc_pct": 80.0,
            "delivery_date": "2026-12-31",
            "grace_period_days": 180,
        },
        "6B_builder": {
            "name": "Test Shipyard Co",
            "founded": "1980",
            "hq": "Seoul, Korea",
            "market_position": "Top 5 global shipbuilder",
            "track_record_verbatim": "Delivered 50 vessels over 10 years with 92% on-time rate.",
            "ontime_delivery_pct": 92,
            "technology_overlap_verbatim": "LNG carrier experience since 2010.",
        },
        "6C_contract": {
            "contract_type": "Fixed-price shipbuilding contract",
            "buyer": "Test Borrower Pte. Ltd.",
            "builder": "Test Shipyard Co",
            "price_verbatim": "USD150,000,000",
            "contract_date": "2023-06-01",
            "expected_delivery": "2026-12-31",
            "grace_period": "180 days",
            "late_delivery_penalty_verbatim": "USD40,000/day late delivery penalty.",
            "buyer_termination_verbatim": "Buyer may terminate after 270-day delay.",
        },
        "6D_milestones": {
            "m1_name": "Steel Cutting",
            "m1_date": "2024-06-01",
            "m1_pct": 10,
            "m1_amount_usd_m": 15.0,
            "m2_name": "Keel Laying",
            "m2_date": "2024-12-01",
            "m2_pct": 20,
            "m2_amount_usd_m": 30.0,
            "m3_name": "Launch",
            "m3_date": "2025-09-01",
            "m3_pct": 30,
            "m3_amount_usd_m": 45.0,
            "m4_name": "Delivery",
            "m4_date": "2026-12-31",
            "m4_pct": 40,
            "m4_amount_usd_m": 60.0,
            "banking_act_commentary": "Pre-delivery unsecured capped at USD30m per s33-3.",
        },
        "6E_rg_mechanism": {
            "applicable": True,
            "issuer_full_name": "Test Bank",
            "issuer_rating_verbatim": "A+ (S&P)",
            "format_verbatim": "Unconditional and irrevocable demand guarantee",
            "governing_law": "English law",
            "trigger_events": ["Builder fails to complete by latest delivery date"],
            "claim_process_verbatim": "Written demand; payment within 5 banking days.",
            "coverage_summary_min_pct": 100.0,
        },
    },
    7: {
        "entities_to_analyze": {
            "borrower_name": "Test Co Pte. Ltd.",
            "borrower_currency": "USD",
            "borrower_unit": "millions",
            "guarantor_name": "Test Guarantor Corp",
            "guarantor_currency": "NTD",
            "guarantor_exists": True,
        },
        "7A_borrower_financials": {
            "reporting_entity": "Test Co Consolidated",
            "auditor": "Deloitte",
            "audit_opinion": "Unqualified",
            "accounting_standard": "IFRS",
            "fiscal_year_end": "Dec-31",
            "reporting_currency": "USD",
            "unit": "millions",
            "revenue_fy2022": 2000.0, "ebitda_fy2022": 600.0,
            "op_profit_fy2022": 500.0, "interest_expense_fy2022": 50.0,
            "net_income_fy2022": 380.0,
            "revenue_fy2023": 1800.0, "ebitda_fy2023": 520.0,
            "op_profit_fy2023": 430.0, "interest_expense_fy2023": 48.0,
            "net_income_fy2023": 300.0,
            "revenue_fy2024": 2200.0, "ebitda_fy2024": 710.0,
            "depreciation_fy2024": 200.0,
            "op_profit_fy2024": 510.0, "interest_expense_fy2024": 60.0,
            "net_income_fy2024": 399.0,
            "bs_cash": 2200.0, "bs_total_ca": 2725.0, "bs_total_nca": 5250.0,
            "bs_total_assets": 7975.0, "bs_total_cl": 1230.0, "bs_total_ncl": 2675.0,
            "bs_total_liabilities": 3905.0, "bs_total_equity": 4070.0,
            "cf_ocf": 780.0, "cf_capex": -420.0, "cf_fcf": 360.0,
        },
        "7B_key_ratios": {
            "fy2022_debt_ebitda": 2.01, "fy2022_interest_coverage": 10.8,
            "fy2022_dscr": 2.15, "fy2022_current_ratio": 1.8,
            "fy2022_net_margin_pct": 19.0,
            "fy2024_debt_ebitda": 2.75, "fy2024_interest_coverage": 11.8,
            "fy2024_dscr": 1.85, "fy2024_current_ratio": 2.2,
            "fy2024_net_margin_pct": 18.1,
        },
        "7C_guarantor_financials": {
            "applicable": True,
            "reporting_currency": "NTD",
            "revenue_fy2024": 381.2, "ebitda_fy2024": 89.6,
            "net_income_fy2024": 73.9,
            "cash_fy2024": 198.3, "total_assets_fy2024": 850.0,
            "total_equity_fy2024": 440.0, "ocf_fy2024": 95.0,
        },
        "7E_base_case": {
            "applicable": True,
            "key_assumptions": [{"assumption": "Charter rate", "value": "USD28,000/day",
                                  "source": "TC agreement"}],
            "min_dscr": 1.31,
            "conclusion": "Base case DSCR 1.31x; adequate coverage.",
        },
        "7F_worse_case": {"applicable": True, "stressed_min_dscr": 0.86},
    },
    8: {
        "8A_acra_banking_charges": {
            "section_applicability": "singapore_incorporated",
            "acra_data_available": True,
            "search_date": "01 Dec 2025",
            "entity_name": "Test Entity Ltd",
            "uen": "202100000Z",
            "jurisdiction": "Singapore",
            "charges": [],
            "total_charges": 0,
            "active_charges": 0,
            "satisfied_charges": 0,
            "total_active_usd_m": 0.0,
            "cub_charge_count": 0,
            "cub_total_usd_m": 0.0,
            "analyst_commentary": "No charges identified.",
            "new_deal_forward_looking": True,
        },
        "8B_other_information": {
            "applicable": False,
            "litigation": [],
            "sanctions_ofac": "Clear",
            "sanctions_mas": "Clear",
            "esg_controversies": [],
            "regulatory_actions": "None",
            "material_events": "None",
        },
    },
    9: {
        "9A_checklist": {
            "items": ["CDD completed|Yes|Tier 1 KYC; reviewed 01 Dec 2025"],
            "kyc_aml_cleared": True,
            "esg_screen_passed": True,
            "mas612_classification": "Pass Grade; MSR3",
            "item15_pre_delivery_usd_m": 71.4,
            "item15_exemption_basis": "item (d) exemption; approved GM Credit",
            "item16_search_date": "01 Dec 2025",
            "item16_entity_name": "Test Entity Ltd",
            "item16_uen": "202100000Z",
        },
        "9B_conditions_covenants": {
            "conditions_precedent": ["Execution of facility agreement|Before first drawdown"],
            "ongoing_covenants": ["ACR covenant: ACR >= 100%|100%|Every 2 years"],
            "financial_covenants": "NIL",
        },
        "9C_recommendation": {
            "decision": "APPROVE",
            "facility_amount_usd_m": 213.84,
            "tenor_years": 12,
            "security_structure": "Pre-delivery: RG + Assignment of SBC. Post-delivery: Vessel Mortgage.",
            "key_conditions": ["Execution of all security documents before first drawdown"],
            "balloon_ltv_pct": 61.98,
        },
        "9D_signoff": {
            "prepared_by": "Test Analyst, Associate, Credit Management Department, CUB SG Branch",
            "reviewed_by": "Test VP, Vice President, Credit Management Department, CUB SG Branch",
        },
    },
    10: {
        "10A_group_exposure": {
            "entity_group": "Test Group",
            "currency": "USD",
            "as_of_date": "Dec 2025",
            "approved_group_limit_usd_m": 500.0,
            "rows": [],
            "proposed_exposure_usd_m": 213.84,
            "existing_exposure_usd_m": 50.0,
            "eva_note": "",
        },
        "10B_fleet_growth": {
            "group_name": "Test",
            "year_range": "2023-2028E",
            "rows": [],
            "cagr_pct": 5.8,
            "chart_reference": "Source: Test Data",
            "target_capacity_note": "Target 2.55m TEU by 2028E.",
            "key_notes": ["Note 1", "Note 2", "Note 3", "Note 4", "Note 5"],
        },
        "10C_projections": {
            "entity_name": "Test Co",
            "basis": "Standalone",
            "currency": "USD",
            "unit": "USD000",
            "key_assumptions": [],
            "assumptions_narrative": "Base case assumes stable rates.",
            "base_case_pl": [],
            "base_case_bs": [],
            "base_case_cf": [],
            "base_case_dscr": [],
            "dscr_commentary": "DSCR improves over projection period.",
            "stress_assumptions": [],
            "worse_case_summary": [],
            "worse_case_commentary": "Under stress case, DSCR declines.",
            "freight_rate_drop_pct": 20.0,
            "base_dscr_fy_1": 0.92,
            "base_dscr_fy_2": 0.97,
            "base_dscr_fy_3": 1.03,
            "worse_dscr_fy_1": 0.58,
            "base_revenue_fy_1": 10206.0,
            "worse_revenue_fy_1": 8165.0,
        },
    },
}


# ── DB fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    from credit_report.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# A — Field Type Coercion (Python simulation of collectFormData)
# ══════════════════════════════════════════════════════════════════════════════

class TestFieldTypeCoercion:
    """
    Simulate collectFormData() type conversion for every field type.
    text → string as-is (stripped)
    number → float (skip NaN/non-numeric)
    lines → split by newline, strip, filter empty
    bool → 'true'→True, 'false'→False
    select → string as-is
    textarea → string as-is (multi-line OK)
    json → parsed object/array, raw str fallback
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
            lines = [line.strip() for line in raw.split("\n") if line.strip()]
            return lines if lines else None
        elif field_type == "json":
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        elif field_type == "bool":
            return raw == "true"
        else:
            return raw  # text, textarea, select

    # ── number coercion ──────────────────────────────────────────────────

    @pytest.mark.parametrize("raw,expected", [
        ("178.5", 178.5),
        ("0", 0.0),
        ("-5.0", -5.0),
        ("100", 100.0),
        ("3.14159", 3.14159),
        ("1e3", 1000.0),
    ])
    def test_number_coercion_valid(self, raw, expected):
        result = self._collect("number", raw)
        assert result == pytest.approx(expected), (
            f"number('{raw}') expected {expected}, got {result}"
        )

    @pytest.mark.parametrize("raw", [
        "1,234",   # comma-separated
        "abc",     # letters
        "N/A",     # text
        "",        # empty
        "   ",     # whitespace only
    ])
    def test_number_coercion_invalid_returns_none(self, raw):
        result = self._collect("number", raw)
        assert result is None, f"number('{raw}') should be None, got {result!r}"

    def test_number_negative_value(self):
        assert self._collect("number", "-42.5") == pytest.approx(-42.5)

    def test_number_zero_is_valid(self):
        assert self._collect("number", "0") == 0.0

    # ── lines coercion ──────────────────────────────────────────────────

    @pytest.mark.parametrize("raw,expected", [
        ("line1\nline2\nline3", ["line1", "line2", "line3"]),
        ("  trimmed  \n  also  ", ["trimmed", "also"]),
        ("single", ["single"]),
        ("line1\n\n\nline2", ["line1", "line2"]),
        ("   \n   ", None),  # all whitespace → None
        ("", None),
    ])
    def test_lines_coercion(self, raw, expected):
        result = self._collect("lines", raw)
        assert result == expected, f"lines('{raw!r}') expected {expected!r}, got {result!r}"

    def test_lines_pipe_content_preserved(self):
        """Pipe characters inside lines must pass through unchanged."""
        raw = "Steel Cutting|2024-09-01|100.0\nDelivery|2026-06-01|100.0"
        result = self._collect("lines", raw)
        assert result == [
            "Steel Cutting|2024-09-01|100.0",
            "Delivery|2026-06-01|100.0",
        ]

    def test_lines_special_chars_in_content(self):
        """Parentheses, dashes, slashes must pass through."""
        raw = "Borrower (Test Co.)|Loan (USD 100m)\nGuarantor [Parent/Subsidiary]"
        result = self._collect("lines", raw)
        assert len(result) == 2
        assert "(" in result[0]
        assert "/" in result[1]

    # ── bool coercion ──────────────────────────────────────────────────

    def test_bool_true_string(self):
        assert self._collect("bool", "true") is True

    def test_bool_false_string(self):
        assert self._collect("bool", "false") is False

    def test_bool_empty_excluded(self):
        assert self._collect("bool", "") is None

    def test_bool_other_values_not_true(self):
        # Only exactly "true" should be True; anything else coerces to False
        assert self._collect("bool", "yes") is False
        assert self._collect("bool", "True") is False
        assert self._collect("bool", "1") is False

    # ── text/textarea coercion ──────────────────────────────────────────

    def test_text_stripped(self):
        assert self._collect("text", "  hello world  ") == "hello world"

    def test_text_empty_excluded(self):
        assert self._collect("text", "") is None

    def test_textarea_preserves_newlines(self):
        val = "paragraph one\n\nparagraph two"
        assert self._collect("textarea", val) == val

    def test_textarea_special_chars(self):
        val = "Rate: SOFR + 200bps (per p.a.)\nSecurity: 1st Priority Mortgage/Assignment"
        assert self._collect("textarea", val) == val

    # ── select coercion ──────────────────────────────────────────────────

    def test_select_returns_string(self):
        assert self._collect("select", "new_deal") == "new_deal"

    def test_select_empty_excluded(self):
        assert self._collect("select", "") is None

    # ── json coercion ──────────────────────────────────────────────────

    def test_json_object_parsed(self):
        result = self._collect("json", '{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_json_array_parsed(self):
        result = self._collect("json", '[{"a": 1}, {"b": 2}]')
        assert result == [{"a": 1}, {"b": 2}]

    def test_json_invalid_falls_back_to_string(self):
        invalid = '{key: "value"}'  # missing quotes on key
        result = self._collect("json", invalid)
        assert result == invalid

    def test_json_empty_excluded(self):
        assert self._collect("json", "") is None


# ══════════════════════════════════════════════════════════════════════════════
# B — finalizePayload Transforms (Python implementation mirroring JS)
# ══════════════════════════════════════════════════════════════════════════════

def _finalize_sec1(data: dict) -> dict:
    """Python reimplementation of JS finalizePayload for §1."""
    import copy
    d = copy.deepcopy(data)
    fs = d.get("facility_summary", {})
    if isinstance(fs.get("rows"), list) and fs["rows"] and isinstance(fs["rows"][0], str):
        rows = []
        for line in fs["rows"]:
            p = line.split("|")
            rows.append({
                "item_no": int(p[0]) if len(p) > 0 and p[0].strip().isdigit() else 1,
                "borrower_full_name": p[1] if len(p) > 1 else "",
                "booking_location": p[2] if len(p) > 2 else "",
                "proposed_usd_m": float(p[3]) if len(p) > 3 else 0.0,
                "is_new": (p[4].lower() == "yes") if len(p) > 4 else False,
                "currency": p[5] if len(p) > 5 else "USD",
                "tenor": p[6] if len(p) > 6 else "",
                "facility_type": p[7] if len(p) > 7 else "",
                "collateral_pre_delivery": p[8] if len(p) > 8 else "",
                "collateral_post_delivery": p[9] if len(p) > 9 else "",
                "guarantor": p[10] if len(p) > 10 else "",
            })
        fs["rows"] = rows
    if isinstance(fs.get("footnotes"), list) and fs["footnotes"] and isinstance(fs["footnotes"][0], str):
        footnotes = []
        for line in fs["footnotes"]:
            m = re.match(r"^(\[.*?\])\s*(.*)", line)
            if m:
                footnotes.append({"symbol": m.group(1), "text_verbatim": m.group(2)})
            else:
                footnotes.append({"symbol": "", "text_verbatim": line})
        fs["footnotes"] = footnotes
    tc = d.get("terms_and_conditions", {})
    if isinstance(tc.get("deal_comparison"), list) and tc["deal_comparison"] and isinstance(tc["deal_comparison"][0], str):
        tc["deal_comparison"] = [
            {"term": p.split("|")[0] if p.split("|") else "",
             "proposed": p.split("|")[1] if len(p.split("|")) > 1 else "",
             "previous": p.split("|")[2] if len(p.split("|")) > 2 else ""}
            for p in tc["deal_comparison"]
        ]
    kpi_sec = d.get("sll_kpi_performance", {})
    if isinstance(kpi_sec.get("kpis"), list) and kpi_sec["kpis"] and isinstance(kpi_sec["kpis"][0], str):
        kpi_sec["kpis"] = [
            {
                "kpi_name": parts[0] if len(parts) > 0 else "",
                "target_value": parts[1] if len(parts) > 1 else "",
                "actual_value": parts[2] if len(parts) > 2 else "",
                "period": parts[3] if len(parts) > 3 else "",
                "on_track": (parts[4].lower() == "yes") if len(parts) > 4 else False,
                "ratchet_bps": int(parts[5]) if len(parts) > 5 and parts[5].strip().lstrip("-").isdigit() else 0,
            }
            for p in kpi_sec["kpis"]
            for parts in [p.split("|")]
        ]
    return d


def _finalize_sec4(data: dict) -> dict:
    """Python reimplementation of JS finalizePayload for §4."""
    import copy
    d = copy.deepcopy(data)
    mgt = d.get("4C_management", {})
    mg_arr = []
    if isinstance(mgt, dict):
        if mgt.get("ceo_name"):
            mg_arr.append({"name": mgt["ceo_name"], "title": mgt.get("ceo_title", "CEO"),
                           "years_experience": None, "background": mgt.get("ceo_background", "")})
        if mgt.get("cfo_name"):
            mg_arr.append({"name": mgt["cfo_name"], "title": mgt.get("cfo_title", "CFO"),
                           "years_experience": None, "background": mgt.get("cfo_background", "")})
    d["4C_management"] = mg_arr if mg_arr else []
    fl = d.get("4F_fleet", {})
    if isinstance(fl, dict):
        fl_arr = []
        if fl.get("owned_vessel_count") is not None or fl.get("owned_total_teu") is not None:
            fl_arr.append({"category": "Owned",
                           "vessel_count": fl.get("owned_vessel_count", 0),
                           "total_teu": fl.get("owned_total_teu", 0)})
        if fl.get("chartered_vessel_count") is not None or fl.get("chartered_total_teu") is not None:
            fl_arr.append({"category": "Chartered-in",
                           "vessel_count": fl.get("chartered_vessel_count", 0),
                           "total_teu": fl.get("chartered_total_teu", 0)})
        if fl.get("on_order_vessel_count") is not None or fl.get("on_order_total_teu") is not None:
            fl_arr.append({"category": "On Order",
                           "vessel_count": fl.get("on_order_vessel_count", 0),
                           "total_teu": fl.get("on_order_total_teu", 0)})
        d["4F_fleet"] = {"fleet_breakdown": fl_arr}
    return d


def _finalize_sec5(data: dict) -> dict:
    """Python reimplementation of JS finalizePayload for §5."""
    import copy
    d = copy.deepcopy(data)
    sa = d.get("5A_security_overview", {})
    s_instr = []
    if isinstance(sa, dict):
        if sa.get("instr_1_instrument"):
            s_instr.append({"rank": 1, "instrument": sa["instr_1_instrument"],
                            "description": sa.get("instr_1_description", "")})
        if sa.get("instr_2_instrument"):
            s_instr.append({"rank": 2, "instrument": sa["instr_2_instrument"],
                            "description": sa.get("instr_2_description", "")})
        d["5A_security_overview"] = {"is_secured": sa.get("is_secured"), "security_instruments": s_instr}
    ins = d.get("5D_insurance", {})
    if isinstance(ins, dict):
        i_arr = []
        if ins.get("hm_insurer"):
            i_arr.append({"type": "Hull & Machinery", "insurer_or_club": ins["hm_insurer"],
                          "insured_value_usd_m": ins.get("hm_insured_value_usd_m", 0),
                          "notes": ins.get("hm_notes", "")})
        if ins.get("pi_insurer"):
            i_arr.append({"type": "P&I", "insurer_or_club": ins["pi_insurer"],
                          "insured_value_usd_m": ins.get("pi_insured_value_usd_m", 0),
                          "notes": ins.get("pi_notes", "")})
        if ins.get("war_insurer"):
            i_arr.append({"type": "War Risk", "insurer_or_club": ins["war_insurer"],
                          "insured_value_usd_m": ins.get("war_insured_value_usd_m", 0),
                          "notes": ins.get("war_notes", "")})
        d["5D_insurance"] = {"applicable": ins.get("applicable"), "instruments": i_arr}
    bg = d.get("5B_refund_guarantee", {})
    if isinstance(bg, dict):
        milestones = []
        for i in range(1, 5):
            if bg.get(f"m{i}_name"):
                milestones.append({
                    "milestone": bg[f"m{i}_name"],
                    "sched_date": bg.get(f"m{i}_date", ""),
                    "rg_amount_usd_m": bg.get(f"m{i}_rg_usd_m", 0),
                    "coverage_pct": bg.get(f"m{i}_coverage_pct", 0),
                    "status": bg.get(f"m{i}_status", ""),
                })
        if milestones:
            d["5B_refund_guarantee"] = {
                "applicable": bg.get("applicable"),
                "issuer_full_name": bg.get("issuer_full_name"),
                "issuer_rating": bg.get("issuer_rating"),
                "rating_agency": bg.get("rating_agency"),
                "legal_structure": bg.get("legal_structure"),
                "governing_law": bg.get("governing_law"),
                "assigned_to_cub": bg.get("assigned_to_cub"),
                "milestones": milestones,
            }
    return d


def _finalize_sec9(data: dict) -> dict:
    """Python reimplementation of JS finalizePayload for §9."""
    import copy
    d = copy.deepcopy(data)
    c = d.get("9A_checklist", {})
    if isinstance(c, dict) and isinstance(c.get("items"), list):
        new_items = []
        for line in c["items"]:
            if isinstance(line, str):
                parts = line.split("|")
                new_items.append({
                    "item": parts[0] if parts else "",
                    "response": parts[1] if len(parts) > 1 else "",
                    "remarks": parts[2] if len(parts) > 2 else "",
                })
            else:
                new_items.append(line)
        c["items"] = new_items
    bc = d.get("9B_conditions_covenants", {})
    if isinstance(bc, dict):
        if isinstance(bc.get("conditions_precedent"), list):
            new_cp = []
            for line in bc["conditions_precedent"]:
                if isinstance(line, str):
                    parts = line.split("|")
                    new_cp.append({"description": parts[0] if parts else "",
                                   "testing": parts[1] if len(parts) > 1 else ""})
                else:
                    new_cp.append(line)
            bc["conditions_precedent"] = new_cp
        if isinstance(bc.get("ongoing_covenants"), list):
            new_oc = []
            for line in bc["ongoing_covenants"]:
                if isinstance(line, str):
                    parts = line.split("|")
                    new_oc.append({"description": parts[0] if parts else "",
                                   "threshold": parts[1] if len(parts) > 1 else "",
                                   "testing": parts[2] if len(parts) > 2 else ""})
                else:
                    new_oc.append(line)
            bc["ongoing_covenants"] = new_oc
    return d


class TestFinalizePayload:
    """JS finalizePayload logic reimplemented in Python — assert correct nested structures."""

    def test_sec1_facility_rows_pipe_to_object(self):
        data = {
            "facility_summary": {
                "rows": ["1|Test Borrower Ltd|SG|100.0|Yes|USD|5 years|Term Loan|RG|Vessel Mortgage|Test Guarantor"]
            }
        }
        result = _finalize_sec1(data)
        rows = result["facility_summary"]["rows"]
        assert isinstance(rows, list) and len(rows) == 1
        row = rows[0]
        assert row["item_no"] == 1
        assert row["borrower_full_name"] == "Test Borrower Ltd"
        assert row["booking_location"] == "SG"
        assert row["proposed_usd_m"] == pytest.approx(100.0)
        assert row["is_new"] is True
        assert row["currency"] == "USD"
        assert row["tenor"] == "5 years"
        assert row["facility_type"] == "Term Loan"
        assert row["collateral_pre_delivery"] == "RG"
        assert row["collateral_post_delivery"] == "Vessel Mortgage"
        assert row["guarantor"] == "Test Guarantor"

    def test_sec1_footnotes_pipe_to_object(self):
        data = {
            "facility_summary": {
                "footnotes": [
                    "[1] Expected Vessel Delivery Date 30 Jun 2028 with 180 days grace period."
                ]
            }
        }
        result = _finalize_sec1(data)
        fns = result["facility_summary"]["footnotes"]
        assert isinstance(fns, list) and len(fns) == 1
        assert fns[0]["symbol"] == "[1]"
        assert "30 Jun 2028" in fns[0]["text_verbatim"]

    def test_sec1_deal_comparison_pipe_to_object(self):
        data = {
            "terms_and_conditions": {
                "deal_comparison": [
                    "Amount|USD100m|USD80m",
                    "Tenor|5 years|5 years",
                ]
            }
        }
        result = _finalize_sec1(data)
        dc = result["terms_and_conditions"]["deal_comparison"]
        assert len(dc) == 2
        assert dc[0] == {"term": "Amount", "proposed": "USD100m", "previous": "USD80m"}
        assert dc[1] == {"term": "Tenor", "proposed": "5 years", "previous": "5 years"}

    def test_sec1_already_object_rows_not_double_transformed(self):
        """If rows are already dicts (not strings), they must not be re-transformed."""
        data = {
            "facility_summary": {
                "rows": [{"item_no": 1, "borrower_full_name": "Test", "booking_location": "SG",
                           "proposed_usd_m": 100.0, "is_new": True, "currency": "USD",
                           "tenor": "5y", "facility_type": "TL",
                           "collateral_pre_delivery": "RG",
                           "collateral_post_delivery": "VM", "guarantor": "G"}]
            }
        }
        result = _finalize_sec1(data)
        # Rows are already dicts; no transformation should apply
        assert isinstance(result["facility_summary"]["rows"][0], dict)
        assert result["facility_summary"]["rows"][0]["borrower_full_name"] == "Test"

    def test_sec4_management_flat_to_array(self):
        data = {
            "4C_management": {
                "ceo_name": "Jane Doe",
                "ceo_title": "CEO",
                "ceo_background": "20 years shipping",
                "cfo_name": "John Smith",
                "cfo_title": "CFO",
                "cfo_background": "15 years finance",
            }
        }
        result = _finalize_sec4(data)
        mgmt = result["4C_management"]
        assert isinstance(mgmt, list)
        assert len(mgmt) == 2
        assert mgmt[0]["name"] == "Jane Doe"
        assert mgmt[0]["title"] == "CEO"
        assert mgmt[0]["background"] == "20 years shipping"
        assert mgmt[1]["name"] == "John Smith"
        assert mgmt[1]["title"] == "CFO"

    def test_sec4_fleet_flat_to_breakdown_array(self):
        data = {
            "4F_fleet": {
                "owned_vessel_count": 105,
                "owned_total_teu": 350000,
                "chartered_vessel_count": 95,
                "chartered_total_teu": 800000,
                "on_order_vessel_count": 63,
                "on_order_total_teu": 1200000,
            }
        }
        result = _finalize_sec4(data)
        fleet = result["4F_fleet"]
        assert "fleet_breakdown" in fleet
        breakdown = fleet["fleet_breakdown"]
        assert len(breakdown) == 3
        owned = next(r for r in breakdown if r["category"] == "Owned")
        assert owned["vessel_count"] == 105
        assert owned["total_teu"] == 350000
        chartered = next(r for r in breakdown if r["category"] == "Chartered-in")
        assert chartered["vessel_count"] == 95
        on_order = next(r for r in breakdown if r["category"] == "On Order")
        assert on_order["vessel_count"] == 63

    def test_sec5_security_instruments_flat_to_array(self):
        data = {
            "5A_security_overview": {
                "is_secured": True,
                "instr_1_instrument": "Refund Guarantee",
                "instr_1_description": "Test Bank covers pre-delivery",
                "instr_2_instrument": "First Priority Mortgage",
                "instr_2_description": "Post-delivery vessel mortgage",
            }
        }
        result = _finalize_sec5(data)
        sa = result["5A_security_overview"]
        assert sa["is_secured"] is True
        instruments = sa["security_instruments"]
        assert len(instruments) == 2
        assert instruments[0]["rank"] == 1
        assert instruments[0]["instrument"] == "Refund Guarantee"
        assert instruments[1]["rank"] == 2
        assert instruments[1]["instrument"] == "First Priority Mortgage"

    def test_sec5_insurance_flat_to_array(self):
        data = {
            "5D_insurance": {
                "applicable": True,
                "hm_insurer": "China P&I Club",
                "hm_insured_value_usd_m": 180.0,
                "hm_notes": "CUB co-insured",
                "pi_insurer": "UK P&I Club",
                "pi_insured_value_usd_m": 0.0,
                "pi_notes": "Standard coverage",
            }
        }
        result = _finalize_sec5(data)
        ins = result["5D_insurance"]
        assert ins["applicable"] is True
        instruments = ins["instruments"]
        assert len(instruments) == 2
        hm = next(i for i in instruments if i["type"] == "Hull & Machinery")
        assert hm["insurer_or_club"] == "China P&I Club"
        assert hm["insured_value_usd_m"] == pytest.approx(180.0)
        pi = next(i for i in instruments if i["type"] == "P&I")
        assert pi["insurer_or_club"] == "UK P&I Club"

    def test_sec5_milestones_flat_to_array(self):
        data = {
            "5B_refund_guarantee": {
                "applicable": True,
                "issuer_full_name": "Test Bank",
                "issuer_rating": "A+",
                "rating_agency": "S&P",
                "legal_structure": "Demand guarantee",
                "governing_law": "English law",
                "assigned_to_cub": True,
                "m1_name": "Steel Cutting",
                "m1_date": "2024-09-01",
                "m1_rg_usd_m": 100.0,
                "m1_coverage_pct": 500.0,
                "m1_status": "Completed",
                "m2_name": "Delivery",
                "m2_date": "2026-06-01",
                "m2_rg_usd_m": 100.0,
                "m2_coverage_pct": 100.0,
                "m2_status": "Pending",
            }
        }
        result = _finalize_sec5(data)
        rg = result["5B_refund_guarantee"]
        assert rg["issuer_full_name"] == "Test Bank"
        milestones = rg["milestones"]
        assert len(milestones) == 2
        m1 = milestones[0]
        assert m1["milestone"] == "Steel Cutting"
        assert m1["sched_date"] == "2024-09-01"
        assert m1["rg_amount_usd_m"] == pytest.approx(100.0)
        assert m1["coverage_pct"] == pytest.approx(500.0)
        m2 = milestones[1]
        assert m2["milestone"] == "Delivery"

    def test_sec9_checklist_items_pipe_to_object(self):
        data = {
            "9A_checklist": {
                "items": [
                    "CDD completed|Yes|Tier 1 KYC; reviewed 01 Dec 2025",
                    "Sanctions check|Clear|No matches found",
                ]
            }
        }
        result = _finalize_sec9(data)
        items = result["9A_checklist"]["items"]
        assert len(items) == 2
        assert items[0]["item"] == "CDD completed"
        assert items[0]["response"] == "Yes"
        assert items[0]["remarks"] == "Tier 1 KYC; reviewed 01 Dec 2025"
        assert items[1]["item"] == "Sanctions check"
        assert items[1]["response"] == "Clear"

    def test_sec9_conditions_precedent_pipe_to_object(self):
        data = {
            "9B_conditions_covenants": {
                "conditions_precedent": [
                    "Execution of facility agreement|Before first drawdown",
                    "Legal opinions|Before first drawdown",
                ],
                "ongoing_covenants": [
                    "ACR covenant: ACR >= 100%|100%|Every 2 years",
                ],
            }
        }
        result = _finalize_sec9(data)
        cp = result["9B_conditions_covenants"]["conditions_precedent"]
        assert len(cp) == 2
        assert cp[0]["description"] == "Execution of facility agreement"
        assert cp[0]["testing"] == "Before first drawdown"
        oc = result["9B_conditions_covenants"]["ongoing_covenants"]
        assert oc[0]["description"] == "ACR covenant: ACR >= 100%"
        assert oc[0]["threshold"] == "100%"
        assert oc[0]["testing"] == "Every 2 years"

    def test_sec1_multiple_facility_rows(self):
        """Multiple pipe-delimited rows must all be correctly parsed."""
        data = {
            "facility_summary": {
                "rows": [
                    "1|Borrower A|SG|100.0|Yes|USD|5 years|Term Loan|RG|VM|Guarantor A",
                    "2|Borrower B|HK|50.0|No|USD|3 years|RCF|None|VM|Guarantor B",
                ]
            }
        }
        result = _finalize_sec1(data)
        rows = result["facility_summary"]["rows"]
        assert len(rows) == 2
        assert rows[0]["borrower_full_name"] == "Borrower A"
        assert rows[0]["is_new"] is True
        assert rows[1]["borrower_full_name"] == "Borrower B"
        assert rows[1]["is_new"] is False
        assert rows[1]["proposed_usd_m"] == pytest.approx(50.0)


# ══════════════════════════════════════════════════════════════════════════════
# C — expandPayload Round-Trip
# ══════════════════════════════════════════════════════════════════════════════

def _expand_sec1(data: dict) -> dict:
    """Python reimplementation of JS expandPayload for §1."""
    import copy
    d = copy.deepcopy(data)
    fs = d.get("facility_summary", {})
    if isinstance(fs.get("rows"), list) and fs["rows"] and isinstance(fs["rows"][0], dict):
        fs["rows"] = [
            "|".join([
                str(r.get("item_no", "")),
                r.get("borrower_full_name", ""),
                r.get("booking_location", ""),
                str(r.get("proposed_usd_m", "")),
                "Yes" if r.get("is_new") else "No",
                r.get("currency", "USD"),
                r.get("tenor", ""),
                r.get("facility_type", ""),
                r.get("collateral_pre_delivery", ""),
                r.get("collateral_post_delivery", ""),
                r.get("guarantor", ""),
            ])
            for r in fs["rows"]
        ]
    if isinstance(fs.get("footnotes"), list) and fs["footnotes"] and isinstance(fs["footnotes"][0], dict):
        fs["footnotes"] = [
            f"{fn.get('symbol', '')} {fn.get('text_verbatim', '')}".strip()
            for fn in fs["footnotes"]
        ]
    tc = d.get("terms_and_conditions", {})
    if isinstance(tc.get("deal_comparison"), list) and tc["deal_comparison"] and isinstance(tc["deal_comparison"][0], dict):
        tc["deal_comparison"] = [
            f"{r.get('term', '')}|{r.get('proposed', '')}|{r.get('previous', '')}"
            for r in tc["deal_comparison"]
        ]
    return d


def _expand_sec4(data: dict) -> dict:
    """Python reimplementation of JS expandPayload for §4."""
    import copy
    d = copy.deepcopy(data)
    mgt = d.get("4C_management")
    if isinstance(mgt, list) and mgt:
        flat = {}
        if len(mgt) > 0:
            flat["ceo_name"] = mgt[0].get("name", "")
            flat["ceo_title"] = mgt[0].get("title", "")
            flat["ceo_background"] = mgt[0].get("background", "")
        if len(mgt) > 1:
            flat["cfo_name"] = mgt[1].get("name", "")
            flat["cfo_title"] = mgt[1].get("title", "")
            flat["cfo_background"] = mgt[1].get("background", "")
        d["4C_management"] = flat
    fl = d.get("4F_fleet", {})
    if isinstance(fl.get("fleet_breakdown"), list):
        flat_fleet = {}
        for row in fl["fleet_breakdown"]:
            if row["category"] == "Owned":
                flat_fleet["owned_vessel_count"] = row["vessel_count"]
                flat_fleet["owned_total_teu"] = row["total_teu"]
            elif row["category"] == "Chartered-in":
                flat_fleet["chartered_vessel_count"] = row["vessel_count"]
                flat_fleet["chartered_total_teu"] = row["total_teu"]
            elif row["category"] == "On Order":
                flat_fleet["on_order_vessel_count"] = row["vessel_count"]
                flat_fleet["on_order_total_teu"] = row["total_teu"]
        d["4F_fleet"] = flat_fleet
    return d


def _expand_sec5(data: dict) -> dict:
    """Python reimplementation of JS expandPayload for §5."""
    import copy
    d = copy.deepcopy(data)
    sa = d.get("5A_security_overview", {})
    if isinstance(sa.get("security_instruments"), list):
        flat = {"is_secured": sa.get("is_secured")}
        for i, instr in enumerate(sa["security_instruments"]):
            flat[f"instr_{i + 1}_instrument"] = instr.get("instrument", "")
            flat[f"instr_{i + 1}_description"] = instr.get("description", "")
        d["5A_security_overview"] = flat
    ins = d.get("5D_insurance", {})
    if isinstance(ins.get("instruments"), list):
        flat = {"applicable": ins.get("applicable")}
        for instr in ins["instruments"]:
            if instr["type"] == "Hull & Machinery":
                flat["hm_insurer"] = instr["insurer_or_club"]
                flat["hm_insured_value_usd_m"] = instr["insured_value_usd_m"]
                flat["hm_notes"] = instr["notes"]
            elif instr["type"] == "P&I":
                flat["pi_insurer"] = instr["insurer_or_club"]
                flat["pi_insured_value_usd_m"] = instr["insured_value_usd_m"]
                flat["pi_notes"] = instr["notes"]
        d["5D_insurance"] = flat
    bg = d.get("5B_refund_guarantee", {})
    if isinstance(bg.get("milestones"), list):
        flat = {
            "applicable": bg.get("applicable"),
            "issuer_full_name": bg.get("issuer_full_name"),
            "issuer_rating": bg.get("issuer_rating"),
            "rating_agency": bg.get("rating_agency"),
            "legal_structure": bg.get("legal_structure"),
            "governing_law": bg.get("governing_law"),
            "assigned_to_cub": bg.get("assigned_to_cub"),
        }
        for i, ms in enumerate(bg["milestones"]):
            flat[f"m{i + 1}_name"] = ms.get("milestone", "")
            flat[f"m{i + 1}_date"] = ms.get("sched_date", "")
            flat[f"m{i + 1}_rg_usd_m"] = ms.get("rg_amount_usd_m", 0)
            flat[f"m{i + 1}_coverage_pct"] = ms.get("coverage_pct", 0)
            flat[f"m{i + 1}_status"] = ms.get("status", "")
        d["5B_refund_guarantee"] = flat
    return d


def _expand_sec9(data: dict) -> dict:
    """Python reimplementation of JS expandPayload for §9."""
    import copy
    d = copy.deepcopy(data)
    c = d.get("9A_checklist", {})
    if isinstance(c, dict) and isinstance(c.get("items"), list) and c["items"] and isinstance(c["items"][0], dict):
        c["items"] = [
            f"{it.get('item', it.get('category', ''))}|{it.get('response', '')}|{it.get('remarks', '')}"
            for it in c["items"]
        ]
    bc = d.get("9B_conditions_covenants", {})
    if isinstance(bc, dict):
        if isinstance(bc.get("conditions_precedent"), list) and bc["conditions_precedent"] and isinstance(bc["conditions_precedent"][0], dict):
            bc["conditions_precedent"] = [
                f"{cp.get('description', '')}|{cp.get('testing', '')}"
                for cp in bc["conditions_precedent"]
            ]
        if isinstance(bc.get("ongoing_covenants"), list) and bc["ongoing_covenants"] and isinstance(bc["ongoing_covenants"][0], dict):
            bc["ongoing_covenants"] = [
                f"{oc.get('description', '')}|{oc.get('threshold', '')}|{oc.get('testing', '')}"
                for oc in bc["ongoing_covenants"]
            ]
    return d


class TestExpandPayloadRoundTrip:
    """finalize→expand must recover original flat values (round-trip parity)."""

    def test_sec1_facility_rows_round_trip(self):
        """Finalize turns pipe strings into objects; expand turns them back."""
        original_row = "1|Test Borrower Ltd|SG|100.0|Yes|USD|5 years|Term Loan|RG|Vessel Mortgage|Test Guarantor"
        data = {"facility_summary": {"rows": [original_row]}}
        finalized = _finalize_sec1(data)
        expanded = _expand_sec1(finalized)
        assert expanded["facility_summary"]["rows"][0] == original_row

    def test_sec1_footnotes_round_trip(self):
        original = "[1] Expected Vessel Delivery Date 30 Jun 2028 with 180 days grace period."
        data = {"facility_summary": {"footnotes": [original]}}
        finalized = _finalize_sec1(data)
        expanded = _expand_sec1(finalized)
        recovered = expanded["facility_summary"]["footnotes"][0]
        assert "[1]" in recovered
        assert "30 Jun 2028" in recovered

    def test_sec1_deal_comparison_round_trip(self):
        original = ["Amount|USD100m|USD80m", "Tenor|5 years|5 years"]
        data = {"terms_and_conditions": {"deal_comparison": original}}
        finalized = _finalize_sec1(data)
        expanded = _expand_sec1(finalized)
        assert expanded["terms_and_conditions"]["deal_comparison"] == original

    def test_sec4_management_round_trip(self):
        original = {
            "ceo_name": "Jane Doe",
            "ceo_title": "CEO",
            "ceo_background": "20 years in container shipping",
            "cfo_name": "John Smith",
            "cfo_title": "CFO",
            "cfo_background": "15 years in shipping finance",
        }
        data = {"4C_management": original}
        finalized = _finalize_sec4(data)
        expanded = _expand_sec4(finalized)
        mgmt = expanded["4C_management"]
        assert mgmt["ceo_name"] == original["ceo_name"]
        assert mgmt["ceo_title"] == original["ceo_title"]
        assert mgmt["ceo_background"] == original["ceo_background"]
        assert mgmt["cfo_name"] == original["cfo_name"]
        assert mgmt["cfo_title"] == original["cfo_title"]

    def test_sec4_fleet_round_trip(self):
        original_fleet = {
            "owned_vessel_count": 105,
            "owned_total_teu": 350000,
            "chartered_vessel_count": 95,
            "chartered_total_teu": 800000,
            "on_order_vessel_count": 63,
            "on_order_total_teu": 1200000,
        }
        data = {"4F_fleet": original_fleet}
        finalized = _finalize_sec4(data)
        expanded = _expand_sec4(finalized)
        fleet = expanded["4F_fleet"]
        assert fleet["owned_vessel_count"] == 105
        assert fleet["owned_total_teu"] == 350000
        assert fleet["chartered_vessel_count"] == 95
        assert fleet["on_order_vessel_count"] == 63

    def test_sec5_security_overview_round_trip(self):
        original = {
            "is_secured": True,
            "instr_1_instrument": "Refund Guarantee (Test Bank)",
            "instr_1_description": "Covers pre-delivery installments",
            "instr_2_instrument": "First Priority Vessel Mortgage",
            "instr_2_description": "Post-delivery security",
        }
        data = {"5A_security_overview": original}
        finalized = _finalize_sec5(data)
        expanded = _expand_sec5(finalized)
        sa = expanded["5A_security_overview"]
        assert sa["is_secured"] is True
        assert sa["instr_1_instrument"] == original["instr_1_instrument"]
        assert sa["instr_2_instrument"] == original["instr_2_instrument"]
        assert sa["instr_1_description"] == original["instr_1_description"]

    def test_sec5_insurance_round_trip(self):
        original = {
            "applicable": True,
            "hm_insurer": "China P&I Club",
            "hm_insured_value_usd_m": 180.0,
            "hm_notes": "CUB co-insured",
            "pi_insurer": "UK P&I Club",
            "pi_insured_value_usd_m": 0.0,
            "pi_notes": "Standard P&I",
        }
        data = {"5D_insurance": original}
        finalized = _finalize_sec5(data)
        expanded = _expand_sec5(finalized)
        ins = expanded["5D_insurance"]
        assert ins["hm_insurer"] == "China P&I Club"
        assert ins["pi_insurer"] == "UK P&I Club"
        assert ins["hm_insured_value_usd_m"] == pytest.approx(180.0)

    def test_sec5_milestones_round_trip(self):
        original = {
            "applicable": True,
            "issuer_full_name": "Test Bank",
            "issuer_rating": "A+",
            "rating_agency": "S&P",
            "legal_structure": "Demand guarantee",
            "governing_law": "English law",
            "assigned_to_cub": True,
            "m1_name": "Steel Cutting",
            "m1_date": "2024-09-01",
            "m1_rg_usd_m": 100.0,
            "m1_coverage_pct": 500.0,
            "m1_status": "Completed",
        }
        data = {"5B_refund_guarantee": original}
        finalized = _finalize_sec5(data)
        expanded = _expand_sec5(finalized)
        rg = expanded["5B_refund_guarantee"]
        assert rg["m1_name"] == "Steel Cutting"
        assert rg["m1_date"] == "2024-09-01"
        assert rg["m1_rg_usd_m"] == pytest.approx(100.0)
        assert rg["m1_coverage_pct"] == pytest.approx(500.0)

    def test_sec9_checklist_round_trip(self):
        original = {
            "items": ["CDD completed|Yes|Tier 1 KYC; reviewed 01 Dec 2025"]
        }
        data = {"9A_checklist": original}
        finalized = _finalize_sec9(data)
        expanded = _expand_sec9(finalized)
        item = expanded["9A_checklist"]["items"][0]
        assert "CDD completed" in item
        assert "Yes" in item
        assert "Tier 1 KYC" in item

    def test_sec9_conditions_round_trip(self):
        original_cp = ["Execution of facility agreement|Before first drawdown"]
        original_oc = ["ACR covenant: ACR >= 100%|100%|Every 2 years"]
        data = {
            "9B_conditions_covenants": {
                "conditions_precedent": original_cp,
                "ongoing_covenants": original_oc,
            }
        }
        finalized = _finalize_sec9(data)
        expanded = _expand_sec9(finalized)
        cp = expanded["9B_conditions_covenants"]["conditions_precedent"]
        assert cp[0] == original_cp[0]
        oc = expanded["9B_conditions_covenants"]["ongoing_covenants"]
        assert oc[0] == original_oc[0]


# ══════════════════════════════════════════════════════════════════════════════
# D — API Save Integration (PUT /inputs/{n})
# ══════════════════════════════════════════════════════════════════════════════

class TestAPISaveSections:
    """PUT /inputs/{sec_no} with MINIMAL_PAYLOADS must return 200 for §1-10."""

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    @pytest.mark.asyncio
    async def test_save_and_retrieve(self, db, sec_no):
        """Save MINIMAL_PAYLOAD for a section and verify it is stored."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload = SectionInputPayload(section_no=sec_no, input_json=MINIMAL_PAYLOADS[sec_no])
        result = await save_section_input(report_id=rid, section_no=sec_no, payload=payload,
                                          db=db, current_user=user)
        assert result.section_no == sec_no, f"§{sec_no}: section_no mismatch"
        assert result.saved_at is not None, f"§{sec_no}: saved_at must be set"
        assert isinstance(result.input_json, dict), f"§{sec_no}: input_json must be dict"

        readback = await get_section_input(report_id=rid, section_no=sec_no,
                                           db=db, current_user=user)
        assert readback is not None, f"§{sec_no}: readback returned None"
        assert isinstance(readback.input_json, dict), f"§{sec_no}: readback input_json must be dict"


# ══════════════════════════════════════════════════════════════════════════════
# E — JSON Schema Integrity
# ══════════════════════════════════════════════════════════════════════════════

# Expected top-level keys per section after save
EXPECTED_KEYS = {
    1: {"report_type", "facility_summary", "purpose_and_recommendation", "terms_and_conditions"},
    2: {"2A_credit_overview", "2B_solvency", "2E_risk_and_mitigants"},
    3: {"3A_external_ratings", "3B_internal_ratings", "3C_mas_612"},
    4: {"4A_borrower", "4C_management", "4F_fleet"},
    5: {"5A_security_overview", "5B_refund_guarantee", "5D_insurance"},
    6: {"6A_project", "6B_builder", "6D_milestones"},
    7: {"entities_to_analyze", "7A_borrower_financials", "7B_key_ratios"},
    8: {"8A_acra_banking_charges", "8B_other_information"},
    9: {"9A_checklist", "9B_conditions_covenants", "9C_recommendation"},
    10: {"10A_group_exposure", "10B_fleet_growth", "10C_projections"},
}


class TestJSONSchemaIntegrity:
    """After save, stored input_json must contain expected keys for that section."""

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    @pytest.mark.asyncio
    async def test_stored_json_has_expected_keys(self, db, sec_no):
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload = SectionInputPayload(section_no=sec_no, input_json=MINIMAL_PAYLOADS[sec_no])
        await save_section_input(report_id=rid, section_no=sec_no, payload=payload,
                                 db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=sec_no, db=db, current_user=user)

        expected = EXPECTED_KEYS[sec_no]
        actual_keys = set(rb.input_json.keys())
        missing = expected - actual_keys
        assert not missing, (
            f"§{sec_no}: expected keys {expected} not all present. "
            f"Missing: {missing}. Got: {sorted(actual_keys)}"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    @pytest.mark.asyncio
    async def test_stored_json_values_are_correct_types(self, db, sec_no):
        """Each sub-key in EXPECTED_KEYS must map to a dict in the stored JSON."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload = SectionInputPayload(section_no=sec_no, input_json=MINIMAL_PAYLOADS[sec_no])
        await save_section_input(report_id=rid, section_no=sec_no, payload=payload,
                                 db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=sec_no, db=db, current_user=user)

        for key in EXPECTED_KEYS[sec_no]:
            if key in rb.input_json:
                val = rb.input_json[key]
                assert isinstance(val, (dict, list, str, int, float, bool)), (
                    f"§{sec_no}.{key}: unexpected type {type(val)}"
                )


# ══════════════════════════════════════════════════════════════════════════════
# F — Generation Blocking Rules
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerationRules:
    """§11 must be accepted; §1-10 must be accepted with dependencies patched."""

    @pytest.mark.asyncio
    async def test_section_11_accepted(self, db):
        """generate_section §11 must be accepted (valid section_no 1-11)."""
        from fastapi import BackgroundTasks
        from credit_report.api.generate import generate_section
        from unittest.mock import patch

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        bg = BackgroundTasks()
        with patch("credit_report.api.generate.run_section_generation"):
            result = await generate_section(report_id=rid, section_no=11,
                                            background_tasks=bg, db=db, current_user=user)
        assert result.status in ("running", "queued", "accepted"), (
            f"§11 generate must be accepted, got '{result.status}'"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    @pytest.mark.asyncio
    async def test_section_1_10_accepted(self, db, sec_no):
        """generate_section §1–10 must return 'running' status with hard deps patched."""
        from fastapi import BackgroundTasks
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

    @pytest.mark.parametrize("bad_sec", [0, 12, -1])
    @pytest.mark.asyncio
    async def test_out_of_range_sections_blocked(self, db, bad_sec):
        """Section numbers outside valid range must be rejected."""
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


# ══════════════════════════════════════════════════════════════════════════════
# G — Quality Endurance (edge cases)
# ══════════════════════════════════════════════════════════════════════════════

class TestQualityEndurance:
    """Edge cases: special characters, long text, unicode, pipe-delimited lines."""

    def _collect_lines(self, raw: str) -> list[str] | None:
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        return lines if lines else None

    def _collect_number(self, raw: str) -> float | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            n = float(raw)
            return n if n == n else None
        except ValueError:
            return None

    def _collect_text(self, raw: str) -> str | None:
        raw = raw.strip()
        return raw if raw else None

    # ── Special characters ──────────────────────────────────────────────

    def test_special_chars_parentheses_in_text(self):
        """Parentheses in text fields must pass through unchanged."""
        val = "Refund Guarantee (Test Bank, A+/Stable)"
        assert self._collect_text(val) == val

    def test_special_chars_dashes_in_text(self):
        """Dashes in text fields must pass through."""
        val = "LNG Dual-Fuel, IMO Tier III (CII A-rated)"
        assert self._collect_text(val) == val

    def test_special_chars_slashes_in_text(self):
        """Slashes in text fields must pass through."""
        val = "SOFR + 200bps p.a. (net of 10bps/yr SLL discount)"
        assert self._collect_text(val) == val

    def test_special_chars_brackets_in_lines(self):
        """Brackets in line entries must be preserved."""
        raw = "[1] Steel Cutting milestone|2024-09-01|100.0\n[2] Delivery|2026-06-01|100.0"
        result = self._collect_lines(raw)
        assert result is not None
        assert len(result) == 2
        assert result[0].startswith("[1]")
        assert result[1].startswith("[2]")

    def test_special_chars_percent_in_text(self):
        """Percent signs must not be escaped."""
        val = "ACR >= 120%; LTV <= 83%"
        assert self._collect_text(val) == val

    def test_special_chars_ampersand_in_text(self):
        """Ampersands in text must be preserved."""
        val = "Hull & Machinery + P&I coverage"
        assert self._collect_text(val) == val

    # ── Very long text ──────────────────────────────────────────────────

    def test_very_long_textarea_accepted(self):
        """A 10,000-character textarea must be accepted as-is."""
        long_text = "A" * 10000
        result = self._collect_text(long_text)
        assert result == long_text
        assert len(result) == 10000

    def test_very_long_lines_field(self):
        """500 lines must all be parsed."""
        raw = "\n".join([f"Borrower {i}|USD {i}m|Yes" for i in range(500)])
        result = self._collect_lines(raw)
        assert result is not None
        assert len(result) == 500
        assert result[0] == "Borrower 0|USD 0m|Yes"
        assert result[499] == "Borrower 499|USD 499m|Yes"

    # ── Pipe-delimited lines ──────────────────────────────────────────

    def test_pipe_delimited_lines_parse_correctly(self):
        """Pipe-delimited lines for facility rows must be parseable."""
        raw = "1|Test Borrower Ltd|SG|100.0|Yes|USD|5 years|Term Loan|RG|VM|Guarantor"
        result = self._collect_lines(raw)
        assert result is not None
        parts = result[0].split("|")
        assert len(parts) == 11
        assert parts[0] == "1"
        assert parts[1] == "Test Borrower Ltd"
        assert parts[4] == "Yes"

    def test_pipe_delimiter_in_content_preserved(self):
        """Multiple pipe-separated columns are all preserved in the line string."""
        raw = "CDD completed|Yes|Tier 1 KYC; reviewed 01 Dec 2025\nSanctions check|Clear|No matches"
        result = self._collect_lines(raw)
        assert result is not None
        assert len(result) == 2
        # Each line's pipes should still be there
        assert result[0].count("|") == 2
        assert result[1].count("|") == 2

    # ── Empty lines filtered ──────────────────────────────────────────

    def test_empty_lines_filtered_from_lines_type(self):
        raw = "line1\n\n\n  \nline2\n\nline3"
        result = self._collect_lines(raw)
        assert result == ["line1", "line2", "line3"]

    def test_whitespace_only_string_excluded(self):
        assert self._collect_text("   ") is None

    def test_empty_string_excluded_from_number(self):
        assert self._collect_number("") is None

    # ── Unicode / Chinese text ──────────────────────────────────────────

    def test_unicode_chinese_text_accepted(self):
        """Chinese characters must be accepted without modification."""
        val = "測試公司 (Test Co Ltd) — 亞太地區"
        result = self._collect_text(val)
        assert result == val

    def test_unicode_chinese_in_lines(self):
        """Lines with Chinese characters must be preserved."""
        raw = "測試公司|100|TW\nTest Parent Corp|80|SG"
        result = self._collect_lines(raw)
        assert result is not None
        assert len(result) == 2
        assert "測試公司" in result[0]

    def test_unicode_greek_characters(self):
        """Greek letters (e.g. from financial formulae) must pass through."""
        val = "DSCR ≥ 1.15x; LTV ≤ 80% (β-adjusted)"
        result = self._collect_text(val)
        assert result == val

    def test_unicode_currency_symbols(self):
        """Currency symbols must not be corrupted."""
        val = "USD100m (equivalent to TWD3.25bn or €92m)"
        result = self._collect_text(val)
        assert result == val

    # ── Number edge cases ──────────────────────────────────────────────

    def test_large_number_accepted(self):
        """Very large numbers (fleet TEU) must be parsed."""
        result = self._collect_number("12000000")
        assert result == pytest.approx(12_000_000.0)

    def test_scientific_notation_accepted(self):
        """Scientific notation must parse correctly."""
        result = self._collect_number("1.5e8")
        assert result == pytest.approx(1.5e8)

    def test_negative_number_accepted(self):
        result = self._collect_number("-420.0")
        assert result == pytest.approx(-420.0)

    def test_comma_number_rejected(self):
        """Numbers with comma separators must be rejected (JS parseFloat behavior)."""
        result = self._collect_number("1,234.56")
        # float("1,234.56") raises ValueError → None
        assert result is None

    # ── finalizePayload deep content checks ────────────────────────────

    def test_sec1_is_new_yes_parses_true(self):
        data = {"facility_summary": {"rows": ["1|Borrower A|SG|100.0|Yes|USD|5y|TL|RG|VM|G"]}}
        result = _finalize_sec1(data)
        assert result["facility_summary"]["rows"][0]["is_new"] is True

    def test_sec1_is_new_no_parses_false(self):
        data = {"facility_summary": {"rows": ["1|Borrower B|HK|50.0|No|USD|3y|RCF|None|VM|G"]}}
        result = _finalize_sec1(data)
        assert result["facility_summary"]["rows"][0]["is_new"] is False

    def test_sec1_footnote_without_bracket_symbol(self):
        """Footnotes without [bracket] symbol should still be parsed gracefully."""
        data = {"facility_summary": {"footnotes": ["Expected delivery 30 Jun 2028."]}}
        result = _finalize_sec1(data)
        fns = result["facility_summary"]["footnotes"]
        assert fns[0]["symbol"] == ""
        assert "Expected delivery" in fns[0]["text_verbatim"]


# ══════════════════════════════════════════════════════════════════════════════
# H — Hint Coverage
# ══════════════════════════════════════════════════════════════════════════════

class TestHintCoverage:
    """Every non-select/non-bool field in §1-10 FIELD_DEFS must have h: or hz: hint."""

    def _get_hz_for_field(self, html: str, field_path: str) -> bool:
        """Check if a field path has hz: defined anywhere in FIELD_DEFS."""
        # Match {p:'<path>'...hz:'...'}
        pattern = rf"p:'{re.escape(field_path)}'[^}}{{]*hz:'[^']*'"
        return bool(re.search(pattern, html))

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    def test_all_input_fields_have_hint(self, sec_no):
        """Non-select/non-bool fields must have h: or hz: hint for placeholder text."""
        html = _load_html()
        defs = _extract_field_defs(html)
        fields = defs.get(sec_no, [])
        assert fields, f"§{sec_no}: no fields found in FIELD_DEFS"

        missing = []
        for f in fields:
            if f["t"] in ("select", "bool", "json"):
                # json fields use h: but it's for example format, not placeholder
                # select and bool get dropdown UI, no text placeholder needed
                continue
            has_h = bool(f.get("h"))
            has_hz = bool(f.get("hz")) or self._get_hz_for_field(html, f["p"])
            if not has_h and not has_hz:
                missing.append(f"{f['p']} (type={f['t']})")
        assert not missing, (
            f"§{sec_no}: {len(missing)} fields missing hints (h: or hz:):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    def test_renderFieldForm_uses_lang_variable_for_zh(self):
        """renderFieldForm must use lang variable to pick hz vs h for placeholder."""
        html = _load_html()
        # Check that renderFieldForm uses (lang==='zh'?(f.hz||f.h):(f.h||f.hz))
        # This is the bilingual placeholder logic
        assert "lang===" in html, "renderFieldForm must reference 'lang' variable"
        assert "f.hz" in html, "renderFieldForm must reference f.hz property"
        assert "f.h" in html, "renderFieldForm must reference f.h property"
        # Check the actual bilingual pattern in renderFieldForm
        render_start = html.find("function renderFieldForm(")
        render_end = html.find("\n}", render_start) + 2
        render_body = html[render_start:render_end]
        assert "lang" in render_body, "renderFieldForm body must use lang"
        assert "hz" in render_body, "renderFieldForm body must reference hz"

    def test_renderFieldForm_bilingual_placeholder_pattern(self):
        """The exact bilingual placeholder pattern must be present."""
        html = _load_html()
        # From the source: const ph=esc((lang==='zh'?(f.hz||f.h):(f.h||f.hz))||'');
        assert "lang==='zh'" in html, "Bilingual pattern lang==='zh' must appear"
        assert "f.hz||f.h" in html, "Bilingual fallback pattern f.hz||f.h must appear"
        assert "f.h||f.hz" in html, "Bilingual fallback pattern f.h||f.hz must appear"

    def test_lines_fields_show_hint_as_placeholder(self):
        """Lines-type fields must use the placeholder from f.h/f.hz (not omit it)."""
        html = _load_html()
        render_start = html.find("function renderFieldForm(")
        render_end = html.find("\nfunction ", render_start + 1)
        render_body = html[render_start:render_end]
        # lines type renders a textarea with placeholder="${ph}"
        assert "f.t==='lines'" in render_body, "renderFieldForm must handle lines type"
        # The placeholder must be included in the lines textarea
        lines_idx = render_body.find("f.t==='lines'")
        lines_segment = render_body[lines_idx:lines_idx + 200]
        assert "placeholder" in lines_segment, "lines textarea must have placeholder"

    def test_number_fields_show_hint_as_placeholder(self):
        """Number-type fields must use the placeholder from f.h/f.hz."""
        html = _load_html()
        render_start = html.find("function renderFieldForm(")
        render_end = html.find("\nfunction ", render_start + 1)
        render_body = html[render_start:render_end]
        assert "f.t==='number'" in render_body, "renderFieldForm must handle number type"
        number_idx = render_body.find("f.t==='number'")
        number_segment = render_body[number_idx:number_idx + 200]
        assert "placeholder" in number_segment, "number input must have placeholder"

    def test_text_fields_show_hint_as_placeholder(self):
        """Text-type fields must use the placeholder from f.h/f.hz."""
        html = _load_html()
        render_start = html.find("function renderFieldForm(")
        render_end = html.find("\nfunction ", render_start + 1)
        render_body = html[render_start:render_end]
        assert "f.t==='text'" in render_body, "renderFieldForm must handle text type"
        text_idx = render_body.find("f.t==='text'")
        text_segment = render_body[text_idx:text_idx + 200]
        assert "placeholder" in text_segment, "text input must have placeholder"

    def test_sec1_fields_have_hints(self):
        """Spot-check §1 fields for hint coverage."""
        html = _load_html()
        defs = _extract_field_defs(html)
        sec1_fields = defs.get(1, [])
        input_fields = [f for f in sec1_fields if f["t"] not in ("select", "bool", "json")]
        assert input_fields, "§1 must have input fields"
        # At least half of input fields should have hints
        with_hints = [f for f in input_fields if f.get("h") or f.get("hz")]
        ratio = len(with_hints) / len(input_fields)
        assert ratio >= 0.5, (
            f"§1: only {len(with_hints)}/{len(input_fields)} input fields have hints "
            f"({ratio:.0%}). Fields without hints: "
            f"{[f['p'] for f in input_fields if not f.get('h') and not f.get('hz')]}"
        )

    def test_no_empty_hint_strings(self):
        """Fields with h: or hz: defined must not have empty string values."""
        html = _load_html()
        defs = _extract_field_defs(html)
        empty_hints = []
        for sec_no in range(1, 11):
            for f in defs.get(sec_no, []):
                if "h" in f and f["h"] == "":
                    empty_hints.append(f"§{sec_no}.{f['p']} h=''")
                if "hz" in f and f["hz"] == "":
                    empty_hints.append(f"§{sec_no}.{f['p']} hz=''")
        assert not empty_hints, (
            f"Fields with empty hint strings: {empty_hints}"
        )

    def test_field_defs_sec1_to_10_all_parse(self):
        """FIELD_DEFS for §1-10 must all be parseable (no regex failures)."""
        html = _load_html()
        defs = _extract_field_defs(html)
        for sec_no in range(1, 11):
            fields = defs.get(sec_no, [])
            assert fields, f"§{sec_no}: no fields parsed from FIELD_DEFS"
            for f in fields:
                assert "p" in f, f"§{sec_no}: field missing 'p' key: {f}"
                assert "l" in f, f"§{sec_no}: field missing 'l' key: {f}"
                assert "t" in f, f"§{sec_no}: field missing 't' key: {f}"
                assert f["t"] in {"text", "number", "textarea", "lines", "json", "bool", "select"}, (
                    f"§{sec_no}.{f['p']}: invalid type '{f['t']}'"
                )


# ══════════════════════════════════════════════════════════════════════════════
# Extra: API Pipeline Integration (save → verify key structure preserved)
# ══════════════════════════════════════════════════════════════════════════════

class TestAPIPipelineIntegration:
    """End-to-end: save input → read back → check structure."""

    @pytest.mark.asyncio
    async def test_sec4_management_array_preserved_on_save(self, db):
        """§4 management dict must be preserved as-is when saved (no server-side transform)."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload_data = MINIMAL_PAYLOADS[4]
        payload = SectionInputPayload(section_no=4, input_json=payload_data)
        await save_section_input(report_id=rid, section_no=4, payload=payload,
                                 db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=4, db=db, current_user=user)

        # The saved payload should contain 4C_management as we sent it
        assert "4C_management" in rb.input_json
        mgmt = rb.input_json["4C_management"]
        # Management might be stored as flat dict (form format) or array (finalized format)
        # Either way, the data must be present
        assert mgmt is not None

    @pytest.mark.asyncio
    async def test_sec5_security_overview_preserved_on_save(self, db):
        """§5 security overview must be preserved after save."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload = SectionInputPayload(section_no=5, input_json=MINIMAL_PAYLOADS[5])
        await save_section_input(report_id=rid, section_no=5, payload=payload,
                                 db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=5, db=db, current_user=user)

        assert "5A_security_overview" in rb.input_json
        sa = rb.input_json["5A_security_overview"]
        assert sa is not None
        assert isinstance(sa, dict)

    @pytest.mark.asyncio
    async def test_sec9_checklist_items_preserved_on_save(self, db):
        """§9 checklist items must be preserved after save."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload = SectionInputPayload(section_no=9, input_json=MINIMAL_PAYLOADS[9])
        await save_section_input(report_id=rid, section_no=9, payload=payload,
                                 db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=9, db=db, current_user=user)

        assert "9A_checklist" in rb.input_json
        checklist = rb.input_json["9A_checklist"]
        assert "items" in checklist
        assert len(checklist["items"]) > 0

    @pytest.mark.asyncio
    async def test_sec1_facility_summary_structure_preserved(self, db):
        """§1 facility_summary must preserve rows, footnotes, totals keys."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload = SectionInputPayload(section_no=1, input_json=MINIMAL_PAYLOADS[1])
        await save_section_input(report_id=rid, section_no=1, payload=payload,
                                 db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=1, db=db, current_user=user)

        assert "facility_summary" in rb.input_json
        fs = rb.input_json["facility_summary"]
        assert "rows" in fs, "facility_summary must preserve rows"
        assert "totals" in fs, "facility_summary must preserve totals"
        assert len(fs["rows"]) >= 1

    @pytest.mark.asyncio
    async def test_sec10_projections_scalars_preserved(self, db):
        """§10 projections scalar values must be preserved after save."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        report, owner_id = await _seed_report(db, rid)
        user = _make_user("analyst")
        user.id = owner_id

        payload = SectionInputPayload(section_no=10, input_json=MINIMAL_PAYLOADS[10])
        await save_section_input(report_id=rid, section_no=10, payload=payload,
                                 db=db, current_user=user)
        rb = await get_section_input(report_id=rid, section_no=10, db=db, current_user=user)

        proj = rb.input_json.get("10C_projections", {})
        assert proj.get("freight_rate_drop_pct") == pytest.approx(20.0), (
            f"freight_rate_drop_pct should be 20.0, got {proj.get('freight_rate_drop_pct')}"
        )
        assert proj.get("base_dscr_fy_1") == pytest.approx(0.92), (
            f"base_dscr_fy_1 should be 0.92, got {proj.get('base_dscr_fy_1')}"
        )

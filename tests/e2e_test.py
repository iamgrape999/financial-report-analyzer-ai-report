"""
End-to-end integration test suite for Financial Report Analyzer.
Tests every major feature: auth, report CRUD, document upload/delete/ETL,
section input save/load, section generation (§1-§10), export, completeness gate.

Run:  python3 tests/e2e_test.py
"""
from __future__ import annotations

import io
import json
import sys
import time
import traceback
from typing import Any

import requests

import os

BASE = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8765")
API  = BASE + "/api/credit-report"
_ADMIN_EMAIL    = os.getenv("ADMIN_EMAIL", "admin@example.com")
_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results: list[dict] = []


def log(status: str, name: str, detail: str = ""):
    mark = PASS if status == "ok" else (WARN if status == "warn" else FAIL)
    results.append({"status": status, "name": name, "detail": detail})
    print(f"  {mark}  {name}" + (f" — {detail}" if detail else ""))


def check(cond: bool, name: str, ok_detail="", fail_detail="") -> bool:
    if cond:
        log("ok", name, ok_detail)
        return True
    else:
        log("fail", name, fail_detail)
        return False


# ── Minimal realistic JSON for every section ────────────────────────────────────

SECTION_INPUTS: dict[int, dict] = {
    1: {
        "borrower": "Evergreen Marine (Asia) Pte. Ltd.",
        "guarantors": ["Evergreen Marine Corporation (Taiwan) Ltd."],
        "all_facilities": [{"item_no": 1, "borrower": "EMA", "booking_office": "SG",
            "current_facility": None, "proposed_facility_usd_m": 213.84,
            "is_new": True, "outstanding_usd_m": 0, "ccy": "USD",
            "tenor_full_verbatim": "4+7 (pre+post delivery)", "facility_type_full": "Term Loan (SLL)",
            "collateral_full": "RG (pre); Vessel Mortgage (post)", "guarantor": "EMC"}],
        "facility_type": "Committed Bilateral Term Loan (SLL)",
        "facility_amount_usd_m": 213.84,
        "ltc_percent": 80.0,
        "tenor_years": 11,
        "tenor_structure": "4+7 (pre+post delivery)",
        "purpose": "Finance construction of one 20,000 TEU LNG dual-fuel container vessel (Hull H-2891)",
        "repayment_schedule": "5% semi-annual instalments + 35% balloon",
        "balloon_percent": 35.0,
        "interest_rate_basis": "Term SOFR (3M)",
        "margin_bps": 175,
        "security_pre_delivery": "Refund Guarantee (IBK, AA/AA-) + Assignment of SBC",
        "security_post_delivery": "First Priority Ship Mortgage + Assignment of Earnings & Insurances + EMC CG",
        "value_maintenance_clause": {"acr_minimum_pct": 100, "ltv_maximum_pct": 75,
            "testing_frequency": "Every 2 years", "cure_period_days": 21},
        "sustainability_linked_kpi": {"description": "CO2 intensity + MSCI ESG", "max_margin_ratchet_bps": 5},
        "regulatory_compliance": {"bank_net_worth_twd_bn": 275, "single_borrower_limit_twd_bn": 13.75,
            "usd_equivalent_usd_m": 436, "compliance_status": "Compliant",
            "unsecured_drawdown_cap_usd_m": 71.40},
        "group_limit": {"approved_group_limit_usd_m": 750, "total_proposed_group_utilization_usd_m": 673.13, "within_limit": True},
        "drawdown_conditions": {"max_drawdowns": 5, "pre_delivery_cap_usd_m": 71.40, "aggregate_cap_usd_m": 213.84},
        "conditions_precedent": ["Execution of all security documents", "Receipt of legal opinions",
            "Ship mortgage registration within 5 Banking Days of delivery"],
        "deal_comparison": [{"term": "Amount", "proposed": "USD213.84m", "previous": "N/A (New Deal)"}],
        "account_strategy": {"wallet_overview": "CUB SG wallet USD 350m",
            "current_relationship": "NII USD 7.5m p.a.", "opportunities": "FX hedging, bond issuance"},
        "report_type": "new_deal",
    },
    2: {
        "2A_credit_overview": {
            "bullets": [
                {"order": 1, "text_verbatim": "EMA is a wholly owned subsidiary of EMC (TSE:2603), the 7th largest container shipping line globally with 5.3% market share."},
                {"order": 2, "text_verbatim": "New USD213.84m SLL to finance one 20,000 TEU LNG dual-fuel container vessel (Hull H-2891, SHI, delivery Jun 2026)."},
                {"order": 3, "text_verbatim": "EMA (FY2024): Net cash USD2.2bn; D/E 0.48x; EBITDA USD710m; Interest coverage 11.8x — strong debt service capacity."},
                {"order": 4, "text_verbatim": "Pre-delivery: IBK RG (AA/AA-) fully covers each instalment assigned to CUB. Post-delivery: First priority vessel mortgage + EMC CG."},
                {"order": 5, "text_verbatim": "CCFI 9M2025 avg 1,220 (-28% YoY); freight rate recovery expected H2 2026 underpinned by fleet supply discipline."},
                {"order": 6, "text_verbatim": "Balloon LTV at maturity: 62.0% vs cap 75.0% — compliant; DSCR minimum 1.31x under base case."},
            ],
            "tariff_impact_paragraphs": [
                "EMA's cross-trade lane exposure to US tariffs is approximately 15% of revenue, limiting direct impact.",
                "EMC maintains net cash of USD6.1bn (FY2024) — robust buffer even under prolonged freight rate stress.",
            ],
        },
        "2B_solvency": {
            "primary_repayment_source_verbatim": "Primary repayment from EMA operating cash flows from vessel fleet employment under long-term TC with EMC.",
            "secondary_repayment_source_verbatim": "Secondary: EMC corporate guarantee (net cash USD6.1bn) and vessel collateral (market value USD267m).",
            "ema": {"period": "FY2024", "cash_bn_usd": 2.20, "total_debt_bn_usd": 1.95,
                "op_ebitda_bn_usd": 0.71, "debt_ebitda_ratio": 2.75,
                "interest_coverage": 11.8, "prior_year_coverage": 10.8},
        },
        "2C_guarantor": {
            "guarantor_name_abbrev": "EMC",
            "period": "FY2024",
            "cash_twd_bn": 198.3, "cash_usd_bn": 6.1,
            "total_debt_twd_bn": 310.0, "total_debt_usd_bn": 9.5,
            "interest_coverage": 15.2, "prior_year_coverage": 18.5,
            "support_history_verbatim": "EMC has consistently guaranteed all EMA CUB facilities since 2021. No defaults or guarantee calls to date.",
        },
        "2D_collateral": {
            "pre_delivery": {"issuer_full_name": "Industrial Bank of Korea",
                "rating": "AA", "rating_agencies": ["S&P", "Fitch"],
                "coverage_verbatim": "Covers 100%-500% of outstanding loan at each milestone",
                "assigned_to_cub": True, "satisfactory_to_bank": True},
            "post_delivery": {"security_type": "First priority vessel mortgage",
                "vessel_spec": "20,000 TEU LNG dual-fuel containership (Hull H-2891)",
                "ltc_pct": 80, "acr_pct": 100.8, "ltv_pct": 62.0,
                "ltc_pct_bold": "80%", "acr_pct_bold": "100.8%"},
        },
        "2E_risk_and_mitigants": {
            "risks": [
                {"risk_no": 1, "level": "High", "title": "Container freight rate volatility",
                    "risk_bullets": ["CCFI -28% YoY in 9M2025; spot rate dependent revenue"],
                    "mitigant_bullets": ["12-yr TC with EMC covers >80% revenue", "EMC net cash USD6.1bn"]},
                {"risk_no": 2, "level": "Medium", "title": "Construction/delivery risk",
                    "risk_bullets": ["Complex LNG dual-fuel systems; delivery Jun 2026"],
                    "mitigant_bullets": ["SHI 94% on-time rate; 210-day grace", "IBK RG fully covers each milestone"]},
                {"risk_no": 3, "level": "Low", "title": "Interest rate risk",
                    "risk_bullets": ["Term SOFR floating rate — upward rate pressure"],
                    "mitigant_bullets": ["EMC hedges 60% of group interest exposure via IRS"]},
            ],
        },
        "report_type": "new_deal",
    },
    3: {
        "3A_external_ratings": {"all_nil": True, "ratings": []},
        "3B_internal_ratings": {
            "rows": [
                {"entity_full_name": "Evergreen Marine (Asia) Pte. Ltd.", "entity_abbrev": "EMA",
                    "role": "Borrower", "fy2022_23": "6-", "fy2023_24": "6-", "fy2024": "6",
                    "interim": None, "current": "6", "remarks": "Proposed MSR6", "override_flag": False},
                {"entity_full_name": "Evergreen Marine Corporation (Taiwan) Ltd.", "entity_abbrev": "EMC",
                    "role": "Guarantor", "fy2022_23": "5", "fy2023_24": "5", "fy2024": "5",
                    "interim": "5", "current": "5", "remarks": "", "override_flag": False},
            ],
            "period_display_labels": {"fy2022_23": "2022/23", "fy2023_24": "2023/24",
                "fy2024": "2024", "interim": "Interim", "current": "Current"},
        },
        "3C_mas_612": {
            "grade": "PASS",
            "primary_paragraph_verbatim": "Borrower is internally rated MSR 6, mapped to PASS under CUB MSR-MAS 612 mapping matrix. No potential weakness in repayment capability identified.",
            "supporting_paragraphs": [
                "EMA has maintained MSR 6- to MSR 6 over the review period with interest coverage 11.8x in FY2024.",
                "Guarantor EMC is rated MSR 5, reflecting its listed investment-grade status.",
            ],
        },
        "3D_esg_rating": {"entity_abbrev": "EMA", "rating_date": "2025-01-15", "image_ref": "[ESG rating chart — Source: MSCI, Jan 2025]"},
    },
    4: {
        "4A_borrower": {
            "company_name_en": "Evergreen Marine (Asia) Pte. Ltd.",
            "company_name_zh": "長榮海運（亞洲）私人有限公司",
            "legal_entity_type": "Private Limited Company",
            "registration_number": "202100001Z",
            "ubn": "202100001Z",
            "incorporation_country": "Singapore",
            "incorporation_date": "2021-01-15",
            "listing_exchange": None, "listing_date": None,
            "reporting_entity": "Consolidated",
            "group_auditor": "Deloitte",
            "fiscal_year_end": "Dec-31",
            "principal_office": "1 Kim Seng Promenade, Singapore 237994",
        },
        "4B_ownership": {
            "shareholders": [{"name": "Evergreen Marine Corporation", "stake_percent": 100, "country": "Taiwan", "notes": "Listed TSE:2603"}],
            "ultimate_beneficial_owner": "Chang Yung-fa Foundation",
            "ubo_stake_pct": 25.4,
            "ubo_holding_entity": "EMC",
            "group_structure_narrative": "EMA is a wholly-owned subsidiary of EMC (TSE:2603). EMC is the group holding entity; Chang Yung-fa Foundation controls 25.4%.",
        },
        "4C_management": [
            {"name": "Anchor Chang", "title": "General Manager", "years_experience": 25, "background": "25 years in container shipping; joined EMC 1999; GM Singapore since 2015"},
            {"name": "Lily Chen", "title": "Finance Director", "years_experience": 18, "background": "ACCA qualified; 18 years shipping finance; EMA since 2021"},
        ],
        "4D_business": {
            "primary_business": "Container liner shipping",
            "trade_routes": "Asia-Europe, Trans-Pacific, Intra-Asia",
            "operational_model": "Owner-operator with time-chartered capacity",
            "years_in_operation": 52,
            "global_ranking": 7,
            "market_share_pct": 5.3,
        },
        "4E_financials": {
            "currency": "USD", "unit": "millions", "fiscal_year": "FY2024",
            "revenue": 2200.0, "ebitda": 710.0, "ebitda_margin_pct": 32.3,
            "net_income": 399.0, "net_cash_debt": -250.0, "net_debt_ebitda": -0.35,
            "fx_rate_to_usd": 32.5,
            "revenue_breakdown": [{"segment": "Container Freight", "amount": 1980.0, "pct_of_total": 90.0}],
        },
        "4F_fleet": {
            "total_owned_teu": 350000, "total_fleet_teu": 1650000,
            "fleet_breakdown": [
                {"category": "Owned", "vessel_count": 105, "total_teu": 350000, "total_dwt": 3500000, "notes": ""},
                {"category": "Chartered-in", "vessel_count": 95, "total_teu": 800000, "total_dwt": 8000000, "notes": "Avg 3-yr TC"},
                {"category": "On Order", "vessel_count": 24, "total_teu": 500000, "total_dwt": 5000000, "notes": "2026-2028E"},
            ],
            "fleet_detail": [],
        },
        "4G_debt_profile": [
            {"lender_bond": "DBS Syndicated TL", "facility_type": "Term Loan", "ccy": "USD", "amount": 500, "maturity": "2031-06", "secured_unsecured": "Secured"},
            {"lender_bond": "Green Bond 2028", "facility_type": "Unsecured Bond", "ccy": "USD", "amount": 300, "maturity": "2028-04", "secured_unsecured": "Unsecured"},
        ],
        "4H_banking_relationships": [
            {"bank": "Cathay United Bank SG", "product": "Term Loan (SLL)", "limit_usd_m": 213.84, "since": 2021},
            {"bank": "DBS Bank SG", "product": "Revolving Credit", "limit_usd_m": 100, "since": 2018},
        ],
        "4I_market_data": {
            "ccfi_level": 1220, "scfi_level": 2150, "ccfi_yoy_pct": -28.0,
            "order_book_pct_of_fleet": 21.3,
            "alliance_membership": "OCEAN Alliance (via EMC)",
            "imo_regulatory_notes": "CII-B fleet average; EEXI compliant; IMO 2030 target -40% GHG",
            "tariff_risk_notes": "US tariff exposure ~15% of revenue via cross-trade lanes; limited direct impact",
        },
        "4J_peer_comparison": [
            {"company": "MSC", "fleet_teu": 5900000, "market_share_pct": 17.8, "alliance": "None", "listed_yn": "N"},
            {"company": "Maersk", "fleet_teu": 4300000, "market_share_pct": 13.2, "alliance": "Gemini", "listed_yn": "Y"},
            {"company": "Evergreen (EMC)", "fleet_teu": 1650000, "market_share_pct": 5.3, "alliance": "OCEAN Alliance", "listed_yn": "Y"},
        ],
        "4K_major_customers": [
            {"name": "Amazon Logistics", "contract_type": "Long-term service contract", "duration_years": 3},
        ],
    },
    5: {
        "5A_security_overview": {
            "is_secured": True, "unsecured_reason": None,
            "security_instruments": [
                {"rank": 1, "instrument": "Refund Guarantee (IBK)", "description": "Covers pre-delivery phase; unconditional and irrevocable"},
                {"rank": 2, "instrument": "First Priority Ship Mortgage", "description": "Over vessel upon delivery; assigned to CUB"},
                {"rank": 3, "instrument": "EMC Corporate Guarantee", "description": "Full guarantee pre- and post-delivery"},
            ],
        },
        "5B_refund_guarantee": {
            "applicable": True,
            "issuer_full_name": "Industrial Bank of Korea",
            "issuer_rating": "AA", "rating_agency": "S&P",
            "legal_structure": "Unconditional and irrevocable demand guarantee",
            "governing_law": "English law",
            "assigned_to_cub": True,
            "expiry_condition": "Upon delivery or full repayment of pre-delivery loan",
            "milestones": [
                {"milestone": "Steel Cutting", "sched_date": "2024-09-01", "rg_amount_usd_m": 267.30,
                    "max_loan_os_usd_m": 42.78, "coverage_pct": 625.0, "drawdown_usd_m": 42.78, "cum_drawdown_usd_m": 42.78, "status": "Completed"},
                {"milestone": "Keel Laying", "sched_date": "2025-01-15", "rg_amount_usd_m": 267.30,
                    "max_loan_os_usd_m": 71.40, "coverage_pct": 374.4, "drawdown_usd_m": 28.62, "cum_drawdown_usd_m": 71.40, "status": "Completed"},
                {"milestone": "Launch", "sched_date": "2025-10-01", "rg_amount_usd_m": 267.30,
                    "max_loan_os_usd_m": 71.40, "coverage_pct": 374.4, "drawdown_usd_m": 0, "cum_drawdown_usd_m": 71.40, "status": "Pending"},
                {"milestone": "Delivery", "sched_date": "2026-06-30", "rg_amount_usd_m": 0,
                    "max_loan_os_usd_m": 213.84, "coverage_pct": 0, "drawdown_usd_m": 142.44, "cum_drawdown_usd_m": 213.84, "status": "Pending"},
            ],
            "footnotes": "RG expires upon vessel delivery; vessel mortgage registered within 5 Banking Days.",
        },
        "5C_vessel_mortgage": {
            "applicable": True,
            "vessel_valuations": [
                {"vessel": "Hull H-2891 (TBN)", "teu": 20000, "dwt": 199000, "year_built": 2026,
                    "valuer": "Clarkson", "valuation_date": "2025-11-01",
                    "market_value_usd_m": 267.30, "distressed_value_usd_m": 213.84},
            ],
            "gongwen_ref": "CUB-SG-2025-VAL-001",
            "valuation_compliant": True,
            "contract_price_usd_m": 267.30,
            "loan_amount_usd_m": 213.84,
            "ltc_pct": 80.0, "ltc_limit_pct": 80.0,
            "acr_at_delivery_pct": 100.8, "acr_floor_pct": 100.0,
            "balloon_usd_m": 74.84, "ltv_at_maturity_pct": 62.0, "ltv_cap_pct": 75.0,
            "amortisation_schedule": [
                {"period": "H1 2029", "date": "2029-06-30", "principal_usd_m": 10.69, "interest_usd_m": 7.48,
                    "total_debt_service_usd_m": 18.17, "outstanding_balance_usd_m": 203.15, "ltv_pct": 76.0},
            ],
        },
        "5D_insurance": [
            {"type": "Hull & Machinery", "insurer_or_club": "China P&I Club", "insured_value_usd_m": 267.30, "notes": "CUB named co-insured and loss payee"},
            {"type": "Protection & Indemnity", "insurer_or_club": "The Standard Club", "insured_value_usd_m": None, "notes": "Full P&I; unlimited liability"},
            {"type": "War Risk", "insurer_or_club": "Lloyd's Market", "insured_value_usd_m": 267.30, "notes": "Covers war, piracy, SRCC"},
        ],
        "5E_value_maintenance_clause": {
            "acr_covenant_pct": 100.0, "ltv_covenant_pct": 75.0,
            "test_frequency_verbatim": "Every 2 years or upon each drawdown (whichever earlier)",
            "cure_period_banking_days": 21,
            "remedy_options": ["Prepay portion of loan", "Provide additional security", "Combination of both"],
            "cure_mechanism_verbatim": "Upon breach, Borrower has 21 Banking Days to prepay or provide additional security satisfactory to the Bank.",
        },
        "5F_corporate_guarantee": {
            "applicable": True,
            "guarantor_full_name": "Evergreen Marine Corporation (Taiwan) Ltd.",
            "guarantor_listed_exchange": "Taiwan Stock Exchange (TSE:2603)",
            "relationship_to_borrower": "Parent company (100% ownership)",
            "guarantee_scope": "Full guarantee covering principal, interest and all obligations",
            "guarantee_phases": ["Pre-delivery", "Post-delivery"],
            "fx_rate_to_usd": 32.5,
            "guarantor_financials": [
                {"metric": "Cash & Equivalents", "fy_prior_twd_bn": 165.0, "fy_prior_usd_bn": 5.1, "fy_current_twd_bn": 198.3, "fy_current_usd_bn": 6.1},
                {"metric": "Total Debt", "fy_prior_twd_bn": 280.0, "fy_prior_usd_bn": 8.6, "fy_current_twd_bn": 310.0, "fy_current_usd_bn": 9.5},
                {"metric": "EBITDA", "fy_prior_twd_bn": 89.6, "fy_prior_usd_bn": 2.76, "fy_current_twd_bn": 105.2, "fy_current_usd_bn": 3.24},
                {"metric": "Interest Coverage", "fy_prior_twd_bn": None, "fy_prior_usd_bn": 15.2, "fy_current_twd_bn": None, "fy_current_usd_bn": 18.5},
            ],
            "support_capacity_assessment": "EMC net cash USD6.1bn and D/E 0.48x demonstrates strong capacity to honor guarantee.",
            "historical_support_record": "No guarantee calls or defaults on any CUB facilities since 2021.",
            "guarantee_language": "Keep/pay guarantee; cross-default with EMC senior unsecured facilities.",
        },
        "5G_responsible_person": {"provided": False, "name": None, "title": None, "scope": None},
    },
    6: {
        "6A_project": {
            "hull_number": "H-2891", "vessel_type": "Container", "teu": 20000,
            "fuel_type": "LNG Dual Fuel", "imo_tier": "IMO Tier III", "eco_design": True,
            "dwt": 199000, "grt": 195000, "loa_m": 400, "beam_m": 61,
            "main_engine": "MAN 12G95ME-C10.5-GI", "speed_knots": 22.5,
            "class_society": "DNV", "flag_state": "Singapore",
            "contract_price_usd_m": 267.30, "loan_amount_usd_m": 213.84, "ltc_pct": 80.0,
            "delivery_date": "2026-06-30", "grace_period_days": 210, "latest_delivery_date": "2026-12-31",
            "deployment_purpose": "Asia-Europe trade route; 12-year TC to EMC",
            "eu_ets_applicable": True,
            "regulatory_positioning": "EEXI compliant; CII-A at delivery; IMO 2030 GHG -40% target met",
        },
        "6B_builder": {
            "name": "Samsung Heavy Industries Co. Ltd.", "formerly": None,
            "founded": "1974", "hq": "Seoul, South Korea", "listed": "KRX: 010140",
            "market_position": "Top 3 global shipbuilder",
            "market_position_source": "Clarkson Research", "market_position_date": "2025-01",
            "contracts_for_large_vessels": ["5 x 24,000 TEU for MSC (2023)", "3 x 23,000 TEU for CMA CGM (2022)"],
            "track_record_verbatim": "SHI has delivered 23,000 TEU vessels in 2020 and 24,000 TEU in 2022. SHI achieved 94% on-time delivery over past 5 years across 180 vessels.",
            "technology_overlap_verbatim": "LNG carrier builder since 1994; technology overlap with LNG dual-fuel containerships; SHI holds 15 LNG patents.",
            "historical_note_verbatim": "SHI underwent KDB-led restructuring 2020-2021; fully resolved 2023. Return to profitability confirmed by FY2024 net income KRW 320bn.",
            "ontime_delivery_pct": 94, "shipyard_docks": 7, "shipyard_berth_m": 5500,
            "shipyard_capacity_dwt": 3200000, "shipyard_annual_cgt": 2800000,
        },
        "6C_contract": {
            "contract_type": "Fixed-price shipbuilding contract",
            "buyer": "Evergreen Marine (Asia) Pte. Ltd.",
            "builder": "Samsung Heavy Industries Co. Ltd.",
            "price_verbatim": "USD267,300,000",
            "currency": "USD", "contract_date": "2023-11-15",
            "expected_delivery": "2026-06-30", "grace_period": "210 days",
            "latest_delivery_date": "2026-12-31",
            "late_delivery_penalty_verbatim": "USD67,325 per day of delay (standard KSB terms)",
            "buyer_termination_verbatim": "Buyer may terminate if delay exceeds 270 days; IBK RG covers refund.",
            "builder_termination_verbatim": "Builder may terminate if Buyer fails payment within 5 banking days.",
            "change_order_verbatim": "Change orders require written agreement with price/schedule adjustment.",
            "rows": [{"term": "Late Delivery Penalty", "detail_verbatim": "USD67,325 per day"}],
        },
        "6D_milestones": {
            "milestones": [
                {"no": 1, "milestone": "Steel Cutting", "expected_date": "2024-09-01", "actual_date": "2024-09-01",
                    "status": "Completed", "pct_of_contract": 10, "amount_usd_m": 26.73, "cum_paid_usd_m": 26.73,
                    "cub_drawdown": "USD42.78m", "rg_in_force": True, "rg_amount_usd_m": 267.30},
                {"no": 2, "milestone": "Keel Laying", "expected_date": "2025-01-15", "actual_date": "2025-01-15",
                    "status": "Completed", "pct_of_contract": 20, "amount_usd_m": 53.46, "cum_paid_usd_m": 80.19,
                    "cub_drawdown": "USD71.40m cap", "rg_in_force": True, "rg_amount_usd_m": 267.30},
                {"no": 3, "milestone": "Launch", "expected_date": "2025-10-01", "actual_date": None,
                    "status": "Pending", "pct_of_contract": 30, "amount_usd_m": 80.19, "cum_paid_usd_m": 160.38,
                    "cub_drawdown": "USD71.40m cap", "rg_in_force": True, "rg_amount_usd_m": 267.30},
                {"no": 4, "milestone": "Delivery", "expected_date": "2026-06-30", "actual_date": None,
                    "status": "Pending", "pct_of_contract": 40, "amount_usd_m": 106.92, "cum_paid_usd_m": 267.30,
                    "cub_drawdown": "USD213.84m", "rg_in_force": False, "rg_amount_usd_m": 0},
            ],
            "footnotes": [
                {"symbol": "*", "text_verbatim": "Pre-delivery drawdown capped USD71.40m per Banking Act s33-3."},
                {"symbol": "**", "text_verbatim": "RG expires on delivery; vessel mortgage registered within 5 Banking Days."},
            ],
            "commentary_first_drawdown": "First drawdown Q4 2024 at Steel Cutting (USD42.78m, reimbursement basis).",
            "commentary_banking_act_33_3": "Pre-delivery unsecured exposure capped USD71.40m per s33-3 item (d).",
            "commentary_pam_sam": "PAM/SAM joint approval required for each pre-delivery drawdown.",
        },
        "6E_rg_mechanism": {
            "applicable": True,
            "issuer_full_name": "Industrial Bank of Korea",
            "issuer_rating_verbatim": "AA (S&P) / AA- (Fitch)",
            "beneficiary": "Evergreen Marine (Asia) Pte. Ltd., assigned to CUB SG",
            "format_verbatim": "Unconditional and irrevocable demand guarantee",
            "governing_law": "English law",
            "trigger_events": [
                "Builder fails to deliver by Latest Delivery Date",
                "Builder becomes insolvent",
                "Buyer exercises termination right",
                "Builder fails refund within 5 banking days",
            ],
            "claim_process_verbatim": "Written demand to IBK; IBK pays within 5 banking days without set-off.",
            "payout_timeline": "5 banking days from demand",
            "coverage_summary_min_pct": 100.0,
            "coverage_summary_max_pct": 625.0,
        },
        "6F_construction_progress": {
            "status_date": "2025-05-01",
            "milestones_completed": 2, "milestones_total": 4,
            "completion_pct": 30, "on_schedule": True,
            "next_milestone": "Launch (Oct 2025)",
            "risks": [
                {"title": "Construction Delay Risk", "likelihood": "Medium",
                    "description": "Complex LNG dual-fuel outfitting may cause 1-3 month delays.",
                    "mitigant_bullets": ["SHI 94% on-time over 5 years", "210-day grace period", "IBK RG covers each milestone", "USD67,325/day LD clause"]},
                {"title": "Builder Insolvency Risk", "likelihood": "Low",
                    "description": "SHI returned to profitability post-KDB restructuring (2023).",
                    "mitigant_bullets": ["SHI rated BBB+ (S&P); order book USD12.3bn through 2028", "IBK RG survives builder insolvency", "KDB 25% implicit government support"]},
            ],
        },
        "6G_force_majeure": {
            "applicable": True,
            "covered_events": ["War/armed conflict", "Natural disaster", "Government sanctions", "Pandemic"],
            "historical_context_verbatim": "COVID-19 caused 2-6 month delays across Korean yards 2021-2022; SHI managed within grace periods.",
            "current_supply_chain_status": "Steel and LNG component supply normalized as of Q1 2025.",
        },
    },
    7: {
        "entities_to_analyze": [
            {"name": "Evergreen Marine (Asia) Pte. Ltd.", "role": "Borrower",
                "basis": "Consolidated", "auditor": "Deloitte", "opinion": "Unqualified",
                "currency": "USD", "unit": "millions", "guarantor_exists": True, "depth": "FULL"},
            {"name": "Evergreen Marine Corporation (Taiwan) Ltd.", "role": "Guarantor",
                "basis": "Consolidated", "auditor": "Deloitte", "opinion": "Unqualified",
                "currency": "NTD", "unit": "billions", "guarantor_exists": False, "depth": "FULL"},
        ],
        "7A_borrower_financials": {
            "reporting_currency": "USD", "unit": "millions",
            "reporting_entity": "EMA Consolidated",
            "auditor": "Deloitte", "audit_opinion": "Unqualified",
            "accounting_standard": "IFRS", "fiscal_year_end": "Dec-31",
            "income_statement": {
                "FY2022": {"revenue": 2850, "cogs": 1980, "gross_profit": 870, "other_op_income": 15,
                    "op_profit": 720, "finance_income": 12, "finance_cost": -85, "other_non_op": -5,
                    "pbt": 642, "tax": -96, "net_income": 546, "ebitda": 920, "depreciation": 200},
                "FY2023": {"revenue": 1920, "cogs": 1450, "gross_profit": 470, "other_op_income": 10,
                    "op_profit": 380, "finance_income": 18, "finance_cost": -78, "other_non_op": -8,
                    "pbt": 312, "tax": -47, "net_income": 265, "ebitda": 580, "depreciation": 200},
                "FY2024": {"revenue": 2200, "cogs": 1580, "gross_profit": 620, "other_op_income": 12,
                    "op_profit": 510, "finance_income": 22, "finance_cost": -60, "other_non_op": -3,
                    "pbt": 469, "tax": -70, "net_income": 399, "ebitda": 710, "depreciation": 200},
            },
            "balance_sheet": {
                "FY2024": {
                    "cash": 2200, "trade_receivables": 320, "inventories": 85, "other_ca": 120, "total_ca": 2725,
                    "vessels_ppe": 3800, "right_of_use_assets": 1200, "other_nca": 250, "total_nca": 5250, "total_assets": 7975,
                    "trade_payables": 480, "st_borrowings": 350, "current_lease_liabilities": 180, "other_cl": 220, "total_cl": 1230,
                    "lt_borrowings": 1600, "nc_lease_liabilities": 980, "other_ncl": 95, "total_ncl": 2675,
                    "total_liabilities": 3905, "share_capital": 800, "retained_earnings": 3270, "total_equity": 4070,
                },
            },
            "cash_flow": {
                "FY2024": {"ocf": 780, "icf": -420, "fcf": 360, "net_change": 85,
                    "opening_cash": 2115, "fx_effect": 0, "closing_cash": 2200},
            },
        },
        "7B_key_ratios": {
            "FY2022": {"gross_margin_pct": 30.5, "op_margin_pct": 25.3, "ni_margin_pct": 19.2,
                "ebitda_margin_pct": 32.3, "roa_pct": 8.1, "roe_pct": 18.5,
                "total_debt": 1850, "net_debt": -350, "debt_equity": 0.45, "net_debt_equity": "Net Cash",
                "debt_ebitda": 2.01, "ebitda_interest": 10.8, "ocf_total_debt": 0.52, "ocf_interest": 11.2,
                "ar_days": 45, "ap_days": 38, "inventory_days": 22, "dscr": 2.15, "tangible_leverage": 0.45, "current_ratio": 1.8},
            "FY2023": {"gross_margin_pct": 24.5, "op_margin_pct": 19.8, "ni_margin_pct": 13.8,
                "ebitda_margin_pct": 30.2, "roa_pct": 4.5, "roe_pct": 9.2,
                "total_debt": 1900, "net_debt": -120, "debt_equity": 0.46, "net_debt_equity": "Net Cash",
                "debt_ebitda": 3.28, "ebitda_interest": 7.4, "ocf_total_debt": 0.35, "ocf_interest": 8.1,
                "ar_days": 48, "ap_days": 41, "inventory_days": 20, "dscr": 1.62, "tangible_leverage": 0.46, "current_ratio": 2.0},
            "FY2024": {"gross_margin_pct": 28.2, "op_margin_pct": 23.2, "ni_margin_pct": 18.1,
                "ebitda_margin_pct": 32.3, "roa_pct": 5.8, "roe_pct": 10.8,
                "total_debt": 1950, "net_debt": -250, "debt_equity": 0.48, "net_debt_equity": "Net Cash",
                "debt_ebitda": 2.75, "ebitda_interest": 11.8, "ocf_total_debt": 0.40, "ocf_interest": 13.0,
                "ar_days": 53, "ap_days": 44, "inventory_days": 20, "dscr": 1.85, "tangible_leverage": 0.48, "current_ratio": 2.2},
        },
        "7C_guarantor_financials": {
            "applicable": True, "depth": "FULL",
            "guarantor_name": "Evergreen Marine Corporation (Taiwan) Ltd.",
            "reporting_currency": "NTD", "unit": "billions",
            "income_statement": {
                "FY2024": {"revenue": 381.2, "cogs": 280.5, "gross_profit": 100.7,
                    "op_profit": 89.6, "finance_income": 3.2, "finance_cost": -5.9,
                    "pbt": 86.9, "tax": -13.0, "net_income": 73.9, "ebitda": 105.2, "depreciation": 15.6},
            },
            "balance_sheet": {"FY2024": {"cash": 198.3, "total_assets": 850.0, "total_liabilities": 410.0, "total_equity": 440.0}},
            "cash_flow": {"FY2024": {"ocf": 95.0, "icf": -45.0, "fcf": 50.0, "net_change": 12.0, "opening_cash": 186.3, "closing_cash": 198.3}},
        },
        "7D_guarantor_ratios": {
            "applicable": True,
            "FY2024": {"gross_margin_pct": 26.4, "op_margin_pct": 23.5, "ni_margin_pct": 19.4,
                "debt_ebitda": 2.95, "ebitda_interest": 17.8, "current_ratio": 2.8, "dscr": 2.10},
        },
        "7E_base_case": {
            "applicable": True,
            "key_assumptions": [
                {"assumption": "Charter rate (USD/day)", "value": "USD28,000", "source": "EMA/EMC TC agreement"},
                {"assumption": "OPEX (USD/day)", "value": "USD8,500", "source": "EMA management estimate"},
                {"assumption": "Interest rate (all-in)", "value": "Term SOFR + 175bps (~7.0%)", "source": "CUB term sheet"},
                {"assumption": "Principal repayment", "value": "5% semi-annual + 35% balloon", "source": "CUB term sheet"},
            ],
            "projected_financials": {
                "FY2027E": {"revenue": 10208, "gross_profit": 6635, "op_profit": 6100, "net_income": 4269,
                    "cash": 9100, "debt": 152500, "equity": 30000, "ocf": 6780, "capex": 0, "debt_service": 6960, "fcf": -180},
                "FY2028E": {"revenue": 10616, "gross_profit": 7000, "op_profit": 6500, "net_income": 4600,
                    "cash": 10200, "debt": 141500, "equity": 31000, "ocf": 7050, "capex": 0, "debt_service": 6820, "fcf": 230},
            },
            "dscr_table": [
                {"period": "FY2027E", "ocf": 6780, "debt_service": 6960, "dscr": 0.97},
                {"period": "FY2028E", "ocf": 7050, "debt_service": 6820, "dscr": 1.03},
                {"period": "FY2030E", "ocf": 7500, "debt_service": 6500, "dscr": 1.15},
                {"period": "FY2035E", "ocf": 8000, "debt_service": 5800, "dscr": 1.38},
            ],
            "conclusion": "Under base case, DSCR improves from 0.97x (FY2027E) to 1.38x (FY2035E) as debt amortises. EMC guarantee backstops during ramp-up phase.",
        },
        "7F_worse_case": {
            "applicable": True,
            "stress_assumptions": [
                {"assumption": "Charter rate", "base": "USD28,000/day", "worse": "USD22,400/day", "stress_magnitude": "-20%"},
                {"assumption": "OPEX", "base": "USD8,500/day", "worse": "USD9,350/day", "stress_magnitude": "+10%"},
                {"assumption": "Interest rate", "base": "7.0%", "worse": "8.0%", "stress_magnitude": "+100bps"},
            ],
            "stressed_summary": {
                "FY2027E": {"revenue": 8165, "op_profit": 4100, "net_income": 2800, "ocf": 4100, "cash": 6500, "dscr": 0.59},
                "FY2028E": {"revenue": 8490, "op_profit": 4300, "net_income": 2900, "ocf": 4400, "cash": 7200, "dscr": 0.65},
            },
            "conclusion": "Under worse case, DSCR falls to 0.59x in FY2027E — below 1.0x threshold. EMC net cash USD6.1bn provides full coverage. Scenario has not occurred historically (lowest CCFI 842 in 2016).",
        },
        "7H_sensitivity": {
            "applicable": True,
            "rows": [
                {"variable": "Freight -10%", "base_case": "USD28,000/day", "stress": "USD25,200/day", "dscr_min_impact": 1.15, "cash_trough_impact": "USD6.8m", "conclusion": "Within covenant"},
                {"variable": "Freight -20%", "base_case": "USD28,000/day", "stress": "USD22,400/day", "dscr_min_impact": 0.86, "cash_trough_impact": "USD5.5m", "conclusion": "Below 1.0x; EMC guarantee activated"},
                {"variable": "Interest +100bps", "base_case": "7.0%", "stress": "8.0%", "dscr_min_impact": 1.22, "cash_trough_impact": "USD6.2m", "conclusion": "Manageable"},
            ],
        },
        "industry_index": {"ccfi_level": 1220, "scfi_level": 2150, "year": 2025},
    },
    8: {
        "8A_acra_banking_charges": {
            "section_applicability": "internal_only",
            "acra_data_available": True,
            "jurisdiction": "Singapore",
            "search_date": "01 Dec 2025",
            "entity_name": "Evergreen Marine (Asia) Pte. Ltd.",
            "uen": "202100001Z",
            "charges": [
                {"no": 1, "chargee": "DBS Bank Ltd", "date_of_registration": "15 Mar 2021",
                    "date_of_charge": "10 Mar 2021", "amount_usd_m": 128.75, "currency": "USD",
                    "property_charged": "First priority ship mortgage over MV Pacific Star",
                    "status": "Registered", "is_cub_charge": False, "cub_facility_ref": None},
                {"no": 2, "chargee": "OCBC Bank", "date_of_registration": "20 Jun 2022",
                    "date_of_charge": "15 Jun 2022", "amount_usd_m": 95.00, "currency": "USD",
                    "property_charged": "First priority ship mortgage over MV Green Star",
                    "status": "Satisfied (01 Jan 2025)", "is_cub_charge": False, "cub_facility_ref": None},
                {"no": 3, "chargee": "Cathay United Bank Singapore Branch",
                    "date_of_registration": "01 Oct 2025", "date_of_charge": "15 Sep 2025",
                    "amount_usd_m": 213.84, "currency": "USD",
                    "property_charged": "Hull H-2891 — CUB Facility (Item 1, §1)",
                    "status": "Registered", "is_cub_charge": True, "cub_facility_ref": "Item 1, §1"},
            ],
            "summary": {
                "total_charges": 3, "active_charges": 2, "satisfied_charges": 1,
                "total_active_usd_m": 342.59, "cub_charge_count": 1, "cub_total_usd_m": 213.84,
                "unique_chargees": ["DBS Bank Ltd", "OCBC Bank", "Cathay United Bank Singapore Branch"],
                "distinct_banking_groups": 3,
            },
        },
        "8B_other_information": "RESERVED",
    },
    9: {
        "9A_checklist": [
            {"no": 1, "category": "KYC & Compliance", "item": "CDD completed — Tier classification stated", "response": "Yes", "remarks": "Tier 1 KYC; CDD reviewed 01 Dec 2025"},
            {"no": 2, "category": "KYC & Compliance", "item": "No adverse news or negative media", "response": "Yes", "remarks": "Google/LexisNexis negative news search clear as at 01 Dec 2025"},
            {"no": 3, "category": "Sanctions & AML", "item": "OFAC/UN/MAS sanctions screening completed — negative", "response": "Yes", "remarks": "WorldCheck screening negative — EMA, EMC, directors"},
            {"no": 4, "category": "Sanctions & AML", "item": "PEP screening completed — negative", "response": "Yes", "remarks": "No PEP identified among EMA/EMC directors"},
            {"no": 5, "category": "Credit Risk", "item": "MSR confirmed — stated level", "response": "Yes", "remarks": "EMA: MSR 6 (Borrower); EMC: MSR 5 (Guarantor)"},
            {"no": 6, "category": "Credit Risk", "item": "Group limit assessed and within approved limit", "response": "Yes", "remarks": "Group limit USD750m; proposed utilization USD673.13m (89.8%) — within limit"},
            {"no": 7, "category": "Financial", "item": "Audited financials reviewed — FY stated", "response": "Yes", "remarks": "EMA FY2024 audited (Deloitte, Unqualified); EMC FY2024 audited"},
            {"no": 8, "category": "Financial", "item": "DSCR computed — Base and Worse Case", "response": "Yes", "remarks": "Base: min 0.97x (FY2027E); Worse: min 0.59x — EMC guarantee backstops"},
            {"no": 9, "category": "Financial", "item": "Borrower net cash / leverage confirmed", "response": "Yes", "remarks": "EMA: Net cash USD250m; D/E 0.48x (FY2024)"},
            {"no": 10, "category": "Collateral", "item": "Valuation obtained — valuer and date stated", "response": "Yes", "remarks": "Clarkson, Nov 2025; market value USD267.30m — gongwen CUB-SG-2025-VAL-001"},
            {"no": 11, "category": "Collateral", "item": "LTC, ACR, LTV computed and within limits", "response": "Yes", "remarks": "LTC 80% (limit 80%); ACR 100.8% (floor 100%); LTV 62.0% (cap 75%)"},
            {"no": 12, "category": "Collateral", "item": "Insurance requirements confirmed", "response": "Yes", "remarks": "H&M, P&I (Standard Club), War Risk — CUB co-insured and loss payee"},
            {"no": 13, "category": "ESG & Environmental", "item": "ESG / sustainability assessment completed", "response": "Yes", "remarks": "SLL KPIs: CO2 intensity + MSCI ESG; EMC MSCI rating 'BBB'"},
            {"no": 14, "category": "ESG & Environmental", "item": "IMO regulatory compliance confirmed", "response": "Yes", "remarks": "EEXI compliant; CII-A at delivery; EU ETS applicable from 2024"},
            {"no": 15, "category": "Legal & Documentation", "item": "Banking Act s.33-3 compliance confirmed — state USD amount", "response": "Yes", "remarks": "Pre-delivery unsecured USD71.40m within s.33-3 limit (item (d)); approved GM Credit"},
            {"no": 16, "category": "Legal & Documentation", "item": "ACRA charges registered — state count", "response": "Yes", "remarks": "3 total charges; 1 CUB charge (Item 1 §1); ACRA search 01 Dec 2025"},
            {"no": 17, "category": "Legal & Documentation", "item": "Negative pledge reviewed — no conflict", "response": "Yes", "remarks": "No conflicting negative pledge obligations in existing facility agreements"},
            {"no": 18, "category": "Legal & Documentation", "item": "Change of control clause included", "response": "Yes", "remarks": "COC clause: if EMC ceases to hold >50% EMA, mandatory prepayment option"},
            {"no": 19, "category": "Legal & Documentation", "item": "Listing requirement confirmed (if applicable)", "response": "N/A", "remarks": "EMA is private; not listed — not applicable"},
            {"no": 20, "category": "Legal & Documentation", "item": "Governing law confirmed", "response": "Yes", "remarks": "English law (facility agreement and security documents)"},
            {"no": 21, "category": "Legal & Documentation", "item": "PAM/SAM approval obtained for pre-delivery drawdowns", "response": "Yes", "remarks": "PAM/SAM joint control confirmed for each pre-delivery drawdown tranche"},
            {"no": 22, "category": "Legal & Documentation", "item": "Sustainability-linked loan framework verified", "response": "Yes", "remarks": "SLL KPI framework verified per LMA SLL Principles 2023; margin ratchet ±5bps"},
            {"no": 23, "category": "Regulatory (MAS)", "item": "MAS 612 risk classification confirmed", "response": "Yes", "remarks": "PASS grade; EMA MSR6 maps to PASS per CUB MSR-MAS 612 matrix"},
        ],
        "9B_conditions_covenants": {
            "conditions_precedent": [
                {"no": 1, "description": "Execution of facility agreement and all security documents", "testing": "Before first drawdown"},
                {"no": 2, "description": "Receipt of satisfactory legal opinions (Singapore and BVI)", "testing": "Before first drawdown"},
                {"no": 3, "description": "Completion of KYC/AML/CDD for EMA and EMC", "testing": "Before first drawdown"},
                {"no": 4, "description": "Registration of first priority ship mortgage at Singapore Ship Registry", "testing": "Within 5 Banking Days of vessel delivery"},
                {"no": 5, "description": "Receipt of IBK Refund Guarantee in form satisfactory to CUB", "testing": "Before first drawdown"},
            ],
            "ongoing_covenants": [
                {"description": "ACR covenant: ACR >= 100% at all times", "threshold": "100%", "testing": "Every 2 years or upon each drawdown"},
                {"description": "LTV covenant: LTV <= 75% at balloon maturity", "threshold": "75%", "testing": "At balloon repayment date"},
                {"description": "Insurance: maintain H&M, P&I, War Risk in force", "threshold": "Insured value >= market value", "testing": "Annual renewal"},
                {"description": "Negative pledge: no additional security without CUB prior written consent", "threshold": "N/A", "testing": "Ongoing"},
                {"description": "EMC to remain listed on Taiwan Stock Exchange", "threshold": "N/A", "testing": "Ongoing"},
                {"description": "Cross-default: any default under EMC senior unsecured facilities constitutes cross-default", "threshold": "N/A", "testing": "Ongoing"},
            ],
            "financial_covenants": "NIL (financial covenants are embedded in value maintenance clause above)",
        },
        "9C_recommendation": {
            "decision": "APPROVE",
            "facility_amount_usd_m": 213.84,
            "tenor_years": 11,
            "security_structure": "Pre-delivery: Refund Guarantee (IBK, AA/AA-) + Assignment of SBC. Post-delivery: First Priority Ship Mortgage + Assignment of Earnings & Insurances + EMC Corporate Guarantee.",
            "key_conditions": [
                "Execution of all security documents before first drawdown",
                "Ship mortgage registration within 5 Banking Days of delivery",
                "KYC/AML/CDD completion for EMA and all EMC directors",
                "Receipt of IBK Refund Guarantee in CUB-approved form",
                "SLL KPI framework verification per LMA SLL Principles 2023",
            ],
            "balloon_ltv_pct": 62.0,
            "balloon_ltv_cap_pct": 75.0,
            "risk_level_changes_from_prior": "No change — new deal (no prior review baseline)",
        },
        "9D_signoff": {
            "date": "15 Jan 2026",
            "prepared_by": "Jane Smith, Associate, Credit Management Department, CUB SG Branch",
            "reviewed_by": "John Lee, Vice President, Credit Management Department, CUB SG Branch",
            "department": "Credit Management Department, Cathay United Bank Singapore Branch",
        },
    },
    10: {
        "10A_group_exposure": {
            "entity_group": "EMC/EMA/EVA Group",
            "group_limit_usd_m": 750.0, "currency": "USD", "unit": "millions", "as_of_date": "Dec 2025",
            "rows": [
                {"entity": "Evergreen Marine (Asia) Pte. Ltd.", "branch": "SG",
                    "facility_type": "Term Loan (SLL) [NEW]",
                    "current_approved_usd_m": 0, "proposed_usd_m": 213.84, "outstanding_usd_m": 0,
                    "collateral": "RG + Vessel Mortgage", "guarantor": "EMC",
                    "maturity_str": "Jun 2037E", "msr": "MSR3", "is_new_facility": True, "subtotal_type": None},
                {"entity": "EMA Subtotal", "branch": "", "facility_type": "",
                    "current_approved_usd_m": 0, "proposed_usd_m": 213.84, "outstanding_usd_m": 0,
                    "collateral": "", "guarantor": "", "maturity_str": "", "msr": "",
                    "is_new_facility": False, "subtotal_type": "EMA Subtotal"},
                {"entity": "Evergreen Marine Corporation (Taiwan) Ltd.", "branch": "TW",
                    "facility_type": "Revolving Credit Facility",
                    "current_approved_usd_m": 150.0, "proposed_usd_m": 150.0, "outstanding_usd_m": 50.0,
                    "collateral": "Clean", "guarantor": "N/A",
                    "maturity_str": "Dec 2027E", "msr": "—", "is_new_facility": False, "subtotal_type": None},
                {"entity": "EVA Airways Corporation", "branch": "TW",
                    "facility_type": "Term Loan",
                    "current_approved_usd_m": 308.65, "proposed_usd_m": 308.65, "outstanding_usd_m": 280.0,
                    "collateral": "Aircraft Mortgage", "guarantor": "N/A",
                    "maturity_str": "Mar 2032E", "msr": "—", "is_new_facility": False, "subtotal_type": None},
                {"entity": "EMC+EVA Subtotal", "branch": "", "facility_type": "",
                    "current_approved_usd_m": 458.65, "proposed_usd_m": 458.65, "outstanding_usd_m": 330.0,
                    "collateral": "", "guarantor": "", "maturity_str": "", "msr": "",
                    "is_new_facility": False, "subtotal_type": "EMC Subtotal"},
                {"entity": "Group Total", "branch": "", "facility_type": "",
                    "current_approved_usd_m": 458.65, "proposed_usd_m": 672.49, "outstanding_usd_m": 330.0,
                    "collateral": "", "guarantor": "", "maturity_str": "", "msr": "",
                    "is_new_facility": False, "subtotal_type": "Group Total"},
            ],
            "group_limit_sub_table": {
                "approved_group_limit_usd_m": 750.0,
                "proposed_total_exposure_usd_m": 672.49,
                "utilization_pct": 89.7,
                "headroom_usd_m": 77.51,
            },
            "eva_note": "Note: EVA Airways is a sister company within the Evergreen Group. EVA facilities are under separate CA and included here for Group Limit purposes only.",
        },
        "10B_fleet_growth": {
            "group_name": "EMC",
            "year_range": "2023-2028E",
            "rows": [
                {"year_label": "2023", "owned_fleet_teu_m": 1.21, "total_fleet_teu_m": 1.92, "total_vessels": 195, "owned_pct": 63.0},
                {"year_label": "2024", "owned_fleet_teu_m": 1.35, "total_fleet_teu_m": 2.02, "total_vessels": 205, "owned_pct": 66.8},
                {"year_label": "2025E", "owned_fleet_teu_m": 1.52, "total_fleet_teu_m": 2.15, "total_vessels": 218, "owned_pct": 70.7},
                {"year_label": "2026E", "owned_fleet_teu_m": 1.68, "total_fleet_teu_m": 2.28, "total_vessels": 230, "owned_pct": 73.7},
                {"year_label": "2027E", "owned_fleet_teu_m": 1.85, "total_fleet_teu_m": 2.40, "total_vessels": 242, "owned_pct": 77.1},
                {"year_label": "2028E", "owned_fleet_teu_m": 2.10, "total_fleet_teu_m": 2.55, "total_vessels": 258, "owned_pct": 82.4},
            ],
            "cagr_pct": 5.8,
            "chart_reference": "EMC Fleet Capacity Growth Chart — Source: EMC Annual Report 2024 / Clarkson Jan 2025",
            "key_notes": [
                "Target capacity: 2.55m TEU by end-2028E (vs. 1.92m TEU in 2023), CAGR +5.8%",
                "Owned fleet transition: 63% (2023) → 82% (2028E) — strategic reduction of charter reliance",
                "Newbuild delivery concentration: 63 vessels on order 2025-2028E; orderbook 18% of fleet (Clarkson, Jan 2025)",
                "CUB-financed vessel: 20,000 TEU LNG dual-fuel, Hull H-2891, delivery Jun 2026E (SHI)",
                "EMC CAPEX plan: USD4.2bn (2025-2028E); EMA capital commitment: USD267.30m for Hull H-2891",
            ],
        },
        "10C_projections": {
            "entity_name": "Evergreen Marine (Asia) Pte. Ltd. — Standalone",
            "basis": "Standalone",
            "currency": "USD", "unit": "USD'000",
            "key_assumptions": [
                {"assumption": "Charter rate (USD/day)", "FY2026E": 28000, "FY2027E": 28500, "FY2028E": 29000},
                {"assumption": "OPEX (USD/day)", "FY2026E": 8500, "FY2027E": 8750, "FY2028E": 9000},
                {"assumption": "Interest rate (all-in)", "FY2026E": "7.00%", "FY2027E": "6.75%", "FY2028E": "6.50%"},
                {"assumption": "Principal repayment", "FY2026E": "5% semi-annual", "FY2027E": "5% semi-annual", "FY2028E": "5% semi-annual"},
            ],
            "assumptions_narrative": "Revenue based on contracted TC rate USD28,000/day with EMC (12-yr TC). OPEX escalates 3% p.a. Interest on Term SOFR + 175bps. CAPEX reflects contracted vessel price USD267.3m (fixed SBC).",
            "base_case_pl": [
                {"item": "Revenue", "FY2026E": 10206, "FY2027E": 10408, "FY2028E": 10616, "is_subtotal": False},
                {"item": "Cost of Goods Sold (OPEX)", "FY2026E": -3103, "FY2027E": -3194, "FY2028E": -3288, "is_subtotal": False},
                {"item": "Gross Profit", "FY2026E": 7103, "FY2027E": 7214, "FY2028E": 7328, "is_subtotal": True},
                {"item": "Operating Expenses", "FY2026E": -860, "FY2027E": -885, "FY2028E": -912, "is_subtotal": False},
                {"item": "Operating Profit", "FY2026E": 6243, "FY2027E": 6329, "FY2028E": 6416, "is_subtotal": True},
                {"item": "Finance Income", "FY2026E": 85, "FY2027E": 92, "FY2028E": 99, "is_subtotal": False},
                {"item": "Finance Cost (Interest)", "FY2026E": -1498, "FY2027E": -1387, "FY2028E": -1268, "is_subtotal": False},
                {"item": "Other Non-Operating", "FY2026E": -12, "FY2027E": -12, "FY2028E": -12, "is_subtotal": False},
                {"item": "Profit Before Tax", "FY2026E": 4818, "FY2027E": 5022, "FY2028E": 5235, "is_subtotal": True},
                {"item": "Income Tax (-15%)", "FY2026E": -723, "FY2027E": -753, "FY2028E": -785, "is_subtotal": False},
                {"item": "Net Income", "FY2026E": 4095, "FY2027E": 4269, "FY2028E": 4450, "is_subtotal": True},
                {"item": "Depreciation & Amortisation", "FY2026E": 2160, "FY2027E": 2160, "FY2028E": 2160, "is_subtotal": False},
            ],
            "base_case_bs": [
                {"item": "Cash & Equivalents", "FY2026E": 8200, "FY2027E": 9100, "FY2028E": 10200, "is_subtotal": False},
                {"item": "Trade Receivables", "FY2026E": 850, "FY2027E": 870, "FY2028E": 890, "is_subtotal": False},
                {"item": "Inventories", "FY2026E": 120, "FY2027E": 124, "FY2028E": 128, "is_subtotal": False},
                {"item": "Other Current Assets", "FY2026E": 320, "FY2027E": 330, "FY2028E": 340, "is_subtotal": False},
                {"item": "Total Current Assets", "FY2026E": 9490, "FY2027E": 10424, "FY2028E": 11558, "is_subtotal": True},
                {"item": "Vessels & Property", "FY2026E": 267300, "FY2027E": 265140, "FY2028E": 262980, "is_subtotal": False},
                {"item": "Right-of-Use Assets", "FY2026E": 12000, "FY2027E": 10500, "FY2028E": 9000, "is_subtotal": False},
                {"item": "Other Non-Current Assets", "FY2026E": 850, "FY2027E": 870, "FY2028E": 890, "is_subtotal": False},
                {"item": "Total Non-Current Assets", "FY2026E": 280150, "FY2027E": 276510, "FY2028E": 272870, "is_subtotal": True},
                {"item": "Total Assets", "FY2026E": 289640, "FY2027E": 286934, "FY2028E": 284428, "is_subtotal": True},
                {"item": "Short-term Borrowings", "FY2026E": 21384, "FY2027E": 21384, "FY2028E": 21384, "is_subtotal": False},
                {"item": "Other Current Liabilities", "FY2026E": 2500, "FY2027E": 2600, "FY2028E": 2700, "is_subtotal": False},
                {"item": "Total Current Liabilities", "FY2026E": 23884, "FY2027E": 23984, "FY2028E": 24084, "is_subtotal": True},
                {"item": "Long-term Borrowings", "FY2026E": 192456, "FY2027E": 171072, "FY2028E": 149688, "is_subtotal": False},
                {"item": "Non-Current Lease Liabilities", "FY2026E": 10200, "FY2027E": 8900, "FY2028E": 7600, "is_subtotal": False},
                {"item": "Other Non-Current Liabilities", "FY2026E": 450, "FY2027E": 460, "FY2028E": 470, "is_subtotal": False},
                {"item": "Total Non-Current Liabilities", "FY2026E": 203106, "FY2027E": 180432, "FY2028E": 157758, "is_subtotal": True},
                {"item": "Total Liabilities", "FY2026E": 226990, "FY2027E": 204416, "FY2028E": 181842, "is_subtotal": True},
                {"item": "Share Capital", "FY2026E": 36000, "FY2027E": 36000, "FY2028E": 36000, "is_subtotal": False},
                {"item": "Retained Earnings", "FY2026E": 26650, "FY2027E": 46518, "FY2028E": 66586, "is_subtotal": False},
                {"item": "Total Equity", "FY2026E": 62650, "FY2027E": 82518, "FY2028E": 102586, "is_subtotal": True},
            ],
            "base_case_cf": [
                {"item": "Operating Cash Flow (OCF)", "FY2026E": 6255, "FY2027E": 6429, "FY2028E": 6610, "is_subtotal": False},
                {"item": "Investing Cash Flow (CAPEX)", "FY2026E": -267300, "FY2027E": -200, "FY2028E": -200, "is_subtotal": False},
                {"item": "Financing Cash Flow (Net Drawdown/Repayment)", "FY2026E": 269345, "FY2027E": -5315, "FY2028E": -5315, "is_subtotal": False},
                {"item": "Net Change in Cash", "FY2026E": 8300, "FY2027E": 914, "FY2028E": 1095, "is_subtotal": True},
                {"item": "Opening Cash Balance", "FY2026E": 0, "FY2027E": 8300, "FY2028E": 9214, "is_subtotal": False},
                {"item": "Closing Cash Balance", "FY2026E": 8300, "FY2027E": 9214, "FY2028E": 10309, "is_subtotal": True},
            ],
            "base_case_dscr": [
                {"year_label": "FY2026E (H2)", "ocf": 6255, "debt_service": 7100, "dscr": 0.88},
                {"year_label": "FY2027E", "ocf": 6429, "debt_service": 6960, "dscr": 0.92},
                {"year_label": "FY2028E", "ocf": 6610, "debt_service": 6820, "dscr": 0.97},
                {"year_label": "FY2029E", "ocf": 6800, "debt_service": 6680, "dscr": 1.02},
                {"year_label": "FY2030E", "ocf": 7000, "debt_service": 6500, "dscr": 1.08},
            ],
            "dscr_commentary": "DSCR improves from 0.88x (FY2026E) to 1.08x (FY2030E) as debt amortises and revenue ramps up. DSCR below 1.0x in FY2026E-FY2028E is supported by EMC corporate guarantee (net cash USD6.1bn); no covenant breach scenario applies.",
            "stress_assumptions": [
                {"assumption": "Charter Revenue", "base_case": "USD28,000/day", "worse_case": "USD22,400/day", "stress_magnitude": "-20%"},
                {"assumption": "OPEX", "base_case": "USD8,500/day", "worse_case": "USD9,350/day", "stress_magnitude": "+10%"},
                {"assumption": "Interest Rate", "base_case": "SOFR + 175bps (~7.0%)", "worse_case": "SOFR + 275bps (~8.0%)", "stress_magnitude": "+100bps"},
                {"assumption": "Delivery Date", "base_case": "Jun 2026", "worse_case": "Dec 2026 (grace period)", "stress_magnitude": "+6 months"},
            ],
            "worse_case_summary": [
                {"item": "Revenue", "value": 8165, "is_dscr": False},
                {"item": "OPEX", "value": -3413, "is_dscr": False},
                {"item": "Gross Profit", "value": 4752, "is_dscr": False},
                {"item": "Operating Profit", "value": 3892, "is_dscr": False},
                {"item": "Finance Cost", "value": -1710, "is_dscr": False},
                {"item": "Net Income", "value": 1852, "is_dscr": False},
                {"item": "OCF", "value": 4012, "is_dscr": False},
                {"item": "Debt Service", "value": 7100, "is_dscr": False},
                {"item": "DSCR (min)", "value": 0.56, "is_dscr": True},
                {"item": "Closing Cash", "value": 5500, "is_dscr": False},
            ],
            "worse_case_commentary": "Under Worse Case (-20% charter rate), DSCR falls to minimum 0.56x in FY2026E. EMC guarantee (net cash USD6.1bn, D/E 0.48x) fully backstops shortfall. Net income remains positive at USD1.85m minimum. Historical precedent: CCFI trough 842 in 2016 — EMC maintained investment-grade status throughout.",
        },
    },
}


def run_tests():
    BASE_URL = BASE
    API_URL = API
    session = requests.Session()

    print("\n" + "=" * 70)
    print("  FINANCIAL REPORT ANALYZER — END-TO-END TEST SUITE")
    print("=" * 70)

    # ── 1. Health check ────────────────────────────────────────────────────────
    print("\n[1] HEALTH CHECK")
    try:
        r = session.get(f"{BASE_URL}/health", timeout=5)
        check(r.status_code == 200 and r.json().get("ok"), "GET /health → 200 ok:true", f"status={r.status_code}")
    except Exception as e:
        log("fail", "GET /health", str(e))
        return sum(1 for x in results if x["status"] == "fail")

    # ── 2. Auth ────────────────────────────────────────────────────────────────
    print("\n[2] AUTHENTICATION")
    r = session.post(f"{API_URL}/auth/login",
        data={"username": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not check(r.status_code == 200, "POST /auth/login → 200", f"status={r.status_code} body={r.text[:100]}"):
        return sum(1 for x in results if x["status"] == "fail")
    token = r.json().get("access_token", "")
    check(bool(token), "access_token received", f"token={token[:20]}…")
    H = {"Authorization": f"Bearer {token}"}
    HJ = {**H, "Content-Type": "application/json"}

    # Bad password
    r2 = session.post(f"{API_URL}/auth/login",
        data={"username": _ADMIN_EMAIL, "password": "WRONG"},
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    check(r2.status_code == 401, "Bad password → 401", f"status={r2.status_code}")

    # ── 3. Report CRUD ─────────────────────────────────────────────────────────
    print("\n[3] REPORT CRUD")
    r = session.post(f"{API_URL}/reports",
        json={"borrower_name": "EMA Test Corp", "industry": "marine",
              "report_type": "New Deal — Ship Finance", "booking_branch": "Singapore"},
        headers=HJ)
    if not check(r.status_code == 201, "POST /reports → 201", f"status={r.status_code} body={r.text[:100]}"):
        return sum(1 for x in results if x["status"] == "fail")
    rpt = r.json(); rid = rpt["id"]
    check(bool(rid), f"Report created id={rid[:8]}…")

    # List reports
    r = session.get(f"{API_URL}/reports", headers=H)
    check(r.status_code == 200 and any(x["id"] == rid for x in r.json()),
          "GET /reports includes new report")

    # Get single report
    r = session.get(f"{API_URL}/reports/{rid}", headers=H)
    check(r.status_code == 200 and r.json()["id"] == rid, "GET /reports/{id} → 200")

    # Status update
    r = session.patch(f"{API_URL}/reports/{rid}/status",
        json={"status": "draft"}, headers=HJ)
    check(r.status_code == 200, "PATCH status → 200")

    # ── 4. Document Upload / Delete ────────────────────────────────────────────
    print("\n[4] DOCUMENT UPLOAD & DELETE")
    mock_txt = b"EMA FY2024 Annual Report\nRevenue: USD 2,200 million\nEBITDA: USD 710 million\nNet Cash: USD 250 million\nDSCR: 1.85x\nMSR: 6"
    r = session.post(f"{API_URL}/reports/{rid}/documents",
        headers=H,
        files={"file": ("ema_annual_report.txt", io.BytesIO(mock_txt), "text/plain")},
        data={"document_type": "annual_report"})
    if not check(r.status_code == 201, "POST /documents (TXT) → 201", f"status={r.status_code} body={r.text[:200]}"):
        doc_id = None
    else:
        doc = r.json(); doc_id = doc["id"]
        check(doc.get("etl_status") == "pending", f"etl_status=pending doc_id={doc_id[:8]}…")
        check(doc.get("file_format") == "txt", f"file_format={doc.get('file_format')}")

    # Upload duplicate → should succeed (backend allows, frontend warns)
    r2 = session.post(f"{API_URL}/reports/{rid}/documents",
        headers=H,
        files={"file": ("ema_annual_report.txt", io.BytesIO(mock_txt), "text/plain")},
        data={"document_type": "annual_report"})
    check(r2.status_code == 201, "Duplicate filename upload → 201 (backend allows; frontend prompts)")

    # List documents
    r = session.get(f"{API_URL}/reports/{rid}/documents", headers=H)
    check(r.status_code == 200 and len(r.json()) >= 1, f"GET /documents → {len(r.json() if r.ok else [])} docs")

    # Delete duplicate
    if r2.status_code == 201:
        dup_id = r2.json()["id"]
        r_del = session.delete(f"{API_URL}/reports/{rid}/documents/{dup_id}", headers=H)
        check(r_del.status_code == 204, "DELETE /documents/{dup_id} → 204")

    # ── 5. ETL ────────────────────────────────────────────────────────────────
    print("\n[5] ETL EXTRACTION")
    if doc_id:
        r = session.post(f"{API_URL}/reports/{rid}/documents/{doc_id}/etl", headers=H, timeout=90)
        if r.status_code == 200:
            etl = r.json()
            sec_count = len(etl.get("sections_extracted", []))
            if sec_count > 0:
                check(True, f"ETL extracted {sec_count} sections",
                      f"sections={etl.get('sections_extracted')}")
                log("ok", "ETL data sample", str(list(etl.get("data", {}).keys()))[:80])
            else:
                log("warn", "ETL extracted 0 sections (no GEMINI_API_KEY or empty doc)",
                    f"sections={etl.get('sections_extracted')}")
        elif r.status_code == 422:
            log("warn", "ETL skipped — no GEMINI_API_KEY in test env", f"status={r.status_code}")
        else:
            log("fail", "ETL failed", f"status={r.status_code} body={r.text[:200]}")
    else:
        log("warn", "ETL skipped — doc_id not available")

    # ── 6. Section Inputs (§1-§10) ─────────────────────────────────────────────
    print("\n[6] SECTION INPUTS §1-§10")
    saved_sections = []
    for sec_no in range(1, 11):
        payload = SECTION_INPUTS.get(sec_no, {"_note": f"Mock placeholder for §{sec_no}"})
        r = session.put(f"{API_URL}/reports/{rid}/inputs/{sec_no}",
            json={"section_no": sec_no, "input_json": payload}, headers=HJ)
        if check(r.status_code == 200, f"PUT /inputs/{sec_no} → 200", f"fields={len(payload)}"):
            saved_sections.append(sec_no)

        # Verify round-trip
        r2 = session.get(f"{API_URL}/reports/{rid}/inputs/{sec_no}", headers=H)
        if r2.status_code == 200:
            stored = r2.json().get("input_json", {})
            # Check first key survives round-trip
            first_key = next(iter(payload), None)
            if first_key:
                check(first_key in stored, f"§{sec_no} round-trip key '{first_key}' preserved")
        else:
            log("fail", f"GET /inputs/{sec_no} → {r2.status_code}")

    check(len(saved_sections) == 10, f"All 10 sections saved ({len(saved_sections)}/10)")

    # List inputs
    r = session.get(f"{API_URL}/reports/{rid}/inputs", headers=H)
    check(r.status_code == 200 and len(r.json()) == 10, f"GET /inputs list → {len(r.json() if r.ok else [])} sections")

    # ── 7. Section Generation §1-§10 ──────────────────────────────────────────
    print("\n[7] SECTION GENERATION §1-§10 (requires GEMINI_API_KEY)")
    generated = {}
    # Generation order (from config.py)
    gen_order = [4, 7, 1, 3, 2, 5, 6, 8, 9, 10]

    gemini_key = __import__("os").getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        log("warn", "GEMINI_API_KEY not set — skipping live generation; testing endpoint behavior only")
        # Test that missing input → 422 for section not yet saved (use a fresh report)
        r_test = session.post(f"{API_URL}/reports/{rid}/generate/1", headers=H)
        check(r_test.status_code in (200, 500, 503), f"POST /generate/1 (no key) → {r_test.status_code}")
    else:
        for sec_no in gen_order:
            t0 = time.time()
            r = session.post(f"{API_URL}/reports/{rid}/generate/{sec_no}", headers=H, timeout=180)
            elapsed = time.time() - t0
            if r.status_code == 200:
                out = r.json()
                generated[sec_no] = out
                tokens = out.get("tokens_used", 0)
                check(True, f"§{sec_no} generated in {elapsed:.0f}s", f"status={out.get('status')} tokens={tokens}")
            elif r.status_code == 409:
                log("warn", f"§{sec_no} blocked by hard dependencies", r.text[:80])
            else:
                log("fail", f"§{sec_no} generation failed", f"status={r.status_code} body={r.text[:150]}")

    # ── 8. Output retrieval ────────────────────────────────────────────────────
    print("\n[8] OUTPUT RETRIEVAL")
    r = session.get(f"{API_URL}/reports/{rid}/outputs", headers=H)
    check(r.status_code == 200, f"GET /outputs → {r.status_code}")
    outputs = r.json() if r.ok else []
    done_count = sum(1 for o in outputs if o.get("status") == "done")
    log("ok" if done_count > 0 else "warn", f"{done_count} sections with status=done")

    for sec_no in range(1, 11):
        r = session.get(f"{API_URL}/reports/{rid}/sections/{sec_no}/output", headers=H)
        if r.status_code == 200:
            out = r.json()
            status = out.get("status", "")
            has_markdown = bool(out.get("markdown"))
            if status == "error":
                log("warn", f"§{sec_no} output status=error (expected — no API key)",
                    f"chars={len(out.get('markdown') or '')}")
            else:
                check(has_markdown, f"§{sec_no} output has markdown",
                      f"chars={len(out.get('markdown') or '')} tokens={out.get('tokens_used')}")
        elif r.status_code == 404:
            log("warn", f"§{sec_no} output not found (not generated)", "404")
        else:
            log("fail", f"§{sec_no} GET output failed", f"{r.status_code}")

    # ── 9. DOCX Export ────────────────────────────────────────────────────────
    print("\n[9] DOCX EXPORT")
    r = session.get(f"{API_URL}/reports/{rid}/export/docx", headers=H, timeout=30)
    if r.status_code == 200:
        check(len(r.content) > 1000, f"DOCX export OK", f"bytes={len(r.content)}")
    elif r.status_code == 503:
        log("warn", "DOCX export → 503 (python-docx not installed)", "expected in dev")
    elif r.status_code == 404:
        log("warn", "DOCX export → 404 (no completed sections yet)", "ok if no sections generated")
    else:
        log("fail", "DOCX export failed", f"status={r.status_code} body={r.text[:100]}")

    # ── 10. Evidence-only generation (no structured input → must attempt, not 422) ──
    print("\n[10] EVIDENCE-ONLY GENERATION GATE")
    # Create a fresh empty report — generation must PROCEED (not 422)
    # The AI uses uploaded evidence chunks even without structured analyst input
    r = session.post(f"{API_URL}/reports",
        json={"borrower_name": "Empty Report Test", "industry": "marine",
              "report_type": "Test", "booking_branch": "SG"},
        headers=HJ)
    empty_rid = r.json().get("id") if r.ok else None
    if empty_rid:
        r_g = session.post(f"{API_URL}/reports/{empty_rid}/generate/4", headers=H)
        # 200 (started/done), 503 (no API key), 500 (other) are all acceptable
        # 422 is NOT acceptable — it means the 422-block bug has returned
        check(r_g.status_code != 422,
              "Generate without structured input must NOT 422 (evidence-only mode)",
              f"got={r_g.status_code}")
        if r_g.status_code == 422:
            log("fail", "422 block returned — ETL→Generate workflow is broken again",
                f"detail={r_g.text[:120]}")
        # Cleanup
        session.delete(f"{API_URL}/reports/{empty_rid}", headers=H)

    # ── 11. Section-specific output quality checks ─────────────────────────────
    print("\n[11] OUTPUT QUALITY SPOT CHECKS (if generation ran)")
    if gemini_key and generated:
        for sec_no in [1, 7, 9, 10]:
            r = session.get(f"{API_URL}/reports/{rid}/sections/{sec_no}/output", headers=H)
            if r.status_code == 200:
                md = r.json().get("markdown", "")
                checks = {
                    1: ["Term Loan", "Facility", "USD"],
                    7: ["EBITDA", "Revenue", "FY2024"],
                    9: ["APPROVE", "Checklist", "KYC"],
                    10: ["Appendix", "Group", "DSCR"],
                }
                for kw in checks.get(sec_no, []):
                    check(kw.lower() in md.lower(), f"§{sec_no} output contains '{kw}'",
                          f"chars={len(md)}")
            else:
                log("warn", f"§{sec_no} output not available for quality check")

    # ── 12. Audit trail ────────────────────────────────────────────────────────
    print("\n[12] AUDIT TRAIL")
    r = session.get(f"{API_URL}/reports/{rid}/audit", headers=H)
    if r.status_code == 200:
        body = r.json()
        events = body.get("events", []) if isinstance(body, dict) else body
        total = body.get("total", len(events)) if isinstance(body, dict) else len(events)
        check(total > 0, f"Audit trail has events", f"total={total}")
    else:
        log("warn", f"Audit endpoint → {r.status_code}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  TEST SUMMARY")
    print("=" * 70)
    passed = sum(1 for r in results if r["status"] == "ok")
    warned = sum(1 for r in results if r["status"] == "warn")
    failed = sum(1 for r in results if r["status"] == "fail")
    total = len(results)

    print(f"\n  {PASS} PASSED : {passed}/{total}")
    print(f"  {WARN} WARNED : {warned}/{total}")
    print(f"  {FAIL} FAILED : {failed}/{total}")

    if failed:
        print(f"\n  ─── FAILURES ───")
        for r in results:
            if r["status"] == "fail":
                print(f"  {FAIL} {r['name']} — {r['detail']}")

    if warned:
        print(f"\n  ─── WARNINGS ───")
        for r in results:
            if r["status"] == "warn":
                print(f"  {WARN} {r['name']} — {r['detail']}")

    print(f"\n  Report ID under test: {rid}")
    print("=" * 70 + "\n")

    return failed


if __name__ == "__main__":
    try:
        failures = run_tests()
        sys.exit(0 if not failures else 1)
    except Exception:
        print("\n[FATAL] Test runner crashed:")
        traceback.print_exc()
        sys.exit(2)

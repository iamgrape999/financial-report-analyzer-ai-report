"""AI-powered ETL: extract structured section data from uploaded documents.

Flow:
  1. Load document text from filesystem
  2. Determine target sections based on document type
  3. Call Gemini with a structured extraction prompt
  4. Return {section_no: {field: value}} dict — callers decide what to save
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Which sections are relevant for each document type
DOCUMENT_SECTION_MAP: dict[str, list[int]] = {
    "annual_report":         [4, 7, 3, 2, 10],      # no §1 — credit facility not in annual reports
    "financial_statement":   [7, 4, 2, 10],          # no §1/§5 — collateral not in financial statements
    "analyst_presentation":  [4, 7, 3, 10],          # company IR — no §1/§2 (bank-internal sections)
    "interim_report":        [7, 4, 2, 3],
    "valuation_report":      [5, 10, 6],
    "charter_agreement":     [1, 6, 5],
    "shipbuilding_contract": [6, 1, 5],
    "kyc_document":          [9, 1, 4],
    "legal_document":        [8, 1, 9],
    "external_report":       [11, 4, 7],             # broker research — §11 first; no §2/§3 (bank-internal)
    "other":                 [4, 7, 1],
}

ETL_SYSTEM_PROMPT = """\
You are a specialized data extraction AI for maritime / corporate credit reports at an international commercial bank.

Your task: read the provided document excerpt and extract structured JSON data for specific credit report sections.

Rules:
- Extract ONLY what is explicitly stated in the document — never fabricate or guess
- Use null for any field not found in the document
- Documents may be written in Traditional Chinese (繁體中文), Simplified Chinese (简体中文), or English — extract data faithfully from whichever language is used
- Financial figures: preserve the document's ORIGINAL currency and units (NTD/TWD billions, USD millions, HKD millions, etc.); note the currency and unit explicitly in your response
- Dates: YYYY-MM-DD format or YYYY-QN (e.g. 2026-Q2)
- Arrays: use [] when empty; include all items found
- Return ONLY a valid JSON object. No markdown, no commentary, no code fences.
- Structure the JSON with integer section numbers as keys (e.g. "4", "7", "11")
- Each section key maps to a flat or nested object matching the schema described
- Schema keys shown as "FY_YYYY" are TEMPLATES: replace with actual years e.g. "2023", "2024", "2025", "2026F", "2027F" — one key per year found in the document
- Schema keys shown as "QN_YYYY" are TEMPLATES: replace with actual quarter-year labels e.g. "1Q25", "2Q25", "1Q26F" — one key per quarter found in the document
- For annual and quarterly estimate tables in broker/analyst reports: extract ALL periods present in the document, both historical and forecast (marked F or E)
- IMPORTANT: always return the full JSON even if most fields are null; never truncate the output
"""

# Extraction schema description per section (tells the model what fields to look for)
SECTION_EXTRACTION_SCHEMA: dict[int, str] = {
    1: """Section 1 — Credit Facility & Case Details:
{
  metadata: {report_type (new_deal/annual_review/new_deal_and_annual_review), branch, industry,
    report_date, as_at_date, group_name},
  facility_summary: {
    rows[{item_no, borrower, booking_office, current_facility, current_facility_mtm,
      proposed_facility, proposed_facility_is_new (bool), lapsed_date,
      outstanding, outstanding_as_at_date, ccy, tenor_full_verbatim,
      facility_type_full, collateral_full, guarantor}],
    totals: {total_credit_limit_usd_m, psr_spot_limit_usd_m, psr_mtm_usd_m},
    footnotes[{symbol (*/^/**/#), text_verbatim}],
    appendix_ref_verbatim
  },
  regulatory_compliance: {
    banking_act_33_3: {requirement_verbatim, borrower_name, compliant_yn,
      bank_nw_twd_bn, limit_5pct_twd_bn, limit_5pct_usd_m, fx_rate, fx_date, calculation_line},
    unsecured_exposure_table[{label, credit_limit_usd_m, unsecured_usd_m, secured_usd_m,
      parenthetical_note}],
    ntd_exposure_twd_m, usd_ntd_sum_note,
    valuation: {valuer, gongwen_ref, valuation_date, amount_exact_verbatim},
    pam_sam_text_verbatim,
    group_limit_verbatim
  },
  purpose_and_recommendation: {
    purpose_text_verbatim, facility_amount_usd_m, facility_type_full, tenor_verbatim,
    vessel_name, vessel_type, teu_capacity, dwt, fuel_type_full_verbatim,
    builder, builder_country, contract_price_exact_verbatim, ltc_pct,
    guarantor_full_legal_name,
    pre_delivery_security_verbatim, post_delivery_security_verbatim,
    acr_pct, ltv_pct, value_maintenance_verbatim,
    psr_formula_verbatim, psr_purpose
  },
  terms_and_conditions: {
    tc_rows[{field, content_verbatim}],
    deal_comparison_rows[{term, proposed_deal, previous_deal}]
  },
  account_strategy: {
    wallet_overview_verbatim,
    current_relationship_verbatim,
    opportunities_verbatim,
    nii_usd_m, tmu_pct, deposits_verbatim, capital_market_verbatim,
    upfront_fee_verbatim, treasury_hedging_verbatim
  }
}""",

    2: """Section 2 — Overall Comments:
{
  2A_credit_overview: {
    bullets[{order (1-6), text_verbatim}],
    tariff_impact_paragraphs[]
  },
  2B_solvency: {
    primary_repayment_source_verbatim,
    secondary_repayment_source_verbatim,
    deal_dscr: {period_label, dscr_value, dscr_floor, notes},
    ema: {period, cash_bn_usd, total_debt_bn_usd, op_ebitda_bn_usd,
      debt_ebitda_ratio, interest_coverage, prior_year_coverage}
  },
  2C_guarantor: {
    guarantor_name_abbrev, period,
    cash_twd_bn, cash_usd_bn, total_debt_twd_bn, total_debt_usd_bn,
    interest_coverage, prior_year_coverage,
    support_history_verbatim
  },
  2D_collateral: {
    pre_delivery: {issuer_full_name, rating, rating_agencies[], coverage_verbatim,
      assigned_to_cub (bool), satisfactory_to_bank (bool)},
    post_delivery: {security_type, vessel_spec, ltc_pct, acr_pct, ltv_pct,
      ltc_pct_bold, acr_pct_bold}
  },
  2E_risk_and_mitigants: {
    risks[{risk_no, level, title, risk_bullets[], mitigant_bullets[]}]
  }
}""",

    3: """Section 3 — Credit Ratings:
{
  3A_external_ratings: {
    all_nil (bool),
    ratings[{entity_abbrev, sp, sp_outlook, moodys, moodys_outlook,
      fitch, fitch_outlook, rating_actions[]}]
  },
  3B_internal_ratings: {
    rows[{entity_full_name, entity_abbrev, role,
      fy2022_23, fy2023_24, fy2024, interim, current,
      generated_rating, override_rating, final_rating,
      remarks, override_flag (bool)}],
    period_display_labels: {[json_key]: display_name}
  },
  3C_mas_612: {
    grade (PASS/SPECIAL_MENTION/SUBSTANDARD/DOUBTFUL/LOSS),
    primary_paragraph_verbatim,
    supporting_paragraphs[]
  },
  3D_esg_rating: {
    entity_abbrev, rating_date, image_ref
  }
}""",

    4: """Section 4 — Corporate History and Overview:
{
  "4A_borrower": {
    "company_name_en": null,
    "company_name_zh": null,
    "legal_entity_type": null,
    "registration_number": null,
    "ubn": null,
    "incorporation_country": null,
    "incorporation_date": null,
    "listing_exchange": null,
    "listing_date": null,
    "reporting_entity": null,
    "group_auditor": null,
    "fiscal_year_end": null,
    "principal_office": null
  },
  "4B_ownership": {
    "shareholders": [{"name": null, "stake_percent": null, "country": null, "notes": null}],
    "ultimate_beneficial_owner": null,
    "ubo_stake_pct": null,
    "ubo_holding_entity": null,
    "group_structure_narrative": null
  },
  "4C_management": [
    {"name": null, "title": null, "years_experience": null, "background": null}
  ],
  "4D_business": {
    "primary_business": null,
    "trade_routes": null,
    "operational_model": null,
    "years_in_operation": null,
    "global_ranking": null,
    "market_share_pct": null,
    "countries_served": null,
    "subsidiaries_and_agents": null,
    "terminals_owned": null,
    "annual_cargo_volume_m_teu": null,
    "global_service_routes": null
  },
  "4E_financials": {
    "currency": null,
    "unit": null,
    "fiscal_year": null,
    "revenue": null,
    "ebitda": null,
    "ebitda_margin_pct": null,
    "net_income": null,
    "net_income_attributable_to_parent": null,
    "net_cash_debt": null,
    "net_debt_ebitda": null,
    "fx_rate_to_usd": null,
    "revenue_breakdown": [{"segment": null, "amount": null, "pct_of_total": null}],
    "cogs_breakdown": [{"cost_item": null, "pct_of_total": null}]
  },
  "4F_fleet": {
    "total_owned_teu": null,
    "total_fleet_teu": null,
    "total_fleet_teu_m": null,
    "total_vessels": null,
    "fleet_breakdown": [
      {"category": null, "vessel_count": null, "total_teu": null, "total_dwt": null, "notes": null}
    ],
    "fleet_detail": [
      {"vessel_name": null, "type": null, "teu": null, "dwt": null,
       "year_built": null, "flag": null, "class_society": null, "employment": null}
    ],
    "orderbook": [
      {"vessel_name": null, "type": null, "teu": null, "dwt": null,
       "builder": null, "expected_delivery": null, "financed_by": null, "notes": null}
    ],
    "capex_plan": [
      {"year": null, "capex_usd_m": null, "description": null}
    ]
  },
  "4G_debt_profile": [
    {"lender_bond": null, "facility_type": null, "ccy": null,
     "amount": null, "maturity": null, "secured_unsecured": null}
  ],
  "4H_banking_relationships": [
    {"bank": null, "product": null, "limit_usd_m": null, "since": null}
  ],
  "4I_market_data": {
    "ccfi_level": null,
    "scfi_level": null,
    "ccfi_yoy_pct": null,
    "order_book_pct_of_fleet": null,
    "alliance_membership": null,
    "imo_regulatory_notes": null,
    "tariff_risk_notes": null,
    "freight_rate_spot_date": null,
    "freight_rate_by_route": {
      "far_east_north_america_usd_teu": null,
      "far_east_europe_usd_teu": null,
      "far_east_us_west_coast_usd_teu": null,
      "far_east_us_east_coast_usd_teu": null,
      "far_east_mediterranean_usd_teu": null
    },
    "scfi_quarterly_history": [
      {"period": null, "scfi": null, "fe_us_west": null, "fe_us_east": null,
       "fe_europe": null, "fe_mediterranean": null}
    ],
    "scfi_event_log": [
      {"date": null, "scfi_level": null, "change_pct": null,
       "wave_direction": null, "event_trigger": null}
    ],
    "fuel_cost_pct_of_cogs": [
      {"period": null, "fuel_pct": null}
    ],
    "oil_prices": [
      {"date_range": null, "wti_usd_bbl": null, "brent_usd_bbl": null}
    ],
    "wti_latest": null,
    "brent_latest": null,
    "geopolitical_chokepoints": [],
    "world_uncertainty_index_level": null,
    "world_uncertainty_index_date": null
  },
  "4J_peer_comparison": [
    {"company": null, "fleet_teu": null, "market_share_pct": null,
     "alliance": null, "listed_yn": null}
  ],
  "4K_major_customers": [
    {"name": null, "contract_type": null, "duration_years": null}
  ],
  "4L_macro_context": {
    "source": null,
    "report_date": null,
    "gdp_projections": [
      {"region": null, "gdp_2024_pct": null, "gdp_2025_pct": null,
       "gdp_2026_pct": null, "gdp_2027_pct": null}
    ],
    "fleet_supply_demand": [
      {"year": null, "fleet_capacity_mteu": null,
       "capacity_growth_pct": null, "throughput_growth_pct": null}
    ],
    "supply_demand_forecast_by_institute": [
      {"institute": null, "metric": null,
       "yr_2023_pct": null, "yr_2024_pct": null, "yr_2025_pct": null,
       "yr_2026F_pct": null, "yr_2027F_pct": null}
    ],
    "excess_supply_by_institute": [
      {"institute": null, "yr_2025_pct": null,
       "yr_2026F_pct": null, "yr_2027F_pct": null}
    ],
    "key_market_drivers": [],
    "geopolitical_risk_narrative": null,
    "seasonal_cargo_profile": {
      "peak_months": [],
      "trough_months": [],
      "peak_season_desc": null,
      "trough_season_desc": null,
      "special_distortion_year": null,
      "special_distortion_notes": null
    }
  },
  "4M_alliance_history": {
    "alliance_name": null,
    "alliance_members": [],
    "current_phase": null,
    "history": [
      {"phase": null, "period_start": null, "period_end": null,
       "routes_count": null, "vessels_count": null, "capacity_wan_teu": null}
    ]
  },
  "4N_trade_route_volume": [
    {
      "route": null,
      "volume_unit": null,
      "annual_yoy_pct": null,
      "annual_data": [
        {"year": null, "volume_wan_teu": null, "yoy_pct": null}
      ],
      "quarterly_data": [
        {"period": null, "period_type": null,
         "volume_prior_year": null, "volume_current_year": null, "yoy_pct": null}
      ],
      "monthly_data": [
        {"month": null, "volume_prior_year": null,
         "volume_current_year": null, "yoy_pct": null}
      ]
    }
  ],
  "4O_weekly_capacity_by_route": [
    {
      "route": null,
      "yoy_change_pct": null,
      "quarterly_capacity": [
        {"year": null, "quarter": null, "weekly_capacity_teu": null}
      ]
    }
  ]
}""",

    5: """Section 5 — Collateral / Responsible Person / Guarantor / Support:
{
  "5A_security_overview": {
    "is_secured": null,
    "unsecured_reason": null,
    "security_instruments": [{"rank": null, "instrument": null, "description": null}]
  },
  "5B_refund_guarantee": {
    "applicable": null,
    "issuer_full_name": null,
    "issuer_rating": null,
    "rating_agency": null,
    "legal_structure": null,
    "governing_law": null,
    "assigned_to_cub": null,
    "expiry_condition": null,
    "milestones": [
      {"milestone": null, "sched_date": null, "rg_amount_usd_m": null,
       "max_loan_os_usd_m": null, "coverage_pct": null,
       "drawdown_usd_m": null, "cum_drawdown_usd_m": null, "status": null}
    ],
    "lag_time_days": null,
    "lag_time_analysis": null,
    "footnotes": null
  },
  "5C_vessel_mortgage": {
    "applicable": null,
    "vessel_valuations": [
      {"vessel": null, "teu": null, "dwt": null, "year_built": null,
       "valuer": null, "valuation_date": null,
       "market_value_usd_m": null, "distressed_value_usd_m": null}
    ],
    "gongwen_ref": null,
    "valuation_compliant": null,
    "contract_price_usd_m": null,
    "loan_amount_usd_m": null,
    "ltc_pct": null,
    "ltc_limit_pct": null,
    "acr_at_delivery_pct": null,
    "acr_floor_pct": null,
    "balloon_usd_m": null,
    "ltv_at_maturity_pct": null,
    "ltv_cap_pct": null,
    "amortisation_schedule": [
      {"period": null, "date": null, "principal_usd_m": null, "interest_usd_m": null,
       "total_debt_service_usd_m": null, "outstanding_balance_usd_m": null,
       "vessel_value_usd_m": null, "acr_pct": null, "ltv_pct": null}
    ]
  },
  "5D_insurance": [
    {"type": null, "insurer_or_club": null, "insured_value_usd_m": null, "notes": null}
  ],
  "5E_value_maintenance_clause": {
    "acr_covenant_pct": null,
    "ltv_covenant_pct": null,
    "test_frequency_verbatim": null,
    "cure_period_banking_days": null,
    "remedy_options": [],
    "cure_mechanism_verbatim": null
  },
  "5F_corporate_guarantee": {
    "applicable": null,
    "guarantor_full_name": null,
    "guarantor_listed_exchange": null,
    "relationship_to_borrower": null,
    "guarantee_scope": null,
    "guarantee_phases": [],
    "fx_rate_to_usd": null,
    "guarantor_financials": [
      {"metric": null, "fy_prior_twd_bn": null, "fy_prior_usd_bn": null,
       "fy_current_twd_bn": null, "fy_current_usd_bn": null}
    ],
    "support_capacity_assessment": null,
    "historical_support_record": null,
    "guarantee_language": null
  },
  "5G_responsible_person": {
    "provided": null,
    "name": null,
    "title": null,
    "scope": null
  }
}""",

    6: """Section 6 — Project Analysis:
{
  "6A_project": {
    "hull_number": null, "vessel_type": null, "teu": null, "fuel_type": null,
    "imo_tier": null, "eco_design": null, "dwt": null, "grt": null,
    "loa_m": null, "beam_m": null, "main_engine": null, "speed_knots": null,
    "class_society": null, "flag_state": null,
    "contract_price_usd_m": null, "loan_amount_usd_m": null, "ltc_pct": null,
    "delivery_date": null, "grace_period_days": null, "latest_delivery_date": null,
    "deployment_purpose": null, "eu_ets_applicable": null,
    "regulatory_positioning": null
  },
  "6B_builder": {
    "name": null, "formerly": null, "founded": null, "hq": null, "listed": null,
    "market_position": null, "market_position_source": null,
    "market_position_date": null,
    "contracts_for_large_vessels": [],
    "track_record_verbatim": null,
    "technology_overlap_verbatim": null,
    "historical_note_verbatim": null,
    "ontime_delivery_pct": null, "shipyard_docks": null,
    "shipyard_berth_m": null, "shipyard_capacity_dwt": null,
    "shipyard_annual_cgt": null
  },
  "6C_contract": {
    "contract_type": null, "buyer": null, "builder": null,
    "price_verbatim": null, "currency": null, "contract_date": null,
    "expected_delivery": null, "grace_period": null, "latest_delivery_date": null,
    "late_delivery_penalty_verbatim": null,
    "buyer_termination_verbatim": null,
    "builder_termination_verbatim": null,
    "change_order_verbatim": null,
    "rows": [{"term": null, "detail_verbatim": null}]
  },
  "6D_milestones": {
    "milestones": [
      {"no": null, "milestone": null, "expected_date": null, "actual_date": null,
       "status": null, "pct_of_contract": null, "amount_usd_m": null,
       "cum_paid_usd_m": null, "cub_drawdown": null,
       "rg_in_force": null, "rg_amount_usd_m": null}
    ],
    "footnotes": [{"symbol": null, "text_verbatim": null}],
    "commentary_first_drawdown": null,
    "commentary_banking_act_33_3": null,
    "commentary_pam_sam": null
  },
  "6E_rg_mechanism": {
    "applicable": null, "issuer_full_name": null,
    "issuer_rating_verbatim": null, "beneficiary": null,
    "format_verbatim": null, "governing_law": null,
    "trigger_events": [], "claim_process_verbatim": null,
    "payout_timeline": null,
    "coverage_summary_min_pct": null, "coverage_summary_max_pct": null
  },
  "6F_construction_progress": {
    "status_date": null, "milestones_completed": null, "milestones_total": null,
    "completion_pct": null, "on_schedule": null, "next_milestone": null,
    "risks": [
      {"title": null, "likelihood": null, "description": null,
       "mitigant_bullets": []}
    ]
  },
  "6G_force_majeure": {
    "applicable": null, "covered_events": [],
    "historical_context_verbatim": null,
    "current_supply_chain_status": null
  }
}""",

    7: """Section 7 — Financial Analysis:
{
  "entities_to_analyze": [
    {"name": null, "role": null, "basis": null, "auditor": null, "opinion": null,
     "currency": null, "unit": null, "guarantor_exists": null, "depth": null}
  ],
  "7A_borrower_financials": {
    "reporting_currency": null, "unit": null, "reporting_entity": null,
    "auditor": null, "audit_opinion": null,
    "accounting_standard": null, "fiscal_year_end": null,
    "income_statement": {"FY_YYYY": {
      "revenue": null, "cogs": null, "gross_profit": null,
      "other_op_income": null, "op_profit": null,
      "finance_income": null, "finance_cost": null, "other_non_op": null,
      "pbt": null, "tax": null, "net_income": null,
      "minority_interest": null, "net_income_to_parent": null,
      "eps": null,
      "ebitda": null, "depreciation": null}},
    "quarterly_income_statement": {"QN_YYYY": {
      "revenue": null, "op_profit": null, "net_income": null,
      "eps": null, "gross_margin_pct": null, "op_margin_pct": null, "ni_margin_pct": null}},
    "balance_sheet": {"FY_YYYY": {
      "cash": null, "trade_receivables": null, "inventories": null,
      "other_ca": null, "total_ca": null,
      "vessels_ppe": null, "right_of_use_assets": null,
      "intangible_assets": null,
      "other_nca": null, "total_nca": null, "total_assets": null,
      "trade_payables": null, "st_borrowings": null,
      "current_lease_liabilities": null, "other_cl": null, "total_cl": null,
      "lt_borrowings": null, "nc_lease_liabilities": null,
      "other_ncl": null, "total_ncl": null, "total_liabilities": null,
      "share_capital": null, "retained_earnings": null,
      "controlling_interest_equity": null,
      "non_controlling_interest": null,
      "total_equity": null}},
    "cash_flow": {"FY_YYYY": {
      "ocf": null, "capex": null, "icf": null, "fcf": null, "net_change": null,
      "opening_cash": null, "fx_effect": null, "closing_cash": null}}
  },
  "7B_key_ratios": {"FY_YYYY": {
    "gross_margin_pct": null, "op_margin_pct": null,
    "ni_margin_pct": null, "ebitda_margin_pct": null,
    "roa_pct": null, "roe_pct": null,
    "total_debt": null, "net_debt": null,
    "debt_equity": null, "net_debt_equity": null, "debt_ebitda": null,
    "ebitda_interest": null, "ocf_total_debt": null, "ocf_interest": null,
    "ar_days": null, "ap_days": null, "inventory_days": null,
    "dscr": null, "tangible_leverage": null, "current_ratio": null}},
  "7C_guarantor_financials": {
    "applicable": null, "depth": null,
    "guarantor_name": null, "reporting_currency": null, "unit": null,
    "income_statement": {}, "balance_sheet": {}, "cash_flow": {}
  },
  "7D_guarantor_ratios": {"applicable": null, "FY_YYYY": {}},
  "7E_base_case": {
    "applicable": null,
    "key_assumptions": [{"assumption": null, "value": null, "source": null}],
    "projected_financials": {"FY_YYYY": {
      "revenue": null, "gross_profit": null, "op_profit": null, "net_income": null,
      "cash": null, "debt": null, "equity": null,
      "ocf": null, "capex": null, "debt_service": null, "fcf": null}},
    "dscr_table": [{"period": null, "ocf": null, "debt_service": null, "dscr": null}],
    "conclusion": null
  },
  "7F_worse_case": {
    "applicable": null,
    "stress_assumptions": [
      {"assumption": null, "base": null, "worse": null, "stress_magnitude": null}
    ],
    "stressed_summary": {"FY_YYYY": {
      "revenue": null, "op_profit": null, "net_income": null,
      "ocf": null, "cash": null, "dscr": null}},
    "conclusion": null
  },
  "7G_lessee_financials": {"applicable": null, "lessees": []},
  "7H_sensitivity": {
    "applicable": null,
    "rows": [{"variable": null, "base_case": null, "stress": null,
      "dscr_min_impact": null, "cash_trough_impact": null, "conclusion": null}]
  },
  "7I_quarterly_kpis": [
    {
      "quarter": null,
      "revenue": null,
      "gross_margin_pct": null,
      "op_margin_pct": null,
      "ni_margin_pct": null,
      "interest_coverage_x": null,
      "current_ratio_pct": null,
      "debt_ratio_pct": null
    }
  ],
  "industry_index": {"ccfi_level": null, "scfi_level": null, "year": null},
  "fx_exposure": null, "off_balance_sheet": null, "accounting_notes": null
}""",

    8: """Section 8 — ACRA Banking Charges:
{
  "8A_acra_banking_charges": {
    "section_applicability": "internal_only | not_applicable",
    "acra_data_available": true,
    "jurisdiction": "Singapore",
    "search_date": "DD MMM YYYY",
    "entity_name": "Full legal entity name",
    "uen": "ACRA UEN",
    "charges": [
      {
        "no": 1,
        "chargee": "Full bank/lender name",
        "date_of_registration": "DD MMM YYYY",
        "date_of_charge": "DD MMM YYYY",
        "amount_usd_m": 0.0,
        "currency": "USD | SGD | other",
        "property_charged": "Description of charged property (include CUB annotation if is_cub_charge)",
        "status": "Registered | Satisfied (DD MMM YYYY)",
        "is_cub_charge": false,
        "cub_facility_ref": null
      }
    ],
    "summary": {
      "total_charges": 0,
      "active_charges": 0,
      "satisfied_charges": 0,
      "total_active_usd_m": 0.0,
      "cub_charge_count": 0,
      "cub_total_usd_m": 0.0,
      "unique_chargees": [],
      "distinct_banking_groups": 0
    }
  },
}
""",

    9: """Section 9 — Credit Analysis Checklist & Recommendation:
{
  "9A_checklist": [
    {
      "no": 1,
      "category": "KYC & Compliance | Sanctions & AML | Credit Risk | Financial | Collateral | Legal & Documentation | ESG & Environmental | Regulatory (MAS)",
      "item": "Checklist item description",
      "response": "Yes | No* | N/A",
      "remarks": "Specific figures, names, dates required (e.g. MSR level, DSCR, valuer+date, §33-3 amount)"
    }
  ],
  "9B_conditions_covenants": {
    "conditions_precedent": [
      {"no": 1, "description": "CP description", "testing": "Before first drawdown | Before vessel delivery"}
    ],
    "ongoing_covenants": [
      {"description": "Covenant description", "threshold": "e.g. ACR >= 100%", "testing": "Every 2 years | Semi-annual | Annual | Ongoing"}
    ],
    "financial_covenants": [
      {"covenant": null, "threshold": null, "testing_frequency": null}
    ]
  },
  "9C_recommendation": {
    "decision": "APPROVE | APPROVE WITH CONDITIONS | DECLINE",
    "facility_amount_usd_m": 0.0,
    "tenor_years": 0,
    "security_structure": "brief description",
    "key_conditions": ["condition 1", "condition 2"],
    "balloon_ltv_pct": null,
    "balloon_ltv_cap_pct": null,
    "margin_bps": null,
    "pricing_summary": null,
    "risk_level_changes_from_prior": "None | Improved | Deteriorated — reason"
  },
  "9D_signoff": {
    "date": "DD MMM YYYY",
    "prepared_by": "Name, Title",
    "reviewed_by": "Name, Title",
    "department": "Credit Management Department, CUB SG Branch"
  }
}""",

    10: """Section 10 — Appendix (3 input blocks):
{
  "10A_group_exposure": {
    "entity_group": "Group name (e.g. EMC/EMA/EVA Group)",
    "group_limit_usd_m": 0.0,
    "currency": "USD",
    "unit": "millions",
    "as_of_date": "MMM YYYY",
    "rows": [
      {
        "entity": "Legal entity name",
        "branch": "SG | TW | HK | etc.",
        "facility_type": "Term Loan (SLL) | RCF | etc.",
        "current_approved_usd_m": 0.0,
        "proposed_usd_m": 0.0,
        "outstanding_usd_m": 0.0,
        "collateral": "RG + Vessel Mortgage | Clean | etc.",
        "guarantor": "EMC | None | etc.",
        "maturity_str": "Dec 2034E",
        "msr": "MSR3 | —",
        "is_new_facility": false,
        "subtotal_type": null
      }
    ],
    "group_limit_sub_table": {
      "approved_group_limit_usd_m": 0.0,
      "proposed_total_exposure_usd_m": 0.0,
      "utilization_pct": 0.0,
      "headroom_usd_m": 0.0
    },
    "eva_note": null
  },
  "10B_fleet_growth": {
    "group_name": "EMC",
    "year_range": "2023-2028E",
    "rows": [
      {
        "year_label": "2023",
        "owned_fleet_teu_m": 0.0,
        "total_fleet_teu_m": 0.0,
        "total_vessels": 0,
        "owned_pct": 0.0,
        "yoy_growth_pct": 0.0
      }
    ],
    "cagr_pct": 0.0,
    "chart_reference": "EMC Fleet Capacity Growth Chart — Source: [Source] [Date] / EMC Investor Presentation",
    "key_notes": [
      "Target capacity: [X] TEU by [year]",
      "Owned fleet transition: X% → Y% — reducing charter reliance",
      "Newbuild delivery: [X] vessels; orderbook [Y] vessels (Source: [Z])",
      "CUB-financed vessel: [X] TEU, Hull No. [Y], delivery [date]",
      "EMC CAPEX plan: USD[X]m; EMA capital commitment: USD[Y]m (as of [date])"
    ]
  },
  "10C_projections": {
    "entity_name": "EMA Standalone",
    "basis": "Standalone",
    "currency": "USD",
    "unit": "USD'000",
    "key_assumptions": [
      {"assumption": "Charter rate (USD/day)", "FY2026E": 28000, "FY2027E": 28500}
    ],
    "assumptions_narrative": "Revenue growth assumes [basis]. COGS reflects [basis]. CAPEX per [basis].",
    "base_case_pl": [
      {"item": "Revenue", "FY2026E": 0, "FY2027E": 0, "is_subtotal": false},
      {"item": "Cost of Goods Sold", "FY2026E": 0, "FY2027E": 0, "is_subtotal": false},
      {"item": "Gross Profit", "FY2026E": 0, "FY2027E": 0, "is_subtotal": true}
    ],
    "base_case_bs": [
      {"item": "Cash & Equivalents", "FY2026E": 0, "FY2027E": 0, "is_subtotal": false},
      {"item": "Total Current Assets", "FY2026E": 0, "FY2027E": 0, "is_subtotal": true}
    ],
    "base_case_cf": [
      {"item": "Operating Cash Flow", "FY2026E": 0, "FY2027E": 0, "is_subtotal": false},
      {"item": "Closing Cash", "FY2026E": 0, "FY2027E": 0, "is_subtotal": true}
    ],
    "base_case_dscr": [
      {"year_label": "FY2026E", "ocf": 0, "debt_service": 0, "dscr": 0.0}
    ],
    "dscr_commentary": "DSCR remains above [X]x throughout. Minimum DSCR of [X]x occurs in [years].",
    "stress_assumptions": [
      {"assumption": "Revenue", "base_case": "[X]", "worse_case": "[Y]", "stress_magnitude": "-20%"}
    ],
    "worse_case_summary": [
      {"item": "Revenue", "value": 0, "is_dscr": false},
      {"item": "DSCR", "value": 0.0, "is_dscr": true}
    ],
    "worse_case_commentary": "Under Worse Case, DSCR declines to minimum [X]x in [year] but remains above 1.0x..."
  },
  "10D_monthly_shipping_ops": [
    {
      "month": null,
      "volume_wan_teu": null,
      "avg_freight_rate_usd_teu": null,
      "fuel_cost_usd_ton": null
    }
  ],
  "10E_quarterly_revenue_by_year": [
    {
      "quarter": null,
      "years": {}
    }
  ],
  "10F_fleet_capacity_growth": {
    "group_name": null,
    "unit": "TEU",
    "rows": [
      {"year": null, "fleet_capacity_teu": null, "growth_pct_vs_base": null,
       "is_forecast": null}
    ],
    "cagr_pct": null
  },
  "10G_peer_newbuilding": {
    "period": null,
    "unit": "000 TEU",
    "source": null,
    "carriers": [
      {"carrier": null, "newbuilding_teu": null, "rank": null}
    ]
  }
}""",

    11: """Section 11 — Analyst / Broker Research Report:
{
  "11A_report_meta": {
    "analyst_firm": null,
    "analyst_name": null,
    "analyst_email": null,
    "report_date": null,
    "subject_company_en": null,
    "subject_company_zh": null,
    "subject_ticker": null,
    "report_type": null,
    "language": null,
    "pages": null,
    "data_sources": []
  },
  "11B_rating": {
    "current_rating": null,
    "current_rating_zh": null,
    "rating_change": null,
    "prior_rating": null,
    "target_price_3m": null,
    "target_price_12m": null,
    "target_price_currency": null,
    "current_price": null,
    "current_price_currency": null,
    "upside_pct": null,
    "rating_history": [
      {"date": null, "rating": null, "target_price": null, "note": null}
    ],
    "estimate_revision": {
      "prior_target_price": null,
      "current_target_price": null,
      "target_price_valuation_basis": null,
      "prior_revenue_estimate": null,
      "current_revenue_estimate": null,
      "revenue_estimate_unit": null,
      "prior_eps_estimate": null,
      "current_eps_estimate": null,
      "estimate_year": null
    }
  },
  "11C_company_fundamentals": {
    "currency": null,
    "unit": null,
    "share_capital_m_shares": null,
    "market_cap": null,
    "book_value_per_share": null,
    "book_value_forecast_per_share": null,
    "book_value_forecast_year": null,
    "net_cash_per_share": null,
    "foreign_holding_pct": null,
    "institutional_holding_pct": null,
    "insider_holding_pct": null,
    "margin_balance_shares": null,
    "dividend_yield_pct": null,
    "shares_for_eps_calc": null,
    "debt_ratio_pct": null,
    "esg_rating_sustainalytics": null,
    "esg_risk_tier": null,
    "product_mix_notes": null,
    "revenue_geographic_mix": [
      {"region": null, "pct_of_revenue": null, "year": null}
    ],
    "revenue_composition_by_year": [
      {"year": null, "freight_income_pct": null, "agency_fees_pct": null,
       "slottage_income_pct": null, "container_construction_pct": null, "others_pct": null}
    ]
  },
  "11D_investment_thesis": {
    "summary_verbatim": null,
    "bull_points": [],
    "key_catalysts": [],
    "risks": [],
    "valuation_comment": null,
    "key_industry_drivers": []
  },
  "11E_annual_income_statement": {
    "currency": null,
    "unit": null,
    "periods": [
      {
        "year": null,
        "is_forecast": null,
        "revenue": null,
        "cogs": null,
        "gross_profit": null,
        "op_expenses": null,
        "op_profit": null,
        "ebitda": null,
        "non_op_income": null,
        "pre_tax_income": null,
        "tax": null,
        "net_income": null,
        "minority_interest": null,
        "net_income_to_parent": null,
        "eps": null,
        "diluted_eps": null,
        "gross_margin_pct": null,
        "ebitda_margin_pct": null,
        "op_margin_pct": null,
        "ni_margin_pct": null,
        "revenue_yoy_pct": null,
        "gross_profit_yoy_pct": null,
        "op_profit_yoy_pct": null,
        "net_income_yoy_pct": null,
        "eps_yoy_pct": null,
        "per_ratio": null,
        "pbr_ratio": null,
        "roe_pct": null,
        "roa_pct": null,
        "cash_dividend_per_share": null,
        "cash_dividend_yield_pct": null,
        "payout_ratio_pct": null
      }
    ]
  },
  "11F_quarterly_income_statement": {
    "currency": null,
    "unit": null,
    "periods": [
      {
        "quarter": null,
        "is_forecast": null,
        "revenue": null,
        "cogs": null,
        "gross_profit": null,
        "op_expenses": null,
        "op_profit": null,
        "non_op_income": null,
        "pre_tax_income": null,
        "tax": null,
        "net_income": null,
        "minority_interest": null,
        "net_income_to_parent": null,
        "eps": null,
        "gross_margin_pct": null,
        "pre_tax_margin_pct": null,
        "op_margin_pct": null,
        "ni_margin_pct": null,
        "ebitda": null,
        "revenue_qoq_pct": null,
        "revenue_yoy_pct": null,
        "op_profit_qoq_pct": null,
        "op_profit_yoy_pct": null,
        "pre_tax_qoq_pct": null,
        "pre_tax_yoy_pct": null,
        "net_income_qoq_pct": null,
        "net_income_yoy_pct": null,
        "eps_qoq_pct": null,
        "eps_yoy_pct": null,
        "effective_tax_rate_pct": null,
        "days_of_inventory": null,
        "days_of_receivables": null,
        "days_of_payables": null,
        "cash_conversion_cycle": null,
        "quarterly_free_cash_flow": null
      }
    ]
  },
  "11G_balance_sheet": {
    "currency": null,
    "unit": null,
    "periods": [
      {
        "year": null,
        "is_forecast": null,
        "cash": null,
        "accounts_receivable": null,
        "inventory": null,
        "other_current_assets": null,
        "total_current_assets": null,
        "equity_method_investments": null,
        "ppe_net": null,
        "intangible_assets": null,
        "other_non_current_assets": null,
        "total_assets": null,
        "accounts_payable": null,
        "st_borrowings": null,
        "other_current_liabilities": null,
        "total_current_liabilities": null,
        "lt_borrowings": null,
        "other_non_current_liabilities": null,
        "total_non_current_liabilities": null,
        "total_liabilities": null,
        "share_capital": null,
        "retained_earnings": null,
        "controlling_interest_equity": null,
        "non_controlling_interest": null,
        "total_equity": null,
        "total_liabilities_and_equity": null
      }
    ]
  },
  "11H_cash_flow": {
    "currency": null,
    "unit": null,
    "periods": [
      {
        "year": null,
        "is_forecast": null,
        "operating_cash_flow": null,
        "pre_tax_income": null,
        "depreciation_amortization": null,
        "working_capital_change": null,
        "other_operating": null,
        "investing_cash_flow": null,
        "capex": null,
        "lt_investment_change": null,
        "other_investing": null,
        "financing_cash_flow": null,
        "lt_debt_bonds_change": null,
        "capital_increase": null,
        "cash_dividends_paid": null,
        "other_financing": null,
        "fx_effect": null,
        "net_cash_change": null,
        "beginning_cash": null,
        "ending_cash": null,
        "free_cash_flow": null
      }
    ]
  },
  "11I_ratio_analysis": {
    "currency": null,
    "unit": null,
    "periods": [
      {
        "year": null,
        "is_forecast": null,
        "revenue_growth_pct": null,
        "gross_profit_growth_pct": null,
        "op_profit_growth_pct": null,
        "net_income_growth_pct": null,
        "gross_margin_pct": null,
        "ebitda_margin_pct": null,
        "op_margin_pct": null,
        "ni_margin_pct": null,
        "roa_pct": null,
        "roe_pct": null,
        "debt_ratio_pct": null,
        "debt_to_equity_pct": null,
        "current_ratio_pct": null,
        "quick_ratio_pct": null,
        "interest_coverage_x": null,
        "net_debt_to_equity_pct": null,
        "inventory_days": null,
        "ar_days": null,
        "ev_ebitda_ratio": null,
        "price_to_fcf_ratio": null,
        "price_to_sales_ratio": null
      }
    ]
  },
  "11J_valuation_metrics": {
    "pbr_current": null,
    "per_current": null,
    "ev_ebitda_current": null,
    "target_pbr": null,
    "target_per": null,
    "valuation_methodology": null,
    "per_band_levels": [],
    "per_band_chart_start": null,
    "per_band_chart_end": null,
    "pbr_band_levels": [],
    "pbr_band_chart_start": null,
    "pbr_band_chart_end": null,
    "peer_comparison": [
      {
        "company": null,
        "ticker": null,
        "group": null,
        "rating": null,
        "stock_price": null,
        "market_cap_m_usd": null,
        "eps_by_year": [{"year": null, "eps": null, "is_forecast": null}],
        "per_by_year": [{"year": null, "per": null, "is_forecast": null}],
        "eps_growth_pct_by_year": [{"year": null, "growth_pct": null, "is_forecast": null}],
        "roe_pct_by_year": [{"year": null, "roe_pct": null, "is_forecast": null}],
        "bv_per_share_by_year": [{"year": null, "bv": null, "is_forecast": null}],
        "pbr_by_year": [{"year": null, "pbr": null, "is_forecast": null}]
      }
    ]
  },
  "11K_esg": {
    "sustainalytics_total_score": null,
    "sustainalytics_exposure_score_A": null,
    "sustainalytics_execution_score_B": null,
    "sustainalytics_risk_rating": null,
    "sustainalytics_industry_rank": null,
    "sustainalytics_assessment_date": null,
    "key_esg_issues": [],
    "co2_reduction_target_pct": null,
    "co2_base_year": null,
    "co2_target_year": null,
    "carbon_neutral_target_year": null,
    "certifications": [],
    "key_initiatives": [],
    "regulatory_frameworks": []
  },
  "11L_industry_context": {
    "key_market_indicators": [
      {"name": null, "value": null, "unit": null, "date": null}
    ],
    "industry_theme_verbatim": null,
    "market_watch_points": [
      {"theme": null, "detail": null, "impact": null}
    ],
    "forward_outlook_narrative": null
  },
  "11M_quarterly_forecast_comparison": [
    {
      "figure_no": null,
      "figure_title": null,
      "quarter_reviewed": null,
      "currency": null,
      "unit": null,
      "line_items": [
        {
          "item": null,
          "prior_year_same_q_actual": null,
          "prior_year_same_q_label": null,
          "sequential_q_actual": null,
          "sequential_q_label": null,
          "current_q_value": null,
          "current_q_label": null,
          "qoq_pct": null,
          "yoy_pct": null,
          "analyst_prior_estimate": null,
          "market_consensus": null,
          "variance_vs_analyst_pct": null,
          "variance_vs_consensus_pct": null
        }
      ],
      "key_ratios": [
        {
          "ratio_name": null,
          "prior_year_same_q_pct": null,
          "sequential_q_pct": null,
          "current_q_pct": null,
          "qoq_bps": null,
          "yoy_bps": null,
          "analyst_estimate_pct": null,
          "market_estimate_pct": null,
          "variance_vs_analyst_bps": null,
          "variance_vs_consensus_bps": null
        }
      ]
    }
  ],
  "11N_monthly_revenue": [
    {
      "month": null,
      "revenue": null,
      "mom_pct": null,
      "yoy_pct": null,
      "notes": null
    }
  ],
  "11O_estimate_revision_detail": [
    {
      "year": null,
      "currency": null,
      "unit": null,
      "revenue_revised": null,
      "revenue_prior": null,
      "revenue_change_pct": null,
      "gross_profit_revised": null,
      "gross_profit_prior": null,
      "gross_profit_change_pct": null,
      "op_profit_revised": null,
      "op_profit_prior": null,
      "op_profit_change_pct": null,
      "pre_tax_profit_revised": null,
      "pre_tax_profit_prior": null,
      "pre_tax_profit_change_pct": null,
      "net_profit_revised": null,
      "net_profit_prior": null,
      "net_profit_change_pct": null,
      "eps_revised": null,
      "eps_prior": null,
      "eps_change_pct": null,
      "gross_margin_revised_pct": null,
      "gross_margin_prior_pct": null,
      "gross_margin_change_bps": null,
      "op_margin_revised_pct": null,
      "op_margin_prior_pct": null,
      "op_margin_change_bps": null,
      "net_margin_revised_pct": null,
      "net_margin_prior_pct": null,
      "net_margin_change_bps": null
    }
  ]
}""",
}


# Maximum characters sent per Gemini call. Gemini 1.5 Pro supports ~2M tokens;
# 350 000 chars ≈ 87 500 tokens — generous headroom for schema + response.
_ETL_CHUNK_SIZE = 350_000
_ETL_CHUNK_OVERLAP = 10_000


def _build_etl_prompt(
    document_type: str, text: str, section_nos: list[int], chunk_info: str = ""
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for Gemini ETL extraction."""
    schema_parts = "\n\n".join(
        SECTION_EXTRACTION_SCHEMA[n] for n in section_nos if n in SECTION_EXTRACTION_SCHEMA
    )
    doc_type_label = document_type.replace("_", " ").title()
    chunk_note = f"\n[Document chunk: {chunk_info}]\n" if chunk_info else ""
    user_prompt = (
        f"Document type: {doc_type_label}{chunk_note}\n\n"
        f"Target sections to extract: {section_nos}\n\n"
        f"Required JSON schema (extract these fields if present):\n{schema_parts}\n\n"
        f"---DOCUMENT TEXT START---\n{text}\n---DOCUMENT TEXT END---\n\n"
        "Return ONLY valid JSON with section numbers (as strings) as keys. "
        "Example: {\"4\": {\"4A_borrower\": {\"company_name_zh\": \"...\", ...}, ...}, "
        "\"7\": {\"7A_borrower_financials\": {...}}}\n"
        "Extract whatever data IS present — partial extraction is better than returning nothing."
    )
    logger.info(
        "[ETL] _build_etl_prompt: doc_type=%s sections=%s "
        "system_prompt_chars=%d schema_chars=%d text_chars=%d user_prompt_chars=%d chunk=%r",
        document_type, section_nos,
        len(ETL_SYSTEM_PROMPT), len(schema_parts), len(text), len(user_prompt), chunk_info,
    )
    return ETL_SYSTEM_PROMPT, user_prompt


def _deep_merge_etl(base: dict, overlay: dict) -> dict:
    """Merge two ETL result dicts: overlay fills nulls in base; lists are extended (no dupes)."""
    merged: dict = dict(base)
    for k, v in overlay.items():
        if k not in merged or merged[k] is None:
            merged[k] = v
        elif isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge_etl(merged[k], v)
        elif isinstance(merged[k], list) and isinstance(v, list):
            # Append items from overlay that aren't already in base
            existing_strs = {json.dumps(item, sort_keys=True) for item in merged[k]}
            merged[k] = list(merged[k]) + [
                item for item in v if json.dumps(item, sort_keys=True) not in existing_strs
            ]
        # If base already has a non-null scalar, keep it (base wins)
    return merged


async def _call_gemini_etl_once(
    document_type: str,
    text_chunk: str,
    target_sections: list[int],
    chunk_info: str = "",
) -> dict[int, dict]:
    """Single Gemini ETL call — returns {section_no: data}. Used by chunked and non-chunked paths."""
    from google import genai
    from google.genai import types as genai_types
    from credit_report.config import GEMINI_API_KEY, GEMINI_ETL_MODEL

    system_prompt, user_prompt = _build_etl_prompt(document_type, text_chunk, target_sections, chunk_info)

    client = genai.Client(api_key=GEMINI_API_KEY)
    t_gemini = time.perf_counter()
    response = await client.aio.models.generate_content(
        model=GEMINI_ETL_MODEL,
        contents=user_prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=65536,
        ),
    )
    gemini_ms = (time.perf_counter() - t_gemini) * 1000
    raw = (response.text or "").strip()

    finish_reason = "unknown"
    try:
        finish_reason = str(response.candidates[0].finish_reason) if response.candidates else "no_candidates"
    except Exception:
        pass

    logger.info("[ETL] Gemini response chunk=%r: elapsed=%.0fms chars=%d finish_reason=%s",
                chunk_info, gemini_ms, len(raw), finish_reason)

    if finish_reason not in ("FinishReason.STOP", "STOP", "1", "unknown"):
        logger.warning("[ETL] Gemini finish_reason=%s chunk=%r — may be truncated. tail=%r",
                       finish_reason, chunk_info, raw[-300:])

    if not raw:
        return {}

    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]

    parsed = _parse_json_tolerant(raw, document_type)
    if parsed is None:
        return {}

    result: dict[int, dict] = {}
    for k, v in parsed.items():
        try:
            sec_no = int(k)
            if isinstance(v, dict) and v and _has_any_value(v):
                result[sec_no] = v
        except (ValueError, TypeError):
            pass
    return result


async def _etl_document_chunked(
    text: str,
    document_type: str,
    target_sections: list[int],
    t_start: float,
) -> dict[int, dict]:
    """Process large documents by splitting into overlapping chunks and merging results."""
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunk = text[i: i + _ETL_CHUNK_SIZE]
        if chunk.strip():
            chunks.append(chunk)
        i += _ETL_CHUNK_SIZE - _ETL_CHUNK_OVERLAP

    n_chunks = len(chunks)
    logger.info("[ETL] chunked mode: doc_chars=%d chunks=%d chunk_size=%d overlap=%d",
                len(text), n_chunks, _ETL_CHUNK_SIZE, _ETL_CHUNK_OVERLAP)

    merged: dict[int, dict] = {}
    for idx, chunk in enumerate(chunks):
        chunk_info = f"{idx + 1}/{n_chunks}"
        try:
            chunk_result = await _call_gemini_etl_once(
                document_type=document_type,
                text_chunk=chunk,
                target_sections=target_sections,
                chunk_info=chunk_info,
            )
            # Deep-merge: first chunk populates, subsequent chunks fill nulls
            for sec_no, sec_data in chunk_result.items():
                if sec_no in merged:
                    merged[sec_no] = _deep_merge_etl(merged[sec_no], sec_data)
                else:
                    merged[sec_no] = sec_data
            logger.info("[ETL] chunk %s extracted sections=%s", chunk_info, list(chunk_result.keys()))
        except Exception as chunk_exc:
            logger.warning("[ETL] chunk %s FAILED: %s — continuing with other chunks", chunk_info, chunk_exc)

    total_ms = (time.perf_counter() - t_start) * 1000
    logger.info("[ETL] chunked ETL complete: chunks=%d sections=%s total_ms=%.0f",
                n_chunks, list(merged.keys()), total_ms)
    return merged


async def etl_document(
    text: str,
    document_type: str,
    section_nos: Optional[list[int]] = None,
) -> dict[int, dict]:
    """
    Use Gemini to extract structured section data from document text.

    Args:
        text: Full extracted document text
        document_type: One of the DOCUMENT_SECTION_MAP keys
        section_nos: Override which sections to extract (default: from DOCUMENT_SECTION_MAP)

    Returns:
        {section_no: {field: value}} — only sections with non-empty extraction
    """
    t_start = time.perf_counter()
    target_sections = section_nos or DOCUMENT_SECTION_MAP.get(document_type, [4, 7])

    logger.info(
        "[ETL] etl_document: START doc_type=%s target_sections=%s text_chars=%d",
        document_type, target_sections, len(text),
    )

    if not target_sections:
        logger.warning("[ETL] etl_document: no target sections for doc_type=%s — returning empty", document_type)
        return {}

    text = text.strip()
    if not text:
        logger.warning("[ETL] etl_document: EMPTY document text — cannot extract anything")
        return {}

    # Log a sample of the text Gemini will receive
    sample_head = text[:300].replace("\n", " ")
    sample_tail = text[-200:].replace("\n", " ") if len(text) > 300 else ""
    logger.info("[ETL] document text sample (head): %r", sample_head)
    if sample_tail:
        logger.info("[ETL] document text sample (tail): %r", sample_tail)

    from credit_report.config import GEMINI_API_KEY

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured — cannot run ETL extraction")

    # For documents exceeding _ETL_CHUNK_SIZE, split into overlapping chunks and merge.
    # This eliminates the previous 400 000-char silent truncation that caused data loss.
    if len(text) > _ETL_CHUNK_SIZE:
        return await _etl_document_chunked(
            text=text,
            document_type=document_type,
            target_sections=target_sections,
            t_start=t_start,
        )

    try:
        result = await _call_gemini_etl_once(
            document_type=document_type,
            text_chunk=text,
            target_sections=target_sections,
        )
        total_ms = (time.perf_counter() - t_start) * 1000
        logger.info(
            "[ETL] etl_document: COMPLETE doc_type=%s total_elapsed=%.0fms "
            "sections_extracted=%s field_counts=%s",
            document_type, total_ms,
            list(result.keys()),
            {k: len(v) for k, v in result.items()},
        )
        if not result:
            logger.warning(
                "[ETL] etl_document: NO DATA EXTRACTED for doc_type=%s — "
                "all sections returned null-only values. "
                "Check: (1) document text quality, (2) document type match, "
                "(3) Gemini raw response above for clues.",
                document_type,
            )
        return result

    except Exception as exc:
        total_ms = (time.perf_counter() - t_start) * 1000
        logger.exception(
            "[ETL] etl_document: EXCEPTION after %.0fms doc_type=%s: %s",
            total_ms, document_type, exc,
        )
        return {}


def _has_any_value(obj) -> bool:
    """Recursively check if a dict/list contains ANY non-null leaf value."""
    if obj is None:
        return False
    if isinstance(obj, dict):
        return any(_has_any_value(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_any_value(item) for item in obj)
    return True  # scalar non-None value


def _parse_json_tolerant(raw: str, doc_type: str) -> dict | None:
    """Parse JSON, with fallback to recover truncated output."""
    # Normal parse
    try:
        parsed = json.loads(raw)
        logger.info("[ETL] JSON parse: SUCCESS chars=%d doc_type=%s", len(raw), doc_type)
        return parsed
    except json.JSONDecodeError as e:
        logger.warning(
            "[ETL] JSON parse: FAILED doc_type=%s error=%s at pos=%d "
            "— attempting truncation recovery",
            doc_type, e.msg, e.pos,
        )

    # Attempt to recover truncated JSON: find the last complete top-level section.
    # Gemini sometimes cuts output mid-stream when hitting token limit.
    logger.warning(
        "[ETL] JSON recovery: scanning for complete sections in %d chars raw_tail=%r",
        len(raw), raw[-200:],
    )
    recovered = {}
    import re
    # Extract all complete "N": { ... } top-level sections using brace counting
    i = 0
    while i < len(raw):
        m = re.search(r'"(\d+)"\s*:', raw[i:])
        if not m:
            break
        key_start = i + m.start()
        after_colon = i + m.end()
        # find the start of the value
        j = after_colon
        while j < len(raw) and raw[j] in ' \t\n\r':
            j += 1
        if j >= len(raw) or raw[j] != '{':
            i = after_colon
            continue
        # count braces to find end of this section
        depth = 0
        end = j
        while end < len(raw):
            if raw[end] == '{':
                depth += 1
            elif raw[end] == '}':
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if depth == 0:
            section_json = raw[j:end + 1]
            try:
                section_data = json.loads(section_json)
                recovered[m.group(1)] = section_data
            except json.JSONDecodeError:
                pass
        i = after_colon

    if recovered:
        logger.info("etl_document: recovered %d sections from truncated JSON doc_type=%s",
                    len(recovered), doc_type)
        return recovered
    logger.error("etl_document: JSON recovery failed doc_type=%s raw_head=%r", doc_type, raw[:300])
    return None


# ── Mapping of ETL section fields → CanonicalFact metric names ───────────────────────────────
# (section_no, sub_key, field_dotted_path, metric_name, unit)
# Dotted paths (e.g. "deal_dscr.dscr_value") are supported for nested fields.
# Only scalar-numeric fields meaningful as standalone facts are included here.
_ETL_FACT_MAP: list[tuple[int, str, str, str, Optional[str]]] = [
    # §1 — Credit Facility
    (1, "facility_summary", "totals.total_credit_limit_usd_m", "credit_limit_usd_m",   "mn"),
    (1, "facility_summary", "totals.psr_spot_limit_usd_m",     "psr_spot_limit_usd_m", "mn"),
    (1, "account_strategy", "nii_usd_m",                       "nii_usd_m",            "mn"),

    # §2 — Overall Comments
    (2, "2B_solvency", "deal_dscr.dscr_value",          "dscr",                  None),
    (2, "2B_solvency", "ema.debt_ebitda_ratio",          "debt_ebitda",           None),
    (2, "2B_solvency", "ema.interest_coverage",          "interest_coverage",     None),
    (2, "2B_solvency", "ema.op_ebitda_bn_usd",           "ema_ebitda_usd_bn",     "bn"),
    (2, "2B_solvency", "ema.total_debt_bn_usd",          "ema_total_debt_usd_bn", "bn"),
    (2, "2C_guarantor", "cash_usd_bn",                   "guarantor_cash_usd_bn",      "bn"),
    (2, "2C_guarantor", "total_debt_usd_bn",             "guarantor_total_debt_usd_bn","bn"),
    (2, "2C_guarantor", "interest_coverage",             "guarantor_interest_coverage", None),

    # §4 — Corporate History
    (4, "4D_business", "market_share_pct",             "market_share_pct",          None),
    (4, "4D_business", "annual_cargo_volume_m_teu",    "annual_cargo_volume_m_teu", "mn"),
    (4, "4E_financials", "revenue",                    "revenue",                   "mn"),
    (4, "4E_financials", "ebitda",                     "ebitda",                    "mn"),
    (4, "4E_financials", "net_income",                 "net_income",                "mn"),
    (4, "4F_fleet",    "total_fleet_teu",              "total_fleet_teu",           None),
    (4, "4F_fleet",    "total_vessels",                "total_vessels",             None),

    # §5 — Collateral / Guarantor
    (5, "5B_refund_guarantee", "lag_time_days",        "rg_lag_time_days",          None),
    (5, "5C_vessel_mortgage",  "contract_price_usd_m", "contract_price_usd_m",      "mn"),
    (5, "5C_vessel_mortgage",  "loan_amount_usd_m",    "loan_amount_usd_m",         "mn"),

    # §6 — Ship Finance / Project Analysis
    (6, "6A_project", "contract_price_usd_m", "contract_price_usd_m", "mn"),
    (6, "6A_project", "loan_amount_usd_m",    "loan_amount_usd_m",    "mn"),
    (6, "6A_project", "ltc_pct",              "ltc_pct",              None),
    (6, "6A_project", "teu",                  "vessel_teu",           None),
    (6, "6A_project", "dwt",                  "vessel_dwt",           None),
    (6, "6B_builder", "ontime_delivery_pct",  "builder_ontime_pct",   None),

    # §7 — handled by _extract_section7_facts() due to dynamic FY_YYYY nesting

    # §8 — ACRA Banking Charges
    (8, "8A_acra_banking_charges", "summary.total_active_usd_m", "acra_total_active_usd_m", "mn"),
    (8, "8A_acra_banking_charges", "summary.cub_total_usd_m",    "acra_cub_total_usd_m",    "mn"),

    # §9 — Recommendation
    (9, "9C_recommendation", "facility_amount_usd_m", "proposed_facility_usd_m", "mn"),
    (9, "9C_recommendation", "tenor_years",            "proposed_tenor_years",    None),
    (9, "9C_recommendation", "balloon_ltv_pct",        "balloon_ltv_pct",         None),
    (9, "9C_recommendation", "margin_bps",             "margin_bps",              None),
]


def _get_nested(d: dict, dotted_path: str):
    """Get a value from a nested dict by dotted key path (e.g. 'totals.total_credit_limit_usd_m')."""
    cur = d
    for key in dotted_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _normalise_year_key(key: str) -> str:
    """Convert FY_YYYY / YYYY / YYYYF to canonical 'FY2024' period string."""
    import re
    s = str(key).strip()
    if "YYYY" in s or "QN" in s:
        return s  # template placeholder — skip
    if re.match(r"^FY\d{4}", s):
        return s  # already canonical: FY2024, FY2024F
    m = re.match(r"^FY_(\d{4})([A-Za-z]?)$", s)
    if m:
        return f"FY{m.group(1)}{m.group(2).upper()}"
    m = re.match(r"^(\d{4})([FEfe]?)$", s)
    if m:
        suffix = m.group(2).upper()
        return f"FY{m.group(1)}{suffix}"
    return s  # quarterly keys (1Q25) or other — keep as-is


def _extract_section7_facts(
    sec7: dict,
    report_id: str,
    doc_id: str,
    default_entity: str,
    default_currency: str,
    seen: set,
) -> list[dict]:
    """Extract multi-year financial facts from §7's FY_YYYY nested structure.

    §7A uses income_statement/{year}/{field}, balance_sheet/{year}/{field},
    cash_flow/{year}/{field}. §7B uses 7B_key_ratios/{year}/{field}.
    Each year becomes a separate CanonicalFact with period='FY{year}'.
    """
    facts: list[dict] = []

    fin = sec7.get("7A_borrower_financials") or {}
    if not isinstance(fin, dict):
        fin = {}

    raw_ccy = str(fin.get("reporting_currency") or "").strip().upper()
    currency = raw_ccy if raw_ccy and len(raw_ccy) == 3 else (default_currency or "USD")

    raw_entity = str(fin.get("reporting_entity") or "").strip()
    entity = raw_entity or default_entity or "borrower"

    def _push(metric: str, raw_val, period: str, unit: str = "mn") -> None:
        dedup = f"{metric}|{entity}|{period}"
        if dedup in seen or raw_val is None:
            return
        num_val = _try_float(raw_val)
        if num_val is None:
            return
        seen.add(dedup)
        facts.append({
            "report_id": report_id,
            "metric_name": metric,
            "entity": entity,
            "period": period,
            "value": num_val,
            "value_text": str(raw_val),
            "currency": currency,
            "unit": unit or "",
            "source_type": "pdf_extraction",
            "source_priority": 3,
            "source_evidence_id": doc_id,
            "source_section_no": 7,
            "state": "extracted",
        })

    # ── Income statement ────────────────────────────────────────────────────────
    income_stmt = fin.get("income_statement") or {}
    if isinstance(income_stmt, dict):
        for year_key, yr in income_stmt.items():
            if not isinstance(yr, dict):
                continue
            p = _normalise_year_key(year_key)
            _push("revenue",                yr.get("revenue"),               p)
            _push("ebitda",                 yr.get("ebitda"),                p)
            _push("gross_profit",           yr.get("gross_profit"),          p)
            _push("net_income",             yr.get("net_income"),            p)
            _push("net_income_to_parent",   yr.get("net_income_to_parent"),  p)
            _push("interest_expense",       yr.get("finance_cost"),          p)
            _push("depreciation",           yr.get("depreciation"),          p)

    # ── Balance sheet ──────────────────────────────────────────────────────────
    bal_sheet = fin.get("balance_sheet") or {}
    if isinstance(bal_sheet, dict):
        for year_key, yr in bal_sheet.items():
            if not isinstance(yr, dict):
                continue
            p = _normalise_year_key(year_key)
            _push("cash_and_equivalents",  yr.get("cash"),          p)
            _push("total_equity",          yr.get("total_equity"),  p)
            _push("total_assets",          yr.get("total_assets"),  p)
            # Derive total_debt = short-term + long-term borrowings
            st = _try_float(yr.get("st_borrowings"))
            lt = _try_float(yr.get("lt_borrowings"))
            if st is not None and lt is not None:
                dedup = f"total_debt|{entity}|{p}"
                if dedup not in seen:
                    seen.add(dedup)
                    facts.append({
                        "report_id": report_id,
                        "metric_name": "total_debt",
                        "entity": entity,
                        "period": p,
                        "value": round(st + lt, 4),
                        "value_text": f"st={st}+lt={lt}",
                        "currency": currency,
                        "unit": "mn",
                        "source_type": "pdf_extraction",
                        "source_priority": 3,
                        "source_evidence_id": doc_id,
                        "source_section_no": 7,
                        "state": "extracted",
                    })

    # ── Cash flow ──────────────────────────────────────────────────────────────
    cf = fin.get("cash_flow") or {}
    if isinstance(cf, dict):
        for year_key, yr in cf.items():
            if not isinstance(yr, dict):
                continue
            p = _normalise_year_key(year_key)
            _push("cash_flow_from_operations", yr.get("ocf"),   p)
            _push("capex",                     yr.get("capex"), p)
            _push("free_cash_flow",            yr.get("fcf"),   p)

    # ── 7B key ratios (FY_YYYY nested) ────────────────────────────────────────
    key_ratios = sec7.get("7B_key_ratios") or {}
    if isinstance(key_ratios, dict):
        for year_key, yr in key_ratios.items():
            if not isinstance(yr, dict):
                continue
            p = _normalise_year_key(year_key)
            _push("total_debt",        yr.get("total_debt"),        p)
            _push("net_debt",          yr.get("net_debt"),          p)
            _push("debt_ebitda",       yr.get("debt_ebitda"),       p, unit="")
            _push("interest_coverage", yr.get("ebitda_interest"),   p, unit="")
            _push("dscr",              yr.get("dscr"),               p, unit="")
            _push("ebitda_margin_pct", yr.get("ebitda_margin_pct"), p, unit="")
            _push("gross_margin_pct",  yr.get("gross_margin_pct"),  p, unit="")
            _push("net_margin_pct",    yr.get("ni_margin_pct"),     p, unit="")
            _push("roe_pct",           yr.get("roe_pct"),           p, unit="")
            _push("debt_to_equity",    yr.get("debt_equity"),       p, unit="")
            _push("current_ratio",     yr.get("current_ratio"),     p, unit="")
            _push("ocf_interest",      yr.get("ocf_interest"),      p, unit="")

    logger.debug(
        "[ETL] _extract_section7_facts: entity=%r facts=%d", entity, len(facts)
    )
    return facts


# ── Numeric unit normalizer ───────────────────────────────────────────────────

_UNIT_MULTIPLIERS: dict[str, float] = {
    # English scale words (output unit: mn = millions)
    "trillion":     1_000_000.0,
    "trillions":    1_000_000.0,
    "billion":      1_000.0,
    "billions":     1_000.0,
    "bn":           1_000.0,
    "b":            1_000.0,
    "million":      1.0,
    "millions":     1.0,
    "mn":           1.0,
    "m":            1.0,
    "thousand":     0.001,
    "thousands":    0.001,
    "k":            0.001,
    # CJK scale words
    "兆":           1_000_000.0,
    "億":           100.0,
    "萬":           0.01,
    "万":           0.01,
}

_CURRENCY_PATTERNS: list[tuple[str, str]] = [
    ("NT$", "TWD"), ("NTD", "TWD"), ("TWD", "TWD"),
    ("HK$", "HKD"), ("HKD", "HKD"),
    ("US$", "USD"), ("USD", "USD"), ("$", "USD"),
    ("EUR", "EUR"), ("€", "EUR"),
    ("GBP", "GBP"), ("£", "GBP"),
    ("JPY", "JPY"), ("¥", "JPY"),
    ("CNY", "CNY"), ("RMB", "CNY"),
    ("SGD", "SGD"),
    ("KRW", "KRW"),
]


def parse_financial_value(raw: str) -> tuple[Optional[float], str, str]:
    """Parse a raw financial string into (value_in_mn, currency_code, unit).

    Examples:
        "NT$2,345 billion"  → (2_345_000.0,  "TWD", "mn")
        "USD 500 million"   → (500.0,         "USD", "mn")
        "5.2兆"             → (5_200_000.0,   "",    "mn")
        "1,234"             → (1234.0,         "",    "")
        "n/a"               → (None,           "",    "")
    """
    import re

    if raw is None:
        return None, "", ""
    s = str(raw).strip()
    if not s or s.lower() in ("n/a", "na", "-", "none", "null", "–", "—"):
        return None, "", ""

    # Detect currency
    currency = ""
    for pattern, code in _CURRENCY_PATTERNS:
        if pattern in s:
            currency = code
            s = s.replace(pattern, "").strip()
            break

    # Detect unit multiplier
    multiplier = None
    unit_out = ""
    s_lower = s.lower()
    for word, mult in sorted(_UNIT_MULTIPLIERS.items(), key=lambda x: -len(x[0])):
        if word in s_lower:
            multiplier = mult
            unit_out = "mn"
            # Remove the unit word from string
            s = re.sub(re.escape(word), "", s, flags=re.IGNORECASE).strip()
            break

    # Extract the numeric part
    clean = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
    if not clean:
        return None, currency, unit_out or ""
    try:
        num = float(clean)
    except ValueError:
        return None, currency, unit_out or ""

    if multiplier is not None:
        num = round(num * multiplier, 6)
        return num, currency, "mn"
    return num, currency, ""


def _try_float(val) -> Optional[float]:
    """Safely convert a value to float; return None if not possible."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        parsed, _, _ = parse_financial_value(val)
        return parsed
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _extract_entity_period_currency(
    extracted: dict[int, dict],
    default_entity: str,
    default_period: str,
    default_currency: str,
) -> tuple[str, str, str]:
    """Derive entity name, fiscal period, and currency from ETL output where available."""
    sec4 = extracted.get(4, {}) if isinstance(extracted.get(4), dict) else {}
    borrower = sec4.get("4A_borrower", {}) or {}
    financials = sec4.get("4E_financials", {}) or {}

    # Entity: prefer English name, fall back to Chinese
    entity = (
        (borrower.get("company_name_en") or "").strip()
        or (borrower.get("company_name_zh") or "").strip()
        or default_entity
    )

    # Period: prefer 4E_financials.fiscal_year, then 4A_borrower.fiscal_year_end
    raw_period = (
        str(financials.get("fiscal_year") or "").strip()
        or str(borrower.get("fiscal_year_end") or "").strip()
    )
    if raw_period and raw_period != "None":
        # Normalise to "FY2024" form if it's a bare 4-digit year
        period = f"FY{raw_period}" if raw_period.isdigit() and len(raw_period) == 4 else raw_period
    else:
        period = default_period

    # Currency: prefer 4E_financials.currency
    raw_ccy = str(financials.get("currency") or "").strip().upper()
    currency = raw_ccy if raw_ccy and len(raw_ccy) == 3 else default_currency

    return entity, period, currency


def build_canonical_facts_from_etl(
    report_id: str,
    doc_id: str,
    extracted: dict[int, dict],
    entity: str = "",
    period: str = "",
    currency: str = "USD",
) -> list[dict]:
    """
    Convert an ETL extraction result into a list of CanonicalFact dicts for upsert.

    Entity name and fiscal period are extracted dynamically from the document when
    available (§4A company_name_en, §4E fiscal_year); defaults apply as fallback.
    Only maps well-known scalar numeric fields from _ETL_FACT_MAP.
    Returns an empty list if nothing mappable is found.
    """
    # Resolve entity/period/currency from document content
    resolved_entity, resolved_period, resolved_currency = _extract_entity_period_currency(
        extracted,
        default_entity=entity or "borrower",
        default_period=period or "FY2024",
        default_currency=currency or "USD",
    )

    facts: list[dict] = []
    seen: set[str] = set()  # deduplicate by (metric_name, entity, period)

    # §7 uses dynamic FY_YYYY nesting — handled by dedicated extractor
    sec7 = extracted.get(7)
    if isinstance(sec7, dict):
        facts.extend(_extract_section7_facts(
            sec7=sec7,
            report_id=report_id,
            doc_id=doc_id,
            default_entity=resolved_entity,
            default_currency=resolved_currency,
            seen=seen,
        ))

    for sec_no, sub_key, field, metric_name, unit in _ETL_FACT_MAP:
        sec_data = extracted.get(sec_no)
        if not isinstance(sec_data, dict):
            continue
        sub = sec_data.get(sub_key)
        if not isinstance(sub, dict):
            continue
        raw_val = _get_nested(sub, field)
        if raw_val is None:
            continue

        # For string values, try to extract embedded currency/unit
        if isinstance(raw_val, str):
            num_val, parsed_currency, parsed_unit = parse_financial_value(raw_val)
            fact_currency = parsed_currency or resolved_currency
            fact_unit = parsed_unit or unit
        else:
            num_val = _try_float(raw_val)
            fact_currency = resolved_currency
            fact_unit = unit

        if num_val is None:
            continue

        dedup_key = f"{metric_name}|{resolved_entity}|{resolved_period}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        facts.append({
            "report_id": report_id,
            "metric_name": metric_name,
            "entity": resolved_entity,
            "period": resolved_period,
            "value": num_val,
            "value_text": str(raw_val),
            "currency": fact_currency,
            "unit": fact_unit or "",
            "source_type": "pdf_extraction",
            "source_priority": 3,
            "source_evidence_id": doc_id,
            "source_section_no": sec_no,
            "state": "extracted",
        })

    logger.info(
        "[ETL] build_canonical_facts_from_etl: report=%s doc=%s facts_found=%d",
        report_id, doc_id, len(facts),
    )
    return facts

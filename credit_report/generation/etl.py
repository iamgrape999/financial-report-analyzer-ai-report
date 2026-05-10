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
from typing import Optional

logger = logging.getLogger(__name__)

# Which sections are relevant for each document type
DOCUMENT_SECTION_MAP: dict[str, list[int]] = {
    "annual_report":         [4, 7, 3, 2, 10],
    "financial_statement":   [7, 4, 2, 10],
    "analyst_presentation":  [4, 7, 2, 3, 10],
    "interim_report":        [7, 4, 2],
    "valuation_report":      [5, 10],
    "charter_agreement":     [1, 6],
    "shipbuilding_contract": [6, 1],
    "kyc_document":          [9],
    "legal_document":        [8, 1],
    "external_report":       [3, 4, 7],
    "other":                 [4, 7],
}

ETL_SYSTEM_PROMPT = """\
You are a specialized data extraction AI for maritime / corporate credit reports at an international commercial bank.

Your task: read the provided document excerpt and extract structured JSON data for specific credit report sections.

Rules:
- Extract ONLY what is explicitly stated in the document — never fabricate or guess
- Use null for any field not found in the document
- Financial figures: use USD millions unless the document states otherwise
- Dates: YYYY-MM-DD format or YYYY-QN (e.g. 2026-Q2)
- Arrays: use [] when empty; include all items found
- Return ONLY a valid JSON object. No markdown, no commentary, no code fences.
- Structure the JSON with integer section numbers as keys (e.g. "4", "7")
- Each section key maps to a flat or nested object matching the schema described
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

    4: """Section 4 — Corporate Background:
{company_name_en, company_name_zh, legal_entity_type, registration_number, ubn,
incorporation_date, listing_date, stock_exchange, incorporation_country,
ultimate_beneficial_owner, chairman, general_manager, finance_manager,
shareholders[{name, stake_percent, country}],
key_management[{name, title, years_experience, background}],
business_description, primary_business, years_in_operation,
global_ranking, teu_capacity, market_share_pct,
fleet_owned[{vessel, type, dwt, teu, year_built, flag, charter}],
total_fleet_teu, major_customers[{name, contract_type, duration_years}],
annual_revenue, currency, reporting_entity,
ebitda, market_position, group_auditor,
banking_relationships[{bank, product}],
peer_comparison[{company, fleet_teu, market_share_pct}]}""",

    5: """Section 5 — Collateral / Support:
{collateral_type, refund_guarantee{issuer, issuer_rating, amount_usd_m,
  coverage_pct_max_exposure, coverage_pct_first_milestones, expiry, legal_structure},
vessel_valuations[{vessel, dwt, teu, year_built, market_value_usd_m, distressed_value_usd_m,
  valuation_date, valuer, valuation_basis}],
contract_price_usd_m, loan_amount_usd_m,
ltc_percent, acr_percent, ltv_at_maturity_percent, balloon_usd_m, balloon_ltv_pct,
value_maintenance_clause{acr_threshold_pct, ltv_threshold_pct, test_frequency_years,
  cure_period_banking_days, remedy_options[]},
insurance{h_and_m, p_and_i, war_risk},
guarantor_support{market_cap_usd_m, total_fleet_teu, guarantee_scope, support_history},
responsible_person_guarantee (bool),
collateral_adequacy_conclusion}""",

    6: """Section 6 — Project Analysis:
{vessel_name, hull_number, vessel_type, vessel_class, dwt, teu, grt, loa_m, beam_m,
main_engine, speed_knots, eco_design (bool), imo_tier,
shipyard, shipyard_country, shipyard_docks, shipyard_berth_m, shipyard_capacity_dwt,
shipyard_annual_cgt, shipyard_ontime_delivery_pct, shipyard_rating,
class_society, flag_state, contract_date, contract_price_usd_m,
payment_milestones[{milestone, status, date, pct, amount_usd_m, cub_drawdown_usd_m}],
delivery_date, grace_period_days,
construction_progress_vessels_delivered, construction_progress_value_pct,
construction_risk_assessment,
charterer, charterer_credit_rating,
charter_rate_usd_day, charter_duration_years, charter_type,
project_risk_ratings[{risk_category, rating, comments}]}""",

    7: """Section 7 — Financial Analysis:
{reporting_currency, unit, reporting_entity, auditor, audit_opinion,
accounting_standard, fiscal_year_end,
income_statement{FY_YYYY{revenue, opex, gross_profit, ebitda, depreciation, ebit,
  interest_expense, pbt, tax, net_income}},
balance_sheet{FY_YYYY{cash, trade_receivables, current_assets, pp_e, total_assets,
  short_term_debt, trade_payables, current_liabilities, long_term_debt, total_debt, equity}},
cash_flow{FY_YYYY{cfo, capex, cfi, cff, net_change}},
key_ratios{FY_YYYY{dscr, debt_ebitda, tangible_leverage, current_ratio,
  net_margin_pct, roa_pct, roe_pct, ebitda_interest_cover}},
industry_index{ccfi_level, scfi_level, year},
facility_dscr_projection{FY_YYYY{revenue, opex, ebitda, debt_service, dscr}},
fx_exposure, off_balance_sheet, accounting_notes}""",

    8: """Section 8 — Changes in Engaged Banks / ACRA Charges:
{acra_search{entity_name, uen, search_date, total_charges},
charges[{charge_no, chargee, charge_date, amount, property_charged, status}],
engaged_banks[{bank, facility_type, committed_usd_m, outstanding_usd_m, since_year}],
banking_pattern_assessment,
credit_exposure_concentration,
new_facility_impact}""",

    9: """Section 9 — Credit Analysis Checklist:
{checklist[{category, item, status, remarks}],
formal_recommendation,
approval_authority,
conditions_precedent[{cp_no, description, status}],
covenants[{type, description, threshold, frequency}],
acr_covenant{threshold_pct, test_frequency, cure_period_days},
listing_requirement, insurance_requirement,
negative_pledge (bool), change_of_control_clause (bool),
information_undertakings[],
signoff_date, signoff_officer}""",

    10: """Section 10 — Appendix:
{group_exposure_table[{entity, facility_type, limit_usd_m, outstanding_usd_m,
  msr_rating, collateral, expiry}],
fleet_growth_targets[{year, owned_teu, managed_teu, total_teu}],
dscr_projections_base[{year, period, revenue, opex, ebitda, depreciation,
  interest, principal, debt_service, dscr, outstanding_balance}],
dscr_projections_worse[{year, period, revenue, opex, ebitda, depreciation,
  interest, principal, debt_service, dscr, outstanding_balance}],
sensitivity_analysis[{scenario, charter_rate_usd_day, min_dscr, conclusion}],
loan_repayment_schedule[{period, principal, interest, total, balance}],
blocking_data_gaps[{section, field, gap_description, data_source_needed}],
market_overview, references[]}""",
}


def _build_etl_prompt(document_type: str, text: str, section_nos: list[int]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for Gemini ETL extraction."""
    schema_parts = "\n\n".join(
        SECTION_EXTRACTION_SCHEMA[n] for n in section_nos if n in SECTION_EXTRACTION_SCHEMA
    )
    doc_type_label = document_type.replace("_", " ").title()
    user_prompt = (
        f"Document type: {doc_type_label}\n\n"
        f"Target sections to extract: {section_nos}\n\n"
        f"Required JSON schema (extract these fields if present):\n{schema_parts}\n\n"
        f"---DOCUMENT TEXT START---\n{text[:28000]}\n---DOCUMENT TEXT END---\n\n"
        "Return ONLY valid JSON with section numbers (as strings) as keys. "
        "Example: {\"4\": {\"company_name\": \"...\", ...}, \"7\": {\"income_statement\": {...}}}"
    )
    return ETL_SYSTEM_PROMPT, user_prompt


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
    target_sections = section_nos or DOCUMENT_SECTION_MAP.get(document_type, [4, 7])
    if not target_sections:
        return {}

    text = text.strip()
    if not text:
        logger.warning("etl_document: empty document text, skipping")
        return {}

    import anthropic
    from credit_report.config import ANTHROPIC_API_KEY, CREDIT_REPORT_MODEL

    system_prompt, user_prompt = _build_etl_prompt(document_type, text, target_sections)

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=CREDIT_REPORT_MODEL,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = (response.content[0].text or "").strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[: raw.rfind("```")]

        parsed = json.loads(raw)
        result: dict[int, dict] = {}
        for k, v in parsed.items():
            try:
                sec_no = int(k)
                if isinstance(v, dict) and v:
                    # Remove null-only sections
                    non_null = {fk: fv for fk, fv in v.items() if fv is not None}
                    if non_null:
                        result[sec_no] = non_null
            except (ValueError, TypeError):
                continue

        logger.info("etl_document: extracted sections=%s doc_type=%s fields=%s",
                    list(result.keys()), document_type,
                    {k: len(v) for k, v in result.items()})
        return result

    except json.JSONDecodeError as e:
        logger.error("etl_document: JSON parse error: %s — raw=%r", e, raw[:500] if 'raw' in dir() else "")
        return {}
    except Exception as exc:
        logger.exception("etl_document: extraction failed doc_type=%s: %s", document_type, exc)
        return {}

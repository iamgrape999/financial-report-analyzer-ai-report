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
    1: """Section 1 — Facility Structure:
{borrower, guarantors[], facility_type, facility_amount_usd_m, tenor_years, availability_period_months,
purpose, repayment_schedule, bullet_at_maturity_usd_m, interest_rate_basis, margin_bps,
commitment_fee_bps, arrangement_fee_bps, collateral[], financial_covenants[], governing_law,
agent_bank, lenders[{bank, commitment_usd_m}]}""",

    2: """Section 2 — Overall Comments:
{credit_decision (APPROVE/DECLINE/CONDITIONAL), recommendation_rationale,
key_strengths[], key_concerns[], conditions_precedent[], dscr_avg, ltc_percent}""",

    3: """Section 3 — Credit Risk:
{internal_rating, rating_rationale, pd_basis_points, pd_assessment, lgd_percent,
mas_612_applicable (bool), mas_612_classification, industry_risk_level,
esg_category (A/B/C), esg_screening, climate_risk, sanctions_ofac, sanctions_eu,
sanctions_mas, sanctions_un, key_risks[], mitigants[], country_risk}""",

    4: """Section 4 — Borrower Background:
{company_name, legal_entity_type, registration_number, incorporation_date,
incorporation_country, ultimate_beneficial_owner, shareholders[{name, stake_percent, country}],
key_management[{name, title, years_experience, background}],
business_description, primary_business, years_in_operation,
fleet_owned[{vessel, type, dwt, year_built, flag, charter}],
total_fleet_dwt, major_customers[{name, contract_type, duration_years, rate_usd_day}],
annual_revenue_usd_m, ebitda_usd_m, market_position, group_auditor, banking_relationships[{bank, product}]}""",

    5: """Section 5 — Collateral:
{collateral_type, vessel_valuations[{vessel, dwt, year_built, market_value_usd_m, distressed_value_usd_m,
valuation_date, valuer, valuation_basis}], contract_price_usd_m, loan_amount_usd_m,
ltc_percent, acr_percent, ltv_at_maturity_percent,
refund_guarantee{issuer, issuer_rating, amount_usd_m, expiry, covers},
insurance{h_and_m, p_and_i, war_risk},
additional_security[], collateral_adequacy_conclusion}""",

    6: """Section 6 — Project / Ship Finance:
{vessel_name, hull_number, vessel_type, vessel_class, dwt, grt, loa_m, beam_m,
main_engine, speed_knots, eco_design (bool), imo_tier,
shipyard, shipyard_country, shipyard_rating, class_society, flag_state,
contract_date, contract_price_usd_m,
payment_milestones[{milestone, date, pct, amount_usd_m}],
delivery_date, construction_supervisor, construction_risk_assessment,
employment_post_delivery, charterer, charterer_credit_rating,
charter_rate_usd_day, charter_duration_years, charter_type}""",

    7: """Section 7 — Financial Analysis:
{reporting_currency, unit, auditor, audit_opinion, accounting_standard, fiscal_year_end,
income_statement{FY_YYYY{revenue, opex, gross_profit, ebitda, depreciation, ebit,
  interest_expense, pbt, tax, net_income}},
balance_sheet{FY_YYYY{cash, trade_receivables, current_assets, pp_e, total_assets,
  short_term_debt, trade_payables, current_liabilities, long_term_debt, total_debt, equity}},
cash_flow{FY_YYYY{cfo, capex, cfi, cff, net_change}},
key_ratios{FY_YYYY{dscr, debt_ebitda, debt_equity, current_ratio, net_margin_pct,
  roa_pct, roe_pct, interest_cover}},
facility_dscr_projection{FY_YYYY{revenue, opex, ebitda, debt_service, dscr}},
fx_exposure, off_balance_sheet, accounting_notes}""",

    8: """Section 8 — Legal Documentation:
{facility_agreement{type, date, parties[], amount_usd_m},
security_documents[{doc, vessel, amount_usd_m, status}],
existing_charges[{charge, beneficiary, amount_usd_m, maturity}],
cross_default_threshold_usd_m, pari_passu_clause (bool), negative_pledge (bool),
legal_opinions[{jurisdiction, law_firm, date}],
legal_counsel_borrower, legal_counsel_bank, governing_law, dispute_resolution,
conditions_precedent[], conditions_subsequent[]}""",

    9: """Section 9 — Compliance Checklist:
{kyc{completed (bool), completion_date, review_date, kyc_tier, cdd_level, documents_received[]},
aml{cleared (bool), screening_date, adverse_media},
sanctions{ofac, eu, mas, un, hm_treasury, screening_date, screening_system},
pep{status, related_pep (bool), close_associate_pep (bool)},
tax_compliance{country, fatca_classification, crs_status},
environmental{eexi, cii, cii_year, poseidon_principles, imo_2030_readiness},
mas_regulations{banking_act_s33_3, total_exposure_usd_m, concentration_pct, single_borrower_limit_pct},
internal_approvals_required[], regulatory_approvals, watch_list, country_risk_approval}""",

    10: """Section 10 — Appendix:
{dscr_projections[{year, period, revenue_usd_m, opex_usd_m, ebitda_usd_m,
  depreciation_usd_m, interest_usd_m, principal_usd_m, debt_service_usd_m, dscr, outstanding_balance_usd_m}],
fleet_schedule[{vessel, type, dwt, year_built, flag, class, current_charter, market_value_usd_m,
  existing_mortgage}],
sensitivity_analysis[{scenario, charter_rate_usd_day, min_dscr, conclusion}],
loan_repayment_schedule[{period, principal, interest, total, balance}],
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

    from google import genai
    from google.genai import types as genai_types
    from credit_report.config import GEMINI_API_KEY, GEMINI_MODEL

    system_prompt, user_prompt = _build_etl_prompt(document_type, text, target_sections)

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=8192,
                temperature=0.1,
            ),
        )
        raw = (response.text or "").strip()

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

"""
Section completeness validation and auto-fill for mandatory sub-sections.

Why this module exists:
- §2 requires exactly 5 tables (T1-T5). If the AI truncates at T1 or T2,
  the analyst sees an incomplete report with no indication of what is missing.
- §1 requires up to 7 sub-sections (Facility Table, Regulatory, Purpose, T&Cs,
  Deal Comparison, Account Strategy, etc.) whose presence depends on report_type
  and which input keys are populated.
- §3 requires exactly 4 sub-sections (External Ratings, Internal Ratings / MSR
  Table, MAS 612 Loan Grading, ESG Rating) — all unconditional. With an 8 192-
  token default budget, sections with multiple override entities and long Remarks
  can truncate before MAS 612 or ESG are emitted.
- §5 sub-sections are conditional on facility type (secured/unsecured), whether
  a refund guarantee exists (pre-delivery only), and whether a corporate guarantor
  is present. C-7 and C-8 (Responsible Person Guarantee + Adequacy Conclusion)
  are unconditional and most likely to be truncated.
- §6 applies only to shipbuilding/pre-delivery facilities. When not applicable the
  AI emits a single sentence; when applicable the 8 bold-header sub-sections
  (Project Overview → Project Economics) are all required except C-5 RG Mechanism
  (only when 6E_rg_mechanism.applicable) and C-7 Force Majeure (only when
  6G_force_majeure data is provided). Truncation risk is highest at C-6 and C-7.
- §7 is the quantitative backbone of the report. C-1 (Borrower Historical
  Financials — P&L + BS + CF) and C-2 (Borrower Summary Statistics — ≥18 ratios)
  are always mandatory. C-3/C-4 (Guarantor Financials/Stats) are conditional on
  guarantor_exists or 7C data. C-5/C-6 (Base Case + Worse Case Projections) are
  conditional on 7E_base_case.applicable. C-7 (Lessee Financials) is conditional
  on 7G_lessee_financials data. C-8 (Sensitivity Analysis) is conditional on
  projections or 7H_sensitivity data. With a 16 384-token primary budget,
  truncation risk is highest at C-6, C-7, and C-8 (end of a dense section).
- With 16 384-token budgets, §1/§2 usually fit — but edge cases still occur.
- This module detects gaps and issues a targeted fill call for only the missing
  sub-sections, without re-running the expensive full generation.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# §4 — Nine unconditional sub-sections (C-1 to C-9) + Banking Relationships
# QA gates G-3 through G-7 confirm all items are mandatory for every report.
# Truncation risk is highest at C-8, C-9 and Banking Relationships (end of
# section), which is why the prompt budget is already raised to 12 288.
# ─────────────────────────────────────────────────────────────────────────────

_S4_REQUIRED: list[tuple[str, str]] = [
    ("**C-1.",            "C-1 Corporate Identity"),
    ("**C-2.",            "C-2 Ownership & Group Structure"),
    ("**C-3.",            "C-3 Key Management"),
    ("**C-4.",            "C-4 Business Overview"),
    ("**C-5.",            "C-5 Revenue & Financial Highlights"),
    ("**C-6.",            "C-6 Fleet Profile"),
    ("**C-7.",            "C-7 Debt Profile"),
    ("**C-8.",            "C-8 Market Analysis"),
    ("**C-9.",            "C-9 Peer Comparison"),
    ("banking relationships", "Banking Relationships Table (Section E)"),
]


# ─────────────────────────────────────────────────────────────────────────────
# §3 — Four unconditional sub-sections (prompt Section I)
# "§3 is NOT complete until: External Ratings + MSR Table (with Remarks) +
#  MAS 612 (all paragraphs) + ESG are ALL present."
#
# Special case: the MSR table header (**Internal ratings:**) can be present but
# the table body (entity data rows) can be missing — the AI generates only the
# table column headers and separator row without any entity rows. This produces
# a rendered table with a header but no data, causing the "weird format" where
# the table header appears but no rows follow it.
# Detection: after finding **Internal ratings:** in the markdown, extract the
# block up to the next bold section header and count non-separator pipe rows.
# Fewer than 2 non-separator pipe rows (header row only) → MSR table is empty.
# ─────────────────────────────────────────────────────────────────────────────

_S3_REQUIRED: list[tuple[str, str]] = [
    ("**External ratings:**",       "External Ratings"),
    ("**MAS 612 Loan Grading:**",   "MAS 612 Loan Grading (4 paragraphs)"),
    ("**ESG ratings:**",            "ESG Rating"),
]


def _check_section3(markdown: str) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for §3 sub-sections absent or incomplete.

    Extends the simple marker check with MSR table body validation:
    if **Internal ratings:** is present but the table block contains fewer than
    2 non-separator pipe rows (i.e. only the column header row was generated),
    the MSR table is treated as missing and flagged for a fill call.
    """
    md_lower = markdown.lower()
    missing: list[tuple[str, str]] = []

    # ① External Ratings
    if "**external ratings:**" not in md_lower:
        missing.append(("**External ratings:**", "External Ratings"))

    # ② Internal Ratings — header presence + non-empty table body
    has_ratings_header = "**internal ratings:**" in md_lower
    if not has_ratings_header:
        missing.append(("**Internal ratings:**", "Internal Ratings (MSR Table with entity rows)"))
    else:
        # Extract the block between "**internal ratings:**" and the next bold section
        start = md_lower.find("**internal ratings:**")
        # Next bold marker could be MAS 612 or ESG
        next_section = len(markdown)
        for next_marker in ("**mas 612", "**esg ratings"):
            pos = md_lower.find(next_marker, start + 1)
            if pos != -1 and pos < next_section:
                next_section = pos
        ratings_block = markdown[start:next_section]
        # Count pipe rows that are NOT the separator row (lines with |---|)
        non_sep_rows = [
            line for line in ratings_block.splitlines()
            if line.strip().startswith("|") and "---" not in line
        ]
        # A populated MSR table has at minimum: column header row + sub-header row + ≥1 entity row
        # Requiring ≥2 non-separator rows catches the "header only" truncation case
        if len(non_sep_rows) < 2:
            missing.append(("**Internal ratings:**", "Internal Ratings (MSR Table — entity data rows missing)"))

    # ③ MAS 612 Loan Grading
    if "**mas 612 loan grading:**" not in md_lower:
        missing.append(("**MAS 612 Loan Grading:**", "MAS 612 Loan Grading (4 paragraphs)"))

    # ④ ESG Rating
    if "**esg ratings:**" not in md_lower:
        missing.append(("**ESG ratings:**", "ESG Rating"))

    return missing


def replace_empty_msr_block(markdown: str, fill_text: str) -> str | None:
    """
    Inline-replace a broken **Internal ratings:** block with *fill_text* at the
    same position in the §3 markdown.

    Returns the corrected markdown when a broken block (< 2 non-separator pipe
    rows) is found and replaced, or None when no replacement is needed (block
    absent or already populated — caller falls back to normal append).

    Why inline replacement instead of strip+append:
    The §3 output order is External → Internal → MAS 612 → ESG. Stripping the
    block and appending the fill at the end would place Internal Ratings after
    MAS 612/ESG, breaking the expected section order.
    """
    md_lower = markdown.lower()
    start = md_lower.find("**internal ratings:**")
    if start == -1:
        return None

    # Locate end of the broken block (start of the next bold section)
    next_section = len(markdown)
    for next_marker in ("**mas 612", "**esg ratings"):
        pos = md_lower.find(next_marker, start + 1)
        if pos != -1 and pos < next_section:
            next_section = pos

    block = markdown[start:next_section]
    non_sep_rows = [
        line for line in block.splitlines()
        if line.strip().startswith("|") and "---" not in line
    ]

    if len(non_sep_rows) >= 2:
        return None  # Table already has data rows — no replacement needed

    # Replace broken block with the filled content, preserving surrounding text
    before = markdown[:start].rstrip()
    after = markdown[next_section:].lstrip("\n")
    separator = "\n\n"
    return before + separator + fill_text.strip() + (separator + after if after else "")


# ─────────────────────────────────────────────────────────────────────────────
# §2 — Five unconditional two-column tables
# ─────────────────────────────────────────────────────────────────────────────

_S2_REQUIRED: list[tuple[str, str]] = [
    ("**Credit Overview**",                      "T1 Credit Overview"),
    ("**Solvency**",                             "T2 Solvency"),
    ("**The Guarantor and their Supportive",     "T3 Guarantor and Supportive Performance"),
    ("**Collateral Summary**",                   "T4 Collateral Summary"),
    ("**Risk and Mitigants**",                   "T5 Risk and Mitigants"),
]


# ─────────────────────────────────────────────────────────────────────────────
# §1 — Conditional sub-sections (depend on report_type + input keys)
# ─────────────────────────────────────────────────────────────────────────────

def _check_section1(markdown: str, input_json: dict) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for sub-sections that are absent from the
    §1 markdown but are expected given the supplied input_json.

    Logic mirrors the prompt's Section D (Conditional Logic) and Rule 3:
      new_deal        → Facility Table + Regulatory + Purpose + T&Cs +
                        Deal Comparison + Account Strategy
      annual_review   → Facility Table + Regulatory + Purpose (brief) +
                        Account Strategy
      new_deal_and_annual_review → all of the above
    """
    md_lower = markdown.lower()
    missing: list[tuple[str, str]] = []

    report_type: str = str(
        (input_json.get("metadata") or {}).get("report_type", "new_deal")
    ).lower()
    is_new_deal = "new_deal" in report_type  # covers new_deal and new_deal_and_annual_review

    # ① Facility Table — always required (most reliable marker: 11-column header)
    has_facility_table = (
        "proposed facility" in md_lower
        or "outstanding (as at" in md_lower
        or "outstanding as at" in md_lower
    )
    if not has_facility_table:
        missing.append(("Proposed Facility", "Facility Summary Table (11 columns)"))

    # ② Regulatory Compliance / Banking Act 33-3
    #    Only expected when the input provides banking_act_33_3 data.
    reg = input_json.get("regulatory_compliance") or {}
    if reg.get("banking_act_33_3"):
        if "33-3" not in markdown and "banking act" not in md_lower:
            missing.append(("33-3", "Regulatory Compliance (Banking Act 33-3)"))

    # ③ Unsecured Exposure table
    #    Only expected when unsecured_exposure_table is provided in input.
    if reg.get("unsecured_exposure_table"):
        if "unsecured exposure" not in md_lower:
            missing.append(("Unsecured Exposure", "Unsecured Exposure Table"))

    # ④ Purpose of Report — required when purpose_and_recommendation is provided.
    purp = input_json.get("purpose_and_recommendation") or {}
    if purp.get("purpose_text") or purp.get("vessel_specs"):
        purpose_present = (
            "purpose of report" in md_lower
            or "purpose:" in md_lower
            or "the purpose" in md_lower
        )
        if not purpose_present:
            missing.append(("Purpose of Report", "Purpose of Report"))

    # ⑤ Terms & Conditions (21 fields) — new_deal only, when tc_rows provided.
    tc = input_json.get("terms_and_conditions") or {}
    if is_new_deal and (tc.get("tc_rows") or tc.get("deal_comparison_rows")):
        tc_present = any(
            m in md_lower for m in [
                "value maintenance", "conditions precedent", "upfront fee",
                "interest period", "mandatory prepayment", "drawdown",
            ]
        )
        if not tc_present:
            missing.append(("Value Maintenance", "Terms & Conditions Table (21 fields)"))

    # ⑥ Deal Comparison — new_deal only, when deal_comparison_rows provided.
    if is_new_deal and tc.get("deal_comparison_rows"):
        if "deal comparison" not in md_lower:
            missing.append(("Deal Comparison", "Deal Comparison Table (≥11 columns)"))

    # ⑦ Account Strategy — always required when account_strategy data is provided.
    acct = input_json.get("account_strategy") or {}
    if acct:
        acct_present = (
            "account strategy" in md_lower
            or "wallet overview" in md_lower
            or "immediate opportunities" in md_lower
        )
        if not acct_present:
            missing.append(("Account Strategy", "Account Strategy (5 sub-sections)"))

    return missing


# ─────────────────────────────────────────────────────────────────────────────
# §6 — Conditional sub-sections (only for shipbuilding / pre-delivery facilities)
#
# Detection uses bold topic headers that the prompt mandates (NO C-N. prefix
# in §6 output — prompt says "Bold topic headers only", no sub-numbering).
# Early-exit when "not applicable" appears in markdown OR when 6A_project has
# no vessel data (hull_number / teu / contract_price all null).
# QA gates F-1..F-9 confirm all 8 sub-sections are required when applicable.
# ─────────────────────────────────────────────────────────────────────────────

def _check_section6(markdown: str, input_json: dict) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for §6 sub-sections absent from *markdown*.

    Returns empty list immediately when:
    - The markdown contains "not applicable" (AI correctly reported N/A).
    - The input_json carries no meaningful project data (no hull/TEU/price).
    """
    md_lower = markdown.lower()

    # If AI declared the section not applicable, nothing is missing by design
    if "not applicable" in md_lower:
        return []

    sec6a = input_json.get("6A_project") or {}
    sec6e = input_json.get("6E_rg_mechanism") or {}
    sec6g = input_json.get("6G_force_majeure") or {}

    # If no project data supplied, the section is not applicable — skip check
    has_project_data = bool(
        sec6a.get("hull_number")
        or sec6a.get("teu")
        or sec6a.get("contract_price_usd_m")
    )
    if not has_project_data:
        return []

    missing: list[tuple[str, str]] = []

    # C-1 Project Overview — always required when applicable
    if "**project overview" not in md_lower:
        missing.append(("**Project Overview**", "C-1 Project Overview"))

    # C-2 Builder Assessment — always required when applicable
    if "**builder assessment" not in md_lower:
        missing.append(("**Builder Assessment**", "C-2 Builder Assessment"))

    # C-3 Contract Structure — always required when applicable
    if "**contract structure" not in md_lower:
        missing.append(("**Contract Structure**", "C-3 Contract Structure"))

    # C-4 Payment & Delivery Schedule — always required when applicable
    if "**payment" not in md_lower:
        missing.append(("**Payment & Delivery Schedule**", "C-4 Payment & Delivery Schedule"))

    # C-5 RG Mechanism — only when refund guarantee data is provided
    rg_applicable = bool(sec6e.get("applicable") or sec6e.get("issuer_full_name"))
    if rg_applicable:
        if "**rg mechanism" not in md_lower:
            missing.append(("**RG Mechanism**", "C-5 RG Mechanism"))

    # C-6 Construction Progress & Risk — always required when applicable
    if "**construction progress" not in md_lower:
        missing.append(("**Construction Progress", "C-6 Construction Progress & Risk"))

    # C-7 Force Majeure — only when force majeure data is provided
    fm_applicable = bool(
        sec6g.get("applicable")
        or sec6g.get("covered_events")
        or sec6g.get("historical_context_verbatim")
    )
    if fm_applicable:
        if "**force majeure" not in md_lower:
            missing.append(("**Force Majeure**", "C-7 Force Majeure"))

    # C-8 Project Economics — always present (one-sentence cross-reference)
    if "**project economics" not in md_lower:
        missing.append(("**Project Economics**", "C-8 Project Economics"))

    return missing


# ─────────────────────────────────────────────────────────────────────────────
# §5 — Conditional sub-sections (depend on security type + input keys)
#
# QA gate F-2: if unsecured, C-2 through C-5 are absent by design.
# QA gate F-7: C-8 Collateral Adequacy Conclusion is always required.
# C-7 Responsible Person Guarantee is always required (even if "none").
# Truncation risk is highest at C-6, C-7, C-8 (end of a verbose section).
# ─────────────────────────────────────────────────────────────────────────────

def _check_section5(markdown: str, input_json: dict) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for §5 sub-sections absent from *markdown*
    but expected given the supplied input_json.

    Detection strategy: bold sub-header prefix **C-N. (lowercase match) is the
    most reliable marker because the prompt mandates these exact labels.
    """
    md_lower = markdown.lower()
    missing: list[tuple[str, str]] = []

    sec5a = input_json.get("5A_security_overview") or {}
    sec5b = input_json.get("5B_refund_guarantee") or {}
    sec5c = input_json.get("5C_vessel_mortgage") or {}
    sec5d = input_json.get("5D_insurance") or []
    sec5e = input_json.get("5E_value_maintenance_clause") or {}
    sec5f = input_json.get("5F_corporate_guarantee") or {}

    is_secured: bool | None = sec5a.get("is_secured")
    # Treat as secured when uncertain but mortgage data is present
    has_mortgage_data = bool(sec5c.get("applicable") or sec5c.get("vessel_valuations"))
    secured = is_secured or (is_secured is None and has_mortgage_data)

    # ① C-0 Security Package Overview — always required
    if "**c-0." not in md_lower and "security package" not in md_lower:
        missing.append(("**C-0.", "C-0 Security Package Overview"))

    # ② C-1 Pre-Delivery Security (Refund Guarantee) — only when RG data provided
    rg_applicable = bool(
        sec5b.get("applicable")
        or sec5b.get("issuer_full_name")
        or sec5b.get("milestones")
    )
    if rg_applicable:
        if "**c-1." not in md_lower and "refund guarantee" not in md_lower:
            missing.append(("**C-1.", "C-1 Pre-Delivery Security — Refund Guarantee"))

    # ③–⑥ C-2 through C-5 — only when secured
    if secured:
        # C-2 Post-Delivery Security — First Priority Mortgage
        if "**c-2." not in md_lower and "first priority mortgage" not in md_lower:
            missing.append(("**C-2.", "C-2 Post-Delivery Security — First Priority Mortgage"))

        # C-3 Amortisation Profile — when amortisation schedule data exists
        has_amort = bool(sec5c.get("amortisation_schedule") or sec5c.get("loan_amount_usd_m"))
        if has_amort:
            if "**c-3." not in md_lower and "amortisation" not in md_lower and "repayment schedule" not in md_lower:
                missing.append(("**C-3.", "C-3 Amortisation Profile (Loan Repayment Schedule)"))

        # C-4 Insurance — when insurance data provided
        if sec5d:
            if "**c-4." not in md_lower:
                missing.append(("**C-4.", "C-4 Insurance"))

        # C-5 Value Maintenance Clause — when VMC data provided
        has_vmc = bool(sec5e.get("acr_covenant_pct") or sec5e.get("cure_mechanism_verbatim") or sec5e.get("ltv_covenant_pct"))
        if has_vmc:
            if "**c-5." not in md_lower and "value maintenance" not in md_lower:
                missing.append(("**C-5.", "C-5 Value Maintenance Clause"))

    # ⑦ C-6 Corporate Guarantee — only when guarantor data provided
    guarantor_applicable = bool(sec5f.get("applicable") or sec5f.get("guarantor_full_name"))
    if guarantor_applicable:
        if "**c-6." not in md_lower and "corporate guarantee" not in md_lower:
            missing.append(("**C-6.", "C-6 Corporate Guarantee & Guarantor Financial Capacity"))

    # ⑧ C-7 Responsible Person Guarantee — always required (even if "none")
    if "**c-7." not in md_lower and "responsible person guarantee" not in md_lower:
        missing.append(("**C-7.", "C-7 Responsible Person Guarantee"))

    # ⑨ C-8 Collateral Adequacy Conclusion — always required (QA F-7)
    if "**c-8." not in md_lower and "collateral adequacy" not in md_lower:
        missing.append(("**C-8.", "C-8 Collateral Adequacy Conclusion"))

    return missing


# ─────────────────────────────────────────────────────────────────────────────
# §7 — Financial Analysis: 2 unconditional + 6 conditional sub-sections
#
# Detection uses **C-N. prefix (same convention as §4/§5) with unique phrase
# fallbacks. Conditionality mirrors the prompt trigger logic:
#   C-3/C-4: entities_to_analyze[].guarantor_exists or 7C_guarantor_financials.applicable
#   C-5/C-6: 7E_base_case.applicable or meaningful projection data
#   C-7:     7G_lessee_financials.applicable or non-empty lessees list
#   C-8:     same as C-5 or 7H_sensitivity.applicable
# Truncation risk is highest at C-6/C-7/C-8 (end of a dense, multi-table section).
# ─────────────────────────────────────────────────────────────────────────────

def _check_section7(markdown: str, input_json: dict) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for §7 sub-sections absent from *markdown*
    but expected given the supplied input_json.

    C-1 and C-2 are unconditionally mandatory.
    C-3 through C-8 are conditional on the corresponding input keys.
    """
    md_lower = markdown.lower()
    missing: list[tuple[str, str]] = []

    # ① C-1 Borrower Historical Financials — MANDATORY (P&L + BS + CF)
    if "**c-1." not in md_lower and "borrower historical financials" not in md_lower:
        missing.append(("**C-1.", "C-1 Borrower Historical Financials (P&L + BS + CF tables)"))

    # ② C-2 Borrower Summary Statistics — MANDATORY (≥18 ratio rows)
    if "**c-2." not in md_lower and "summary statistics" not in md_lower:
        missing.append(("**C-2.", "C-2 Borrower Summary Statistics (≥18 ratio rows)"))

    # Determine conditionality for C-3/C-4 from entities list or 7C data
    entities = input_json.get("entities_to_analyze") or []
    if isinstance(entities, dict):
        entities = [entities]
    guarantor_7c = input_json.get("7C_guarantor_financials") or {}
    guarantor_exists = (
        any(bool(e.get("guarantor_exists")) for e in entities)
        or bool(guarantor_7c.get("applicable"))
        or bool(guarantor_7c.get("guarantor_name"))
    )

    # ③ C-3 Guarantor Financials — conditional on guarantor_exists
    if guarantor_exists:
        if "**c-3." not in md_lower and "guarantor financials" not in md_lower:
            missing.append(("**C-3.", "C-3 Guarantor Financials (P&L + BS + CF)"))

    # ④ C-4 Guarantor Summary Statistics — conditional on guarantor_exists
    if guarantor_exists:
        if "**c-4." not in md_lower and "guarantor summary" not in md_lower:
            missing.append(("**C-4.", "C-4 Guarantor Summary Statistics"))

    # Determine conditionality for C-5/C-6/C-8 from 7E_base_case data
    base_case = input_json.get("7E_base_case") or {}
    has_projections = bool(
        base_case.get("applicable")
        or any(bool(row.get("assumption")) for row in (base_case.get("key_assumptions") or []))
        or any(bool(row.get("dscr")) for row in (base_case.get("dscr_table") or []))
    )

    # ⑤ C-5 Base Case Projections — conditional on projection data
    if has_projections:
        if "**c-5." not in md_lower and "base case projections" not in md_lower:
            missing.append(("**C-5.", "C-5 Base Case Projections (Key Assumptions + Financials + DSCR)"))

    # ⑥ C-6 Worse Case — mandatory when C-5 present ("C-5 exists → C-6 is MANDATORY")
    worse_case = input_json.get("7F_worse_case") or {}
    has_worse_case = has_projections or bool(
        worse_case.get("applicable")
        or any(bool(row.get("assumption")) for row in (worse_case.get("stress_assumptions") or []))
    )
    if has_worse_case:
        if "**c-6." not in md_lower and "worse case" not in md_lower:
            missing.append(("**C-6.", "C-6 Worse Case (Stress Assumptions + Stressed Summary tables)"))

    # ⑦ C-7 Lessee Financials — conditional on 7G_lessee_financials data
    lessee_data = input_json.get("7G_lessee_financials") or {}
    has_lessee = bool(
        lessee_data.get("applicable")
        or any(bool(l) for l in (lessee_data.get("lessees") or []))
    )
    if has_lessee:
        if "**c-7." not in md_lower and "lessee financials" not in md_lower:
            missing.append(("**C-7.", "C-7 Lessee Financials"))

    # ⑧ C-8 Sensitivity Analysis — conditional on projections or 7H_sensitivity data
    sensitivity_data = input_json.get("7H_sensitivity") or {}
    has_sensitivity = has_projections or bool(
        sensitivity_data.get("applicable")
        or any(bool(row.get("variable")) for row in (sensitivity_data.get("rows") or []))
    )
    if has_sensitivity:
        if "**c-8." not in md_lower and "sensitivity analysis" not in md_lower:
            missing.append(("**C-8.", "C-8 Sensitivity Analysis (6-column table)"))

    return missing


# ─────────────────────────────────────────────────────────────────────────────
# §8 — Changes in Engaged Banks (ACRA Banking Charges)
#
# §8 is DATA-DRIVEN:
#   acra_data_available == false → AI emits one "Not Available" sentence only.
#     Nothing is missing by design — return [] immediately.
#   acra_data_available == true  → Full C-1 content is required:
#     a) Search Metadata: opening sentence with ACRA date + entity + UEN
#     b) Charges Table:   8-column (# | Chargee | Date Reg | Date Charge |
#                         Amount (USD m) | Currency | Property Charged | Status)
#     c) Summary:         4-line exact format (Total charges / Total active
#                         amount / CUB charges / Unique chargees)
#     d) CA Commentary:   ≥4 bullet points (Volume+Trend, CUB Position,
#                         Satisfied Charges, Red Flags + "no unusual patterns")
#     e) Forward-looking: MANDATORY for new_deal/renewal when the input
#                         explicitly signals a new secured facility (via
#                         8A_acra_banking_charges.has_proposed_facility or
#                         proposed_facility_amount_usd_m > 0).
#
# Truncation risk: charges table with many entities + commentary bullets.
# Primary token budget stays at default (8 192) — §8 is typically compact.
# Fill budget: 6 144 tokens.
# ─────────────────────────────────────────────────────────────────────────────
# §10 — Appendix (3 conditional appendices)
# Appendix I   (CUB Group Exposure)      — requires 10A_group_exposure.rows
# Appendix II  (Fleet Capacity Growth)   — requires 10B_fleet_growth.rows
# Appendix III (EMA Detailed Projections)— requires 10C_projections to be truthy
#
# Each appendix is only checked when the corresponding input block is present
# and non-empty.  Empty 10A/10B/10C means the appendix is not applicable and
# must not be flagged as missing.
#
# Appendix III is the densest block: Key Assumptions table + narrative,
# Base Case P&L (≥12 rows) / BS (≥16 rows) / CF (≥6 rows), DSCR table
# (separate from CF!) + DSCR commentary, Worse Case stress comparison table
# + summary table + Worse Case commentary.  All 10 sub-components are checked.
# ─────────────────────────────────────────────────────────────────────────────

def _check_section10(markdown: str, input_json: dict) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for §10 sub-sections absent from *markdown*.

    Appendix I  — conditional on 10A_group_exposure rows
    Appendix II — conditional on 10B_fleet_growth rows
    Appendix III — conditional on 10C_projections being truthy

    Truncation risk is highest at the Appendix III tail (Worse Case commentary
    and Worse Case Summary Table) and at Appendix II Key Note #5 (CAPEX note).
    """
    md_lower = markdown.lower()
    missing: list[tuple[str, str]] = []

    # ─── Appendix I: CUB Group Exposure Table ────────────────────────────────
    exposure_sec = input_json.get("10A_group_exposure") or {}
    if exposure_sec and (exposure_sec.get("rows") or exposure_sec.get("group_limit_sub_table")):

        # ① Appendix I heading
        has_app1_heading = (
            "appendix i" in md_lower
            or ("cub" in md_lower and "exposure" in md_lower)
        )
        if not has_app1_heading:
            missing.append((
                "Appendix I",
                "A-I Heading (Appendix I: CUB's Exposure to [Group Name])",
            ))

        # ② 10-column exposure table — "current approved" is a unique column label
        if "current approved" not in md_lower:
            missing.append((
                "Current Approved",
                "A-I Exposure Table (10 cols: Entity | Branch | Facility Type | "
                "Current Approved | Proposed | Outstanding | Collateral | Guarantor | Maturity | MSR)",
            ))

        # ③ Group Limit sub-table
        if "group limit" not in md_lower:
            missing.append((
                "Group Limit",
                "A-I Group Limit Sub-table (Approved Group Limit | Proposed Total | "
                "**Utilization** | Headroom)",
            ))

    # ─── Appendix II: EMC Fleet Capacity Growth ───────────────────────────────
    fleet_sec = input_json.get("10B_fleet_growth") or {}
    if fleet_sec and fleet_sec.get("rows"):

        # ④ Appendix II heading
        has_app2_heading = (
            "appendix ii" in md_lower
            or ("fleet" in md_lower and "capacity growth" in md_lower)
        )
        if not has_app2_heading:
            missing.append((
                "Appendix II",
                "A-II Heading (Appendix II: EMC Capacity Growth Targets [Year Range])",
            ))

        # ⑤ 5-column fleet table (Owned% is the key mandatory 5th column)
        has_fleet_table = "owned" in md_lower and (
            "total fleet" in md_lower or "total vessels" in md_lower
        )
        if not has_fleet_table:
            missing.append((
                "Owned Fleet",
                "A-II Fleet Table (5 cols: Year | Owned Fleet (TEU m) | Total Fleet (TEU m) | "
                "Total Vessels | Owned%)",
            ))

        # ⑥ CAPEX Key Note #5 — most commonly truncated mandatory bullet
        if "capex" not in md_lower:
            missing.append((
                "CAPEX",
                "A-II Key Note #5: EMC CAPEX plan (USD amount) + EMA capital commitment "
                "(USD amount + date) — MANDATORY",
            ))

    # ─── Appendix III: EMA Detailed Financial Projections ────────────────────
    proj_sec = input_json.get("10C_projections") or {}
    if proj_sec:

        # ⑦ Key Assumptions table
        has_assumptions = (
            "key assumptions" in md_lower
            or "| assumption |" in md_lower
        )
        if not has_assumptions:
            missing.append((
                "Key Assumptions",
                "A-III Key Assumptions Table (| Assumption | FY[Y]E | … | — one row per assumption, no 'Same' cells)",
            ))

        # ⑧ Assumptions narrative (italic paragraph below the table)
        has_assumptions_narrative = (
            "revenue growth assumes" in md_lower
            or "cogs reflects" in md_lower
            or "assumptions narrative" in md_lower
        )
        if not has_assumptions_narrative:
            missing.append((
                "Assumptions Narrative",
                "A-III Assumptions Narrative (italic: 'Revenue growth assumes… COGS reflects… CAPEX per…')",
            ))

        # ⑨ Base Case P&L — detect via first and last bold subtotals
        has_base_pl = "gross profit" in md_lower and "net income" in md_lower
        if not has_base_pl:
            missing.append((
                "Gross Profit",
                "A-III Base Case P&L (≥12 rows: Revenue | COGS | Gross Profit | … | Net Income)",
            ))

        # ⑩ Base Case Balance Sheet — first and last bold subtotals
        has_base_bs = "total current assets" in md_lower and "total equity" in md_lower
        if not has_base_bs:
            missing.append((
                "Total Current Assets",
                "A-III Base Case Balance Sheet (≥16 rows: Total Current Assets → Total Equity)",
            ))

        # ⑪ Base Case Cash Flow — detect via unique Opening/Closing cash pair
        has_base_cf = "operating cash flow" in md_lower and "closing cash" in md_lower
        if not has_base_cf:
            missing.append((
                "Operating Cash Flow",
                "A-III Base Case Cash Flow (≥6 rows: Operating CF | Investing CF | "
                "Financing CF | Net Change | Opening Cash | Closing Cash)",
            ))

        # ⑫ DSCR Analysis table — must be SEPARATE from CF
        #    Detect "debt service" + "dscr" in same document (table-header phrases)
        has_dscr_table = (
            "debt service" in md_lower and "dscr" in md_lower
        ) or "total debt service" in md_lower
        if not has_dscr_table:
            missing.append((
                "DSCR",
                "A-III DSCR Analysis Table (SEPARATE from CF: OCF | Total Debt Service (P+I) | DSCR — 'x' suffix)",
            ))

        # ⑬ DSCR commentary (italic paragraph)
        has_dscr_commentary = (
            "dscr remains" in md_lower
            or "minimum dscr" in md_lower
        )
        if not has_dscr_commentary:
            missing.append((
                "DSCR Commentary",
                "A-III DSCR Commentary (italic: 'DSCR remains above Xx throughout… Minimum DSCR of Xx in [years]')",
            ))

        # ⑭ Worse Case stress assumptions comparison table
        has_wc_stress = (
            "stress magnitude" in md_lower
            or ("worse case" in md_lower and "base case" in md_lower and "stress" in md_lower)
        )
        if not has_wc_stress:
            missing.append((
                "Stress Magnitude",
                "A-III Worse Case Stress Assumptions Table "
                "(Assumption | Base Case | Worse Case | Stress Magnitude — ≥4 rows)",
            ))

        # ⑮ Worse Case summary table
        has_wc_summary = (
            "worse case summary" in md_lower
            or "stressed summary" in md_lower
            or ("worse case" in md_lower and "net income" in md_lower and "dscr" in md_lower)
        )
        if not has_wc_summary:
            missing.append((
                "Worse Case Summary",
                "A-III Worse Case Stressed Summary Table "
                "(Revenue | Operating Profit | Net Income | OCF | Cash Balance | DSCR)",
            ))

        # ⑯ Worse Case commentary (italic paragraph)
        has_wc_commentary = (
            "under worse case" in md_lower
            or "under the worse case" in md_lower
        )
        if not has_wc_commentary:
            missing.append((
                "Worse Case Commentary",
                "A-III Worse Case Commentary (italic: 'Under Worse Case, DSCR declines to minimum Xx in [year]…')",
            ))

    return missing


# ─────────────────────────────────────────────────────────────────────────────

def _check_section9(markdown: str, input_json: dict) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for §9 sub-sections absent from *markdown*.

    §9 is always required — every credit report must have a checklist and
    recommendation. Four components are mandatory:

    C-1: 23-item, 5-column checklist table + 2 mandatory footnotes:
         - Item 15 footnote: Banking Act s.33-3 + exemption basis
         - Item 16 footnote: ACRA search date + UEN + CUB charge cross-ref
    C-2: Conditions Precedent table + Ongoing Covenants table
    C-3: RECOMMENDATION block (exact bold-label format with Decision, Facility
         Amount, Tenor, Security Structure, Key Conditions, Balloon LTV,
         Risk Level vs. Prior Review)
    C-4: Sign-Off block (Prepared by / Reviewed by / Date)

    Detection for Item 15/16 footnotes uses specific sub-phrases that appear
    only in the footnotes (not in the checklist table body itself).
    """
    md_lower = markdown.lower()
    missing: list[tuple[str, str]] = []

    # ① C-1 Checklist Table — 23-item 5-column table header
    has_checklist = (
        "| # |" in md_lower
        or ("| category |" in md_lower and "checklist item" in md_lower)
        or ("checklist item" in md_lower and "|" in markdown)
    )
    if not has_checklist:
        missing.append((
            "| # |",
            "C-1 23-Item Checklist Table (5 cols: # | Category | Checklist Item | Response | Remarks)",
        ))
    else:
        # Verify completeness: item 23 (MAS 612 classification) must be present
        has_item_23 = (
            "| 23 |" in markdown
            or "| 23" in markdown
            or ("23." in markdown and "mas 612" in md_lower)
        )
        if not has_item_23:
            missing.append((
                "| 23 |",
                "C-1 Checklist incomplete — Item 23 (MAS 612 classification) absent "
                "(checklist likely truncated after item 14-22)",
            ))

    # ② Item 15 footnote — Banking Act s.33-3 + exemption basis
    #    Footnote-specific phrase: "exemption basis" or "pre-delivery unsecured drawdown"
    has_item15_footnote = (
        "exemption basis" in md_lower
        or "pre-delivery unsecured drawdown" in md_lower
        or ("item 15" in md_lower and "s.33-3" in markdown)
    )
    if not has_item15_footnote:
        missing.append((
            "Item 15",
            "C-1 Footnote for Item 15 (Banking Act s.33-3: unsecured drawdown amount + "
            "exemption basis + internal approval reference)",
        ))

    # ③ Item 16 footnote — ACRA charge search + UEN + CUB charge reference
    #    Footnote-specific phrase: "acra charge search conducted"
    has_item16_footnote = (
        "acra charge search conducted" in md_lower
        or ("item 16" in md_lower and "uen" in md_lower and "cub charge" in md_lower)
    )
    if not has_item16_footnote:
        missing.append((
            "Item 16",
            "C-1 Footnote for Item 16 (ACRA charge search date + entity + UEN + "
            "CUB charge cross-reference to §1)",
        ))

    # ④ C-2a Conditions Precedent table
    if "conditions precedent" not in md_lower and "condition precedent" not in md_lower:
        missing.append((
            "Conditions Precedent",
            "C-2a Conditions Precedent Table (No. | Description | Testing)",
        ))

    # ⑤ C-2b Ongoing Covenants table
    if "ongoing covenants" not in md_lower and not (
        "covenant" in md_lower and "threshold" in md_lower
    ):
        missing.append((
            "Ongoing Covenants",
            "C-2b Ongoing Covenants Table (Description | Threshold/Requirement | Testing)",
        ))

    # ⑥ C-3 Recommendation block — exact bold-label format
    has_recommendation = (
        "**recommendation:**" in md_lower
        or "**decision:**" in md_lower
        or "**facility amount:**" in md_lower
    )
    if not has_recommendation:
        missing.append((
            "**RECOMMENDATION:**",
            "C-3 Recommendation Block (**Decision** / **Facility Amount** / **Tenor** / "
            "**Security Structure** / **Key Conditions** / **Balloon LTV** / "
            "**Risk Level vs. Prior Review**)",
        ))
    else:
        # Verify tail fields (most likely to truncate)
        has_balloon_ltv = "balloon ltv" in md_lower
        has_risk_level = "risk level" in md_lower and "prior review" in md_lower
        if not has_balloon_ltv or not has_risk_level:
            missing.append((
                "**Balloon LTV:**",
                "C-3 Recommendation — **Balloon LTV** and/or **Risk Level vs. Prior Review** "
                "field(s) truncated",
            ))

    # ⑦ C-4 Sign-Off block
    if "prepared by:" not in md_lower and "prepared by :" not in md_lower:
        missing.append((
            "Prepared by:",
            "C-4 Sign-Off Block (Prepared by: / Reviewed by: / Date:)",
        ))

    return missing


def _check_section8(markdown: str, input_json: dict) -> list[tuple[str, str]]:
    """
    Return (marker, label) pairs for §8 sub-sections absent from *markdown*.

    Returns an empty list immediately when:
    - No 8A_acra_banking_charges input is supplied.
    - acra_data_available is explicitly False (AI correctly outputs "Not Available").
    """
    acra_sec = input_json.get("8A_acra_banking_charges") or {}

    # No input → cannot determine applicability; skip checks
    if not acra_sec:
        return []

    acra_available = acra_sec.get("acra_data_available")

    # Explicitly not available → "Not Available" statement is correct; nothing missing
    if acra_available is False or (
        isinstance(acra_available, str)
        and acra_available.strip().lower() in ("false", "no", "0", "n")
    ):
        return []

    md_lower = markdown.lower()
    missing: list[tuple[str, str]] = []

    # ① Search Metadata — ACRA search date + entity UEN opening sentence
    has_metadata = (
        "acra search" in md_lower
        or "uen:" in md_lower
        or "uen :" in md_lower
        or ("acra" in md_lower and "registered charges" in md_lower)
    )
    if not has_metadata:
        missing.append((
            "ACRA search",
            "C-1a Search Metadata (ACRA search date + entity + UEN opening sentence)",
        ))

    # ② Charges Table — 8-column table with Chargee column header
    has_charges_table = (
        "| chargee |" in md_lower
        or ("chargee" in md_lower and "date of registration" in md_lower)
        or ("chargee" in md_lower and "property charged" in md_lower)
    )
    if not has_charges_table:
        missing.append((
            "| Chargee |",
            "C-1b Charges Table (8 cols: # | Chargee | Date Reg | Date Charge "
            "| Amount (USD m) | Currency | Property Charged | Status)",
        ))

    # ③ Summary paragraph — mandatory 4-line exact format
    has_summary = "total charges:" in md_lower or "total active amount:" in md_lower
    if not has_summary:
        missing.append((
            "Total charges:",
            "C-1c Summary (Total charges / Total active amount / CUB charges / Unique chargees)",
        ))

    # ④ CA Commentary — ≥4 bullet points mandatory
    bullet_lines = [
        ln for ln in markdown.splitlines()
        if ln.strip().startswith(("-", "•", "*")) and len(ln.strip()) > 3
    ]
    if len(bullet_lines) < 4:
        missing.append((
            "- ",
            "C-1d CA Commentary (≥4 bullets: Volume+Trend, CUB Position, "
            "Satisfied Charges, Red Flags / 'No unusual patterns')",
        ))

    # ⑤ Forward-looking bullet — mandatory for new_deal/renewal when a new
    #    secured facility is indicated in the input (proposed charge to ACRA).
    report_type = str(
        (input_json.get("metadata") or {}).get("report_type", "")
    ).lower()
    is_new_deal_or_renewal = "new_deal" in report_type or "renewal" in report_type
    has_proposed_facility = bool(
        acra_sec.get("has_proposed_facility")
        or acra_sec.get("proposed_facility_amount_usd_m")
    )
    if is_new_deal_or_renewal and has_proposed_facility:
        has_forward_looking = (
            "upon execution" in md_lower
            or "additional charge will be registered" in md_lower
            or "bringing cub total to" in md_lower
        )
        if not has_forward_looking:
            missing.append((
                "upon execution",
                "C-1d Bullet #6 Forward-looking (upon execution of proposed facility → "
                "additional ACRA charge for CUB, updated total)",
            ))

    return missing


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def check_section_completeness(
    section_no: int,
    markdown: str,
    input_json: Optional[dict] = None,
) -> list[tuple[str, str]]:
    """
    Return a list of (marker, label) pairs for sub-sections that are absent
    from *markdown* but expected for this section.

    Returns empty list when:
    - The section has no completeness requirements configured here.
    - All expected sub-sections are present.

    *input_json* is required for §1 (conditional logic). Passing None for §1
    causes only the unconditionally mandatory items (Facility Table, Account
    Strategy) to be checked — a safe but partial check.
    """
    if section_no == 1:
        return _check_section1(markdown, input_json or {})

    if section_no == 5:
        return _check_section5(markdown, input_json or {})

    if section_no == 6:
        return _check_section6(markdown, input_json or {})

    if section_no == 7:
        return _check_section7(markdown, input_json or {})

    if section_no == 8:
        return _check_section8(markdown, input_json or {})

    if section_no == 9:
        return _check_section9(markdown, input_json or {})

    if section_no == 10:
        return _check_section10(markdown, input_json or {})

    if section_no == 4:
        md_lower = markdown.lower()
        return [
            (marker, label)
            for marker, label in _S4_REQUIRED
            if marker.lower() not in md_lower
        ]

    if section_no == 2:
        md_lower = markdown.lower()
        return [
            (marker, label)
            for marker, label in _S2_REQUIRED
            if marker.lower() not in md_lower
        ]

    if section_no == 3:
        return _check_section3(markdown)

    return []


# ─────────────────────────────────────────────────────────────────────────────
# Fill prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_fill_system_prompt(section_no: int) -> str:
    if section_no == 6:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §6 'Project Analysis' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. Use bold topic headers exactly: **Project Overview**, **Builder Assessment**, "
            "**Contract Structure**, **Payment & Delivery Schedule**, **RG Mechanism**, "
            "**Construction Progress & Risk**, **Force Majeure**, **Project Economics**. "
            "NO C-N. prefix numbering — bold topic headers only.\n"
            "3. **Payment & Delivery Schedule**: 11-column table "
            "(# | Milestone | Expected Date | Actual Date | Status | % of Contract | "
            "Amount (USD m) | Cumulative Paid (USD m) | CUB Drawdown | RG In Force | RG Amount (USD m)); "
            "Status must be full text ('✅ Completed' / '⏳ Pending' / '⚠️ Delayed'); "
            "BOTH footnotes (* and **) mandatory.\n"
            "4. **Construction Progress & Risk**: status line (milestones X/Y | completion % | "
            "next milestone); for EACH risk: bold title, likelihood label, description, "
            "then ALL mitigant bullets (3-5) — NEVER compress to single sentences.\n"
            "5. **Force Majeure**: standalone paragraph — covered events + historical context + "
            "current supply chain status.\n"
            "6. **RG Mechanism**: exact issuer rating (AA ≠ AA-); trigger events numbered; "
            "coverage % with both min and max figures.\n"
            "7. **Project Economics**: one cross-reference sentence only — "
            "'Vessel earnings projections, breakeven freight rate analysis, and detailed "
            "cash flow projections are covered in Section 7: Financial Analysis.'\n"
            "8. ZERO credit judgments — no 'satisfactory', 'low risk', 'manageable'. "
            "NO source-referencing phrases.\n"
            "9. Banking Act always '33-3' (NOT '333'). RG rating: reproduce verbatim.\n"
            "10. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 10:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §10 'Appendix' section. "
            "The caller specifies exactly which appendix sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. Appendix I — CUB Exposure Table:\n"
            "   - EXACTLY 10 columns: Entity | Branch | Facility Type | Current Approved | "
            "Proposed | Outstanding | Collateral | Guarantor | Maturity | MSR.\n"
            "   - New facilities: mark **[NEW]** bold in the Facility Type cell.\n"
            "   - Subtotal rows in bold: **EMA Subtotal** / **EMC Subtotal** / "
            "**EVA Subtotal** / **Group Total** — embedded in the same table.\n"
            "   - Group Limit sub-table below the main table: | Item | Amount (USD m) | "
            "with 4 rows (Approved Group Limit / Proposed Total Exposure / "
            "**Utilization** (bold) / Headroom).\n"
            "   - Maturity format: 'Dec 2034E' — NEVER 'Dec 2034 (est.)' or '(E)'.\n"
            "   - Column header MUST be 'Current Approved' (not 'Approved').\n"
            "3. Appendix II — EMC Fleet Growth Table:\n"
            "   - EXACTLY 5 columns: Year | Owned Fleet (TEU million) | "
            "Total Fleet (TEU million) | Total Vessels | Owned%.\n"
            "   - Owned% column MANDATORY (e.g. 63%, 67% … 88%).\n"
            "   - CAGR line immediately below the table in bold (e.g. **CAGR: 6.2%**).\n"
            "   - Chart Reference: italic sentence — exact format:\n"
            "     *[EMC Fleet Capacity Growth Chart — Source: [Source] [Date] / "
            "EMC Investor Presentation]*\n"
            "   - Key Notes: minimum 5 numbered bullets. Note #5 MUST state:\n"
            "     'EMC CAPEX plan: USD[X]m; EMA capital commitment: USD[Y]m (as of [date]).'\n"
            "4. Appendix III — EMA Detailed Financial Projections:\n"
            "   - Key Assumptions: table with all projection years individually — "
            "NEVER write 'Same' in any cell.\n"
            "   - Assumptions Narrative: italic paragraph beginning "
            "'Revenue growth assumes… COGS reflects… CAPEX per…'\n"
            "   - Base Case P&L: ≥12 rows; bold subtotals: Gross Profit / Operating Profit / "
            "Profit Before Tax / Net Income; negatives in parentheses: (7,755,000).\n"
            "   - Base Case BS: ≥16 rows; bold subtotals: Total Current Assets / "
            "Total Non-Current Assets / Total Assets / "
            "Total Current Liabilities / Total Non-Current Liabilities / "
            "Total Liabilities / Total Equity.\n"
            "   - Base Case CF: ≥6 rows; SEPARATE table from DSCR; mandatory rows: "
            "Operating Cash Flow / Investing Cash Flow / Financing Cash Flow / "
            "Net Change in Cash / Opening Cash / Closing Cash.\n"
            "   - DSCR Table: SEPARATE from CF — NEVER combined; "
            "columns: FY[Y]E | OCF | Total Debt Service (P+I) | DSCR; "
            "'x' suffix on all DSCR values (e.g. 5.6x).\n"
            "   - DSCR Commentary: italic paragraph — "
            "'DSCR remains above [X]x throughout… Minimum DSCR of [X]x occurs in [years].'\n"
            "   - Worse Case Stress: comparison table — "
            "Assumption | Base Case | Worse Case | Stress Magnitude; minimum 4 rows "
            "(Revenue, COGS%, SOFR, Dividend).\n"
            "   - Worse Case Summary: table rows: Revenue / Operating Profit / Net Income / "
            "OCF / Cash Balance / DSCR.\n"
            "   - Worse Case Commentary: italic paragraph — 'Under Worse Case, DSCR declines "
            "to minimum [X]x in [year] but remains above 1.0x…'\n"
            "5. Numbers: USD'000 with commas. Negatives in parentheses. "
            "'E' suffix on all projected year labels (e.g. FY2026E).\n"
            "6. Context lines: italic using *…* — NO 'Context' or 'System note' labels.\n"
            "7. ZERO credit judgments ('satisfactory', 'manageable', "
            "'well-positioned' FORBIDDEN).\n"
            "8. NEVER use source-referencing phrases. State facts directly.\n"
            "9. NEVER compress projection tables — every row of data must appear in full.\n"
            "10. No '10.' prefix and no 'Appendix' heading — sub-section titles only.\n"
            "11. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 9:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §9 "
            "'Credit Analysis Checklist & Recommendation' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. C-1 Checklist Table: EXACTLY 23 items, EXACTLY 5 columns — "
            "| # | Category | Checklist Item | Response | Remarks |. "
            "Response MUST be bold: **Yes** / **No\\*** / **N/A** — NEVER ✓/✗ symbols. "
            "DO NOT add, remove, or reorder any of the 23 items.\n"
            "3. C-1 Mandatory footnotes (below table — BOTH required):\n"
            "   * Item 15: Pre-delivery unsecured drawdown of USD[X]m is within the "
            "Banking Act s.33-3 single-borrower unsecured limit. "
            "Exemption basis: [item (d) / other]. CUB internal approval reference: [ref].\n"
            "   * Item 16: ACRA charge search conducted on [DD MMM YYYY] for [entity name] "
            "(UEN: [UEN]). CUB charge(s): [Item #, §1 cross-reference].\n"
            "4. C-2a Conditions Precedent table: | No. | Description | Testing |. "
            "Testing values: 'Before first drawdown' / 'Before vessel delivery' / 'Ongoing'. "
            "No sub-numbering (no 1.1, 1.2).\n"
            "5. C-2b Ongoing Covenants table: | Description | Threshold/Requirement | Testing |. "
            "Column header MUST be 'Testing' (NOT 'Frequency' or 'Status'). "
            "Below table: '**Financial Covenants: NIL**' if no covenants beyond ACR/DSCR.\n"
            "6. C-3 Recommendation block — EXACT format (each on its own line, bold labels):\n"
            "   **RECOMMENDATION:**\n"
            "   **Decision:** APPROVE / APPROVE WITH CONDITIONS / DECLINE\n"
            "   **Facility Amount:** USD [X]m\n"
            "   **Tenor:** [X] years from first drawdown\n"
            "   **Security Structure:** [1-2 sentence summary]\n"
            "   **Key Conditions:** numbered list\n"
            "   **Balloon LTV:** [X]% (cap: [Y]%) — Compliant / Breach\n"
            "   **Risk Level vs. Prior Review:** No change / Improved / Deteriorated — [reason]\n"
            "   PROHIBITIONS: NO 'Approval Authority' line. NO 'we recommend'. "
            "NO 'satisfactory', 'low risk', 'manageable'.\n"
            "7. C-4 Sign-Off block (plain text, NOT a table):\n"
            "   Prepared by: [Name], [Title], Credit Management Department, CUB SG Branch\n"
            "   Reviewed by: [Name], [Title], Credit Management Department, CUB SG Branch\n"
            "   Date: [DD MMM YYYY]\n"
            "8. 'Banking Days' (capital B and D — never 'business days'). "
            "'s.33-3' (never '333' or '33-3' without the 's.').\n"
            "9. ZERO credit judgments — 'satisfactory', 'low risk', 'manageable', "
            "'well-mitigated', 'adequate' FORBIDDEN.\n"
            "10. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 8:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §8 'Changes in Engaged Banks' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. C-1a Search Metadata: opening sentence — "
            "'Based on ACRA search dated [DD MMM YYYY], [Entity] (UEN: [UEN]), "
            "a Singapore-incorporated company, has the following registered charges:'\n"
            "3. C-1b Charges Table: EXACTLY 8 columns — "
            "| # | Chargee | Date of Registration | Date of Charge | Amount (USD m) | "
            "Currency | Property Charged | Status |. "
            "Chronological order (earliest first). "
            "Status: 'Registered' OR 'Satisfied ([DD MMM YYYY])' — no other values. "
            "CUB charges: annotate WITHIN the Property Charged cell "
            "(e.g., 'Vessel — Hull No. 4508 — **CUB facility (Item 2, §1)**'). "
            "No separate Notes section outside the table.\n"
            "4. C-1c Summary — EXACT 4-line format (no variations):\n"
            "   Total charges: [X] ([Y] active, [Z] satisfied)\n"
            "   Total active amount: USD [exact amount]m\n"
            "   CUB charges: [N] totaling USD [exact amount]m\n"
            "   Unique chargees: [ACRA names] ([M] distinct banking groups)\n"
            "   Direct sum only — never use 'approximately'. "
            "If one bank appears as multiple branches: note as "
            "'[N] chargees ([M] groups — CUB SG + CUB HO = 1 group)'.\n"
            "5. C-1d CA Commentary: EXACTLY 3-5 BULLET POINTS (NOT prose). Mandatory topics:\n"
            "   Bullet 1 — Volume & trend: charge count over time, pace, pattern.\n"
            "   Bullet 2 — CUB position: §1 Item refs + amounts; confirm consistency.\n"
            "   Bullet 3 — Satisfied charges: context (vessel disposal / repayment / refinancing).\n"
            "   Bullet 4 — Charge type + banking quality: vessel mortgages vs floating charges; "
            "bank profile (international/Japanese/local); flag unknowns.\n"
            "   Bullet 5 — Red flags: explicitly 'No unusual patterns identified' if clean; "
            "OR flag: rapid increase, non-bank chargees, related-party, satisfy-re-register.\n"
            "   Bullet 6 (MANDATORY for new_deal/renewal with new secured facility) — "
            "Forward-looking: 'Upon execution of proposed facility (Item [X], §1, "
            "USD [Y]m for [collateral]), an additional charge will be registered for "
            "CUB [Branch], bringing CUB total to [N+1] charges / USD [Z]m.'\n"
            "6. Amounts: USD [X]m. Dates: DD MMM YYYY. Chargee = ACRA registered name exactly.\n"
            "7. ZERO credit judgments — no 'satisfactory', 'low risk', 'manageable'. "
            "NEVER use source-referencing phrases ('as per', 'according to', 'based on').\n"
            "8. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 7:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §7 'Financial Analysis' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. Use bold sub-headers exactly: **C-1. Borrower Historical Financials**, "
            "**C-2. Borrower Summary Statistics**, **C-3. Guarantor Financials**, "
            "**C-4. Guarantor Summary Statistics**, **C-5. Base Case Projections**, "
            "**C-6. Worse Case**, **C-7. Lessee Financials**, **C-8. Sensitivity Analysis**.\n"
            "3. C-1 (Borrower Historical): Output P&L (≥12 rows) → BS (≥20 rows) → CF (≥7 rows). "
            "Each table preceded by 'Currency: [X] | Unit: [X]' line. "
            "Bold subtotals/totals. Negatives as (1,234). "
            "3-5 CA Commentary bullets after EACH table: YoY absolute+%, margin trends, "
            "one-offs/anomalies, interim vs prior year, forward credit implication.\n"
            "4. C-2 (Summary Statistics): ALL 4 categories — Profitability (GM%, OM%, NM%, "
            "EBITDA%, ROA%, ROE%), Leverage (Total Debt, Net Debt, D/E, ND/E, D/EBITDA), "
            "Coverage (EBITDA/Int, OCF/Debt, OCF/Int), Efficiency (AR Days, AP Days, "
            "Inventory Days) — minimum 18 ratio rows. 3-5 CA Commentary bullets below.\n"
            "5. C-3 (Guarantor Financials): state 'Guarantor Depth: FULL' or "
            "'Guarantor Depth: ABBREVIATED' on first line. "
            "FULL = P&L+BS+CF+Commentary same depth as C-1. "
            "ABBREVIATED = BS + Key Ratios only.\n"
            "6. C-5 (Base Case): ≥3 tables — Key Assumptions TABLE (not prose) + "
            "Projected Financials TABLE (P&L condensed + BS: Cash/Debt/Equity + "
            "CF: OCF/CAPEX/Debt Svc/FCF) + DSCR TABLE (Period | OCF | Debt Service | DSCR). "
            "All years as columns. Conclusion: 2-3 sentences on serviceability, min DSCR, "
            "cash adequacy.\n"
            "7. C-6 (Worse Case): ≥2 tables — Stress Assumptions TABLE "
            "(Assumption | Base | Worse | Stress Magnitude) + Stressed Summary TABLE "
            "(Revenue/OP/NI/OCF/Cash/DSCR per year). "
            "Conclusion: DSCR>1.0x? Cash trough? Guarantor trigger? vs historical worst.\n"
            "8. C-8 (Sensitivity): 6-column table (ALL 6 columns mandatory): "
            "Variable | Base Case | Stress | DSCR Min Impact | Cash Trough Impact | Conclusion. "
            "Include ALL standard variables: Freight -10/-20/-30% | Interest +100/+200bps | "
            "CAPEX +20% | FX ±10% | Delay +6/12M.\n"
            "9. N/M when denominator ≤0. N/A for interim annualization. "
            "'Net Cash' for negative net debt. Pct: 28.4%. Ratios: 0.64x. Commas: 12,164,913.\n"
            "10. ZERO credit judgments — 'satisfactory', 'well-positioned', 'manageable' FORBIDDEN. "
            "NEVER use source-referencing phrases ('as per', 'according to', 'based on input').\n"
            "11. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 5:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §5 "
            "'Collateral / Responsible Person / Guarantor / Support' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. Use bold sub-headers exactly matching the labels: "
            "**C-0. Security Package Overview**, **C-1. Pre-Delivery Security — Refund Guarantee**, "
            "**C-2. Post-Delivery Security — First Priority Mortgage**, "
            "**C-3. Amortisation Profile (Loan Repayment Schedule)**, **C-4. Insurance**, "
            "**C-5. Value Maintenance Clause**, "
            "**C-6. Corporate Guarantee & Guarantor Financial Capacity**, "
            "**C-7. Responsible Person Guarantee**, **C-8. Collateral Adequacy Conclusion**.\n"
            "3. C-1 (RG table): 8 columns (Milestone | Sched. Date | RG Amount (USD m) | "
            "Max Loan O/S (USD m) | Coverage % | Drawdown (USD m) | Cum. Drawdown (USD m) | Status). "
            "Include ALL milestones. Footnote: '[RG = Refund Guarantee; O/S = Outstanding]'.\n"
            "4. C-2 ratios: show formula and actual figures for LTC, ACR at delivery, LTV at maturity.\n"
            "5. C-3: 7-column table (Period | Date | Principal (USD m) | Interest (USD m) | "
            "Total Debt Service (USD m) | Outstanding Balance (USD m) | LTV %); include ALL periods.\n"
            "6. C-5 (VMC): structured legal summary with ACR Covenant, LTV Covenant, Testing, "
            "Cure Period (ALWAYS 'Banking Days' — never 'business days'), Remedy Options, "
            "Cure Mechanism verbatim.\n"
            "7. C-6: dual-currency table (TWD bn | USD bn); state FX rate used.\n"
            "8. C-7: always output — either the guarantee details or "
            "'No responsible person guarantee is required for this facility.'\n"
            "9. C-8: 3-5 sentences covering overall adequacy, key ratios (LTC / ACR / LTV), "
            "coverage progression, bank position vs. peers, conclusion.\n"
            "10. Preserve ALL numbers, percentages, dates exactly as given.\n"
            "11. NEVER use source-referencing phrases. State facts directly.\n"
            "12. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 4:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §4 'Corporate History and Overview' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. Use bold sub-headers exactly matching the labels: "
            "**C-1. Corporate Identity**, **C-2. Ownership & Group Structure**, "
            "**C-3. Key Management**, **C-4. Business Overview**, "
            "**C-5. Revenue & Financial Highlights**, **C-6. Fleet Profile**, "
            "**C-7. Debt Profile**, **C-8. Market Analysis**, **C-9. Peer Comparison**.\n"
            "3. For Banking Relationships (Section E): bold heading '**Banking Relationships**' "
            "followed by a table: Bank | Product | Limit (USD m) | Since\n"
            "4. C-1: two-column Markdown table (Item | Detail) with ≥8 rows.\n"
            "5. C-2: shareholders table (Name | Stake % | Country | Notes) + UBO statement + "
            "group structure narrative.\n"
            "6. C-3: management table (Name | Title | Experience (years) | Background) + "
            "stability assessment.\n"
            "7. C-9: peer table (Company | Fleet TEU | Market Share % | Alliance | Listed) — "
            "top-5 global lines + borrower row bolded.\n"
            "8. Preserve ALL numbers, percentages, dates, and entity names exactly as given.\n"
            "9. NEVER use source-referencing phrases ('as per the input', 'according to', etc.).\n"
            "10. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 3:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §3 'Credit Ratings' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble.\n"
            "2. For External Ratings: if all entities are NIL → ONE sentence only "
            "('**External ratings:** NIL. [Entity] is not externally rated.'). "
            "If rated → bold title then table: Entity | S&P | Moody's | Fitch | Rating Date | Comment.\n"
            "3. For Internal Ratings (MSR Table): bold title '**Internal ratings:**' then STRICT "
            "6-column table with sub-header row "
            "(Entity | Period-1 | Period-2 | Interim | Current | Remarks). "
            "Sub-header row: blank | blank | blank | Generated | Generated | Proposed. "
            "Null/missing MSR → '—' (em dash, NEVER blank). EXACTLY 6 columns.\n"
            "4. For MAS 612 Loan Grading: bold title '**MAS 612 Loan Grading:**' as standalone line, "
            "then EXACTLY 4 SEPARATE paragraphs (NOT bullets, NOT merged): "
            "Para 1 = MSR-to-PASS mapping + recommendation; "
            "Para 2 = account conduct from input; "
            "Para 3 = financial profile + Net Cash + '(See Section 7: Financial Analysis)'; "
            "Para 4 = financial projections capability statement.\n"
            "5. For ESG Rating: bold title '**ESG ratings:**' then entity abbreviation line, "
            "ESG Rating Date line, and image reference line — no scores, no narrative.\n"
            "6. Preserve ALL MSR values exactly (6- ≠ 6, 3+ ≠ 3). "
            "Preserve '(Override)' tags. Preserve regulatory phrases verbatim.\n"
            "7. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 1:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §1 'Credit Facility and Case Details' section. "
            "The caller specifies exactly which sub-sections are missing. "
            "Rules:\n"
            "1. Output ONLY the missing sub-sections — no heading, no preamble, no summary.\n"
            "2. Preserve ALL numbers, dates, legal names, and terminology exactly as in the input JSON.\n"
            "3. Use 'Banking Days' (never 'days'); '33-3' (never '333'); 'Y/N' (never 'Yes/No').\n"
            "4. For Facility Table: 11 mandatory columns; [NEW] only in Proposed Facility column.\n"
            "5. For T&Cs: output all 21 fields in a two-column Markdown table (Field | Content).\n"
            "6. For Deal Comparison: output ALL rows and ALL columns — NEVER compress to prose.\n"
            "7. For Account Strategy: output all 5 sub-sections (Wallet Overview, Current State, "
            "Immediate Opportunities, Future Opportunities, Other Opportunities).\n"
            "8. Start immediately with the first missing sub-section — no introductory text."
        )

    if section_no == 2:
        return (
            "You are a credit report engine for CUB Singapore Branch. "
            "You are completing a PARTIALLY generated §2 Overall Comments section. "
            "The caller will tell you exactly which tables are missing. "
            "Rules:\n"
            "1. Output ONLY the missing tables — no preamble, no heading, no summary.\n"
            "2. Each table MUST follow the exact two-column Markdown format: "
            "column 1 = section label (bold, first row only; subsequent rows blank), "
            "column 2 = content. NEVER merge tables.\n"
            "3. If input data for a table is absent/null, use the mandatory placeholder: "
            "[<section> data not yet provided — please complete the input form]\n"
            "4. Preserve all numbers, dates, and entity names exactly as given in the input JSON.\n"
            "5. Start output immediately with the first missing table — no introductory text."
        )

    return "You are a credit analyst. Output ONLY the missing sections requested."


def _build_fill_user_prompt(
    section_no: int,
    missing: list[tuple[str, str]],
    existing_markdown: str,
    input_json: dict,
    output_language: str,
) -> str:
    import json as _json

    missing_labels = ", ".join(label for _, label in missing)
    existing_tail = existing_markdown[-1500:] if len(existing_markdown) > 1500 else existing_markdown

    if section_no == 10:
        # §10 can be 10 000+ tokens — use a longer tail and larger input cap
        existing_tail_10 = existing_markdown[-2000:] if len(existing_markdown) > 2000 else existing_markdown
        return (
            f"The following sub-sections are MISSING from the already-generated §10 Appendix:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 2000 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail_10}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:10000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES:\n"
            "- Appendix I: EXACTLY 10 columns; 'Current Approved' header; "
            "**[NEW]** for new facilities; Group Limit sub-table with **Utilization** bold.\n"
            "- Appendix II: EXACTLY 5 columns including Owned%; ≥5 Key Notes; "
            "Note #5 MUST include EMC CAPEX plan + EMA capital commitment.\n"
            "- Appendix III P&L: ≥12 rows; bold subtotals; negatives in parentheses.\n"
            "- Appendix III BS: ≥16 rows; bold Total Current Assets → Total Equity.\n"
            "- Appendix III CF: ≥6 rows; SEPARATE table from DSCR.\n"
            "- DSCR table: SEPARATE from CF; 'x' suffix on all DSCR values.\n"
            "- DSCR commentary: italic, begins 'DSCR remains above…'\n"
            "- Worse Case: comparison table (≥4 rows) + summary table + italic commentary.\n"
            "- NO 'Same' in assumption cells — list each year explicitly.\n"
            "- ZERO credit judgments. NEVER use source-referencing phrases.\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 9:
        return (
            f"The following sub-sections are MISSING from the already-generated §9 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:8000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES for missing sub-sections:\n"
            "- Checklist Table: EXACTLY 23 items; EXACTLY 5 columns; "
            "Response = bold **Yes**/**No\\***/**N/A** (NO ✓/✗ symbols).\n"
            "- Footnote 15 (mandatory): '* Item 15: Pre-delivery unsecured drawdown of "
            "USD[X]m is within the Banking Act s.33-3 single-borrower unsecured limit. "
            "Exemption basis: [item (d)]. CUB internal approval reference: [ref].'\n"
            "- Footnote 16 (mandatory): '* Item 16: ACRA charge search conducted on "
            "[DD MMM YYYY] for [entity] (UEN: [UEN]). CUB charge(s): [Item #, §1 ref].'\n"
            "- Conditions Precedent: | No. | Description | Testing | — no sub-numbering.\n"
            "- Ongoing Covenants: | Description | Threshold/Requirement | Testing | — "
            "column header MUST be 'Testing'. State '**Financial Covenants: NIL**' if none.\n"
            "- RECOMMENDATION block: all 7 bold-label fields mandatory; NO 'Approval Authority'; "
            "NO 'we recommend'; NO 'satisfactory'/'manageable'.\n"
            "- Sign-Off: 'Prepared by:' / 'Reviewed by:' / 'Date:' — plain text, NOT a table.\n"
            "- 'Banking Days' (capital B+D). 's.33-3' (never '333').\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 8:
        return (
            f"The following sub-sections are MISSING from the already-generated §8 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:6000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES for missing sub-sections:\n"
            "- Search Metadata: 'Based on ACRA search dated [DD MMM YYYY], [Entity] (UEN: [UEN])...' — exact format.\n"
            "- Charges Table: EXACTLY 8 columns; chronological; "
            "Status = 'Registered' or 'Satisfied ([DD MMM YYYY])'; "
            "CUB charges annotated in Property Charged cell (NOT a separate column).\n"
            "- Summary: EXACT 4-line format — Total charges / Total active amount / "
            "CUB charges / Unique chargees. Direct sum only (no 'approximately').\n"
            "- Commentary: ≥4 bullets; mandatory: Volume+Trend, CUB Position (§1 refs), "
            "Satisfied Charges, Red Flags ('No unusual patterns' if clean).\n"
            "- Forward-looking bullet (new_deal/renewal): 'Upon execution of proposed "
            "facility (Item [X], §1, USD [Y]m for [collateral]), an additional charge "
            "will be registered for CUB [Branch], bringing CUB total to [N+1] charges / USD [Z]m.'\n"
            "- Amounts: USD [X]m. Dates: DD MMM YYYY. Chargee = ACRA registered name.\n"
            "- ZERO credit judgments. NEVER use source-referencing phrases.\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 6:
        return (
            f"The following sub-sections are MISSING from the already-generated §6 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:8000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES for missing sub-sections:\n"
            "- Payment table: EXACTLY 11 columns; full Status text; BOTH footnotes (* and **).\n"
            "- Construction risks: ALL mitigant bullets (3-5 per risk); never compress.\n"
            "- Force Majeure: standalone paragraph; include historical context verbatim.\n"
            "- RG Mechanism: issuer rating verbatim (AA- ≠ AA); numbered trigger events.\n"
            "- Project Economics: ONE cross-reference sentence only.\n"
            "- ZERO credit judgments ('satisfactory', 'low risk', 'manageable' FORBIDDEN).\n"
            "- NO source-referencing phrases. State facts directly.\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 7:
        return (
            f"The following sub-sections are MISSING from the already-generated §7 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:8000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES for missing sub-sections:\n"
            "- C-1: P&L ≥12 rows; BS ≥20 rows (full detail, do NOT collapse liabilities to one row); "
            "CF ≥7 rows. Currency+Unit line above every table. Bold all subtotals/totals.\n"
            "- C-1/C-3 Commentary: 3-5 bullets each table; YoY absolute+%; interim vs prior year; "
            "flag one-offs; forward credit implication MANDATORY.\n"
            "- C-2/C-4: ≥18 ratio rows; all 4 categories; 3-5 commentary bullets below table.\n"
            "- C-5 Base Case: ≥3 tables (Key Assumptions + Projected Financials + DSCR per year). "
            "Conclusion: min DSCR + cash adequacy + serviceability.\n"
            "- C-6 Worse Case: ≥2 tables (Stress Assumptions + Stressed Summary). "
            "Conclusion: DSCR>1.0x? Cash trough? Compare to historical worst.\n"
            "- C-8 Sensitivity: EXACTLY 6 columns; ALL standard variables (Freight/Interest/CAPEX/FX/Delay).\n"
            "- N/M for denominator ≤0; N/A for interim annualization; 'Net Cash' for negative net debt.\n"
            "- ZERO credit judgments ('satisfactory', 'manageable', 'well-positioned' FORBIDDEN).\n"
            "- NEVER use source-referencing phrases. State financial facts directly.\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 5:
        return (
            f"The following sub-sections are MISSING from the already-generated §5 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:8000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES for missing sub-sections:\n"
            "- C-1 RG table: EXACTLY 8 columns; include ALL milestones; footnote required.\n"
            "- C-3 Amortisation: include ALL periods; 7 columns; final row = balloon.\n"
            "- C-5 VMC: 'Banking Days' (NEVER 'business days'); cure mechanism verbatim.\n"
            "- C-6 Guarantor: dual-currency (TWD bn + USD bn); FX rate stated.\n"
            "- C-7: always output, even if 'No responsible person guarantee is required.'\n"
            "- C-8: 3-5 sentences; include LTC / ACR / LTV ratios.\n"
            "- NEVER use source-referencing phrases. State facts directly.\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 4:
        return (
            f"The following sub-sections are MISSING from the already-generated §4 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:7000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES for missing sub-sections:\n"
            "- C-9: peer table must have borrower row in bold; include top-5 global container lines.\n"
            "- Banking Relationships: heading '**Banking Relationships**' + table.\n"
            "- NEVER use source-referencing phrases. State facts as established truth.\n"
            "- All tables use pipe-format Markdown with header separator row.\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 1:
        return (
            f"The following sub-sections are MISSING from the already-generated §1 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:8000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "Now output ONLY the missing sub-sections in correct Markdown format. "
            "No heading, no explanation. Start directly with the first missing sub-section."
        )

    if section_no == 2:
        return (
            f"The following tables are MISSING from the already-generated §2 output: {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — for context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:6000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "Now output ONLY the missing tables in the exact two-column Markdown format. "
            "No heading, no explanation. Start directly with the first missing table."
        )

    if section_no == 3:
        return (
            f"The following sub-sections are MISSING from the already-generated §3 output:\n"
            f"  {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1500 chars — context only, do NOT repeat):\n"
            f"```\n{existing_tail}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:6000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "CRITICAL RULES for missing sub-sections:\n"
            "- MAS 612: standalone bold title + EXACTLY 4 SEPARATE paragraphs (not bullets).\n"
            "- MSR Table: EXACTLY 6 columns; sub-header row; '—' for null values; NEVER blank cells.\n"
            "- External Ratings NIL → ONE sentence only; if rated → table format.\n"
            "- ESG: 4 lines only (bold title, entity abbrev, date, image ref) — no narrative.\n\n"
            "Now output ONLY the missing sub-sections. "
            "No introduction, no explanation. Start directly with the first missing sub-section."
        )

    return (
        f"Missing sections: {missing_labels}\n\n"
        f"Input JSON: {_json.dumps(input_json, ensure_ascii=False)[:4000]}\n\n"
        "Output ONLY the missing sections."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fill execution
# ─────────────────────────────────────────────────────────────────────────────

async def fill_missing_tables(
    section_no: int,
    existing_markdown: str,
    missing: list[tuple[str, str]],
    input_json: dict,
    api_key: Optional[str] = None,
    model_id: Optional[str] = None,
    output_language: str = "en",
) -> tuple[str, int]:
    """
    Call Gemini to generate only the missing sub-sections and return
    (fill_text, estimated_tokens_used).

    The caller is responsible for appending fill_text to the existing markdown.
    """
    from credit_report.generation.claude_client import call_gemini_raw
    from credit_report.config import CR_SECTION_MAX_TOKENS

    system_prompt = _build_fill_system_prompt(section_no)
    user_prompt = _build_fill_user_prompt(
        section_no, missing, existing_markdown, input_json, output_language
    )

    # §1:  Deal Comparison + Account Strategy are non-compressible → 10 240 tokens
    # §3:  MAS 612 (4 paragraphs) + MSR Table can be verbose → 6 144 tokens
    # §4:  C-9 Peer Comparison table + Banking Relationships → 8 192 tokens
    # §5:  C-3 Amortisation Schedule (up to 24 rows) + C-6 Guarantor → 10 240 tokens
    # §6:  C-4 Payment table (11 col) + Construction risks (3-5 bullets each) → 10 240 tokens
    # §7:  P&L (≥12 rows) + BS (≥20 rows) + CF (≥7 rows) + ratios + projections → 12 288 tokens
    # §8:  Charges table + 4-line summary + ≥4 bullets — compact → 6 144 tokens
    # §9:  23-item checklist + CPs + covenants + recommendation + sign-off → 10 240 tokens
    # §10: Appendix I (10-col exposure) + Appendix II (5-col fleet + 5 notes) +
    #      Appendix III (P&L ≥12 + BS ≥16 + CF ≥6 + DSCR + stress + commentaries) → 16 384 tokens
    # others: 8 192 cap
    if section_no == 1:
        max_tokens = 10240
    elif section_no in (3, 8):
        max_tokens = 6144
    elif section_no in (4, 5, 6, 9):
        max_tokens = 10240
    elif section_no == 7:
        max_tokens = 12288
    elif section_no == 10:
        max_tokens = 16384
    else:
        max_tokens = min(CR_SECTION_MAX_TOKENS, 8192)

    logger.info(
        "[Completeness] fill call section=%d missing=%s max_tokens=%d",
        section_no, [label for _, label in missing], max_tokens,
    )

    fill_text = await call_gemini_raw(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        api_key=api_key,
        model_id=model_id,
    )

    # Rough token estimate (call_gemini_raw doesn't return usage metadata)
    estimated_tokens = (len(user_prompt) + len(fill_text)) // 4

    return fill_text, estimated_tokens

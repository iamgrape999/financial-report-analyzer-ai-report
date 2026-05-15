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
# ─────────────────────────────────────────────────────────────────────────────

_S3_REQUIRED: list[tuple[str, str]] = [
    ("**External ratings:**",       "External Ratings"),
    ("**Internal ratings:**",       "Internal Ratings (MSR Table)"),
    ("**MAS 612 Loan Grading:**",   "MAS 612 Loan Grading (4 paragraphs)"),
    ("**ESG ratings:**",            "ESG Rating"),
]


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
        md_lower = markdown.lower()
        return [
            (marker, label)
            for marker, label in _S3_REQUIRED
            if marker.lower() not in md_lower
        ]

    return []


# ─────────────────────────────────────────────────────────────────────────────
# Fill prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_fill_system_prompt(section_no: int) -> str:
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

    # §1: Deal Comparison + Account Strategy are non-compressible → 10 240 tokens
    # §3: MAS 612 (4 paragraphs) + MSR Table can be verbose → 6 144 tokens
    # §4: C-9 Peer Comparison table + Banking Relationships can be verbose → 8 192 tokens
    # others: 8 192 cap
    if section_no == 1:
        max_tokens = 10240
    elif section_no == 3:
        max_tokens = 6144
    elif section_no == 4:
        max_tokens = 8192
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

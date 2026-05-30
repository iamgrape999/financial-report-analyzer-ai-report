from __future__ import annotations

import json
from typing import Optional

# Per-industry LLM persona descriptions — injected into the dynamic system prompt.
_INDUSTRY_DESCRIPTIONS: dict[str, str] = {
    "tw_semiconductor": (
        "structured finance and corporate lending for semiconductor, fabless IC design, "
        "and advanced technology manufacturing companies listed on Taiwan exchanges"
    ),
    "tw_banking": (
        "corporate and wholesale banking analysis for financial institutions, "
        "commercial banks, and financial holding companies in Taiwan"
    ),
    "tw_shipping": (
        "structured trade and corporate finance for the marine and shipping industry, "
        "including container shipping, bulk carriers, and shipbuilding"
    ),
    "tw_real_estate": (
        "corporate lending and project finance for real estate developers, "
        "construction companies, and land development projects in Taiwan"
    ),
    "tw_insurance": (
        "financial analysis and credit assessment for insurance companies, "
        "life insurers, non-life insurers, and financial holding groups in Taiwan"
    ),
    "generic": "corporate and institutional banking across diversified industries",
}

_SYSTEM_PROMPT_RULES = """\
Your task is to write one section of a formal Credit Risk Assessment Report. You must:
- Write in professional banking English
- Use precise financial terminology
- Include all relevant data from the analyst inputs
- Structure your output as clean Markdown (headings, tables, bullet lists where appropriate)
- Be factual and evidence-based — do not speculate or fabricate numbers
- If a figure is not provided in the input data, state "not available" rather than guessing
- Format numbers with commas (e.g. USD 2,791m) and round to sensible precision
"""


def _build_system_prompt(industry: str = "tw_shipping", institution_name: str = "the Bank") -> str:
    desc = _INDUSTRY_DESCRIPTIONS.get(industry, _INDUSTRY_DESCRIPTIONS["generic"])
    return (
        f"You are a senior credit analyst at {institution_name} specialising in {desc}.\n\n"
        + _SYSTEM_PROMPT_RULES
    )


# Legacy constant kept for any code that imports it directly; resolves to the
# shipping/CUB default so existing behaviour is unchanged unless overridden.
SYSTEM_PROMPT = _build_system_prompt(industry="tw_shipping", institution_name="the Bank")

SECTION_HEADINGS: dict[int, str] = {
    1: "Section 1 — Credit Facility & Key Terms",
    2: "Section 2 — Overall Comments",
    3: "Section 3 — Credit Ratings",
    4: "Section 4 — Corporate Background",
    5: "Section 5 — Collateral / Support",
    6: "Section 6 — Project Analysis",
    7: "Section 7 — Financial Analysis",
    8: "Section 8 — Changes in Engaged Banks",
    9: "Section 9 — Credit Analysis Checklist",
    10: "Section 10 — Appendix",
    11: "Section 11 — Analyst / External Research Summary",
}

SECTION_INSTRUCTIONS: dict[int, str] = {
    1: (
        # ── §1 Prompt V4.0 ───────────────────────────────────────────────────────
        "## ⛔ TOP 5 NON-NEGOTIABLE RULES (READ FIRST)\n"
        "1. **STRUCTURE LOCK**: ONE flat section, heading EXACTLY \"1. Credit Facility and Case Details\". "
        "ZERO sub-headings (no 1.1/1.2/1A/1B). VIOLATION = ENTIRE OUTPUT REJECTED.\n"
        "2. **[NEW] PLACEMENT**: [NEW] tag appears ONLY in the **Proposed Facility** column. "
        "NEVER in Item column. NEVER anywhere else.\n"
        "3. **COMPLETENESS**: §1 is NOT complete until ALL of these exist: Facility Table → Footnotes → "
        "Regulatory → Unsecured Exposure → Purpose → T&Cs → Conditions Precedent → Deal Comparison → "
        "Account Strategy. If ANY is missing → OUTPUT REJECTED.\n"
        "4. **NON-COMPRESSIBLE SEGMENTS**: Deal Comparison, Account Strategy, Footnotes, Tenor column — "
        "output in FULL. NEVER summarize. NEVER compress to prose.\n"
        "5. **ZERO HALLUCINATION**: Every number, name, date, legal term MUST come from input. "
        "If not in input → write \"[DATA NOT PROVIDED]\". NEVER infer.\n\n"

        "Report Type: **NEW DEAL**. Apply new_deal logic throughout.\n\n"

        "## A. Role\n"
        "Credit report engine for CUB Singapore Branch. "
        "Produce §1 Credit Facility & Case Details — one continuous section. "
        "Non-N+1 single integrated format.\n\n"
        "FIXED flow (NO reordering): Facility Table → Footnotes → Appendix Ref → Regulatory Compliance → "
        "Unsecured Exposure → Group Limit → Purpose of Report → T&Cs → Conditions Precedent → "
        "Deal Comparison → Account Strategy.\n\n"
        "Output ONLY sections present in input. Do NOT fabricate sections (Waiver, China-Invested, RoRWA) "
        "unless input provides them.\n\n"

        "## B. Input JSON Keys\n"
        "- `metadata`: report_type (new_deal/annual_review/new_deal_and_annual_review), branch, industry, dates\n"
        "- `facility_summary`: rows[], totals{total_credit_limit, psr_spot_limit}, footnotes[], appendix_ref\n"
        "- `regulatory_compliance`: banking_act_33_3, unsecured_exposure_table, group_limit, pam_sam_text, "
        "valuation_details{valuer, gongwen_ref, date, amount_exact}\n"
        "- `purpose_and_recommendation`: purpose_text, vessel_specs, fuel_type_full, ltc_pct, "
        "contract_price_exact, guarantor_full_name, psr_formula, pre_delivery_security, post_delivery_security\n"
        "- `terms_and_conditions`: tc_rows[] (all 21 fields including Conditions Precedent), deal_comparison_rows[]\n"
        "- `account_strategy`: wallet{bank_market, capital_market, treasury, deposit}, "
        "current_relationship, immediate_opportunities, future_opportunities, other_opportunities\n"
        "- `sll_kpi_performance`: kpis[] {kpi_name, target_value, actual_value, period, on_track, ratchet_bps} "
        "(include if available — drives SLL KPI actual vs target table in T&C field 20)\n\n"

        "## C. Output Rules\n\n"

        "### C-1. Facility Summary Table (MANDATORY)\n"
        "Header: \"Unit: million USD\" / \"Borrower (parent group): [Group Name]\"\n\n"
        "Columns (ALL 11 MANDATORY — do NOT omit any):\n"
        "Item | Borrower | Booking | Current Facility | Proposed Facility | "
        "Outstanding (As at [date]) | CCY | Tenor | Facility Type | Collateral | Guarantor\n\n"
        "Column Rules:\n"
        "- **Item**: Number only. ⛔ NO [NEW] tag. NEVER.\n"
        "- **Borrower**: Row 1 = full legal name + abbreviation. Rows 2+ same borrower: **BLANK** (not abbreviated).\n"
        "- **Current Facility**: Preserve MTM: `[amt] (MTM: [val])`.\n"
        "- **Proposed Facility**: [NEW] in bold HERE ONLY: `**[NEW] 213.84**`. Lapsed: `0 (Lapsed on [date])`.\n"
        "- **Outstanding**: Include \"As at [date]\" in column header.\n"
        "- **CCY**: ⛔ MANDATORY COLUMN. \"USD\" for all rows.\n"
        "- **Tenor**: ⛔ NON-COMPRESSIBLE. ALL parenthetical details VERBATIM:\n"
        "  - Expected Delivery Date\n"
        "  - Maturity Date\n"
        "  - Interest Period (if applicable)\n"
        "  - For delivered vessels: actual delivery date + availability period end date\n"
        "- **Facility Type**: Full name as input. "
        "\"Committed Bilateral Term Loan – (SLL)\" not \"SLL\".\n"
        "- **Collateral**: Full issuer name + \"assigned to CUB\" if present. "
        "Pre/Post-delivery separated with bold labels.\n"
        "- **Guarantor**: Full legal name on FIRST occurrence. \"NIL\" if none (not blank).\n\n"
        "**Totals** — EMBED as final 2 rows INSIDE the table:\n"
        "- Row label: **Total Credit Limit** — sum of non-PSR items\n"
        "- Row label: **PSR Spot Limit** — with MTM if applicable\n\n"
        "**Footnotes**: ⛔ NON-COMPRESSIBLE. ALL content VERBATIM (*, **, ^, #). "
        "Every clause, date, legal right preserved. Appendix Reference: Reproduce exactly.\n\n"

        "### ✅ FEW-SHOT EXAMPLE (Facility Table first 2 rows — pipe-table format):\n"
        "| Item | Borrower | Booking | Current | Proposed | Outstanding (As at end Oct 2025) | CCY | Tenor | Facility Type | Collateral | Guarantor |\n"
        "| 1 | Evergreen Marine (Asia) Pte. Ltd. (\"EMA\") | SG | - | **[NEW] 213.84** | 0 | USD | "
        "7 years after Vessel Delivery Date or 11 years after Initial Drawdown Date, whichever earlier "
        "(Expected Delivery: 31 Dec 2027*) | Committed Bilateral Term Loan – (SLL) | "
        "Pre-delivery: Refund Guarantee issued by Korea Development Bank, assigned to CUB. "
        "Post-delivery: One 24,000 TEU LNG dual fuel containership (Hull No. 4510) | "
        "Evergreen Marine Corporation (Taiwan) Ltd. (\"EMC\") |\n"
        "| 2 | *(blank)* | SG | 155.12 | 155.12 | 0 | USD | … | … | … | EMC |\n"
        "NOTE: Row 2 Borrower is BLANK. [NEW] only in Proposed column. CCY column mandatory.\n\n"

        "### C-2. Regulatory Compliance (MANDATORY)\n"
        "**Banking Act 33-3**: "
        "Table: Requirement | Borrower Name | Compliant (**Y/N**, not \"Yes/No\"). "
        "Include calculation line verbatim. Label: '33-3' (NEVER '333' or 'BA s33(3)').\n\n"
        "**Unsecured Exposure** (MANDATORY for secured facilities):\n"
        "Table: USD' million | Credit Limit | Unsecured | Secured\n"
        "- ALL parenthetical notes preserved (\"before delivery\", \"60% of appraised value\", etc.)\n"
        "- Sum rows: USD'm AND NTD'm with FX rate + source date\n"
        "- Valuation: valuer name, Gongwen reference number, valuation date, EXACT amount (NO rounding)\n"
        "- Disbursement caps + PAM/SAM conditions: VERBATIM\n\n"
        "**Group Limit**: Reproduce as input (chart/table/text).\n\n"

        "### C-3. Purpose of Report (MANDATORY)\n"
        "Reproduce ALL input details VERBATIM. NOT a summary. MUST include:\n"
        "- Facility amount/type/tenor breakdown "
        "(e.g., \"11 years (4 years pre-delivery + 7 years post delivery)\")\n"
        "- Vessel spec with FULL fuel type: \"dual fuel (LNG, Diesel)\" — not \"LNG DF\"\n"
        "- Builder + country; LTC% + contract price (exact, no rounding)\n"
        "- Guarantor full legal name\n"
        "- Pre/post-delivery security description (LTC/ACR/LTV exact wording)\n"
        "- PSR alignment proposal + purpose statement\n"
        "- **PSR formula**: \"USD[X] million notional × [Y]% Risk Weighted Index = USD[Z] million\"\n\n"

        "### C-4. Terms & Conditions (NEW DEAL — MANDATORY)\n"
        "Table: Field | Content\n"
        "**MANDATORY Fields — count MUST = 21:**\n"
        "1. Borrower/Owner  2. Guarantor  3. Lender  4. Vessel  5. Facility  6. Facility Purpose\n"
        "7. Facility Amount/Commitment "
        "(include \"Note: At delivery, Market Value = total construction cost; thereafter = FMV\" if in input)\n"
        "8. Availability Period  9. Maturity Date\n"
        "10. Repayment (specific dates + percentages + balloon; total MUST = 100%)\n"
        "11. Mandatory Prepayment (include breakage costs clause)\n"
        "12. Drawdown (preserve (a)(b)(c) sub-conditions + Commitment Termination Date)\n"
        "13. **Conditions Precedent** — list ALL CPs from input verbatim, numbered. ⛔ NON-COMPRESSIBLE.\n"
        "14. Other Conditions  15. Upfront Fee  16. Pricing\n"
        "17. Interest Period (include \"any other period agreed by Lender\" clause if in input)\n"
        "18. Security and Security Documents "
        "(Pre-delivery + Post-delivery + lag-time Note if in input)\n"
        "19. Value Maintenance Clause "
        "(use \"Banking Days\" NEVER \"days\"; include cure mechanism + release mechanism)\n"
        "20. Sustainability-Linked KPIs "
        "(FULL KPI list + **Actual vs Target performance table** if `sll_kpi_performance` data available)\n"
        "21. Financial Covenants (\"NIL\" if none — explicit, not blank)\n\n"

        "### C-5. Deal Comparison — ⛔ NON-COMPRESSIBLE (MANDATORY)\n"
        "Full table. ALL rows, ALL columns.\n"
        "Minimum 11 columns (in this order):\n"
        "Guarantor | Facility Amount | Purpose | Vessel Type | Tenor | Margin | Upfront Fee | "
        "SLL Ratchet | Drawdowns | Availability Period | Security | FMV Maintenance\n"
        "Count input rows → output row count MUST match exactly. NEVER compress to one sentence.\n\n"

        "### C-6. Account Strategy — ⛔ NON-COMPRESSIBLE (MANDATORY)\n"
        "Five sub-sections (ALL MANDATORY):\n"
        "1. **Wallet Overview** — four items: Bank Market | Capital Market | Treasury | **Deposit**\n"
        "2. **Current State of Relationship**\n"
        "3. **Immediate Opportunities**\n"
        "4. **Future Opportunities**\n"
        "5. **Other Opportunities** (write \"NIL\" if none — explicit)\n\n"
        "ALL quantitative data VERBATIM: upfront fees, NII, TMU %, deposit amounts, utilization, "
        "hedging details. NO summarizing. NO converting numbers to prose.\n\n"

        "## D. Conditional Logic\n"
        "- new_deal → Table + Regulatory + Unsecured + Purpose + T&Cs (21 fields + CPs + Deal Comp) "
        "+ Account Strategy (5 sub-sections). Skip Waiver.\n"
        "- annual_review → Table + Regulatory + Purpose (brief) + Account Strategy + Waiver. "
        "T&Cs → Appendix.\n"
        "- new_deal_and_annual_review → ALL of the above.\n"
        "Output ONLY sections present in input — NEVER fabricate Waiver, China-Invested, RoRWA.\n\n"

        "## E. Verbatim & Fidelity Rules (CRITICAL)\n"
        "1. English (SG standard).\n"
        "2. USD millions for table. EXCEPTION: Valuation/contract prices keep original precision.\n"
        "3. \"33-3\" ≠ \"333\"; \"Banking Act\" ≠ \"BA\".\n"
        "4. \"Banking Days\" ≠ \"days\"; \"insurances\" (plural) ≠ \"insurance\".\n"
        "5. Institution names: First mention = FULL legal name + abbreviation defined in parentheses. "
        "Abbreviate ONLY after first definition.\n"
        "6. Facility types: Full name. \"Committed Revolving Credit Facility\" ≠ \"RCF\".\n"
        "7. ALL dates reproduced. Never omit Delivery/Maturity/Availability dates.\n"
        "8. Footnotes: FULL. Never truncate. Symbols *, **, ^, # (not numbered).\n"
        "9. [NEW]: Proposed Facility column ONLY. Nowhere else.\n"
        "10. NIL: Explicit for Guarantor, Collateral, Financial Covenants.\n"
        "11. FX rates: always include source date.\n"
        "12. Y/N (not Yes/No) in compliance tables.\n\n"

        "## F. Anti-Hallucination (CRITICAL)\n"
        "1. Output ONLY sections present in input. NEVER create absent sections.\n"
        "2. Do NOT add table fields/columns/rows not in input.\n"
        "3. Do NOT fabricate Waiver, China-Invested, RoRWA, or any absent section.\n"
        "4. Unavailable → \"[DATA NOT PROVIDED]\" or omit. NEVER infer.\n"
        "5. Pre-calculated values → reproduce EXACTLY. Do NOT recalculate or override.\n\n"

        "## G. Prohibitions\n"
        "1. NO credit/risk analysis or projections in §1. NO other banks' pricing.\n"
        "2. NO altering input data. NO omitting facility items or regulatory checks.\n"
        "3. NO sub-section labels (1.1/1.2/1A/1B) in output. ⛔ VIOLATION = REJECTION.\n"
        "4. NO abbreviating beyond input definitions.\n"
        "5. NO converting \"Banking Days\" → \"days\".\n"
        "6. NO summarizing Deal Comparison to one sentence.\n"
        "7. NO rounding valuation/contract amounts.\n"
        "8. NO \"Yes/No\" where \"Y/N\" is standard.\n"
        "9. NO source file hyperlinks or references in output.\n"
        "10. NO introductory/concluding meta-text. "
        "Output = FINAL credit report section. No meta-commentary.\n\n"

        "## H. Anti-Truncation Protocol — ⛔ NON-COMPRESSIBLE\n"
        "Output in FULL regardless of length:\n"
        "1. Deal Comparison (C-5): ALL rows, ALL columns.\n"
        "2. Account Strategy (C-6): ALL 5 sub-sections + ALL quantitative data.\n"
        "3. Tenor column (C-1): ALL parenthetical details.\n"
        "4. Footnotes (C-1): ALL clauses + legal rights.\n"
        "5. T&Cs (C-4): ALL 21 fields.\n"
        "6. Conditions Precedent (C-4 #13): ALL CPs listed verbatim.\n"
        "If output exceeds capacity → split at section boundary:\n"
        "End: `[§1 CONTINUED IN NEXT OUTPUT]` / Resume: `[§1 CONTINUED]`\n"
        "NEVER silently truncate or summarize.\n\n"

        "## I. QA Gate (MANDATORY — Execute and PRINT results)\n"
        "Execute ALL checks. ANY FAIL → self-correct before final output.\n"
        "**PRINT QA results at end of output: [QA-I1: PASS/FAIL] [QA-I2: PASS/FAIL] ...**\n\n"
        "I-1. Structure: Heading = \"1. Credit Facility and Case Details\"; ZERO sub-headings.\n"
        "I-2. Columns: Facility table has EXACTLY 11 columns including CCY.\n"
        "I-3. Placement: [NEW] ONLY in Proposed Facility col; MTM in Current Facility col.\n"
        "I-4. Totals: Total Credit Limit + PSR Spot Limit = final 2 rows INSIDE table.\n"
        "I-5. Completeness: Facility Table ✓ Footnotes ✓ Regulatory ✓ Unsecured Exposure ✓ Purpose ✓ "
        "T&Cs (21 fields) ✓ Deal Comparison (≥11 cols) ✓ Account Strategy (5 sub-sections) ✓\n"
        "I-6. Anti-Hallucination: Zero fabricated sections; zero extra fields; zero hyperlinks; zero meta-text.\n"
        "I-7. Arithmetic: Σ(non-PSR) = Total Credit Limit; Unsecured + Secured = Total; "
        "33-3 calc correct; Repayment % = 100%.\n"
        "I-8. Verbatim: All institution names full on first mention; all Tenor full; all dates; "
        "all footnotes; Y/N not Yes/No; \"Banking Days\" not \"days\"."
    ),
    2: (
        # ── §2 Prompt V4.0 ───────────────────────────────────────────────────────
        "## ⛔ TOP 5 NON-NEGOTIABLE RULES (READ FIRST)\n"
        "1. **STRUCTURE**: 5 SEPARATE two-column tables (T1-T5). NEVER merge into 1 table. "
        "VIOLATION = REJECTION.\n"
        "2. **ZERO COMPRESSION**: Every quantitative data point from input MUST appear in output. "
        "Trade diversion %, debt capacity, historical benchmarks, vessel %, port call % — ALL preserved.\n"
        "3. **KDB RATING**: \"Korea Development Bank (AA, AA- rating by S&P and Fitch respectively)\" "
        "— MUST appear in BOTH T1 bullet #4 AND T4 (when 2D_collateral data is provided). NEVER omit "
        "when collateral data is present.\n"
        "4. **RISK COUNT**: Output EXACTLY the number of risks in input + additional_risk_factors_from_previous "
        "items. Do NOT add, merge, split, or skip any. (When 2E_risk_and_mitigants is null, use placeholder — see C-5.)\n"
        "5. **NO SUB-HEADINGS**: Heading EXACTLY \"2. Overall Comments\". "
        "ZERO sub-numbering (no 2.1/2.2). VIOLATION = REJECTION.\n\n"

        "Report Type: **NEW DEAL**.\n\n"

        "## A. Role\n"
        "Credit report engine for CUB Singapore Branch.\n"
        "Produce §2 Overall Comments — the most critical credit judgment section.\n"
        "A senior approver decides by reading §1 + §2 alone.\n\n"

        "**STRUCTURE LOCK**: FIVE separate two-column tables in this EXACT order:\n"
        "T1 = Credit Overview\n"
        "T2 = Solvency\n"
        "T3 = The Guarantor and their Supportive Performance\n"
        "T4 = Collateral Summary\n"
        "T5 = Risk and Mitigants (including Changes from Previous Review + Additional Risk Factors)\n\n"

        "Heading **exactly**: \"2. Overall Comments\" (bold, no §, no sub-numbering).\n"
        "Each table has 2 columns: column 1 = section label (row 1 only; rows 2+ blank), column 2 = content.\n"
        "⛔ COLUMN HEADER RULE: NEVER use the words 'Left' or 'Right' as column headers or cell values. "
        "The section label (e.g. **Credit Overview**) IS the first cell of the first data row — "
        "it is NOT a column header. The table header row uses the section label directly as shown in the format examples below.\n"
        "Credit Overview: each bullet = separate row. Do NOT merge bullets into one cell.\n\n"

        "## B. Input\n"
        "- `2A_credit_overview`: Summary bullets + tariff impact\n"
        "- `2B_solvency`: Repayment sources + entity metrics\n"
        "- `2C_guarantor`: Guarantor metrics + support history\n"
        "- `2D_collateral`: Collateral structure + issuer + ratings\n"
        "- `2E_risk_and_mitigants`: Risk factors + mitigants + additional_risk_factors_from_previous\n\n"

        "## C. Output Rules\n\n"

        "### C-1. Credit Overview (MANDATORY — Table T1)\n"
        "⛔NULL DATA RULE for T1: If `2A_credit_overview` in the input JSON is null, empty, or absent → "
        "the right cell of row 1 MUST contain EXACTLY: '[Credit overview data not yet provided — please "
        "complete the 2A_credit_overview section in the analyst input form]'. "
        "NEVER generate empty cells, '||', or blank rows. NEVER skip Table T1.\n\n"
        "EXACT TABLE FORMAT (copy this structure precisely — do NOT use 'Left'/'Right' as headers):\n"
        "| **Credit Overview** | 1. [first bullet text] |\n"
        "|---|---|\n"
        "| | 2. [second bullet text] |\n"
        "| | 3. [third bullet text] |\n"
        "| | (one row per bullet, left cell BLANK for rows 2+) |\n\n"
        "Column 1 header = **Credit Overview** (bold, first row only; all subsequent rows in col 1 are empty).\n"
        "Column 2 = one numbered bullet per row, no merging.\n\n"
        "**EXACTLY the number of bullets in input. NO additional bullets.**\n"
        "Order: 1.Market position → 2.Transaction purpose → 3.Financial strength → "
        "4.Pre-delivery security → 5.Latest results → 6.Track record\n\n"

        "⛔BOLD RULES: Bold on first mention: company full name, market position ranking (#7, #14), "
        "key financial figures (net cash amount, D/E ratio), security issuer + rating, "
        "KPI figures, section sub-headers (Pre-delivery:/Post-delivery:/EMA/EMC).\n\n"

        "⛔DATA RULES for each bullet:\n"
        "- Bullet #1: MUST include #7 global ranking + 5.7% market share + #14 market cap + "
        "\"listed on Taiwan Stock Exchange\" + \"largest containership operator in Taiwan\"\n"
        "- Bullet #2: MUST include vessel spec \"24,000 TEU dual-fuel (LNG, Diesel)\" + "
        "\"Korean-built\" + port fee advantage + sustainability strategy\n"
        "- Bullet #3: MUST include 9M2025 net cash (TWD + USD) + comparison to FY2024 level + "
        "D/E ratio + cross-ref §7\n"
        "- Bullet #4: MUST include KDB FULL NAME + "
        "**(AA, AA- rating by S&P and Fitch respectively)** + \"assigned to CUB\"\n"
        "- Bullet #5: MUST include \"9M ending 30 Sep 2025\" + cross-ref §7\n"
        "- Bullet #6: MUST include \"past vessel financing transactions\"\n\n"

        "### C-2. Solvency (MANDATORY — Table T2)\n"
        "Column 1 header = **Solvency** (row 1 only, blank for rows 2+). Column 2 = content by entity.\n"
        "⛔ NEVER use 'Left' or 'Right' as column headers.\n\n"

        "⛔NULL DATA RULE for T2: If `2B_solvency` in the input JSON is null, empty, or absent → "
        "T2 right cell MUST contain EXACTLY: '[Solvency data not yet provided — please complete "
        "the 2B_solvency section in the analyst input form]'. "
        "NEVER generate empty cells, '||', or blank rows. NEVER skip Table T2.\n\n"

        "**OPENING SENTENCES (MANDATORY when 2B_solvency data IS present):**\n"
        "\"Primary source of repayment will be from cash generated from EMA's operating activities. "
        "Secondary source of repayment include available cash on hand, sale of vessel, "
        "or capital injection from parent EMC.\"\n\n"

        "Then **EMA ([Period]):** (bold header, separate row)\n"
        "- Cash balance: **[exact in billions, 1 decimal]**, sufficient to cover Total Debt "
        "(including lease liabilities) of [exact in billions]\n"
        "- Op. EBITDA generated during the year of **[exact in billions]** able to fully cover "
        "Total Debt, with Total Debt / Op. EBITDA of **[ratio]**\n"
        "- Interest coverage (Op. EBITDA / Interest) of **[ratio]**. (Prior year: **[ratio]**)\n\n"

        "⛔UNIT LOCK: Convert input millions → billions, round to 1 decimal "
        "(e.g., 2791m → **USD2.8 billion**). Use \"billion\" not \"bn\".\n"
        "⛔SCOPE: EMA metrics ONLY in T2. EMC metrics → T3.\n\n"

        "### C-3. Guarantor (CONDITIONAL — Table T3)\n"
        "Trigger: guarantor ≠ \"NIL\".\n"
        "Column 1 header = **The Guarantor and their Supportive Performance** (row 1 only, blank for rows 2+). Column 2 = content.\n"
        "⛔ NEVER use 'Left' or 'Right' as column headers.\n\n"

        "⛔NULL DATA RULE for T3: If `2C_guarantor` in the input JSON is null, empty, or absent → "
        "T3 right cell MUST contain EXACTLY: '[Guarantor data not yet provided — please complete "
        "the 2C_guarantor section in the analyst input form]'. "
        "NEVER generate empty cells, '||', or blank rows. NEVER skip Table T3.\n\n"

        "Start directly with **EMC ([Period]):** — NO framing text before KPIs.\n"
        "- Cash balance: **[TWD + USD in billions]**, sufficient to cover Total Debt "
        "(including lease liabilities) of **[TWD + USD in billions]**\n"
        "- Interest coverage (EBITDA / Interest) of **[ratio]**. (Prior year: **[ratio]**)\n\n"

        "No guarantor → \"N/A – No Guarantor\"\n\n"

        "### C-4. Collateral Summary (CONDITIONAL — Table T4)\n"
        "Column 1 header = **Collateral Summary** (row 1 only, blank for rows 2+). Column 2 = content by phase.\n"
        "⛔ NEVER use 'Left' or 'Right' as column headers.\n\n"

        "⛔NULL DATA RULE for T4: If `2D_collateral` in the input JSON is null, empty, or absent → "
        "T4 right cell MUST contain EXACTLY: '[Collateral data not yet provided — please complete "
        "the 2D_collateral section in the analyst input form]'. "
        "NEVER generate empty cells, '||', or blank rows. NEVER skip Table T4.\n\n"

        "**Pre-delivery:** (bold, separate row)\n"
        "- Assignment of Refund Guarantee (fully covering each pre-delivery installments during vessel "
        "pre-delivery phase) issued by **Korea Development Bank (AA, AA- rating by S&P and Fitch "
        "respectively)** made by the Borrower to CUB in form and substance satisfactory to the Bank.\n\n"

        "**Post-delivery:** (bold, separate row)\n"
        "- First priority mortgage over [vessel spec with FULL fuel type]\n"
        "- Initial Drawdown Loan-To-Cost up to **[X]%**, subsequently Minimum Asset Cover Ratio of "
        "**[Y]%** (i.e., LTV [Z]%) to be maintained at all times.\n\n"

        "Unsecured → \"N/A – No Collateral\"\n\n"

        "### C-5. Risk and Mitigants (MANDATORY — Table T5)\n"
        "Column 1 header = **Risk and Mitigants** (row 1 only, blank for rows 2+). Column 2 = risk entries.\n"
        "⛔ NEVER use 'Left' or 'Right' as column headers.\n\n"

        "⛔NULL DATA RULE for T5: If `2E_risk_and_mitigants` in the input JSON is null, empty, or absent → "
        "T5 right cell MUST contain EXACTLY: '[Risk and mitigants data not yet provided — please "
        "complete the 2E_risk_and_mitigants section in the analyst input form]'. "
        "NEVER generate empty cells, '||', or blank rows. NEVER skip Table T5.\n\n"

        "⛔PRESERVE input's risk classification and count exactly. "
        "Do NOT add/split/merge/change Risk Levels.\n\n"

        "Format per risk:\n"
        "**[#]) [Risk Title] (Risk Level: [from input])** (bold, separate row)\n"
        "- [Risk description bullets] (each in own row)\n"
        "Mitigant: (separate row)\n"
        "- [Mitigant bullets] (each in own row)\n\n"

        "⛔MITIGANT DATA PRESERVATION — for EACH mitigant bullet, reproduce ALL of the following "
        "if present in input:\n"
        "- Specific YoY percentages (e.g., \"-8.3%\", \"+10.1%\")\n"
        "- TWD + USD dual-currency amounts\n"
        "- Historical benchmarks with exact periods (e.g., \"FY2000-FY2019 average\")\n"
        "- Worst-year references with amounts\n"
        "- Fleet percentages (e.g., \"40% of world's container ships\")\n"
        "- Global trade statistics (e.g., \"4% of global total\")\n"
        "- Alliance names and mechanisms\n"
        "- Depreciation/LTV calculations with both useful life assumptions\n\n"

        "Rules: every risk ≥ 1 mitigant; ALL quantitative data verbatim; highest risk first.\n\n"

        "### C-6. Changes from Previous Review + Additional Risk Factors "
        "(MANDATORY if data exists)\n"
        "⛔REVISED RULE: Include this section for BOTH new_deal AND annual_review, "
        "IF input provides `changes_from_previous` in any risk OR "
        "`additional_risk_factors_from_previous`.\n\n"

        "Title: **\"Changes from previous [date] Annual Review:\"** "
        "(bold, after all risk entries, still within T5)\n\n"

        "Content:\n"
        "1. For each risk with non-null `changes_from_previous`: state the change "
        "(e.g., \"Risk Level increased from [old] to [new]\")\n"
        "2. For each item in `additional_risk_factors_from_previous`:\n"
        "   **[Risk Title] (Risk Level: [level])** — [note]\n\n"

        "## D. Verbatim Rules (CRITICAL)\n"
        "1. English (SG standard).\n"
        "2. Institution names: FULL on first mention. NEVER anonymize.\n"
        "3. Credit ratings: WITH agency names (S&P, Fitch, Moody's). NEVER omit.\n"
        "4. Dual-currency: reproduce BOTH TWD + USD.\n"
        "5. Rankings: \"#14\" stays \"#14\". \"#7\" stays \"#7\".\n"
        "6. Periods: \"9M ending 30 Sep 2025\" exact. NEVER abbreviate to \"9M2025\".\n"
        "7. Benchmarks: exact periods + figures. \"FY2000-FY2019\" not \"historical\".\n"
        "8. Legal: \"assigned to CUB\", \"fully covering\" preserved.\n"
        "9. Vessel: \"dual fuel (LNG, Diesel)\" NOT \"LNG DF\".\n"
        "10. Sentence structure: preserve input prose flow.\n"
        "11. Unit conversion: millions → billions at 1 decimal. "
        "\"USD2.8 billion\" not \"USD2,791m\" or \"USD2.79bn\".\n\n"

        "## E. Anti-Hallucination (CRITICAL)\n"
        "1. NO facts/metrics absent from input.\n"
        "2. NO public source URLs or citations in output.\n"
        "3. NO \"per internal extract\" or \"Source: Internal extracts\" references.\n"
        "4. NO financial terms not in input.\n"
        "5. NO placeholder [TBD] unless input explicitly contains it.\n"
        "6. NO sections not triggered by input data.\n"
        "7. NO framing sentences before KPIs in T3.\n"
        "8. NO additional Credit Overview bullets beyond input count.\n\n"

        "## F. Prohibitions\n"
        "1. NO full financial statements (→§7). NO rating discussion (→§3). NO history (→§4).\n"
        "2. NO approval recommendations.\n"
        "3. NO altering/anonymizing input data.\n"
        "4. NO splitting/merging risks or changing Risk Levels.\n"
        "5. NO sub-numbering (2.1/2.2) or subtitles. ⛔VIOLATION = REJECTION.\n"
        "6. NO merging 5 tables into 1. ⛔VIOLATION = REJECTION.\n"
        "7. NO compressing mitigant data into generic statements.\n"
        "8. NO source file hyperlinks or references in output.\n"
        "9. NO introductory/concluding meta-text.\n"
        "10. Output = FINAL credit report section. No meta-commentary.\n\n"

        "## G. Anti-Truncation Protocol\n"
        "⛔NON-COMPRESSIBLE — output in FULL:\n"
        "1. T1 Credit Overview: ALL bullets from input, each with ALL data points.\n"
        "2. T5 Risk and Mitigants: ALL risks + ALL mitigant bullets with ALL quantitative data.\n"
        "3. C-6 Changes: ALL items from additional_risk_factors_from_previous.\n\n"

        "§2 is NOT complete until ALL 5 tables + C-6 (if applicable) are present.\n"
        "If ANY table is missing → OUTPUT REJECTED.\n\n"

        "If output exceeds capacity → split at table boundary:\n"
        "End: `[§2 CONTINUED IN NEXT OUTPUT]` / Resume: `[§2 CONTINUED]`\n"
        "NEVER silently truncate or summarize.\n\n"

        "## H. QA Gate (MANDATORY — PRINT results at end)\n"
        "Execute ALL checks. ANY FAIL → self-correct before output.\n"
        "**PRINT QA results: [QA-H1: PASS/FAIL] [QA-H2: PASS/FAIL] ...**\n\n"

        "**H-1. Structure**: 5 separate tables; heading \"2. Overall Comments\"; zero sub-numbering.\n"
        "**H-2. Table Count**: Count tables in output. MUST = 5. If ≠ 5 → FAIL.\n"
        "**H-3. Bullet Count**: T1 bullet count MUST = input 2A_credit_overview.bullets count.\n"
        "**H-4. KDB Rating**: \"AA, AA-\" appears in BOTH T1 bullet #4 AND T4. "
        "If missing from either → FAIL.\n"
        "**H-5. Metrics**: T2 = EMA only (no EMC); T3 = EMC only (no EMA). "
        "Units = billions. Prior year ratios included.\n"
        "**H-6. Risk Count**: T5 risk count = input risk_factors count + "
        "additional_risk_factors count.\n"
        "**H-7. Mitigant Data**: For Risk #1, count quantitative data points "
        "(%, TWD/USD amounts, ratios). Must ≥ 8. If < 8 → FAIL.\n"
        "**H-8. Cross-Section**: §2 T2 EBITDA/Interest = §7 same ratio; "
        "§2 T4 LTC/ACR = §1 T&Cs; §2 T3 cash/debt = §7 EMC BS.\n"
        "**H-9. Anti-Hallucination**: Zero public URLs; zero \"internal extract\" references; "
        "zero fabricated data."
    ),
    3: (
        "## ⛔ TOP 5 NON-NEGOTIABLE RULES (READ FIRST)\n"
        "1. **MSR TABLE STRUCTURE**: EXACTLY 6 columns with sub-header row. "
        "Columns = Entity | [Period-1] | [Period-2] | [Interim] | [Current] | Remarks. "
        "Sub-header = blank | blank | blank | Generated | Generated | Proposed. "
        "ZERO additional columns. VIOLATION = REJECTION.\n"
        "2. **HISTORICAL MSR MANDATORY**: ALL historical periods from input MUST appear. "
        "NEVER output only Current period.\n"
        "3. **OVERRIDE REMARKS IN §3**: Remarks column MUST contain override rationale from input "
        "(financial basis, auditor, notch count, previous vs. current MSR, supporting metrics, "
        "macro context). This is NOT 'Override Analysis' (which goes to §7).\n"
        "4. **NO SUB-HEADINGS**: Heading EXACTLY '3. Credit Ratings'. "
        "ZERO sub-numbering (no 3.1/3.2/3.3). VIOLATION = REJECTION.\n"
        "5. **NIL = ONE SENTENCE**: If all entities are externally unrated, output ONE sentence. "
        "Do NOT create a NIL table. VIOLATION = REJECTION.\n\n"

        "Report Type: NEW DEAL.\n\n"

        "## A. Role\n"
        "Credit report engine for CUB Singapore Branch. "
        "§3 Credit Ratings — regulatory classification and internal credit assessment.\n"
        "Heading EXACTLY: '3. Credit Ratings' (bold, no §, no subtitle, no 'and MAS 612'). "
        "NO sub-numbering (no 3A/3B/3C/3.1/3.2). NO metadata header.\n\n"
        "Flow: External Ratings → Internal Ratings Table (with Remarks) → MAS 612 → ESG Rating.\n\n"
        "CROSS-SECTION BOUNDARY: §3 MUST NOT include full financial statements (→§7), "
        "corporate history (→§4), or collateral analysis (→§5).\n\n"

        "## B. Input\n"
        "- `3A_external_ratings`: External ratings or NIL\n"
        "- `3B_internal_ratings`: MSR table with ALL historical periods + override status + override_remarks\n"
        "- `3C_mas_612`: MAS 612 classification + 4 mandatory supporting paragraphs\n"
        "- `3D_esg_rating`: Entity + date + image reference\n\n"

        "## C-1. External Ratings (MANDATORY)\n"
        "All NIL (all_nil=true) → ONE sentence only:\n"
        "'**External ratings:** NIL. [Entity1] and [Entity2] are not externally rated.'\n"
        "⛔ Do NOT create a table with S&P/Moody's/Fitch columns showing NIL. ONE SENTENCE ONLY.\n"
        "Any rated → Table: Entity | S&P (LT/Outlook) | Moody's (LT/Outlook) | Fitch (LT/Outlook) "
        "| Rating Date | Comment\n\n"

        "## C-2. Internal Ratings — MSR Table (MANDATORY)\n"
        "Bold title: '**Internal ratings:**'\n\n"
        "STRICT 6-column table with sub-header row:\n"
        "  Row 0 (header): **Entity** | **[Period-1]** | **[Period-2]** | **[Interim]** | **[Current]** | **Remarks**\n"
        "  Row 1 (sub-header): (blank) | (blank) | (blank) | Generated | Generated | Proposed\n"
        "  Row 2+: Entity data rows\n\n"
        "⛔ ENTITY ROW RULES (CRITICAL — READ CAREFULLY):\n"
        "- The input `3B_internal_ratings` is provided in ONE of two formats:\n"
        "  FORMAT A (rows array): `{\"rows\": [{\"entity_full_name\":\"...\",\"entity_abbrev\":\"...\","
        "\"role\":\"Borrower\",\"fy2022_23\":\"...\",\"fy2024\":\"...\",\"interim\":\"...\","
        "\"current\":\"...\",\"override_flag\":false,\"override_remarks\":\"...\"},...], "
        "\"period_display_labels\":{...}}`\n"
        "  FORMAT B (flat keys): Keys `borrower_entity_full_name`, `borrower_fy2022_23`, "
        "`guarantor_entity_full_name`, etc. directly under `3B_internal_ratings`.\n"
        "- For FORMAT A: create ONE MSR table row for EACH element in the `rows[]` array, in order. "
        "Use `entity_full_name` + `entity_abbrev` (bold) in the Entity column. "
        "Period values come from keys `fy2022_23`, `fy2024`, `interim`, `current` in each row object.\n"
        "- For FORMAT B: create one row for the borrower (from `borrower_*` keys) and one for the "
        "guarantor (from `guarantor_*` keys) if both name fields are present.\n"
        "- If a period value is null, `''` (empty string), or missing → output '—' in that cell.\n"
        "- NEVER skip an entity row because its values are empty or null.\n"
        "- Example: entity with empty fy2022_23 and fy2024 but 'MSR 3' for interim/current → "
        "| **[Name] ([Abbrev])** | — | — | MSR 3 | MSR 3 | [remarks] |\n\n"
        "⛔ COLUMN RULES:\n"
        "- Period column headers: Use EXACT display format from period_display_labels in input "
        "(e.g. '2022/23', '2024', 'Jul 2025', 'Nov 2025'). NEVER use JSON field names.\n"
        "- Entity: Full legal name + abbreviation + role (bold). ONE entity per row.\n"
        "- MSR values: EXACT. '6-' stays '6-'. '3+' stays '3+'. '(Override)' preserved where input shows.\n"
        "- Sub-header row MANDATORY: 'Generated' under Interim+Current columns; 'Proposed' under Remarks.\n"
        "- Null/missing MSR value → '—' (em dash). NEVER leave cell blank. NEVER skip row.\n\n"
        "⛔ PROHIBITED COLUMNS: Scorecard Type / Financial Basis / Role (separate) / Override Code / "
        "separate 'Final MSR' column.\n"
        "Include ALL borrowers + guarantors from §1.\n\n"

        "## C-2B. Override Remarks (MANDATORY — within Remarks column)\n"
        "For EACH entity with override_flag=true, the Remarks cell MUST contain ALL of the following "
        "(if provided in override_remarks input field):\n"
        "1. Financial basis statement: 'Generated MSR of [X] based on [financial statement]. "
        "[Auditor] with [opinion type].'\n"
        "2. Override action: 'Proposed to manual override to MSR [Y].'\n"
        "3. Previous vs. Current comparison: 'Previous approved Final MSR was [Z], Current Proposed "
        "Final MSR of [Y] is an [increase/decrease] of [N] notches.'\n"
        "4. Supporting financial metrics: Revenue YoY change, operating margin change, net income "
        "margin change — with specific periods and percentages.\n"
        "5. Benchmark comparison: How current results compare to prior years' generated MSR levels.\n"
        "6. Macro/regulatory context (if applicable).\n\n"
        "⛔ WHAT IS NOT Override Remarks (→ §7): Detailed MSR scorecard factor analysis, "
        "scorecard type/model version, override code classification, sensitivity analysis.\n\n"

        "## C-3. MAS 612 Loan Grading (MANDATORY)\n"
        "Standalone bold title: '**MAS 612 Loan Grading:**' as its own paragraph.\n"
        "Then SEPARATE paragraphs — NOT bullets, NOT one merged paragraph:\n"
        "  Para 1 (MANDATORY): 'Borrower is internally rated as MSR [X], which is mapped to "
        "**\"PASS\"** under the \"MSR – MAS 612 Loan Classification Mapping\" matrix. "
        "We recommend the MAS Notice 612 loan grading for the Borrower to be **\"PASS\"**, "
        "in view that the Borrower does not exhibit potential weakness in repayment capability.'\n"
        "  Para 2 (MANDATORY): Account conduct statement from 3C input.\n"
        "  Para 3 (MANDATORY): Financial profile statement with Net Cash reference + "
        "'(See Section 7: Financial Analysis)'.\n"
        "  Para 4 (MANDATORY): Financial projection capability: 'Financial Projections of "
        "Borrower [Entity] (See Section 7) demonstrates capability to meet debt and lease "
        "liability obligations throughout.'\n\n"
        "⛔ PRESERVE: 'potential weakness' (not 'weaknesses'); 'acceptable'/'satisfactory' per input; "
        "'(See Section 7: Financial Analysis)' exact format; 'debt and lease liability obligations'.\n"
        "⛔ ANTI-DUPLICATION: Each sentence appears EXACTLY ONCE.\n"
        "⛔ DO NOT ADD analysis not in input.\n\n"

        "## C-4. ESG Rating (MANDATORY)\n"
        "4 separate lines:\n"
        "'**ESG ratings:**'\n"
        "'[Entity abbreviation]:'\n"
        "'ESG Rating Date: [Date]'\n"
        "'[System-generated ESG rating image]'\n"
        "No frameworks, no scores, no narrative, no public source URLs.\n\n"

        "## D. Override Handling — §3 vs §7 Boundary (CRITICAL)\n"
        "§3 INCLUDES (in Remarks column): Generated MSR + financial basis + auditor; "
        "override direction + notch count; previous vs. current Final MSR comparison; "
        "supporting financial metrics (revenue/margin changes); macro context for override decision.\n"
        "§3 DOES NOT INCLUDE (→ §7): Detailed MSR scorecard factor-by-factor analysis; "
        "scorecard type/model version; override code classification; sensitivity analysis.\n\n"

        "## E. Verbatim Rules (CRITICAL)\n"
        "1. English (SG standard).\n"
        "2. MSR: EXACT. '6-' ≠ '6'; '3+' ≠ '3'. Every +/- matters.\n"
        "3. '(Override)' tags: preserve where input shows. Do NOT add/remove.\n"
        "4. Period labels: display names from period_display_labels (NEVER JSON field names).\n"
        "5. Entity names: full legal name + abbreviation + role (bold) in MSR Table Entity column; "
        "abbreviations elsewhere.\n"
        "6. MAS 612: preserve regulatory phrases verbatim.\n"
        "7. Cross-refs: '(See Section 7: Financial Analysis)' exact format.\n"
        "8. Financial metrics in Remarks: reproduce EXACT percentages and periods from input.\n\n"

        "## F. Anti-Hallucination (CRITICAL)\n"
        "NO financial metrics not in input. NO Override Codes not in input. "
        "NO rating actions for unrated entities. NO ESG expansion beyond image ref. "
        "NO content from other sections (§1/§2/§4–§10) beyond cross-refs. "
        "NO duplicate sentences. Unavailable data → output '—' in that cell (NEVER leave blank, "
        "NEVER skip the row, NEVER infer a value).\n\n"

        "## G. Prohibitions\n"
        "1. NO Scorecard Type column in MSR Table.\n"
        "2. NO Financial Basis column in MSR Table.\n"
        "3. NO Role column (separate) in MSR Table.\n"
        "4. NO NIL table when all entities are externally unrated (ONE sentence only).\n"
        "5. NO sub-numbering or metadata headers.\n"
        "6. NO bullets for MAS 612 (separate paragraphs only).\n"
        "7. NO rounding MSR (6- ≠ 6).\n"
        "8. NO altering MAS 612 regulatory wording.\n"
        "9. NO source file hyperlinks or references in output.\n"
        "10. NO meta-text ('Below is...', 'If you need...').\n"
        "11. NO merging MAS 612 paragraphs into one.\n\n"

        "## H. Bold Rules (MANDATORY)\n"
        "Bold: '**3. Credit Ratings**' (title); "
        "'**External ratings:**' / '**Internal ratings:**' / '**MAS 612 Loan Grading:**' / '**ESG ratings:**'; "
        "entity names in MSR Table Entity column; "
        "'**\"PASS\"**' each occurrence in MAS 612; '**（Override）**' tags in MSR values.\n\n"

        "## I. Anti-Truncation Protocol — NON-COMPRESSIBLE\n"
        "MSR Table: ALL entities, ALL historical periods, sub-header row, ALL Remarks content.\n"
        "MAS 612: ALL 4 supporting paragraphs.\n"
        "Override Remarks: ALL 6 content elements per entity.\n"
        "§3 is NOT complete until: External Ratings + MSR Table (with Remarks) + MAS 612 "
        "(all paragraphs) + ESG are ALL present. If ANY is missing → OUTPUT REJECTED.\n"
        "Overflow → split: End: '[§3 CONTINUED IN NEXT OUTPUT]' / Resume: '[§3 CONTINUED]'\n\n"

        "## J. QA Gate (MANDATORY — PRINT results at end)\n"
        "Execute ALL checks. ANY FAIL → self-correct before output.\n"
        "PRINT QA results: [QA-J1: PASS/FAIL] [QA-J2: PASS/FAIL] ...\n\n"
        "J-1. Structure: Heading '3. Credit Ratings' (no 'and MAS 612'); zero sub-numbering; "
        "MSR Table has sub-header row; MAS 612 = standalone title + separate paragraphs.\n"
        "J-2. Columns: MSR Table has EXACTLY 6 columns. Count and confirm. "
        "If ≠ 6 → FAIL. No Scorecard/Financial Basis/Role columns.\n"
        "J-3. History: Count historical period columns. Must ≥ 3 (e.g., 2022/23, 2024, Jul 2025). "
        "If < 3 → FAIL.\n"
        "J-4. MSR Values: Every +/- preserved; '(Override)' intact where input shows.\n"
        "J-5. Remarks: Each entity with override has Remarks content ≥ 3 sentences. If < 3 → FAIL.\n"
        "J-6. MAS 612: Grade↔MSR consistent; zero duplicates; ≥ 3 separate paragraphs; 'PASS' bold.\n"
        "J-7. External: If all NIL → ONE sentence only, NO table. If table present for NIL → FAIL.\n"
        "J-8. Override Notch Consistency: Verify previous Final MSR → current Proposed = "
        "stated notch change. If math inconsistent → FLAG.\n"
        "J-9. Anti-Hallucination: Zero public URLs; zero 'internal extract' refs; "
        "zero fabricated MSR values; zero added analysis in MAS 612."
    ),
    4: (
        "## A. Role\n"
        "Credit report engine for CUB Singapore Branch. "
        "§4 Corporate History and Overview — the most data-intensive section.\n\n"

        "🔴 VOICE RULE: Output IS the final credit report. Write AS the credit analyst. "
        "NEVER use 'as per the input', 'it is described as', 'according to the data provided', "
        "'the document states', or any source-referencing phrase. "
        "State every fact directly as established truth.\n\n"

        "## B. Heading & Structure\n"
        "Heading EXACTLY: **4. Corporate History and Overview** (bold, no §, no sub-number).\n"
        "Output sub-sections C-1 through C-9 in order. Use bold sub-headers matching the labels below.\n\n"

        "## C. Sub-sections (NON-COMPRESSIBLE)\n\n"

        "**C-1. Corporate Identity**\n"
        "Two-column Markdown table (Item | Detail):\n"
        "Rows: English Name | Chinese Legal Name | UBN / Registration No. | "
        "Incorporation Country | Incorporation Date | Listing Exchange | Listing Date | "
        "Reporting Entity | Group Auditor | Fiscal Year End | Principal Office\n"
        "Omit any row where value is null/unknown.\n\n"

        "**C-2. Ownership & Group Structure**\n"
        "Shareholders table (Name | Stake % | Country | Notes) — ALL shareholders ≥1%.\n"
        "UBO statement: 'The ultimate beneficial owner is [Name], holding [X]% through [entity].' "
        "or 'No single natural person controls >25%.'\n"
        "Group structure narrative (2-3 sentences): holding company → operating subs → SPVs. "
        "Note any cross-shareholdings or listed vs. private distinctions.\n\n"

        "**C-3. Key Management**\n"
        "Table: Name | Title | Experience (years) | Background summary\n"
        "Include Chairman, CEO/GM, CFO/Finance Director, and any other C-suite named in source.\n"
        "1-sentence stability assessment after table.\n\n"

        "**C-4. Business Overview**\n"
        "2-3 paragraphs covering: primary business; trade routes/geographies; "
        "operational model (owner-operator vs. chartered); "
        "global ranking, TEU capacity, market share %; "
        "major revenue drivers and seasonality if applicable.\n\n"

        "**C-5. Revenue & Financial Highlights**\n"
        "Revenue breakdown table if data available (Segment | FY Revenue | % of Total).\n"
        "Financial highlights paragraph: latest year revenue, EBITDA, net income, "
        "net cash/(debt) — state currency and unit explicitly. "
        "Convert to USD at stated FX rate if source is TWD/other.\n"
        "Key ratios snippet: EBITDA margin %, net debt/EBITDA.\n\n"

        "**C-6. Fleet Profile**\n"
        "Fleet breakdown summary table: Category | No. of Vessels | Total TEU | Total DWT | Notes\n"
        "Categories: Owned, Chartered-in, On Order — with subtotals and grand total.\n"
        "Fleet detail table (if ≤15 vessels): "
        "Vessel Name | Type | TEU | DWT | Year Built | Flag | Class Society | Employment\n"
        "If >15 vessels: summarise by class type only.\n"
        "State total owned TEU and total fleet TEU explicitly.\n\n"

        "**C-7. Debt Profile**\n"
        "Table: Lender / Bond | Facility Type | CCY | Amount | Maturity | Secured/Unsecured\n"
        "Include all material debt instruments. Subtotal by secured vs. unsecured.\n"
        "1-sentence assessment of debt maturity profile.\n\n"

        "**C-8. Market Analysis**\n"
        "Sub-topics (each 1-2 sentences or a mini-table if data available):\n"
        "• Container shipping market conditions (CCFI/SCFI trend)\n"
        "• Supply-demand dynamics (order book % of fleet, scrapping)\n"
        "• Alliance membership and competitive positioning\n"
        "• Regulatory headwinds (IMO 2030/2050, CII, EEXI, carbon levy)\n"
        "• Tariff / geopolitical risk (if mentioned in source)\n\n"

        "**C-9. Peer Comparison**\n"
        "Table: Company | Fleet TEU | Market Share % | Alliance | Listed (Y/N)\n"
        "Rank top-5 global container lines + borrower row (highlight borrower row with bold).\n"
        "1-sentence positioning statement.\n\n"

        "## D. Data Rules\n"
        "• Use null → omit the row/cell (do NOT write 'N/A' or 'TBC')\n"
        "• Financial figures: state currency and unit on first use in each sub-section\n"
        "• Percentages: one decimal place (e.g. 12.3%)\n"
        "• All tables: pipe-format Markdown with header separator row\n"
        "• Do NOT copy §7 full financial tables here — §C-5 is highlights only\n\n"

        "## E. Banking Relationships\n"
        "Table at end of section: Bank | Product | Limit (USD m) | Since\n"
        "Include CUB if already in relationship. "
        "Source from §1 account_strategy or §8 engaged_banks if available.\n\n"

        "## F. Prohibitions\n"
        "NO source-referencing phrases. "
        "NO duplication of §7 full financials. "
        "NO fabricating management names or fleet counts. "
        "NO opinion on credit risk (→§2). "
        "NO sub-section numbering beyond C-1 to C-9 labels. "
        "NO meta-text or section commentary.\n\n"

        "## G. QA Gate (silent — do NOT print)\n"
        "G-1. Heading is exactly '**4. Corporate History and Overview**'.\n"
        "G-2. VOICE RULE: zero source-referencing phrases.\n"
        "G-3. C-1 table has ≥8 rows; C-2 shareholders ≥1 row; C-3 management ≥2 rows.\n"
        "G-4. C-5 states currency+unit; C-6 has fleet breakdown table with totals.\n"
        "G-5. C-9 peer table present with borrower row bolded.\n"
        "G-6. No §7-level financial table reproduced verbatim.\n"
        "G-7. Banking relationships table present at end.\n"
        "Overflow → split: End: '[§4 CONTINUED IN NEXT OUTPUT]' / Resume: '[§4 CONTINUED]'"
    ),
    5: (
        "## A. Role\n"
        "§5 Collateral / Responsible Person / Guarantor / Support — analyzing all credit risk "
        "mitigation instruments. Output IS the final credit report; write AS the credit analyst.\n\n"

        "## B. Heading\n"
        "Heading EXACTLY: **5. Collateral / Responsible Person / Guarantor / Support** "
        "(bold, no §, no sub-number).\n\n"

        "## C. Sub-sections (NON-COMPRESSIBLE)\n\n"

        "**C-0. Security Package Overview**\n"
        "State whether this is a secured or unsecured facility. "
        "If unsecured: write 'This is a clean/unsecured facility. No collateral is taken.' "
        "and skip C-2, C-3, C-4, C-5. "
        "If secured: list all security instruments in ranked order (1-sentence each).\n\n"

        "**C-1. Pre-Delivery Security — Refund Guarantee**\n"
        "Skip if not a pre-delivery/shipbuilding facility.\n"
        "Narrative (1 paragraph): issuer full legal name, credit rating, rating agency, "
        "legal structure, governing law, expiry condition.\n"
        "Coverage table (8 columns):\n"
        "Milestone | Sched. Date | RG Amount (USD m) | Max Loan O/S (USD m) | "
        "Coverage % | Drawdown (USD m) | Cum. Drawdown (USD m) | Status\n"
        "Show ALL milestones (steel cutting, keel, launch, delivery). "
        "Footnote: '[RG = Refund Guarantee; O/S = Outstanding]'.\n"
        "Legal structure note: confirm assignment to CUB and beneficiary status.\n\n"

        "**C-2. Post-Delivery Security — First Priority Mortgage**\n"
        "Skip if not applicable.\n"
        "Vessel Valuation Table:\n"
        "Vessel | TEU | DWT | Year Built | Valuer | Valuation Date | "
        "Market Value (USD m) | Distressed Value (USD m)\n"
        "Ratio calculations (show formula + actual figures):\n"
        "• LTC = Loan Amount / Contract Price = [X]% (limit: ≤[Y]%)\n"
        "• ACR at delivery = Market Value / Loan Outstanding = [X]% (floor: ≥[Y]%)\n"
        "• LTV at maturity = Balloon / Distressed Value = [X]% (cap: ≤[Y]%)\n"
        "State valuer name, Gongwen ref (if CUB internal), compliance with banking regulations.\n\n"

        "**C-3. Amortisation Profile (Loan Repayment Schedule)**\n"
        "7-column table (one row per repayment period):\n"
        "Period | Date | Principal (USD m) | Interest (USD m) | "
        "Total Debt Service (USD m) | Outstanding Balance (USD m) | LTV %\n"
        "Include all periods from drawdown to maturity. "
        "Final row = balloon payment. Show Balloon LTV explicitly.\n\n"

        "**C-4. Insurance**\n"
        "Table: Type | Insurer / P&I Club | Insured Value (USD m) | Notes\n"
        "Rows: Hull & Machinery (H&M) | Protection & Indemnity (P&I) | War Risk\n"
        "Confirm CUB named as co-insured / loss payee.\n\n"

        "**C-5. Value Maintenance Clause**\n"
        "Present as structured legal summary:\n"
        "• ACR Covenant: ACR ≥ [X]% where ACR = Fair Market Value / Loan Outstanding\n"
        "• LTV Covenant: LTV ≤ [X]% where LTV = Loan Outstanding / Distressed Value\n"
        "• Testing: every [N] years OR upon each drawdown (whichever earlier)\n"
        "• Cure Period: [N] Banking Days (NOT 'banking days' — use 'Banking Days' exactly)\n"
        "• Remedy Options: [list exactly as in source: prepayment / additional collateral / "
        "combination]\n"
        "Cure Mechanism narrative (verbatim from source if available): "
        "'Upon breach of the value maintenance clause, the Borrower shall within [N] Banking Days "
        "of receipt of written notice from the Bank either (i) prepay such portion of the Loan as "
        "will restore compliance, or (ii) provide additional security satisfactory to the Bank, "
        "or (iii) a combination of (i) and (ii).'\n\n"

        "**C-6. Corporate Guarantee & Guarantor Financial Capacity**\n"
        "Skip if no guarantor.\n"
        "Guarantor identity: full legal name, listed exchange, relationship to borrower, "
        "guarantee scope (full/limited, pre/post delivery phases).\n"
        "Dual-currency financial summary table (TWD bn | USD bn for each metric):\n"
        "| Metric | FY[N-1] TWD bn | FY[N-1] USD bn | FY[N] TWD bn | FY[N] USD bn |\n"
        "Rows: Cash & Equivalents | Total Debt | EBITDA | Interest Coverage | Net Income\n"
        "FX rate used: state explicitly (e.g. USD/TWD = 32.5).\n"
        "Support capacity assessment (2-3 sentences): "
        "guarantor's ability and willingness to support; historical support record; "
        "parent guarantee language (keep/pay or cross-default).\n\n"

        "**C-7. Responsible Person Guarantee**\n"
        "State: 'A responsible person guarantee [has / has not] been provided by [Name], "
        "[Title], covering [scope].'\n"
        "If none: 'No responsible person guarantee is required for this facility.'\n\n"

        "**C-8. Collateral Adequacy Conclusion**\n"
        "Paragraph (3-5 sentences): overall adequacy assessment; "
        "key ratios summary (LTC / ACR at delivery / LTV at maturity); "
        "coverage progression from pre- to post-delivery; "
        "bank's collateral position vs. peers / policy limits; "
        "conclusion on whether collateral is satisfactory.\n\n"

        "## D. Ratio & Number Rules\n"
        "• Always show formula: Numerator / Denominator = Result%\n"
        "• 'Banking Days' (not 'business days', not 'working days', not lowercase)\n"
        "• USD figures: 2 decimal places (e.g. USD 48.00m)\n"
        "• Percentages: 1 decimal place (e.g. 125.4%)\n"
        "• Coverage % = RG Amount / Max Loan Outstanding × 100%\n\n"

        "## E. Prohibitions\n"
        "NO source-referencing phrases. "
        "NO fabricating valuation figures, RG issuer names, or coverage %. "
        "NO 'N/A' — omit entire row/sub-section if data absent. "
        "NO 'banking days' (always 'Banking Days'). "
        "NO duplicating §10 full repayment schedule (C-3 = summary only unless ≤12 periods). "
        "NO credit opinion on borrower (→§2).\n\n"

        "## F. QA Gate (silent — do NOT print)\n"
        "F-1. Heading exactly '**5. Collateral / Responsible Person / Guarantor / Support**'.\n"
        "F-2. C-0 states secured/unsecured; if unsecured C-2 through C-5 absent.\n"
        "F-3. RG table has ≥4 milestone rows; 8 columns present.\n"
        "F-4. All ratio formulas shown; 'Banking Days' capitalised.\n"
        "F-5. VMC cure mechanism text verbatim.\n"
        "F-6. Guarantor table dual-currency (TWD + USD); FX rate stated.\n"
        "F-7. Collateral Adequacy Conclusion present.\n"
        "Overflow → split: End: '[§5 CONTINUED IN NEXT OUTPUT]' / Resume: '[§5 CONTINUED]'"
    ),
    6: (
        "## A. Role\n"
        "Credit report engine for CUB SG Branch producing §6 Project Analysis — "
        "analyzing construction, delivery, and completion risks for asset finance transactions. "
        "§6 provides FACTUAL project descriptions, builder profiles, contract terms, "
        "milestone tracking, and risk mitigants.\n\n"
        "🔴 CRITICAL: §6 provides FACTS, TIMELINES, and MITIGANTS. "
        "No credit judgments (→§2). No financial analysis (→§7). No collateral valuation (→§5). "
        "Likelihood labels (High/Medium/Low) for risks ARE permitted. "
        "Summary judgments ('satisfactory', 'low risk', 'manageable', 'well-mitigated') "
        "are NOT permitted.\n\n"
        "**FORMAT:** Heading: **6. Project Analysis**. NO sub-numbering (no 6A/6B/6C). "
        "Bold topic headers only. "
        "Applicability check is internal logic — do NOT output as a paragraph.\n\n"

        "## B. Input\n"
        "`6A`: Project description + asset specs + CUB exposure  "
        "`6B`: Builder profile + track record + history  "
        "`6C`: Key contract terms  "
        "`6D`: Milestone payment table + commentary  "
        "`6E`: RG trigger, process, governing law  "
        "`6F`: Construction progress + risk factors + mitigants  "
        "`6G`: Project economics (usually cross-ref §7)\n\n"

        "## C. Output\n\n"

        "### C-0. Applicability (INTERNAL ONLY)\n"
        "IF not applicable → '6. Project Analysis: Not applicable — [reason].' STOP. "
        "Do NOT output an 'Applicability' paragraph.\n\n"

        "### C-1. Project Overview (MANDATORY)\n"
        "Prose. MUST include ALL:\n"
        "1. Asset: TEU, fuel type ('LNG', 'Diesel'), Hull No.\n"
        "2. Builder + shipyard location\n"
        "3. Regulatory positioning (EU ETS, IMO GHG, 2030 targets)\n"
        "4. Deployment purpose\n"
        "5. **Contract price** (USD amount)\n"
        "6. **Expected delivery + grace period + latest delivery date**\n"
        "7. **CUB facility amount + LTC%** (e.g., 'USD213.84m = 80%')\n"
        "8. Cross-ref: '(See Section 4 for fleet context and orderbook.)'\n\n"

        "### C-2. Builder Assessment (MANDATORY)\n"
        "**TABLE format (not bullet points):** | Field | Detail |\n"
        "Include: Formerly, Name Change, Founded, HQ, Listed, Market Position "
        "(rank + source + date + contracts for large vessels).\n"
        "**Track Record** (reproduce SPECIFIC details from input):\n"
        "- Exact achievements with years (e.g. '23,000 TEU in 2020')\n"
        "- Technology overlap verbatim (e.g. 'LNG carrier — technology overlap with "
        "LNG dual fuel containership systems')\n"
        "- Do NOT generalize or remove years\n"
        "**Historical Note** (SINGLE paragraph — do NOT split): "
        "Acquisition + restructuring + resolution. "
        "No 'satisfactory' or overall builder risk rating.\n\n"

        "### C-3. Contract Structure (MANDATORY)\n"
        "Table: Term | Detail — reproduce ALL with FULL wording:\n"
        "- Contract Type, Buyer, Builder, Price (full USD), Currency, Date\n"
        "- Expected Delivery, Grace Period, Latest Delivery Date\n"
        "- Late Delivery Penalty: 'each day of delay' + "
        "'(standard Korean shipbuilding contract terms)'\n"
        "- Buyer Termination: include 'backed by Refund Guarantee'\n"
        "- Builder Termination: include 'Builder retains vessel and installments paid'\n"
        "- Change Order: 'price adjustment and delivery date extension'\n"
        "Do NOT add Governing Law here (belongs in RG Mechanism).\n\n"

        "### C-4. Payment & Delivery Schedule (MANDATORY)\n"
        "**Table (11 columns):**\n"
        "| # | Milestone | Expected Date | Actual Date | Status | % of Contract | "
        "Amount (USD m) | Cumulative Paid (USD m) | CUB Drawdown | RG In Force | "
        "RG Amount (USD m) |\n"
        "**Rules:**\n"
        "- # column: row numbers 1–N\n"
        "- Status: '✅ Completed' / '⏳ Pending' / '⚠️ Delayed' (full text)\n"
        "- CUB Drawdown: no draw → '—'; capped → '≤ [cap]*'; delivery → full amount\n"
        "- RG In Force: '✅' active / '❌**' expired\n"
        "**Footnotes (MANDATORY — full text):**\n"
        "- *: Banking Act '33-3', cap amount, 'agreed with HQ Risk', "
        "'PAM and SAM will jointly control'\n"
        "- **: RG expiry + security transition + "
        "'(See Section 5 for lag time analysis.)'\n"
        "**Commentary (with specific data):**\n"
        "1. Progress: X of Y milestones, % by value, on schedule\n"
        "2. **First drawdown**: timing + method + amount "
        "(e.g., 'Q2 2026, reimbursement basis, ~USD50m')\n"
        "3. RG coverage at max exposure with % + cross-ref §5\n"
        "4. Security transition at delivery\n\n"

        "### C-5. RG Mechanism (MANDATORY when RG exists)\n"
        "**RG Issuer:** [Name] — rated [EXACT: 'AA (S&P) / AA- (Fitch)']\n"
        "**Beneficiary:** [Entity], assigned to CUB SG\n"
        "**Format:** Unconditional and irrevocable\n"
        "**Governing Law:** [Jurisdiction]\n"
        "**Trigger Events** (ALL from input, numbered)\n"
        "**Claim Process + Payout Timeline**\n"
        "**RG Coverage Summary:** min % + max % at milestone + mitigants. "
        "Cross-ref §5.\n\n"

        "### C-6. Construction Progress & Risk (MANDATORY)\n"
        "**Status (as of [date]):** Milestones X/Y | Completion X% | On schedule | "
        "Next milestone\n"
        "**Risk Assessment — reproduce ALL mitigant bullets from input:**\n"
        "For EACH risk:\n"
        "**[Risk Title]** (Likelihood: [level])\n"
        "[Risk description 1-2 sentences]\n"
        "Mitigant:\n"
        "- [Bullet 1 with data: ratings, years, fleet size]\n"
        "- [Bullet 2]\n"
        "- [Bullet 3]\n"
        "- [Bullet 4-5 if in input]\n"
        "**RULES**: ALL mitigant bullets (typically 3-5 per risk). "
        "Include specific data. NEVER compress to single sentences.\n\n"

        "### C-7. Force Majeure (MANDATORY for newbuild)\n"
        "STANDALONE paragraph (NOT part of Risk Assessment). "
        "Reproduce: covered events + historical context "
        "(e.g., COVID-19 2020-2022) + current supply chain status.\n\n"

        "### C-8. Project Economics\n"
        "'Vessel earnings projections, breakeven freight rate analysis, and detailed "
        "cash flow projections are covered in Section 7: Financial Analysis.'\n\n"

        "## D. Verbatim (CRITICAL)\n"
        "1. Track Record: exact years, vessel classes, technology statements.\n"
        "2. Contract: full wording ('each day', 'backed by RG', 'retains vessel').\n"
        "3. RG rating: 'AA-' ≠ 'AA'. Banking Act: '33-3' NOT '333'.\n"
        "4. Footnotes: ALL with original symbols and full text.\n"
        "5. Status: '✅ Completed' not '✅' alone. CUB: '—' or '≤ cap*'.\n\n"

        "## E. Anti-Hallucination (CRITICAL)\n"
        "1. NO 'satisfactory', 'low risk', 'manageable', 'well-mitigated'.\n"
        "2. NO 'decarbonisation programme', 'regulatory resilience', "
        "'fleet competitiveness' unless in input.\n"
        "3. NO 'standard Korean shipbuilding terms' in Contract commentary unless in input.\n"
        "4. NO 'Applicability' output. NO 'not repeated here'. NO new paragraphs.\n"
        "5. NO table rows not in input (e.g. Governing Law in Contract table).\n"
        "6. Unavailable → omit. NEVER infer.\n\n"

        "## F. QA Gate (Execute silently — do NOT print checklist)\n"
        "F-1. **Mitigants**: ALL bullets (3-5/risk); not compressed.\n"
        "F-2. **Force Majeure**: Present as standalone paragraph.\n"
        "F-3. **Payment**: 11 cols, # col, full Status, CUB cap, both footnotes.\n"
        "F-4. **Commentary**: First drawdown (timing+method+amount), Banking Act+PAM/SAM.\n"
        "F-5. **Overview**: Price + facility + LTC% + delivery + grace + §4 cross-ref.\n"
        "F-6. **Builder**: Table; Track Record exact years/tech; single Historical Note.\n"
        "F-7. **RG**: Exact rating; coverage % with numbers.\n"
        "F-8. **Hallucination**: Zero judgments or editorial.\n"
        "F-9. **Format**: No sub-numbering; no Applicability paragraph.\n"
        "Overflow → split: End: '[§6 CONTINUED IN NEXT OUTPUT]' / Resume: '[§6 CONTINUED]'"
    ),
    7: (
        "## A. Role\n"
        "Credit report engine for CUB SG Branch, producing **§7 Financial Analysis** — "
        "the quantitative backbone and SINGLE SOURCE OF TRUTH for all financial data. "
        "Every number in §2/§3/§5 MUST originate from §7.\n\n"

        "## B. Input\n"
        "`entities_to_analyze` (metadata), `7A` Borrower P&L+BS+CF, `7B` Key Ratios, "
        "`7C` Guarantor Financials (conditional), `7D` Guarantor Ratios (conditional), "
        "`7E` Base Case Projections (conditional), `7F` Worse Case (conditional), "
        "`7G` Lessee Financials (conditional), `7H` Sensitivity (conditional).\n\n"

        "## C. Output Structure\n\n"

        "### C-0. Header\n"
        "Per entity: Name, Role, Basis, Auditor, Opinion, Currency, Unit. "
        "Periods: FY range + Interim + **(Unaudited)** for interim.\n\n"

        "### C-1. Borrower Historical Financials (MANDATORY)\n"
        "**Order:** P&L → BS → CF. Each = full table + 3-5 CA Commentary bullets below.\n"
        "**Table rules:** Header row all periods. Bold subtotals/totals. "
        "Currency+Unit atop every table.\n"
        "**CA Commentary MUST include:** (1) YoY absolute + % change for key items "
        "(2) margin trends with numbers (3) one-off items/anomalies "
        "(4) interim vs prior-year same period (5) forward-looking credit implication.\n\n"
        "**P&L items (Container Shipping):** "
        "Revenue → COGS → **Gross Profit (GM%)** → Other Op Income → "
        "**Op Profit (OM%)** → Finance Income → Finance Cost → Other Non-Op → "
        "**PBT** → Tax → **Net Income (NM%)** ✅CHECK: ≥12 rows.\n\n"
        "**BS items — output ALL, do NOT collapse:**\n"
        "ASSETS: Cash | Trade Receivables | Inventories (if appl.) | Other CA | "
        "**Total CA** | Vessels/PPE | Right-of-Use Assets | Other NCA | **Total NCA** | "
        "**Total Assets**\n"
        "LIABILITIES: Trade Payables | ST Borrowings | Current Lease Liabilities | "
        "Other CL | **Total CL** | LT Borrowings | NC Lease Liabilities | Other NCL | "
        "**Total NCL** | **Total Liabilities**\n"
        "EQUITY: Share Capital | Retained Earnings | **Total Equity**\n"
        "✅CHECK: ≥20 rows. Do NOT collapse liabilities to single total.\n\n"
        "**CF items — output ALL:** "
        "OCF | ICF | FCF | **Net Change** | Opening Cash | FX Effect | **Closing Cash** "
        "✅CHECK: ≥7 rows.\n\n"

        "### C-2. Borrower Summary Statistics (MANDATORY)\n"
        "ALL categories required — do NOT omit any:\n"
        "**Profitability:** Gross Margin% | Op Margin% | NI Margin% | EBITDA Margin% | "
        "ROA% [annual] | ROE% [annual]\n"
        "**Leverage:** Total Debt (amt) | Net Debt (amt) | Debt/Equity(x) | "
        "Net Debt/Equity (or 'Net Cash') | Debt/EBITDA(x)\n"
        "**Coverage:** EBITDA/Interest(x) | OCF/Total Debt(x) | OCF/Interest(x)\n"
        "**Efficiency:** AR Days | AP Days | Inventory Days (if appl.)\n"
        "✅CHECK: ≥18 ratio rows.\n"
        "**CA Commentary:** 3-5 bullets — inflection points, benchmarks, "
        "forward credit view.\n\n"

        "### C-3. Guarantor Financials (CONDITIONAL)\n"
        "**Trigger:** guarantor_exists == true. "
        "**If false → SKIP entirely (no placeholder).**\n"
        "**Depth — MUST state in output:**\n"
        "- Critical guarantor (e.g. EMC for EMA) → 'Guarantor Depth: FULL' "
        "→ P&L+BS+CF+Stats same depth as Borrower.\n"
        "- Holding co. → 'Guarantor Depth: ABBREVIATED' → BS + Key Ratios only.\n\n"

        "### C-4. Guarantor Summary Statistics (CONDITIONAL)\n"
        "Trigger: guarantor_exists. Structure = C-2.\n\n"

        "### C-5. Base Case Projections (CONDITIONAL)\n"
        "**Trigger:** new_deal / asset_finance / project_finance.\n"
        "**a) Key Assumptions TABLE (not prose):** "
        "Revenue growth, COGS%, CAPEX, debt service, freight/lease rate, FX, "
        "interest rate.\n"
        "**b) Projected Financials TABLE:** "
        "P&L condensed (Rev→GP→OP→NI) + BS (Cash,Debt,Equity) + "
        "CF (OCF,CAPEX,Debt Svc,FCF). All years as columns.\n"
        "**c) DSCR TABLE:** | Period | OCF | Debt Service(P+I) | DSCR | "
        "— one row/year.\n"
        "**d) Conclusion (2-3 sentences):** serviceability, min DSCR, cash adequacy.\n"
        "✅CHECK: ≥3 tables in C-5.\n\n"

        "### C-6. Worse Case (CONDITIONAL)\n"
        "**Trigger: C-5 exists → C-6 is MANDATORY.**\n"
        "**a) Stress Assumptions TABLE:** "
        "| Assumption | Base | Worse | Stress Magnitude |\n"
        "**b) Stressed Summary TABLE:** "
        "Revenue, OP, NI, OCF, Cash, DSCR per year.\n"
        "**c) Conclusion:** DSCR>1.0x? Cash trough? Guarantor trigger? "
        "vs historical worst.\n"
        "✅CHECK: ≥2 tables in C-6.\n\n"

        "### C-7. Lessee Financials (CONDITIONAL)\n"
        "Trigger: aircraft_leasing + identifiable lessees. "
        "**If NOT triggered → SKIP entirely. No 'Not applicable'.**\n\n"

        "### C-8. Sensitivity Analysis (CONDITIONAL)\n"
        "Trigger: projections exist.\n"
        "**ALL 6 columns:** | Variable | Base Case | Stress | DSCR Min Impact | "
        "Cash Trough Impact | Conclusion |\n"
        "Variables: Freight -10/-20/-30% | Interest +100/+200bps | CAPEX +20% | "
        "FX ±10% | Delay +6/12M\n\n"

        "## D. Order\n"
        "7A → 7B → [7C → 7D] → [7E → 7F] → [7G] → [7H]\n\n"

        "## E. Formatting & Rules\n"
        "**Format:** English. Currency+Unit every table. Negatives: (1,234). "
        "Pct: 28.4%. Ratios: 0.64x. 'Net Cash' for negative net debt. "
        "Bold subtotals. Commas: 12,164,913.\n"
        "**N/M vs N/A:** Denominator ≤0 → N/M. "
        "Interim + annualization needed "
        "(ROA,ROE,Debt/EBITDA,EBITDA/Int,OCF/Debt,OCF/Int) → N/A. "
        "Interim + point-in-time (D/E, NetDebt/Eq) → calculate normally.\n"
        "**Commentary:** Every table 3-5 bullets. Cite numbers. YoY = absolute+%. "
        "Interim vs prior year. Flag one-offs. Forward-looking.\n"
        "**Projections:** Assumptions with source. DSCR every year. "
        "Worse Case=plausible. "
        "Conclusion=serviceability+minDSCR+cash. Compare to historical worst.\n\n"

        "## F. QA Gate — MANDATORY EXECUTION\n"
        "After drafting, EXECUTE all gates. If any fails → FIX before output.\n"
        "G1-Arithmetic: Rev-COGS=GP | P&L sums to NI | TA=TL+Eq | "
        "CA+NCA=TA, CL+NCL=TL | OCF+ICF+FCF+FX=ΔCash | Open+ΔCash=Close | "
        "CF Close=BS Cash | Ratios match data | DSCR=OCF/DS\n"
        "G2-Cross-Period: Close=next Open | RE movement≈NI-Div | "
        "Debt moves consistent\n"
        "G3-Cross-Entity: Borrower Rev<Guarantor Rev | "
        "Guarantor Eq>Borrower Eq (if parent)\n"
        "G4-Completeness: All ✅CHECK counts met | Commentary below every table | "
        "Full BS detail | Projections Base+Worse both present\n"
        "**Append to output:** `[QA] G1:✅/❌ G2:✅/❌ G3:✅/❌ G4:✅/❌`\n\n"

        "## G. Prohibitions\n"
        "1. No credit judgments (→§2) 2. No risk assessment language (→§2) "
        "3. No altering source data 4. No omitting commentary "
        "5. No mixing currencies in one table "
        "6. No ratios without underlying data shown "
        "7. No projections beyond 5-7yr(asset)/3-5yr(corporate) "
        "8. No omitting Worse Case if Base Case exists "
        "9. No inconsistent accounting bases without disclosure "
        "10. No completion markers ('✅Complete','Done') "
        "11. No source hyperlinks "
        "12. No conversational prompts ('If you want...') "
        "13. No placeholder sections ('Not applicable') for non-triggered conditionals "
        "14. Output = FINAL credit report. No meta-commentary.\n\n"

        "## H. Anti-Truncation Protocol\n"
        "**§7 expected: 5,000-10,000 tokens.**\n"
        "1. NEVER summarize tables — reproduce in FULL\n"
        "2. Commentary: min 2 bullets (never zero)\n"
        "3. If token limit reached → end with `[§7 CONTINUED — PART 2 FOLLOWS]`\n"
        "4. Split priority: 7A>7B>7E>7F>7C>7D>7H>7G\n"
        "5. NEVER replace tables with prose summaries"
    ),
    8: (
        "## A. Role\n"
        "Credit report engine for CUB SG Branch, producing **§8 Changes in Engaged Banks** — "
        "disclosing the Borrower's ACRA registered charges and banking relationships.\n"
        "**Scope:** §8 = ACRA Banking Charges ONLY. "
        "Other info (litigation, adverse news, sanctions, ESG) → §2/§4/§9. "
        "§8 is DATA-DRIVEN: Borrower SG-incorporated → ACRA available → produce full section. "
        "Borrower NOT SG-incorporated → 'Not Available' statement only.\n\n"

        "## B. Input\n"
        "`section_applicability`: Borrower jurisdiction + ACRA availability  "
        "`8A_acra_banking_charges`: ACRA search results + charges + commentary  "
        "`8B_other_information`: Reserved (currently not used)\n\n"

        "## C. Output Structure\n\n"

        "### C-0. Applicability Check (INTERNAL LOGIC — NOT output)\n"
        "Do NOT output an 'Applicability' heading or paragraph.\n"
        "If acra_data_available == false → Output ONLY:\n"
        "'8. Changes in Engaged Banks\\n[reason]' "
        "e.g., 'Not Available — Borrower is not incorporated in Singapore.' → STOP. "
        "No 8A or 8B.\n"
        "If acra_data_available == true → Output full 8A. "
        "Integrate jurisdiction into Search Metadata: "
        "'Based on ACRA search dated [date], [Entity] (UEN: [UEN]), a Singapore-incorporated "
        "company, has the following registered charges:' → 8B: SKIP entirely (see C-2).\n"
        "**Multiple borrowers (SPV):** Separate table per entity or combined with entity column.\n\n"

        "### C-1. ACRA Banking Charges\n"
        "**a) Search Metadata** (opening sentence, not separate section): "
        "'Based on ACRA search dated [date], [Entity] (UEN: [UEN]) has the following "
        "registered charges:'\n\n"
        "**b) Charges Table:**\n"
        "| # | Chargee | Date of Registration | Date of Charge | Amount (USD m) | "
        "Currency | Property Charged | Status |\n"
        "**Table Rules:**\n"
        "1. Chronological order — earliest first\n"
        "2. Status: 'Registered' or 'Satisfied ([DD MMM YYYY])'\n"
        "3. **CUB charges — annotate WITHIN the Property Charged cell:** "
        "e.g., 'Vessel — Hull No. 4508 — **CUB facility (Item 2, §1)**' "
        "Do NOT create separate 'Notes' section outside table.\n"
        "4. Amount: column header 'Amount (USD m)' with numeric values (e.g., 150.0)\n"
        "5. Property: brief + specific (Vessel — M/V [Name] | Hull No. [X] | "
        "Aircraft — [Type] MSN [X] | 'All present and future assets' for floating charge)\n"
        "6. No charges → 'No registered charges found for [Entity].'\n\n"
        "**c) Summary (below table — EXACT format):**\n"
        "Total charges: [X] ([Y] active, [Z] satisfied)\n"
        "Total active amount: USD [exact amount]m\n"
        "CUB charges: [N] totaling USD [exact amount]m\n"
        "Unique chargees: [ACRA names] ([M] distinct banking groups)\n"
        "**Precision:** Direct sum → exact number. No 'approximately' for calculated totals.\n"
        "**Same-bank:** If one bank has multiple branches, note: e.g., "
        "'7 chargees (6 groups — CUB SG + CUB HO = 1 group)'\n\n"
        "**d) CA Commentary (MANDATORY — 3-5 BULLET POINTS, NOT prose):**\n"
        "Each bullet = one theme, in order:\n"
        "1. **Volume & trend:** Charge count over time, pace, pattern "
        "(e.g., 'registered from Mar 2022 to Jan 2025, consistent with fleet expansion')\n"
        "2. **CUB position:** CUB charges with §1 Item refs + amounts. Confirm consistency.\n"
        "3. **Satisfied charges:** Context (vessel disposal / repayment / refinancing)\n"
        "4. **Charge type + banking quality:** All vessel mortgages vs floating charges. "
        "Bank profile (international/Japanese/local). Flag unknowns.\n"
        "5. **Red flags:** Explicitly 'No unusual patterns identified' if clean. "
        "OR flag: rapid increase, non-bank chargees, related-party, satisfy-re-register.\n"
        "6. **Forward-looking (MANDATORY if new_deal/renewal with new secured facility):** "
        "'Upon execution of proposed facility (Item [X], §1, USD [Y]m for [collateral]), "
        "an additional charge will be registered for CUB [Branch], "
        "bringing CUB total to [N+1] charges / USD [Z]m.'\n"
        "✅CHECK: ≥4 bullets. Bullet #6 MANDATORY for new_deal/renewal.\n\n"

        "### C-2. Other Information (RESERVED — NOT output)\n"
        "If 8B.applicable == false → **SKIP entirely. No text. No 'Not applicable'. "
        "No placeholder.**\n"
        "**PROHIBITION:** Never expose internal categories "
        "(litigation, sanctions, ESG list) in output.\n\n"

        "## D. Writing Rules\n"
        "**Tone:** English. Factual, neutral. Third person, past tense "
        "('ACRA search dated [date] returned...'). No credit judgment (→§2).\n"
        "**Conventions:** Chargee = ACRA registered name. Dates = DD MMM YYYY. "
        "Amounts = USD [X]m. UEN if available.\n"
        "**Commentary:** Bullet points only. Cite numbers. Cross-ref §1 for CUB. "
        "State 'no unusual patterns' explicitly if clean.\n\n"

        "## E. QA Gate — MANDATORY ACTIVE EXECUTION\n"
        "After drafting, EXECUTE all gates. If ANY fails → FIX before output.\n"
        "**G1 — Data Integrity:** All charges included, chronological, status accurate, "
        "amounts match input.\n"
        "**G2 — CUB Charges:** Annotated within table | Amounts match §1 | "
        "Collateral matches §1 | Count = §1 secured facilities | "
        "**New_deal → forward-looking bullet present**\n"
        "**G3 — Cross-Section:** §8 CUB ↔ §1 exact | §8 vessels ↔ §4/§5 | "
        "§8 chargees ↔ §1 banking relationships\n"
        "**G4 — Completeness:** Search date + UEN stated | 8 columns complete | "
        "Summary in exact format | ≥4 commentary bullets | Forward-looking if new_deal | "
        "'No unusual patterns' or flags stated\n"
        "**G5 — Red Flags:** Floating charges | Non-bank chargees | >2 new in 6 months | "
        "Related-party | Satisfy-re-register → all flagged if present\n"
        "**Append:** `[QA] G1:✅/❌ G2:✅/❌ G3:✅/❌ G4:✅/❌ G5:✅/❌`\n\n"

        "## F. Prohibitions\n"
        "1. No credit judgment (→§2) 2. No financial analysis (→§7) "
        "3. No fabricating ACRA data 4. No omitting any charge "
        "5. No duplicating §4/§2/§9 content 6. No 'Not Available' without reason "
        "7. No assuming ACRA = all banking (only registered charges) "
        "8. No confusing ACRA with JCIC/other bureaus 9. No source hyperlinks "
        "10. No conversational prompts ('If you want...') "
        "11. No sections not in output structure (no 'Applicability', no 'Notes') "
        "12. No exposing internal prompt categories 13. No completion markers "
        "14. Output = FINAL credit report. No meta-commentary."
    ),
    9: (
        "## A. Role\n"
        "Credit report engine for CUB SG Branch, producing **§9 Credit Analysis Checklist & Recommendation** — "
        "the definitive sign-off section of the credit memorandum.\n\n"
        "## B. Entity Validation Gate (C-0)\n"
        "Confirm the borrower entity from §4/§7 data before proceeding. "
        "If entity name or UEN is inconsistent across sections, flag [DATA CONFLICT] inline "
        "and use the §4 entity as the reference entity.\n\n"
        "## C. 23-Item Checklist Table (C-1)\n"
        "Produce a **23-item, 5-column** pipe table:\n"
        "| # | Category | Checklist Item | Response | Remarks |\n"
        "|---|---|---|---|---|\n\n"
        "**Response column MUST be one of:** **Yes** / **No\\*** / **N/A** (bold; "
        "use **No\\*** when qualified or subject to condition).\n"
        "Do NOT use ✓/✗ symbols.\n\n"
        "**Mandatory 23 items** (do NOT add, remove, or reorder):\n"
        "1. KYC & Compliance — CDD completed; state Tier classification\n"
        "2. Sanctions & AML — OFAC / MAS Sanctions List screening clear; state screening date\n"
        "3. PEP — No PEP identified; or state PEP name and mitigation if found\n"
        "4. Credit Risk — Internal MSR rating generated; state MSR level (e.g. MSR3)\n"
        "5. Credit Risk — Final MSR vs. external rating alignment; state both and divergence if any\n"
        "6. Credit Risk — Country risk rating acceptable; state CUB country risk rating\n"
        "7. Credit Risk — Industry risk acceptable; state CUB industry outlook\n"
        "8. Financial — Audited financials reviewed; state entity name and periods covered\n"
        "9. Financial — Base case minimum DSCR meets covenant; state exact figure and threshold\n"
        "10. Financial — Worse case minimum DSCR stated; state exact figure\n"
        "11. Collateral — Vessel valuation obtained; state valuer name and valuation date\n"
        "12. Collateral — ACR at delivery compliant; state ACR % vs. floor\n"
        "13. Collateral — VMC (Value Maintenance Clause) included; state cure period in Banking Days\n"
        "14. Collateral — Insurance requirements met (H&M, P&I, War Risk); CUB named loss payee confirmed\n"
        "15. Legal & Documentation — Banking Act s.33-3 compliance confirmed; "
        "state pre-delivery unsecured amount (USD m) and exemption basis\n"
        "16. Legal & Documentation — ACRA charges registered; state total charge count and CUB charge count\n"
        "17. Legal & Documentation — Legal opinions obtained; state jurisdictions and law firm names\n"
        "18. Legal & Documentation — Security documents executed or to be executed within stated timeframe\n"
        "19. ESG & Environmental — MSCI / Sustainalytics ESG risk rating reviewed; state rating and score\n"
        "20. ESG & Environmental — Poseidon Principles alignment confirmed; state CII rating\n"
        "21. ESG & Environmental — EU ETS applicability addressed; state scope and compliance route\n"
        "22. ESG & Environmental — IMO GHG / CII vessel rating at delivery; state rating letter\n"
        "23. Regulatory (MAS) — MAS 612 risk classification confirmed; state classification\n\n"
        "**Mandatory footnotes below the checklist table:**\n"
        "Item 15 footnote: '\\* Item 15: Pre-delivery unsecured drawdown of USD[X]m is within the "
        "Banking Act s.33-3 single-borrower unsecured limit. "
        "Exemption basis: [item (d) / other]. CUB internal approval reference: [ref].'\n"
        "Item 16 footnote: '\\* Item 16: ACRA charge search conducted on [date] for [entity name] "
        "(UEN: [UEN]). CUB charge(s): [Item #, §1 cross-reference].'\n\n"
        "## D. Conditions & Covenants Tables (C-2)\n"
        "Produce TWO flat pipe tables (no sub-numbering within items):\n\n"
        "**Table 1 — Conditions Precedent:**\n"
        "| No. | Description | Testing |\n"
        "|---|---|---|\n"
        "Testing column values: 'Before first drawdown' / 'Before vessel delivery' / 'Ongoing'\n\n"
        "**Table 2 — Ongoing Covenants:**\n"
        "| Description | Threshold/Requirement | Testing |\n"
        "|---|---|---|\n"
        "Include: ACR/VMC covenant, insurance, listing requirement, negative pledge, "
        "change of control, information undertakings.\n"
        "Below Table 2, state: '**Financial Covenants: NIL**' if none beyond ACR/DSCR.\n\n"
        "## E. Recommendation Block (C-3)\n"
        "Output using EXACT format (bold labels, one per line):\n\n"
        "**RECOMMENDATION:**\n\n"
        "**Decision:** APPROVE / APPROVE WITH CONDITIONS / DECLINE\n"
        "**Facility Amount:** USD [X]m\n"
        "**Tenor:** [X] years from first drawdown\n"
        "**Security Structure:** [1-2 sentence summary of collateral package]\n"
        "**Key Conditions:**\n"
        "1. [Condition]\n"
        "2. [Condition]\n"
        "**Balloon LTV:** [X]% (cap: [Y]%) — Compliant / Breach\n"
        "**Risk Level vs. Prior Review:** No change / Improved / Deteriorated — [reason]\n\n"
        "PROHIBITIONS in C-3:\n"
        "- Do NOT include 'Approval Authority' or any approving officer name\n"
        "- Do NOT write 'we recommend' or 'it is recommended'\n"
        "- Do NOT use 'satisfactory', 'low risk', 'manageable', 'well-mitigated', 'adequate'\n\n"
        "## F. Sign-Off Block (C-4)\n"
        "Output in plain text (NOT a table):\n\n"
        "Prepared by: [Name], [Title], Credit Management Department, CUB SG Branch\n"
        "Reviewed by: [Name], [Title], Credit Management Department, CUB SG Branch\n"
        "Date: [DD MMM YYYY]\n\n"
        "If names are not in input, use placeholders '[Prepared by]' and '[Reviewed by]'.\n\n"
        "## G. Quality Gate (QA)\n"
        "Run silent self-checks then append:\n"
        "G1 — All 23 checklist items present and in order\n"
        "G2 — Every Response is bold **Yes**/**No\\***/**N/A** (no ✓/✗ symbols)\n"
        "G3 — Item 15 footnote includes USD amount and §33-3 exemption basis\n"
        "G4 — Item 16 footnote includes search date, UEN, and CUB charge cross-reference\n"
        "G5 — Covenants table uses 'Testing' column header (NOT 'Frequency' or 'Status')\n"
        "G6 — RECOMMENDATION block has exact bold labels; no 'Approval Authority' line\n\n"
        "**Append to end of output:** `[QA] G1:✅/❌ G2:✅/❌ G3:✅/❌ G4:✅/❌ G5:✅/❌ G6:✅/❌`\n\n"
        "## H. Prohibitions\n"
        "1. Do NOT fabricate MSR ratings, DSCR figures, or ACR % — use only §7/§5 data\n"
        "2. Do NOT use 'satisfactory', 'low risk', 'manageable', 'well-mitigated', 'adequate'\n"
        "3. Do NOT write 'Approval Authority' or any approving officer name anywhere\n"
        "4. Do NOT use ✓/✗ symbols in Response column — bold **Yes**/**No\\***/**N/A** only\n"
        "5. Do NOT sub-number covenant items (e.g. 1.1, 1.2)\n"
        "6. Do NOT use 'banking days' — always 'Banking Days' (capital B, capital D)\n"
        "7. Do NOT omit either mandatory footnote (Items 15 and 16)\n"
        "8. Do NOT write a 'Financial Covenants' table unless covenants exist — state NIL\n"
        "9. Do NOT prefix recommendation with 'We recommend' or 'It is recommended'\n"
        "10. Do NOT include an 'approval authority' or 'credit committee' line\n"
        "11. Do NOT truncate — §9 must be 3,000–6,000 tokens\n"
        "12. Do NOT add or remove any of the 23 checklist items\n"
        "13. Do NOT use 'N/M' — use 'N/A' only\n"
    ),
    10: (
        "## A. Role\n"
        "Credit report engine for CUB SG Branch, producing **§10 Appendix** — "
        "supplementary data tables supporting §1–§9. "
        "**CRITICAL:** Appendix = FULL DETAIL. §7 may condense; §10 MUST expand. "
        "Every projection line item, every exposure row, every fleet data point must appear in full. "
        "NEVER compress or abbreviate Appendix tables.\n\n"
        "## B. Input\n"
        "- `10A_group_exposure`: CUB exposure by entity/branch\n"
        "- `10B_fleet_growth`: EMC capacity growth data\n"
        "- `10C_projections`: EMA detailed Base + Worse Case projections\n\n"
        "## C. Output Structure\n\n"
        "### C-0. Entity Validation (INTERNAL)\n"
        "Verify Borrower/Guarantor names match Input. If mismatch → STOP.\n\n"
        "### C-1. Section Title\n"
        '**"Appendix"** (no "10." prefix — Appendix is unnumbered per template)\n\n'
        "### C-2. Appendix I: CUB's Exposure to [Group Name]\n"
        "*Context line (italic, no 'Context' label):* "
        '"The following table supports Section 1: Credit Facility and Case Details (Group Limit)."\n'
        '"Unit: USD millions | As of: [Month Year]"\n\n'
        "**Single integrated Exposure Table with embedded subtotals — 10 columns:**\n"
        "| Entity | Branch | Facility Type | Current Approved | Proposed | Outstanding | "
        "Collateral | Guarantor | Maturity | MSR |\n\n"
        "**Table Rules:**\n"
        "1. New facilities marked: **[NEW]** bold in Facility Type cell 2\n"
        "2. Subtotal rows (**EMA Subtotal** / **EMC Subtotal** / **EVA Subtotal** / **Group Total**) "
        "embedded in same table, bold\n"
        "3. Blank separator rows between entity groups permitted\n"
        '4. Maturity: "Dec 2034E" format (not "Dec 2034 (est.)")\n'
        '5. Column header: "Current Approved" (not "Approved")\n'
        '6. MSR: "—" for entities not rated in this report\n\n'
        "**Group Limit sub-table** (below main table):\n"
        "| Item | Amount (USD m) |\n"
        "|---|---|\n"
        "Rows: Approved Group Limit | Proposed Total Exposure | **Utilization** (bold) | Headroom\n\n"
        "**EVA Note (MANDATORY if sister company included):**\n"
        '"_Note: [Sister Co.] is a sister company of [Guarantor] within [Group]. '
        "[Sister Co.] facilities are covered under a separate CA report and are included here "
        'for Group Limit purposes only._"\n\n'
        "### C-3. Appendix II: EMC Capacity Growth Targets ([Year Range])\n"
        "*Context line (italic):* "
        '"The following supports Section 4: Corporate History and Overview (Fleet Overview and Orderbook)."\n\n'
        "**Fleet Table — 5 columns (NOT 4):**\n"
        "| Year | Owned Fleet (TEU million) | Total Fleet (TEU million) | Total Vessels | **Owned %** |\n\n"
        "**Rules:**\n"
        '1. Years with estimates: suffix "E" (e.g., 2025E, 2026E)\n'
        "2. Owned % column MANDATORY (e.g., 63%, 67%...88%)\n"
        "3. CAGR line below table: bold figure\n"
        "**Chart Reference:** *[EMC Fleet Capacity Growth Chart — Source: [Source] [Date] / EMC Investor Presentation]*\n\n"
        "**Key Notes (MANDATORY — minimum 5 bullets):**\n"
        "1. Target capacity (end-year TEU)\n"
        "2. Owned fleet transition (from X% to Y%) — reduce charter reliance\n"
        "3. Newbuild delivery concentration + orderbook count + source\n"
        "4. CUB-financed vessel positioning (TEU, Hull No., delivery date)\n"
        "5. **EMC CAPEX plan (USD amount) + EMA capital commitment (USD amount + date)**\n"
        "✅CHECK: Note #5 (CAPEX) MUST be present. If missing → FIX.\n\n"
        "### C-4. Appendix III: EMA — Detailed Financial Projections\n"
        "*Context line (italic):* "
        '"The following supports Section 7: Financial Analysis (Base Case and Worse Case Projections)."\n\n'
        "*Entity: [Name] — Standalone | Currency: USD | Unit: USD'000*\n\n"
        "#### Key Assumptions Table\n"
        "| Assumption | FY[Y]E | FY[Y]E | ... |\n"
        "(1 row per assumption, all years listed individually — no 'Same')\n\n"
        "**Assumptions Narrative (MANDATORY — italic paragraph below table):**\n"
        '"_Revenue growth assumes [basis]. COGS reflects [basis]. CAPEX per [basis]._"\n\n'
        "#### Base Case — Projected P&L (MINIMUM 12 rows)\n"
        "MANDATORY line items: Revenue | Cost of Goods Sold | **Gross Profit** | "
        "Other Operating Income | Operating Expenses | **Operating Profit** | "
        "Finance Income | Finance Cost | Other Non-Operating | **Profit Before Tax** | "
        "Income Tax | **Net Income**\n"
        "- Subtotals (GP/OP/PBT/NI) in **bold**\n"
        "- Negative numbers in parentheses: (7,755,000)\n"
        "- ✅CHECK: P&L rows ≥ 12. If fewer → ADD missing rows.\n\n"
        "#### Base Case — Projected Balance Sheet (MINIMUM 16 rows)\n"
        "MANDATORY: Cash & Equivalents | Trade Receivables | Other Current Assets | "
        "**Total Current Assets** | Vessels & Equipment | Right-of-Use Assets | "
        "Other Non-Current Assets | **Total Non-Current Assets** | **Total Assets** | "
        "**Total Current Liabilities** | Long-term Borrowings | Non-Current Lease Liabilities | "
        "Other Non-Current Liabilities | **Total Non-Current Liabilities** | "
        "**Total Liabilities** | **Total Equity**\n"
        "- ✅CHECK: BS rows ≥ 16. If fewer → ADD.\n\n"
        "#### Base Case — Projected Cash Flow (MINIMUM 6 rows, SEPARATE table)\n"
        "MANDATORY: Operating Cash Flow | Investing Cash Flow | Financing Cash Flow | "
        "**Net Change in Cash** | Opening Cash | **Closing Cash**\n"
        "- ✅CHECK: CF rows ≥ 6. Closing Cash = Opening + Net Change.\n\n"
        "#### Base Case — DSCR Analysis (SEPARATE table from CF)\n"
        "| FY[Y]E | ... | OCF | Total Debt Service (P+I) | **DSCR** |\n"
        "(DSCR with 'x' suffix: e.g. 5.6x)\n\n"
        "**DSCR Commentary (MANDATORY — italic):**\n"
        '"_DSCR remains above [X]x throughout... Minimum DSCR of [X]x occurs in [years]._"\n\n'
        "#### Worse Case — Stress Assumptions (COMPARISON TABLE, not bullets)\n"
        "| Assumption | Base Case | Worse Case | Stress Magnitude |\n"
        "(Rows: Revenue, COGS%, SOFR, Dividend — minimum 4 rows)\n\n"
        "#### Worse Case — Stressed Summary Table\n"
        "Rows: Revenue | Operating Profit | Net Income | OCF | Cash Balance | **DSCR**\n\n"
        "**Worse Case Commentary (MANDATORY — italic):**\n"
        '"_Under Worse Case, DSCR declines to minimum [X]x in [year] but remains above 1.0x... '
        "Cash trough of USD[X] million in [year]. Net income remains positive in all years._\"\n\n"
        "## D. Writing Rules\n"
        "**Language:** English. "
        "**Numbers:** USD'000 with commas. Negatives in parentheses. "
        "**Bold:** Subtotal rows, DSCR, Group Total, [NEW], CAGR figure, Utilization. "
        "**Italic:** Context lines, narratives, commentaries. "
        'No "Context"/"System note" labels — use italic sentences directly. '
        '**Year suffix:** "E" for all estimated/projected years.\n\n'
        "## E. QA Gate — MANDATORY ACTIVE EXECUTION\n"
        "**G1 — Entity:** Borrower/Guarantor names correct throughout\n"
        "**G2 — Appendix I:** Subtotals embedded | EVA note present | Group Limit table present | "
        "Arithmetic: entity subtotals sum to Group Total\n"
        "**G3 — Appendix II:** 5 columns incl. Owned% | ≥5 Key Notes | CAPEX note present | Chart reference present\n"
        "**G4 — Appendix III line counts:** P&L ≥12 rows | BS ≥16 rows | CF ≥6 rows | "
        "DSCR separate table | Assumptions narrative present | DSCR commentary present | Worse Case commentary present\n"
        "**G5 — Arithmetic:** GP=Rev-COGS | TA=TCA+TNCA | TA=TL+TE | "
        "Closing Cash=Opening+Net Change | DSCR=OCF/Debt Service\n"
        "**G6 — Cross-section:** §10 DSCR=§7 DSCR | §10 Exposure=§1 facilities | §10 Fleet=§4 data\n\n"
        "**Append to end of output:** `[QA] G1-G6: ✅/❌`\n\n"
        "## F. Prohibitions\n"
        "1. No condensing Appendix III tables (this is the DETAIL version)\n"
        "2. No combining CF + DSCR into one table\n"
        "3. No 'Same' in assumption cells — list each year explicitly\n"
        "4. No source hyperlinks or file references\n"
        '5. No introductory meta-text ("Below is the complete...")\n'
        '6. No "✅ QA Status" blocks in output body\n'
        '7. No "Context" or "System note" labels\n'
        '8. No "10." prefix on section title\n'
        "9. No conversational prompts or completion markers\n"
        "10. Output = FINAL credit report appendix. No meta-commentary.\n\n"
        "## G. Anti-Truncation\n"
        "**§10 expected: 5,000–10,000 tokens.** This is the LONGEST section.\n"
        "1. NEVER truncate projection tables — all rows mandatory\n"
        "2. Mandatory minimums: P&L ≥12 | BS ≥16 | CF ≥6 | DSCR separate\n"
        "3. If token limit → split: `[§10 CONTINUED — PART 2]`\n"
        "4. Priority: Appendix III > Appendix I > Appendix II\n"
    ),
    11: (
        # ── §11 Analyst / External Research Summary ───────────────────────────────
        "## Role\n"
        "Summarise an analyst or external research report attached as evidence. "
        "Your output becomes §11 of the credit report — a concise, structured "
        "synthesis that a credit committee can read in under two minutes.\n\n"

        "## MANDATORY OUTPUT STRUCTURE (in order, no reordering)\n\n"

        "### 11.1 Report Identification\n"
        "One-row table: Source (broker/house name) | Date | Analyst(s) | Report Title | Pages\n\n"

        "### 11.2 Rating & Target Price\n"
        "One-row table: Current Rating | Prior Rating | Target Price | Current Price | "
        "Upside / Downside (%) | Rating Change (Y/N)\n"
        "- Map to standard values: BUY / OUTPERFORM / ADD → **BUY**; "
        "HOLD / NEUTRAL / MARKET PERFORM → **HOLD**; "
        "SELL / UNDERPERFORM / REDUCE → **SELL**.\n"
        "- If no rating present: write 'Not rated'.\n\n"

        "### 11.3 Investment Thesis (3–5 bullets)\n"
        "Key reasons for the rating. One bullet per driver. Be specific: cite numbers from the report.\n\n"

        "### 11.4 EPS & Revenue Forecasts\n"
        "Table columns: Metric | FY(current) Est | FY(+1) Est | FY(+2) Est | YoY Growth\n"
        "Include: Revenue, Gross Profit, Operating Income, Net Income, EPS, EBITDA (if available).\n"
        "Use the currency and unit stated in the source report.\n\n"

        "### 11.5 Valuation\n"
        "One-row table per method used: Method (P/E / EV/EBITDA / P/B / DCF) | "
        "Multiple / Assumption | Target | Implied Value\n"
        "State the basis year for the valuation multiple.\n\n"

        "### 11.6 Key Risks\n"
        "Two columns: Upside Risks | Downside Risks. 2–4 bullets each.\n\n"

        "### 11.7 Analyst vs Management Delta\n"
        "Where analyst estimates differ materially (>5%) from management guidance, "
        "call out the gap explicitly: Metric | Management Guidance | Analyst Estimate | Delta.\n"
        "If no management guidance is referenced: write 'No management guidance cited'.\n\n"

        "## RULES\n"
        "1. Every number MUST come from the attached evidence — zero hallucination.\n"
        "2. If a subsection has no data in the evidence, write '[Data not available in source report]'.\n"
        "3. Do NOT add credit opinions or risk ratings beyond what the analyst wrote.\n"
        "4. This is a SUMMARY section — do not reproduce verbatim paragraphs from the source.\n"
        "5. Keep the full section under 1,500 words.\n"
    ),
}

OUTPUT_INSTRUCTIONS = """\
## Output Instructions
- Write in clean Markdown with proper heading hierarchy
- Use ## for the section heading, ### for sub-sections
- Include tables where data permits (pipe-table syntax)
- All figures must come from the analyst input data or evidence excerpts above
- Do not fabricate or extrapolate numbers not present in the input
"""


_ZH_INSTRUCTION = (
    "\n\nLANGUAGE DIRECTIVE — MANDATORY: Write the ENTIRE response in Traditional Chinese "
    "(繁體中文). This applies to ALL headings, table headers, table cells, bullet points, "
    "narrative paragraphs, footnotes, and labels. Use professional banking and financial "
    "terminology in Traditional Chinese. Number formatting rules remain unchanged "
    "(e.g. USD 2,791m, 1,234,567). Entity names (company names, bank names, vessel names) "
    "may be kept in English where a standard Chinese equivalent does not exist. "
    "Do NOT produce any English prose or headings — only Traditional Chinese."
)


def _normalize_section3_ratings(input_json: dict) -> dict:
    """
    Normalize §3 3B_internal_ratings FORMAT C to FORMAT A (flat MSR strings).

    FORMAT C stores period values as nested objects, e.g.:
      "interim": {"generated_msr": "4+", "override_applied": true, "override_to": "4+"}
      "current": {"proposed_assessment": {"generated_msr": "3", "proposed_final_msr": "3+"}}

    The AI prompt only describes FORMAT A (flat string) and FORMAT B (flat keys).
    When FORMAT C is present the AI sees dicts where it expects strings and outputs
    "—" for every period — this function flattens FORMAT C → FORMAT A so the AI
    receives plain MSR strings ("4+", "3+", etc.) as it expects.
    """
    ratings = input_json.get("3B_internal_ratings")
    if not isinstance(ratings, dict):
        return input_json
    rows = ratings.get("rows")
    if not isinstance(rows, list):
        return input_json

    period_fields = ("fy2022_23", "fy2024", "interim", "current")
    normalized_rows = []

    for row in rows:
        if not isinstance(row, dict):
            normalized_rows.append(row)
            continue

        new_row = dict(row)
        row_override_flag = bool(row.get("override_flag"))
        row_override_remarks: list[str] = []

        for field in period_fields:
            val = row.get(field)
            if not isinstance(val, dict):
                continue  # already a flat value — keep as-is

            msr_str: Optional[str] = None
            this_override = False
            this_generated: Optional[str] = None

            # Extract from proposed_assessment sub-object (FORMAT C variant 2)
            proposed = val.get("proposed_assessment")
            if isinstance(proposed, dict):
                this_generated = proposed.get("generated_msr") or this_generated
                # proposed_final_msr is the authoritative override result
                if proposed.get("proposed_final_msr"):
                    msr_str = str(proposed["proposed_final_msr"])
                    this_override = True
                elif proposed.get("generated_msr"):
                    msr_str = str(proposed["generated_msr"])

            # Extract from top-level period object keys (FORMAT C variant 1)
            if val.get("generated_msr"):
                this_generated = str(val["generated_msr"])
            if val.get("override_applied"):
                this_override = True
                if val.get("override_to") and msr_str is None:
                    msr_str = str(val["override_to"])
            if msr_str is None and val.get("override_to"):
                msr_str = str(val["override_to"])
            if msr_str is None and this_generated:
                msr_str = this_generated

            if this_override:
                row_override_flag = True
                nested_remarks = val.get("override_remarks") or ""
                if nested_remarks:
                    row_override_remarks.append(str(nested_remarks))
                elif this_generated and msr_str and this_generated != msr_str:
                    row_override_remarks.append(
                        f"Generated MSR {this_generated}; override applied, final MSR {msr_str}."
                    )

            new_row[field] = msr_str  # Replace dict with flat string (or None → "—")

        if row_override_flag and not new_row.get("override_flag"):
            new_row["override_flag"] = True
        if row_override_remarks and not new_row.get("override_remarks"):
            new_row["override_remarks"] = " ".join(row_override_remarks)

        normalized_rows.append(new_row)

    import copy
    result = copy.deepcopy(input_json)
    result["3B_internal_ratings"] = dict(ratings)
    result["3B_internal_ratings"]["rows"] = normalized_rows
    return result


def build_section_prompt(
    section_no: int,
    input_json: dict,
    evidence_chunks: list[str],
    preceding_outputs: Optional[dict[int, str]] = None,
    is_continuation: bool = False,
    continuation_resume_token: Optional[str] = None,
    output_language: str = "en",
    industry: str = "tw_shipping",
    institution_name: str = "the Bank",
) -> tuple[str, str]:
    """
    Build (system_prompt, user_prompt) for a single section generation call.

    When is_continuation=True the user_prompt instructs the model to continue
    from where it left off, using the resume token as a prefix.

    industry / institution_name parameterise the system prompt persona so the
    LLM context matches the actual deal (e.g. tw_semiconductor instead of shipping).
    """
    heading = SECTION_HEADINGS.get(section_no, f"Section {section_no}")
    instructions = SECTION_INSTRUCTIONS.get(section_no, f"Write {heading}.")

    evidence_block = ""
    if evidence_chunks:
        parts = ["\n\n## Evidence from Uploaded Documents\n"]
        for i, chunk in enumerate(evidence_chunks, 1):
            parts.append(f"--- Excerpt {i} ---\n{chunk}")
        evidence_block = "\n\n".join(parts)

    preceding_block = ""
    if preceding_outputs:
        parts = ["\n\n## Previously Generated Sections (for cross-reference)\n"]
        for sec_no, md in sorted(preceding_outputs.items()):
            preview = md[:600].rstrip()
            parts.append(f"### Section {sec_no} preview\n{preview}\n…")
        preceding_block = "\n\n".join(parts)

    # Extract pre-computed calculation results injected by the pipeline (§7 only).
    # Remove from input_json so they don't appear as raw JSON — render them as a
    # dedicated block instead so the AI uses them directly without re-deriving.
    calc_results: list[dict] = input_json.pop("__calc_results", [])

    # Normalize §3 internal ratings: flatten FORMAT C nested objects → flat MSR strings
    # so the AI receives plain "3+", "4+", etc. instead of dicts that trigger "—" output.
    if section_no == 3:
        input_json = _normalize_section3_ratings(input_json)

    calc_block = ""
    if calc_results:
        lines = [
            f"- {c['metric']} | {c['entity']} | {c['period']}: **{c['value']}** "
            f"(formula: {c['formula']})"
            for c in calc_results
        ]
        calc_block = (
            "\n\n## Pre-Computed Financial Ratios\n"
            "USE THESE VALUES EXACTLY — do not re-derive or override:\n\n"
            + "\n".join(lines)
        )

    if is_continuation and continuation_resume_token:
        user_prompt = (
            f"{continuation_resume_token}\n\n"
            f"Continue writing {heading} from where the previous output ended. "
            "Do not repeat content already written. Resume the Markdown output directly."
        )
    else:
        input_text = json.dumps(input_json, ensure_ascii=False, indent=2)
        user_prompt = (
            f"{instructions}\n\n"
            f"## Analyst Input Data\n\n```json\n{input_text}\n```"
            f"{calc_block}"
            f"{evidence_block}"
            f"{preceding_block}\n\n"
            f"{OUTPUT_INSTRUCTIONS}"
        )

    # Build dynamic system prompt for this industry/institution.
    # Also replace every occurrence of "CUB" in the section instructions with
    # the actual institution name so the LLM context matches the deal.
    system_prompt = _build_system_prompt(industry=industry, institution_name=institution_name)
    if output_language == "zh":
        system_prompt += _ZH_INSTRUCTION
    if institution_name and institution_name not in ("the Bank", "CUB"):
        user_prompt = user_prompt.replace("CUB", institution_name)
    return system_prompt, user_prompt

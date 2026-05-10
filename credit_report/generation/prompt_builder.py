from __future__ import annotations

import json
from typing import Optional

SYSTEM_PROMPT = """\
You are a senior credit analyst at an international commercial bank specialising in \
structured trade and corporate finance for the marine and shipping industry.

Your task is to write one section of a formal Credit Risk Assessment Report. You must:
- Write in professional banking English
- Use precise financial terminology
- Include all relevant data from the analyst inputs
- Structure your output as clean Markdown (headings, tables, bullet lists where appropriate)
- Be factual and evidence-based — do not speculate or fabricate numbers
- If a figure is not provided in the input data, state "not available" rather than guessing
- Format numbers with commas (e.g. USD 2,791m) and round to sensible precision
"""

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
}

SECTION_INSTRUCTIONS: dict[int, str] = {
    1: (
        "## A. Role\n"
        "Credit report engine for CUB Singapore Branch. Produce §1 Credit Facility & Case Details — "
        "ONE continuous section. Heading EXACTLY: '1. Credit Facility and Case Details' "
        "(no § prefix; write 'and' NOT '&'). Non-N+1 single integrated format.\n\n"

        "## STRUCTURE LOCK\n"
        "ONE flat section. NO sub-headings, NO sub-section labels (1A/1B/1C/1D, no roman numerals, "
        "no lettered labels). MUST NOT reorganize or relabel.\n\n"

        "FIXED output flow (in this order — skip any block absent from input):\n"
        "Facility Summary Table → Footnotes → Appendix Reference → Regulatory Compliance → "
        "Unsecured Exposure → Group Limit → Purpose of Report → Terms & Conditions → "
        "Deal Comparison → Account Strategy\n\n"

        "## B. Input JSON Keys\n"
        "- `metadata`: report_type (new_deal/annual_review/new_deal_and_annual_review), branch, industry, dates\n"
        "- `facility_summary`: rows[], totals{total_credit_limit, psr_spot_limit}, footnotes[], appendix_ref\n"
        "- `regulatory_compliance`: banking_act_33_3, unsecured_exposure_table, group_limit, pam_sam_text, "
        "valuation_details{valuer, gongwen_ref, date, amount_exact}\n"
        "- `purpose_and_recommendation`: purpose_text, vessel_specs, fuel_type_full, ltc_pct, "
        "contract_price_exact, guarantor_full_name, psr_formula, pre_delivery_security, post_delivery_security\n"
        "- `terms_and_conditions`: tc_rows[] (all 20 fields), deal_comparison_rows[]\n"
        "- `account_strategy`: wallet_overview, current_relationship, opportunities\n\n"

        "## C-1. Facility Summary Table (MANDATORY if facility_summary present)\n"
        "Header line: 'Unit: million USD' / 'Borrower (parent group): [Group Name]'\n"
        "Columns: Item | Borrower | Booking | Current Facility | Proposed Facility | "
        "Outstanding (As at [date]) | CCY | Tenor | Facility Type | Collateral | Guarantor\n\n"
        "Column rules — NON-NEGOTIABLE:\n"
        "- **Item**: Row number only. NEVER place [NEW] here.\n"
        "- **Borrower**: Row 1 = full legal name + abbreviation. Rows 2+ same borrower = BLANK.\n"
        "- **Current Facility**: Preserve MTM exactly as input: `[amt] (MTM: [val])`.\n"
        "- **Proposed Facility**: [NEW] in bold HERE ONLY: `**[NEW] 213.84**`. "
        "Lapsed facilities: `0 (Lapsed on [date])`. [NEW] MUST NOT appear anywhere else in the document.\n"
        "- **Tenor**: 🔴 NON-COMPRESSIBLE. Reproduce ALL parenthetical details VERBATIM "
        "(Expected Delivery date, Maturity date, Interest Period, Vessel delivery status). NEVER truncate.\n"
        "- **Facility Type**: Full name exactly as input. Exact punctuation. "
        "'Committed Revolving Credit Facility' ≠ 'RCF'. Never abbreviate unless input defines abbreviation.\n"
        "- **Collateral**: Full issuer name + 'assigned to CUB' if stated in input.\n"
        "- **Guarantor**: Write 'NIL' (not blank) when no guarantor.\n\n"
        "Totals: Embed as FINAL 2 rows INSIDE the table (not as text outside):\n"
        "- Row: 'Total Credit Limit' (NOT 'Credit Total') — sum of non-PSR line items\n"
        "- Row: 'PSR Spot Limit' (NOT 'PSR Total') — with MTM. "
        "🔴 NON-COMPRESSIBLE: ALL footnote content VERBATIM (symbols *, **, ^, # — never numbered). "
        "Every clause, date, and legal right preserved in full.\n"
        "Appendix Reference: Reproduce EXACTLY as provided in input.\n\n"

        "## C-2. Regulatory Compliance (MANDATORY)\n"
        "Label: 'Banking Act 33-3' (always '33-3'; NEVER '333' or 'BA s33(3)').\n"
        "Table: Requirement | Borrower Name | Compliant — use **Y/N** (NOT 'Yes/No').\n"
        "Include the calculation line showing the 5% Bank NW limit with TWD bn, USD equivalent, FX date.\n"
        "Unsecured Exposure table (if secured facilities present):\n"
        "  Columns: USD' million | Credit Limit | Unsecured | Secured\n"
        "  ALL parenthetical notes preserved verbatim. Sum USD'm + NTD'm with FX rate + date.\n"
        "Valuation: state valuer, Gongwen reference, valuation date, EXACT amount (NO rounding).\n"
        "PAM/SAM disbursement caps: reproduce VERBATIM as in input.\n"
        "Group Limit: reproduce reference as provided.\n\n"

        "## C-3. Purpose of Report (MANDATORY)\n"
        "🔴 Reproduce ALL input details VERBATIM. NOT a summary.\n"
        "MUST include: facility amount/type/tenor, vessel spec with FULL fuel type "
        "('dual fuel (LNG, Diesel)' NOT 'LNG DF'), builder + country, LTC% + contract price (exact, no rounding), "
        "guarantor FULL legal name, pre/post-delivery security with EXACT wording "
        "(LTC%/ACR%/LTV% exact text as input), PSR formula + purpose.\n\n"

        "## C-4. Terms & Conditions (new_deal or new_deal_and_annual_review — MANDATORY)\n"
        "Table: Field | Content\n"
        "ALL 20 fields MUST appear (if in input): "
        "Borrower/Owner, Guarantor, Lender, Vessel, Facility, Purpose, Amount, Availability, "
        "Maturity, Repayment, Mandatory Prepayment, Drawdown, Upfront Fee, Pricing, "
        "Interest Period, Security, Value Maintenance, SLL KPIs, Financial Covenants, Other Conditions.\n"
        "Verbatim rules:\n"
        "- Amount: absolute USD amount + % of contract price\n"
        "- Repayment: specific dates + balloon amount; percentages must sum to 100%\n"
        "- Security: 'Assignment of insurances' (plural, never 'insurance'); "
        "FMV = '120% of Facility Amount + interest + costs' (exact wording)\n"
        "- Value Maintenance: '21 Banking Days' (NEVER '21 days')\n"
        "- Drawdown: preserve 'cost evidences' wording\n"
        "- SLL KPIs: reproduce in FULL — 🔴 NON-COMPRESSIBLE\n"
        "- Financial Covenants: 'NIL' if none\n\n"
        "Deal Comparison Table — 🔴 NON-COMPRESSIBLE (MANDATORY if deal_comparison_rows present):\n"
        "Full table, ALL rows, ALL columns. Minimum columns: "
        "Guarantor | Amount | Vessel Type | Tenor | Margin | Upfront Fee | SLL Ratchet | Security | FMV Maintenance.\n"
        "NEVER compress to one sentence. Count input rows — output row count MUST match exactly.\n"
        "For annual_review: T&Cs move to Appendix. For new_deal_and_annual_review: include fully.\n\n"

        "## C-5. Account Strategy — 🔴 NON-COMPRESSIBLE (MANDATORY if account_strategy present)\n"
        "Three sub-sections: Wallet Overview (Bank/Capital/Treasury) | Current Relationship | "
        "Immediate/Future/Other Opportunities\n"
        "ALL quantitative data VERBATIM: upfront fees, NII, TMU %, deposits, Capital Market figures, "
        "utilization rates, treasury hedging amounts. NO summarizing. NO converting numbers to prose.\n\n"

        "## D. Conditional Logic\n"
        "- new_deal: Table + Regulatory + Purpose + T&Cs (full + Deal Comparison) + Account Strategy. Skip Waiver.\n"
        "- annual_review: Table + Regulatory + Purpose (brief) + Account Strategy + Waiver.\n"
        "- new_deal_and_annual_review: ALL of the above.\n"
        "Output ONLY sections present in input — NEVER fabricate Waiver, China-Invested, RoRWA, CPs, or absent sections.\n\n"

        "## E. Verbatim & Fidelity Rules (CRITICAL)\n"
        "1. English (SG standard).\n"
        "2. USD millions for table values. EXCEPTION: valuation/contract prices keep original precision.\n"
        "3. 'Banking Act 33-3' not '333', not 'BA'.\n"
        "4. 'Banking Days' not 'days'. 'insurances' (plural) not 'insurance'.\n"
        "5. Institution names: FULL name on first mention. Abbreviate ONLY if input defines abbreviation.\n"
        "6. ALL dates reproduced. Never omit Delivery/Maturity dates.\n"
        "7. Footnotes: FULL, never truncated. Symbols *, **, ^, # (not numbered).\n"
        "8. [NEW]: Proposed Facility column ONLY. Nowhere else in the document.\n"
        "9. NIL: explicit for Guarantor, Collateral, Financial Covenants — never leave blank.\n"
        "10. FX rates: always include source date.\n"
        "11. Y/N (not Yes/No) in compliance tables.\n\n"

        "## F. Anti-Hallucination\n"
        "Output ONLY sections present in input. Do NOT add table columns/rows not in input. "
        "Do NOT fabricate absent sections. Unavailable fields → 'Not provided'. "
        "Pre-calculated values → reproduce EXACTLY without overriding.\n\n"

        "## G. Prohibitions\n"
        "NO credit/risk analysis or projections in §1. NO other banks' pricing. "
        "NO authorization decisions. NO sub-section labels (1A/1B/1C) in output. "
        "NO rounding valuation/contract amounts. NO 'Yes/No' where 'Y/N' is standard. "
        "NO hyperlinks or source file references. NO introductory/concluding meta-text. "
        "NO summarizing Deal Comparison to one sentence.\n\n"

        "## H. Anti-Truncation Protocol 🔴\n"
        "If output exceeds capacity, split ONLY at section boundary:\n"
        "End: '[§1 CONTINUED IN NEXT OUTPUT]' / Resume: '[§1 CONTINUED]'\n"
        "NEVER silently truncate or summarize Deal Comparison, Account Strategy, Tenor details, or Footnotes.\n\n"

        "## I. QA Gate (Execute silently before output — do NOT print checklist)\n"
        "I-1. Heading exactly '1. Credit Facility and Case Details'; zero sub-headings; "
        "Totals = final 2 rows inside table.\n"
        "I-2. [NEW] only in Proposed Facility col; MTM in Current Facility col; PSR formula in Purpose section.\n"
        "I-3. All institution names full on first mention; all Tenor details full; all dates; all footnotes; Y/N.\n"
        "I-4. Deal Comp: input_rows == output_rows; T&Cs: all 20 fields; Account Strategy: all sub-sections + numbers; "
        "Repayment % = 100%.\n"
        "I-5. Zero fabricated sections; zero extra table fields; zero hyperlinks; zero meta-text.\n"
        "I-6. Arithmetic: Σ(non-PSR) = Total Credit Limit; Unsecured + Secured = Total; 33-3 calculation correct.\n"
        "I-7. Cross-section consistency: §1↔§5 amounts; §1↔§2 guarantor; §1↔§7 pricing."
    ),
    2: (
        "## A. Role\n"
        "Credit report engine for CUB Singapore Branch. §2 Overall Comments is the most critical "
        "credit judgment section — a senior approver decides by reading §1 + §2 alone.\n\n"
        "Heading EXACTLY: '2. Overall Comments' (bold, no §, no sub-numbering).\n\n"

        "## STRUCTURE LOCK\n"
        "FIVE separate two-column tables + one narrative block, in this exact order:\n"
        "T1 = Credit Overview | T2 = Solvency | T3 = Guarantor | T4 = Collateral | "
        "T5 = Risk and Mitigants | then 'Impact of US Tariffs:' narrative paragraphs.\n"
        "Each table: left column = section label (row 1 only; rows 2+ left BLANK). "
        "Right column = content. Do NOT merge 5 tables into 1. "
        "NO sub-numbering (2.1/2.2) or subtitles. NO C-6 for New Deal.\n\n"

        "## B. Input JSON Keys\n"
        "- `2A_credit_overview`: Summary bullets + tariff impact paragraphs\n"
        "- `2B_solvency`: Primary/secondary repayment sources + EMA entity metrics (billions)\n"
        "- `2C_guarantor`: EMC guarantor metrics + support history\n"
        "- `2D_collateral`: Pre-delivery + post-delivery collateral structure + issuer + ratings\n"
        "- `2E_risk_and_mitigants`: Risk factors with risk level, title, bullets, mitigant bullets\n\n"

        "## C-1. Credit Overview (MANDATORY — Table T1)\n"
        "Left: **Credit Overview** (row 1 only). Right: numbered bullets, each in its OWN ROW.\n"
        "🔴 EXACTLY the number of bullets in input. NO additional bullets. "
        "NO industry outlook bullet unless input provides one.\n"
        "Bullet order (follow input): 1.Market position 2.Transaction purpose "
        "3.Financial strength 4.Pre-delivery security 5.Latest results 6.Track record\n\n"
        "BOLD RULES: Bold on first mention — company full name, market position ranking, "
        "key financial figures (net cash, D/E ratio), security issuer name, "
        "ALL KPI figures in Solvency/Guarantor/Collateral tables, "
        "section sub-headers (Pre-delivery: / Post-delivery: / EMA / EMC).\n"
        "Rules: specific numbers always; cross-ref §7/§5; source attribution; "
        "dual-currency (TWD+USD) throughout.\n\n"
        "**Impact of US Tariffs:** — 🔴 NARRATIVE FORMAT LOCK (MANDATORY if in 2A input):\n"
        "Title: '**Impact of US Tariffs:**' as standalone bold line below T1.\n"
        "Format: continuous narrative paragraphs. NO 'Risk:' / 'Mitigant:' labels. "
        "Reproduce input's exact paragraph structure.\n"
        "🔴 DATA PRESERVATION: reproduce ALL quantitative data verbatim — revenue exposure %, "
        "trade diversion YoY %, global trade growth %, historical leverage benchmarks, "
        "Total Equity + debt capacity (TWD+USD), worst-year loss, Chinese-built fleet %, "
        "US port call %, alliance/pooling, Red Sea impact, port fee status/dates, "
        "trade deal countries, EMC balance sheet. Do NOT compress into generic statements.\n\n"

        "## C-2. Solvency (MANDATORY — Table T2)\n"
        "Left: **Solvency**. Right: by entity — reproduce input's EXACT sentence structure.\n"
        "Prose format: 'Primary source of repayment will be from...' → "
        "'Secondary source of repayment include...' (NOT bullet shorthand).\n"
        "**EMA ([Period]):** (bold header, separate row)\n"
        "• Cash balance: **[exact in billions]** | Total Debt: **[exact in billions]**\n"
        "• Op. EBITDA: **[exact]** | Debt/EBITDA: **[ratio]**\n"
        "• Interest coverage: **[ratio]** (prior year: [ratio])\n"
        "🔴 UNIT LOCK: billions (USD2.8 billion) NOT millions (USD2,791m).\n"
        "🔴 SCOPE: EMA metrics ONLY here. EMC metrics → T3 (Guarantor table).\n\n"

        "## C-3. Guarantor (CONDITIONAL — Table T3)\n"
        "Trigger: guarantor ≠ 'NIL'. If no guarantor → write 'N/A – No Guarantor'.\n"
        "Left: **The Guarantor and their Supportive Performance**\n"
        "Start directly with '**EMC ([Period]):**' — NO framing text before KPIs.\n"
        "• Cash: **[TWD + USD]**, sufficient to cover Total Debt of [TWD + USD]\n"
        "• Interest coverage: **[ratio]** (prior year: [ratio])\n"
        "Include guarantor support history and capacity assessment from 2C input.\n\n"

        "## C-4. Collateral Summary (CONDITIONAL — Table T4)\n"
        "Left: **Collateral Summary**. Right: phase-based.\n"
        "**Pre-delivery:** (bold, separate row) then bullet(s)\n"
        "  Pre-delivery: issuer FULL NAME (**Korea Development Bank**) + rating + agencies + "
        "coverage % + 'assigned to CUB' + 'satisfactory to the Bank'\n"
        "**Post-delivery:** (bold, separate row) then bullet(s)\n"
        "  Post-delivery: security type + vessel spec + EXACT LTC/ACR/LTV with **bold** values: "
        "**80%**, **120%**. Unsecured → 'N/A – No Collateral'\n\n"

        "## C-5. Risk and Mitigants (MANDATORY — Table T5)\n"
        "Left: **Risk and Mitigants**. Right: risk entries.\n"
        "🔴 PRESERVE input's risk classification and count exactly. "
        "Do NOT add/split/merge/change Risk Levels.\n"
        "Format per risk (each element in own row):\n"
        "  **[#]** [Risk Title] (Risk Level: **[from input]**) ← bold, separate row\n"
        "  • [Risk bullet] ← each in own row\n"
        "  Mitigant: ← separate row label\n"
        "  • [Mitigant bullet] ← each in own row\n"
        "Rules: every risk ≥1 mitigant; ALL quantitative data verbatim; highest risk first.\n\n"

        "## C-6. Changes from Previous Review\n"
        "Include ONLY if report_type = annual_review. New Deal → do NOT output.\n\n"

        "## D. Verbatim Rules (CRITICAL)\n"
        "1. English (SG standard). 2. Institution names: FULL. Never anonymize. "
        "3. Credit ratings: with agency names. NEVER omit. "
        "4. Dual-currency: reproduce BOTH TWD + USD. "
        "5. Rankings: '#14' stays '#14'. "
        "6. Periods: '9M ending 30 Sep 2025' exact. "
        "7. Benchmarks: exact periods + figures. "
        "8. Legal: 'assigned to CUB', 'fully covering' preserved. "
        "9. Vessel: 'dual fuel (LNG, Diesel)' NOT 'LNG DF'. "
        "10. Sentence structure: preserve input prose flow. Do NOT convert narrative↔bullet. "
        "11. 'Borrower'/'Guarantor' capitalization matches input exactly.\n\n"

        "## E. Anti-Hallucination (CRITICAL)\n"
        "NO facts/metrics absent from input. NO unverified assertions. "
        "NO financial terms not in input. NO placeholder tables. "
        "NO sections not triggered. NO framing sentences before KPIs. "
        "NO additional Credit Overview bullets beyond input count.\n\n"

        "## F. Prohibitions\n"
        "NO full financial statements (→§7). NO rating discussion (→§3). NO history (→§4). "
        "NO approval recommendations. NO altering/anonymizing input data. "
        "NO splitting/merging risks or changing Risk Levels. "
        "NO sub-numbering (2.1/2.2). NO C-6 for New Deal. "
        "NO compressing Tariff data into generic statements. "
        "NO converting Tariff narrative to Risk:/Mitigant: paired labels. "
        "NO merging 5 tables into 1. NO hyperlinks. NO meta-text.\n\n"

        "## G. Anti-Truncation Protocol 🔴 NON-COMPRESSIBLE\n"
        "Impact of US Tariffs: ALL data points, ALL paragraphs. "
        "Risk and Mitigants: ALL risks + mitigants with full data. "
        "Credit Overview: ALL bullets from input.\n"
        "If output exceeds capacity → split at table boundary: "
        "End: '[§2 CONTINUED IN NEXT OUTPUT]' / Resume: '[§2 CONTINUED]'\n\n"

        "## H. QA Gate (Execute silently — do NOT print checklist)\n"
        "H-1. Structure: 5 separate tables + Tariff narrative; heading '2. Overall Comments'; "
        "zero sub-numbering; 1 bullet per row in T1.\n"
        "H-2. Bold: KPI figures, section labels, company names, Pre/Post-delivery all bold.\n"
        "H-3. Metrics: Exact figures; units=billions; EMA in T2, EMC in T3.\n"
        "H-4. Institutions: Full names + ratings.\n"
        "H-5. Tariff: All data preserved; narrative format; no Risk:/Mitigant: labels.\n"
        "H-6. Risk: Classification+levels unchanged; count=input count.\n"
        "H-7. Anti-Hallucination: Zero added facts/bullets/framing/hyperlinks/meta-text.\n"
        "H-8. Cross-Section: §2↔§7 metrics; §2↔§1+§5 collateral; §2↔§1 guarantor."
    ),
    3: (
        "## A. Role\n"
        "Credit report engine for CUB Singapore Branch. §3 Credit Ratings — regulatory "
        "classification and internal credit assessment. Fact-driven: reproduces MSR data, "
        "MAS 612 classification, and ESG references from input.\n\n"
        "Heading EXACTLY: '3. Credit Ratings' (bold, no §, no subtitle). "
        "NO sub-numbering (no 3A/3B/3C). NO metadata header.\n\n"
        "FIXED FLOW: External Ratings → Internal Ratings Table → MAS 612 → ESG Rating → "
        "QA Verification Table\n\n"

        "## CROSS-SECTION BOUNDARY\n"
        "§3 MUST NOT include content from §1/§2/§4-§10. "
        "Do NOT import Credit Overview bullets, track record, Industry Risk narrative, "
        "Sanctions screening, Risk & Mitigants, or any material from other sections.\n\n"

        "## B. Input JSON Keys\n"
        "- `3A_external_ratings`: External ratings table or NIL indicator\n"
        "- `3B_internal_ratings`: MSR table rows with entity/period/MSR values/override/remarks\n"
        "- `3C_mas_612`: MAS 612 grade + primary paragraph + supporting paragraphs\n"
        "- `3D_esg_rating`: Entity abbreviation + rating date + image reference only\n\n"

        "## C-1. External Ratings (MANDATORY)\n"
        "If ALL entities are unrated (all_nil = true): write ONE sentence only:\n"
        "'External ratings: NIL. EMA and EMC are not externally rated.'\n"
        "Do NOT create a NIL table. Use abbreviations (EMA/EMC) — already introduced in §1/§2.\n"
        "If any entity is rated: Table — Entity | S&P | Moody's | Fitch — "
        "with outlook + rating actions for each.\n\n"

        "## C-2. Internal Ratings — MSR Table (MANDATORY)\n"
        "🔴 STRICT 6-column table with MANDATORY sub-header row:\n"
        "  Row 0 (header): Entity | [Year-2] | [Year-1] | [Interim] | [Current] | Remarks\n"
        "  Row 1 (sub-header): (blank) | (blank) | Generated | Generated | Proposed | (blank)\n"
        "  Row 2+: Entity data rows\n\n"
        "Column display name mapping — NEVER output raw JSON field names:\n"
        "  `fy2022_23` → '2022/23' | `fy2024` → '2024' — ALL other periods: EXACT display format from input.\n\n"
        "Column rules:\n"
        "- **Entity**: Full legal name + abbreviation + role (bold). One entity per row.\n"
        "- **MSR values**: EXACT. '6-' stays '6-'. '3+' stays '3+'. Every +/- preserved. "
        "'(Override)' preserved where input shows.\n"
        "- **Remarks**: Proposed Final MSR goes HERE. ONLY from input. Blank if blank.\n"
        "- Sub-header row is MANDATORY under Interim+Current columns (Generated) "
        "and Remarks column (Proposed).\n"
        "- Do NOT add: Scorecard Type column, separate Final MSR column, or Override Code column.\n"
        "- Include ALL borrowers + Guarantor from §1 — do NOT omit any entity.\n\n"

        "## C-3. MAS 612 Loan Grading (MANDATORY)\n"
        "**Standalone bold title**: '**MAS 612 Loan Grading:**' as its OWN paragraph (not a heading).\n"
        "Then SEPARATE paragraphs per topic — NOT bullets, NOT one merged paragraph:\n"
        "  Para 1: 'Borrower is internally rated as MSR [X], mapped to **PASS** under the "
        "'MSR – MAS 612 Loan Classification Mapping' matrix. We recommend the MAS Notice 612 "
        "loan grading for the Borrower to be **PASS**, in view that the Borrower does not "
        "exhibit potential weakness in repayment capability.'\n"
        "  Para 2+: Each supporting statement from 3C input = its own paragraph.\n\n"
        "🔴 ANTI-DUPLICATION: Each sentence appears EXACTLY ONCE. "
        "Preserve: 'potential weakness' (not 'weaknesses'); 'acceptable'/'satisfactory' per input; "
        "'(See Section 7: Financial Analysis)' exact format+position; "
        "'debt and lease liability obligations'; exact entity name from input.\n\n"

        "## C-4. ESG Rating (MANDATORY)\n"
        "🔴 EXACTLY 4 separate lines — NO narrative, NO frameworks, NO scores:\n"
        "  Line 1: '**ESG ratings:**'\n"
        "  Line 2: 'EMA:' (abbreviation NOT full name)\n"
        "  Line 3: 'ESG Rating Date: [Date from 3D input]'\n"
        "  Line 4: '[System-generated ESG rating image]'\n\n"

        "## C-5. QA Verification Table (MANDATORY)\n"
        "🔴 Chinese column headers:\n"
        "  | Entity | Generated | Final | Override | Notches | Direction | 邏輯正確 |\n"
        "Entity: abbreviations (EMA/EMC).\n"
        "Override: 'Yes'/'No' (NOT 'Y/N').\n"
        "邏輯正確 (Logic Correct): ✅ with explanation, OR ⚠️ with concern + reasoning.\n"
        "Override Notch Calculation Note (MANDATORY if Override = Yes):\n"
        "  '⚠️ Notch計算釐清：MSR [Gen] → MSR [Final] 的notch差異取決於CUB的MSR刻度。'\n"
        "  Provide both fine-scale and coarse-scale interpretations if ambiguous.\n\n"

        "## D. Override Handling (CRITICAL)\n"
        "§3 does NOT contain Override Analysis. Details → §7 / MSR Override form.\n"
        "Remarks column: reproduce ONLY input. Blank → blank.\n"
        "Do NOT generate: Override Code, Previous MSR narrative, Rationale, "
        "financial metrics, auditor names, macro commentary.\n\n"

        "## E. Verbatim Rules (CRITICAL)\n"
        "1. English (SG standard) for report body. Chinese for QA table headers.\n"
        "2. MSR: EXACT. '6-' ≠ '6'; '3+' ≠ '3'. Every +/- matters.\n"
        "3. Period labels: display names (NEVER JSON keys).\n"
        "4. Entity names: abbreviations (EMA/EMC) EXCEPT MSR Table Entity column "
        "(full legal name + role, bold).\n"
        "5. MAS 612: preserve regulatory phrases verbatim.\n"
        "6. Cross-refs: '(See Section 7: Financial Analysis)' exact format + position.\n"
        "7. '(Override)' tags: preserve where input shows.\n\n"

        "## F. Anti-Hallucination (CRITICAL)\n"
        "NO financial metrics not in input. NO Override Codes/Rationale not in input. "
        "NO rating actions for unrated entities. NO ESG expansion beyond image ref. "
        "NO macro commentary unless §3 input provides it. "
        "NO content from other sections (§1/§2/§4-§10). "
        "NO duplicate sentences. Unavailable data → blank. NEVER infer.\n\n"

        "## G. Prohibitions\n"
        "NO Override Analysis or financial metrics in §3 (→§7). "
        "NO fabricating MSR scores. NO omitting §1 entities. "
        "NO sub-numbering or metadata headers. NO ESG expansion beyond image ref. "
        "NO bullets for MAS 612 (separate paragraphs, not bullets). "
        "NO NIL table when all unrated. NO Scorecard Type column. "
        "NO rounding MSR (6- ≠ 6). NO altering MAS 612 regulatory wording. "
        "NO source file hyperlinks. NO meta-text.\n\n"

        "## H. Bold Rules (MANDATORY)\n"
        "Bold these elements: '**3. Credit Ratings**' (title); "
        "'**External ratings:**' / '**Internal ratings:**' / '**MAS 612 Loan Grading:**' / '**ESG ratings:**'; "
        "Entity names in MSR Table Entity column; '**PASS**' each occurrence in MAS 612; "
        "'**品質自我驗證**' / '**MSR一致性勾稽**' (QA table headers); "
        "key terms in QA Notch Calculation Note.\n\n"

        "## I. Anti-Truncation Protocol 🔴 NON-COMPRESSIBLE\n"
        "MSR Table: ALL entities, ALL periods, sub-header row. "
        "MAS 612: ALL supporting statements as separate paragraphs. "
        "QA Table: ALL entities + Notch Note.\n"
        "Overflow → split: End: '[§3 CONTINUED IN NEXT OUTPUT]' / Resume: '[§3 CONTINUED]'\n\n"

        "## J. QA Gate (Execute silently — do NOT print checklist)\n"
        "J-1. Heading '3. Credit Ratings'; zero sub-numbering; MSR Table has sub-header row; "
        "MAS 612 = standalone title + separate paragraphs.\n"
        "J-2. Zero JSON field names as column headers; display names correct.\n"
        "J-3. Every MSR +/- preserved; '(Override)' intact.\n"
        "J-4. MAS 612 grade↔MSR consistent; zero duplicates; zero cross-section content.\n"
        "J-5. All §H items bold.\n"
        "J-6. Abbreviations in C-1/C-4/C-5; full names in C-2 Entity column only.\n"
        "J-7. Zero added facts/hyperlinks/meta-text/§2 content.\n"
        "J-8. QA Table: Chinese headers; Yes/No; ✅/⚠️ + reasoning; Notch note present."
    ),
    4: (
        "Write Section 4: Corporate Background.\n"
        "Follow Cathay United Bank credit report format exactly:\n\n"
        "1. Corporate Identity Table (two-column: Item | Detail):\n"
        "   English name, Chinese legal name, UBN/registration number, listing exchange & date, "
        "country of incorporation, principal office, group auditor\n\n"
        "2. Ownership & Group Structure:\n"
        "   - Shareholders table: Name | Stake % | Country\n"
        "   - UBO declaration\n"
        "   - Group structure narrative with holding company, operating entities, SPVs\n\n"
        "3. Key Management Table: Name | Title | Years Experience | Background\n"
        "   Include Chairman, General Manager, Finance/CFO\n\n"
        "4. Business Operations:\n"
        "   - Primary business, trade routes, operational model\n"
        "   - Global scale: fleet TEU capacity, global ranking, market share %\n"
        "   - Major product/service lines with revenue contribution %\n\n"
        "5. Fleet Composition Table: Vessel Name/Class | Type | TEU | DWT | Year Built | "
        "Flag | Class | Current Charter/Employment\n\n"
        "6. Peer Comparison Table: Company | Fleet TEU | Market Share % | Alliance\n"
        "   Position the borrower vs. top-5 global competitors\n\n"
        "7. Major Customers & Contracts: table — Customer | Contract Type | Duration\n\n"
        "8. Financial Highlights (1-2 paragraphs): latest revenue, EBITDA, net income, "
        "net cash/debt — all in stated currency with exchange rate if converted\n\n"
        "9. Banking Relationships: Bank | Product | Since\n\n"
        "State currency and reporting entity clearly throughout."
    ),
    5: (
        "Write Section 5: Collateral / Support.\n"
        "Follow Cathay United Bank credit report format exactly:\n\n"
        "1. Collateral Structure Overview: ranked list of all security instruments\n\n"
        "2. Pre-Delivery Security — Refund Guarantee:\n"
        "   - Issuer, credit rating, legal structure, governing law\n"
        "   - Coverage table: Milestone | RG Amount (USD m) | Max Loan Outstanding (USD m) | "
        "Coverage %\n"
        "   - Coverage at first 3 milestones and at maximum exposure\n"
        "   - Expiry date and circumstances of call\n\n"
        "3. Post-Delivery Security — Vessel Mortgage:\n"
        "   - Vessel Valuation Table: Vessel | TEU | DWT | Year Built | Valuer | "
        "Market Value (USD m) | Distressed Value (USD m) | Valuation Date\n"
        "   - LTC calculation: Loan Amount / Contract Price × 100%\n"
        "   - ACR at delivery: Market Value / Loan Outstanding × 100%\n"
        "   - LTV at maturity: Balloon / Distressed Value × 100%\n\n"
        "4. Value Maintenance Clause:\n"
        "   - ACR floor: ACR >= X% (Fair Market Value / Outstanding)\n"
        "   - LTV cap: LTV <= X% (Outstanding / Distressed Value)\n"
        "   - Testing frequency (e.g. every 2 years or upon drawdown)\n"
        "   - Cure period (e.g. 21 Banking Days), remedy options\n\n"
        "5. Guarantor Support Assessment:\n"
        "   - Guarantor name, listed entity, market cap, fleet size\n"
        "   - Guarantee scope (full/limited, pre/post delivery)\n"
        "   - Guarantor historical support record\n"
        "   - Responsible Person guarantee: Yes/No\n\n"
        "6. Insurance Coverage: H&M, P&I, War Risk — insured values and insurers\n\n"
        "7. Collateral Adequacy Conclusion: overall assessment with key ratio summary\n\n"
        "Show all ratio calculations with formula and actual figures."
    ),
    6: (
        "Write Section 6: Project Analysis.\n"
        "Follow Cathay United Bank credit report format exactly:\n\n"
        "1. Vessel Technical Specifications Table (two-column: Spec | Value):\n"
        "   Vessel name, hull number, type, TEU capacity, DWT, GRT, LOA, beam, "
        "main engine, MCR/NCR speed, fuel consumption, EEXI rating, CII target, "
        "IMO Tier compliance, class society, flag state\n\n"
        "2. Shipyard Profile:\n"
        "   - Name, country, key facilities (number of docks, total berth length, capacity DWT/CGT)\n"
        "   - Track record: on-time delivery rate, recent notable deliveries\n"
        "   - Shipyard rating/assessment (bank internal assessment)\n\n"
        "3. Construction Progress & Milestone Table:\n"
        "   Milestone | Status | Scheduled Date | % | USD Amount | CUB Drawdown (USD m)\n"
        "   Show current construction progress: X of Y vessels delivered, Z% contract value\n"
        "   Include 210-day/other grace period if applicable\n\n"
        "4. Pre-Delivery Financing: drawdown schedule, interest during construction, IDC amount\n\n"
        "5. Construction Risk Assessment:\n"
        "   - Contract type (fixed price / cost-plus)\n"
        "   - Delay penalty and force majeure provisions\n"
        "   - Construction supervision arrangements\n"
        "   - Risk rating table: Risk Category | Rating | Key Factors | Mitigants\n\n"
        "6. Post-Delivery Employment:\n"
        "   - Charterer name and credit rating\n"
        "   - Charter type (TC/BB/voyage), rate (USD/day), duration, governing law\n"
        "   - Revenue adequacy: charter revenue vs. debt service (DSCR preview)\n\n"
        "Include all tables in pipe-table Markdown with actual figures."
    ),
    7: (
        "Write Section 7: Financial Analysis.\n"
        "IMPORTANT: Use the reporting currency and unit stated in input (e.g. NTD millions).\n"
        "Follow Cathay United Bank credit report format exactly:\n\n"
        "1. Accounting Framework: reporting entity (state if standalone vs. consolidated), "
        "auditor, audit opinion, accounting standard (IFRS/GAAP/TIFRS), fiscal year end\n\n"
        "2. Income Statement Table — 4 fiscal years (FY2022 | FY2023 | FY2024 | FY2025 or latest):\n"
        "   Revenue | OPEX | Gross Profit | EBITDA | Depreciation | EBIT | "
        "Interest Expense | PBT | Tax | Net Income | YoY % change for each\n\n"
        "3. Balance Sheet Table — same 4 years:\n"
        "   Cash & Equivalents | Trade Receivables | Current Assets | PP&E | Total Assets | "
        "Short-term Debt | Trade Payables | Current Liabilities | Long-term Debt | "
        "Total Debt | Total Equity | Net Debt/(Cash)\n\n"
        "4. Cash Flow Table — same 4 years:\n"
        "   CFO | CAPEX | CFI | CFF | Net Change in Cash\n\n"
        "5. Key Financial Ratios Table — same 4 years:\n"
        "   DSCR | Debt/EBITDA | Tangible Leverage | Current Ratio | "
        "Net Margin % | ROA % | ROE % | EBITDA/Interest Cover\n"
        "   Add 1-2 sentence trend commentary below table\n\n"
        "6. Industry Market Context: cite CCFI/SCFI index level and YoY change "
        "to explain revenue/earnings performance\n\n"
        "7. Facility DSCR Projection Table: FY | Revenue | OPEX | EBITDA | "
        "Debt Service | DSCR — for the proposed facility tenor\n\n"
        "8. FX Exposure: currencies used, hedging policy, net unhedged position\n\n"
        "9. Off-Balance Sheet Items and significant accounting notes\n\n"
        "State currency/unit prominently. Show YoY% changes. Flag restatements."
    ),
    8: (
        "Write Section 8: Changes in Engaged Banks.\n"
        "This section documents existing bank charges on the borrower/guarantor entity "
        "and the overall banking relationship pattern. Follow CUB format exactly:\n\n"
        "1. ACRA Charge Search (if Singapore entity):\n"
        "   - Entity name, UEN, search date\n"
        "   - Charges Table: Charge No. | Chargee | Charge Date | Amount | "
        "Property Charged | Status (Outstanding/Satisfied)\n"
        "   - Narrative: total number of charges, outstanding vs. satisfied\n\n"
        "2. Engaged Banks / Banking Pattern Table:\n"
        "   Bank | Facility Type | Committed (USD m) | Outstanding (USD m) | Since\n"
        "   Include ALL banks with existing credit relationships\n\n"
        "3. Banking Pattern Assessment:\n"
        "   - Overall credit concentration analysis\n"
        "   - New facility impact on total banking exposure\n"
        "   - Relationship banking history with CUB\n\n"
        "4. Credit Exposure Summary:\n"
        "   - Total committed facilities across all banks\n"
        "   - CUB's proposed share/percentage\n"
        "   - Cross-default risk if any bank withdraws\n\n"
        "Use pipe-table Markdown for all tables."
    ),
    9: (
        "Write Section 9: Credit Analysis Checklist.\n"
        "Follow Cathay United Bank credit report format exactly:\n\n"
        "1. Six-Category Compliance Checklist Table:\n"
        "   Category | Checklist Item | Status (✓/✗/N/A) | Remarks\n"
        "   Categories: (1) KYC & Compliance, (2) Sanctions & AML, "
        "(3) Credit Risk, (4) Legal & Documentation, "
        "(5) Environmental & ESG, (6) Regulatory (MAS/Banking Act)\n\n"
        "2. Conditions Precedent & Covenants Table:\n"
        "   No. | Type | Description | Threshold/Requirement | Frequency\n"
        "   Types: CP (condition precedent), ACR covenant, financial covenant, "
        "listing requirement, insurance requirement, information undertaking, "
        "negative pledge, change of control\n\n"
        "3. Formal Recommendation Paragraph:\n"
        "   Credit decision (APPROVE/DECLINE/CONDITIONAL), credit limit, tenor, "
        "security structure, key conditions — written as a formal recommendation\n\n"
        "4. Approval Authority: state name/title of approving officer and authority level\n\n"
        "5. Signoff details: date, officer, department\n\n"
        "Format all tables in pipe-table Markdown."
    ),
    10: (
        "Write Section 10: Appendix.\n"
        "Follow Cathay United Bank credit report format exactly. "
        "All tables must use pipe-table Markdown with actual figures:\n\n"
        "Appendix I — Group Exposure Table:\n"
        "   Entity | Facility Type | Limit (USD m) | Outstanding (USD m) | "
        "MSR Rating | Collateral | Expiry | Remarks\n"
        "   Include all entities in the borrower group with CUB exposure\n\n"
        "Appendix II — Fleet Growth Targets:\n"
        "   Year | Owned TEU | Managed TEU | Total TEU | YoY Growth %\n"
        "   Show 5-year fleet expansion plan (e.g. 2024-2029)\n\n"
        "Appendix III — DSCR Projections (Base Case & Worse Case):\n"
        "   Year | Period | Revenue | OPEX | EBITDA | Depreciation | "
        "Interest | Principal | Debt Service | DSCR | Outstanding Balance\n"
        "   Provide separate tables for Base Case and Worse Case scenarios\n\n"
        "Appendix IV — Sensitivity Analysis Table:\n"
        "   Scenario | Charter Rate (USD/day) | Min DSCR | LTV at Maturity | Conclusion\n\n"
        "Appendix V — Loan Repayment Schedule:\n"
        "   Period | Principal | Interest | Total | Outstanding Balance\n\n"
        "Blocking Data Gaps / QA Table (if applicable):\n"
        "   Section | Field | Gap | Data Source Needed\n"
        "   List any fields that could not be populated due to missing data\n\n"
        "Market Overview: shipping market conditions, CCFI/SCFI/BDI levels, 12-month outlook"
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


def build_section_prompt(
    section_no: int,
    input_json: dict,
    evidence_chunks: list[str],
    preceding_outputs: Optional[dict[int, str]] = None,
    is_continuation: bool = False,
    continuation_resume_token: Optional[str] = None,
) -> tuple[str, str]:
    """
    Build (system_prompt, user_prompt) for a single section generation call.

    When is_continuation=True the user_prompt instructs the model to continue
    from where it left off, using the resume token as a prefix.
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
            f"{evidence_block}"
            f"{preceding_block}\n\n"
            f"{OUTPUT_INSTRUCTIONS}"
        )

    return SYSTEM_PROMPT, user_prompt

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
    1: "Section 1 — Facility Structure & Key Terms",
    2: "Section 2 — Overall Comments",
    3: "Section 3 — Credit Risk Assessment",
    4: "Section 4 — Borrower Background",
    5: "Section 5 — Collateral Assessment",
    6: "Section 6 — Project Overview (Ship Finance)",
    7: "Section 7 — Financial Analysis",
    8: "Section 8 — Legal Documentation & Charges",
    9: "Section 9 — Compliance Checklist",
    10: "Section 10 — Appendix",
}

SECTION_INSTRUCTIONS: dict[int, str] = {
    1: (
        "Write Section 1: Facility Structure & Key Terms.\n"
        "Required content:\n"
        "1. Key Terms Summary Table: Borrower | Guarantor | Facility Type | Amount | Tenor | Repayment | "
        "Interest Rate | Margin | Commitment Fee | Governing Law\n"
        "2. Facility Description: purpose, availability period, drawdown conditions\n"
        "3. Repayment Schedule: payment frequency, balloon/bullet if any\n"
        "4. Pricing: base rate (SOFR/LIBOR), margin in bps, fees\n"
        "5. Collateral Summary: ranked list of all security documents\n"
        "6. Financial Covenants: DSCR threshold, leverage limit, minimum cash\n"
        "7. Information Covenants & Events of Default\n"
        "8. Syndicate Structure (if applicable): each lender and commitment\n"
        "Use a comprehensive key terms table at the start. All figures from analyst input."
    ),
    2: (
        "Write Section 2: Overall Comments (Executive Summary).\n"
        "CRITICAL: This section draws on ALL preceding section content. Write as the final synthesis.\n"
        "Required structure:\n"
        "1. Credit Decision: clearly state APPROVE / DECLINE / CONDITIONAL APPROVE with recommendation rationale\n"
        "2. Borrower Overview: 2-3 sentence summary of borrower strength, track record, market position\n"
        "3. Financial Summary: key ratios (DSCR, leverage, net margin) with 3-year trend — use actual figures\n"
        "4. Collateral Adequacy: LTC%, ACR% at delivery, LTV at maturity — with actual USD figures\n"
        "5. Key Strengths: 4-6 bullet points with supporting data\n"
        "6. Key Risks & Mitigants: paired table — Risk | Mitigant | Residual Risk\n"
        "7. DSCR Analysis: average DSCR over facility tenor, stress scenario outcome\n"
        "8. Conditions Precedent & Conditions Subsequent\n"
        "Be analytical — use specific numbers, not vague qualitative language."
    ),
    3: (
        "Write Section 3: Credit Risk Assessment.\n"
        "Required content:\n"
        "1. Internal Rating: rating assigned with full rationale — compare to peers\n"
        "2. Probability of Default (PD) Assessment: PD in bps, LGD%, Expected Loss\n"
        "3. MAS 612 Classification: Performing/Special Mention/Substandard/Doubtful\n"
        "4. Industry Risk Analysis:\n"
        "   - Shipping market cyclicality and current market conditions (BDI, sector rates)\n"
        "   - Tariff and geopolitical risk exposure\n"
        "   - IMO decarbonization risk (EEXI/CII compliance)\n"
        "5. ESG Assessment: Equator Principles category, environmental rating, Poseidon Principles alignment\n"
        "6. Sanctions Screening: table showing OFAC/EU/MAS/UN/HM Treasury — all Clear or flag hits\n"
        "7. Risk Matrix Table: Risk | Probability | Impact | Rating | Mitigation\n"
        "8. Concentration Risk: exposure vs. MAS 33-3 limit\n"
        "9. Country Risk: borrower and guarantor country risk assessment\n"
        "Include a Risk Matrix summary table."
    ),
    4: (
        "Write Section 4: Borrower Background.\n"
        "Required content:\n"
        "1. Corporate History: founding year, key milestones, group structure diagram description\n"
        "2. Ownership Structure: table of shareholders with stake %, UBO declaration\n"
        "3. Key Management: table with Name | Title | Experience | Background\n"
        "4. Business Operations: primary business description, trade routes, operational model\n"
        "5. Fleet Composition: table — Vessel | Type | DWT | Year Built | Flag | Class | Charter/Employment\n"
        "6. Major Customers & Contracts: table — Customer | Contract Type | Duration | Rate/Volume\n"
        "7. Financial Highlights: latest revenue, EBITDA, net income with commentary\n"
        "8. Market Position: ranking among peers, competitive advantages, market share\n"
        "9. Group Auditor and banking relationships\n"
        "Include fleet composition and customer tables."
    ),
    5: (
        "Write Section 5: Collateral Assessment.\n"
        "Required content:\n"
        "1. Collateral Overview: ranked list of all security instruments\n"
        "2. Vessel Valuation Table: Vessel | DWT | Year | Valuer | Market Value | Distressed Value | Date\n"
        "3. LTC Calculation: Loan Amount / Contract Price × 100 — show working\n"
        "4. ACR at Delivery: Market Value / Loan Amount × 100 — show working\n"
        "5. LTV Schedule: table showing outstanding balance vs. market value over tenor — LTV% and ACR%\n"
        "6. Refund Guarantee Analysis: issuer, credit rating, amount, expiry, coverage scope\n"
        "7. Insurance Coverage: H&M, P&I, War Risk — insured values and insurers\n"
        "8. Additional Security: corporate guarantee, share pledge — with credit assessment\n"
        "9. Collateral Adequacy Conclusion: summary judgment on adequacy\n"
        "All ratio calculations must show the formula and actual figures."
    ),
    6: (
        "Write Section 6: Project Overview (Ship Finance — New Build).\n"
        "Required content:\n"
        "1. Vessel Technical Specifications: DWT, GRT, LOA, beam, main engine, speed, fuel consumption, "
        "EEXI/CII rating, IMO Tier compliance\n"
        "2. Shipyard Profile: name, location, history, class approval, recent deliveries, track record\n"
        "3. Construction Timeline: milestone table — Milestone | Date | Payment % | USD Amount | Bank Funded\n"
        "4. Pre-Delivery Financing Structure: drawdown schedule, interest during construction\n"
        "5. Construction Risk Assessment: fixed-price vs. cost-plus, penalties for delay, supervision\n"
        "6. Post-Delivery Employment: charter party details — charterer, type, rate, duration, governing law\n"
        "7. Charterer Credit Assessment: credit rating, financial strength summary\n"
        "8. Project Risk Analysis: construction, delivery, employment, market risks and mitigants\n"
        "Include milestone payment table and employment details."
    ),
    7: (
        "Write Section 7: Financial Analysis (Most Detailed Section).\n"
        "Required content — ALL with actual figures in tables:\n"
        "1. Accounting Framework: auditor, opinion, standard (IFRS/GAAP), fiscal year end\n"
        "2. Income Statement Table: FY2022 | FY2023 | FY2024 — Revenue, OPEX, Gross Profit, EBITDA, "
        "Depreciation, EBIT, Interest, PBT, Tax, Net Income — plus YoY % change\n"
        "3. Balance Sheet Table: FY2022 | FY2023 | FY2024 — all major line items\n"
        "4. Cash Flow Table: FY2022 | FY2023 | FY2024 — CFO, CAPEX, CFI, CFF, Net Change\n"
        "5. Key Financial Ratios Table: DSCR | Debt/EBITDA | Debt/Equity | Current Ratio | "
        "Net Margin | ROA | ROE | Interest Cover — 3-year trend with commentary\n"
        "6. DSCR Analysis for This Facility: projection table with actual debt service figures\n"
        "7. FX Exposure: currencies, hedging policy, unhedged exposure\n"
        "8. Off-Balance Sheet Items: operating leases, contingent liabilities\n"
        "9. Accounting Notes: any restatements, significant accounting policies\n"
        "This section MUST include comprehensive data tables. Show YoY trends and variance analysis."
    ),
    8: (
        "Write Section 8: Legal Documentation & Charges.\n"
        "Required content:\n"
        "1. Facility Agreement Summary: type, date, parties, amount, key terms\n"
        "2. Security Documents Table: Document | Vessel/Asset | Amount | Status | Registration\n"
        "3. Existing Encumbrances Table: Charge | Beneficiary | Outstanding Amount | Maturity\n"
        "4. Priority of Security: explain ranking of charges — first, second priority\n"
        "5. Key Loan Protections: pari passu, negative pledge, cross-default threshold\n"
        "6. Legal Opinions Table: Jurisdiction | Law Firm | Date | Scope\n"
        "7. Registration Requirements: ACRA (Singapore), ship registry, timing\n"
        "8. Conditions Precedent: comprehensive bulleted list\n"
        "9. Conditions Subsequent: post-drawdown requirements and timeline\n"
        "10. Governing Law & Dispute Resolution: Singapore law, SIAC arbitration\n"
        "Include security documents and legal opinions in table format."
    ),
    9: (
        "Write Section 9: Compliance Checklist.\n"
        "Use a structured checklist format — ✓ PASS / ✗ FAIL / N/A for each item.\n"
        "Required sections:\n"
        "1. KYC/CDD Checklist: documents received, KYC tier, CDD level, review cycle\n"
        "2. AML Screening: adverse media result, transaction monitoring\n"
        "3. Sanctions Screening Table: OFAC | EU | MAS | UN | HM Treasury | OFSI — all results with dates\n"
        "4. PEP Assessment: PEP status, related/associated PEP check\n"
        "5. Tax Compliance: FATCA classification, CRS status, GIIN if applicable\n"
        "6. Environmental Compliance: EEXI rating | CII grade | Poseidon Principles alignment\n"
        "7. MAS Banking Act Section 33(3): single borrower exposure vs. limit — show calculation\n"
        "   Formula: Exposure / Capital Funds × 100% — must be < 15%\n"
        "8. Internal Approvals Required: Credit Committee, Board Risk Committee, Compliance sign-off\n"
        "9. Watch List & Country Risk: internal watch list status, country risk approval\n"
        "Format as a structured checklist table where appropriate."
    ),
    10: (
        "Write Section 10: Appendix.\n"
        "Required content — all as comprehensive data tables:\n"
        "1. DSCR Projection Table (full tenor): Year | Period | Revenue | OPEX | EBITDA | "
        "Depreciation | Interest | Principal | Debt Service | DSCR | Outstanding Balance\n"
        "2. Fleet Schedule Table: Vessel | Type | DWT | Year Built | Flag | Class | "
        "Charter/Employment | Market Value | Existing Mortgage\n"
        "3. Sensitivity / Stress Analysis Table: Scenario | Assumption | Min DSCR | Conclusion\n"
        "4. Loan Repayment Schedule (first 4 quarters minimum): Period | Principal | Interest | Total | Balance\n"
        "5. LTV/ACR Schedule: Year | Outstanding Balance | Market Value | LTV% | ACR%\n"
        "6. Market Overview: current market conditions (BDI/sector rates), 12-month outlook\n"
        "7. Glossary: key terms defined\n"
        "8. References: data sources, valuation reports cited\n"
        "All tables must use pipe-table Markdown syntax with actual figures."
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

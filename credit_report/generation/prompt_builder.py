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
        "Write Section 1: Credit Facility and Case Details.\n"
        "Required structure — follow Cathay United Bank credit report format exactly:\n\n"
        "1. Credit Facility Table: Multi-facility table with columns —\n"
        "   Item | Borrower | Booking Office | Current Facility (USD M) | Proposed Facility (USD M) | "
        "Outstanding (USD M) | CCY | Tenor | Facility Type | Collateral | Guarantor\n"
        "   Mark new facilities with [NEW]. Include ALL facilities for the borrower group.\n"
        "   Add numbered footnotes [1]-[n] below the table for complex items.\n\n"
        "2. Summary Line Items below table:\n"
        "   - Credit limit total: current USD Xm; proposed USD Xm; outstanding USD Xm\n"
        "   - PSR spot/derivative total: current USD Xm (including MTM USD Xm); proposed USD Xm\n"
        "   - Borrower-level proposed exposure total: USD Xm\n\n"
        "3. Regulatory Compliance subsection:\n"
        "   - State Banking Act Section 33-3 requirement: single-borrower unsecured exposure <= 5% Bank NW\n"
        "   - Bank net worth (TWD bn), 5% limit (TWD bn), USD equivalent, exchange rate date\n"
        "   - State whether borrower is a China-invested enterprise\n"
        "   - Compliance table: Borrower | Requirement | Limit | Compliance\n"
        "   - Credit breakdown table: Facility | Credit Limit | Unsecured | Secured\n"
        "   - PAM/SAM unsecured drawdown cap statement (% of contract value and USD amount)\n\n"
        "4. Group Limit subsection: total proposed group utilization vs. approved group limit\n\n"
        "5. Purpose of Report: 2-3 paragraph narrative describing the proposed new facility, "
        "vessel details, security structure, and any ancillary amendments\n\n"
        "6. Terms & Conditions table (two-column: Term | Detail) covering ALL of:\n"
        "   Borrower/Owner | Guarantor | Vessel (specs) | Lender | Facility | Facility Purpose | "
        "Facility Amount/Commitment (with formula) | Availability Period | Maturity Date | "
        "Repayment | Mandatory Prepayment | Drawdown (max drawdowns, caps) | Conditions Precedent | "
        "Other Conditions | Upfront Fee (% and USD amount + annual renewal) | Pricing | "
        "Interest Period | Security and Security Documents (pre-delivery / post-delivery) | "
        "Value Maintenance Clause and Fair Market Value (ACR min%, LTV max%, testing frequency, cure) | "
        "Sustainability-Linked KPI (if applicable) | Financial Covenants\n\n"
        "7. Conditions Precedent: full narrative paragraph listing all CPs\n\n"
        "8. Deal Comparison Table: Term | Proposed Deal | Previous Deal — "
        "compare key terms side by side to show consistency/deviation\n\n"
        "All figures must come from analyst input. Use pipe-table Markdown for all tables."
    ),
    2: (
        "Write Section 2: Overall Comments.\n"
        "CRITICAL: This section synthesises ALL other sections. Generate it LAST.\n"
        "Follow Cathay United Bank credit report format exactly:\n\n"
        "1. Credit Overview Table — two columns: Topic | Comment\n"
        "   Required rows (use actual figures, not vague language):\n"
        "   - Borrower and group: entity description, fleet size/TEU capacity, global ranking, market share\n"
        "   - Listed parent: stock exchange, ticker, revenue (TWD/USD), net income (latest FY)\n"
        "   - Balance sheet: net cash/debt position with date; additional debt capacity; D/E ratio\n"
        "   - Proposed transaction: facility amount, LTC%, vessel type/TEU, delivery date, security summary\n"
        "   - Market context: SCFI/BDI level and YoY change, market drivers, alliance membership benefit\n"
        "   - Main risks: top 3 risks with key mitigants in one sentence each\n\n"
        "2. Solvency Table — two columns: Topic | Comment\n"
        "   Required rows:\n"
        "   - Borrower metrics (FY20XX): total assets, total equity, cash, total debt, net cash/debt, "
        "revenue, EBITDA, net income, D/E, current ratio, interest coverage — all in USD\n"
        "   - Guarantor metrics (FY20XX/QX20XX): same metrics in local currency and USD equivalent; "
        "state the as-of date clearly\n\n"
        "3. Key Strengths: 4-6 bulleted points with specific numbers (amounts, ratios, dates)\n\n"
        "4. Key Risks & Mitigants Table: Risk | Mitigant | Residual Risk\n"
        "   Include at least 4 risks (market, credit, construction/delivery, regulatory)\n\n"
        "5. Collateral Adequacy Summary:\n"
        "   - LTC% at drawdown\n"
        "   - ACR minimum% (post-delivery covenant)\n"
        "   - LTV maximum% (post-delivery covenant)\n"
        "   - Pre-delivery: refund guarantee coverage\n"
        "   - Value maintenance clause: testing frequency and cure mechanism\n\n"
        "6. Credit Decision: state APPROVE / DECLINE / CONDITIONAL APPROVE\n"
        "   Provide recommendation rationale (2-3 sentences with key supporting data)\n\n"
        "7. Conditions Precedent: bulleted list\n\n"
        "Use actual figures from input data. Never use vague qualitative statements without numbers."
    ),
    3: (
        "Write Section 3: Credit Ratings.\n"
        "Follow Cathay United Bank credit report format exactly:\n\n"
        "1. Internal MSR Rating Table:\n"
        "   MSR Rating | MAS 612 Classification | Description\n"
        "   Show the borrower's assigned MSR (e.g. MSR3) mapped to MAS 612 category (Performing / "
        "Special Mention / etc.) with full rationale. Compare to industry peer MSR range.\n\n"
        "2. External ESG Ratings Table:\n"
        "   Rating Agency | Score/Grade | Percentile/Rank | As-of Date\n"
        "   Include: MSCI ESG (e.g. BBB), Sustainalytics Risk Score (e.g. 25.8), "
        "Taiwan CG Corporate Governance ranking percentile.\n\n"
        "3. Industry Risk Assessment: shipping sector cyclicality, tariff exposure, "
        "alliance concentration risk, IMO decarbonization (EEXI/CII) timeline risk\n\n"
        "4. Country Risk: borrower and guarantor incorporation country ratings\n\n"
        "5. ESG & Climate Risk: Poseidon Principles alignment, carbon intensity trajectory, "
        "climate transition risk, green financing eligibility\n\n"
        "6. Sanctions Screening Summary Table:\n"
        "   Screening List | Entity Screened | Result | Date\n"
        "   Cover: OFAC, EU, MAS, UN, HM Treasury — state 'Clear' or any hits\n\n"
        "7. Key Risks & Mitigants: 3-5 rows — Risk | Mitigant | Residual Rating\n\n"
        "All ratings must come from input data. State the rating scale clearly."
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

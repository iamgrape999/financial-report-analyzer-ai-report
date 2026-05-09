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
        "Cover: borrower, guarantor, facility type and amount, tenor, purpose of facility, "
        "repayment schedule, interest rate/margin, collateral summary, covenants, governing law.\n"
        "Use sub-headings and a key terms summary table."
    ),
    2: (
        "Write Section 2: Overall Comments.\n"
        "Provide an executive summary covering: borrower solvency (key ratios with figures), "
        "guarantor solvency, collateral adequacy (LTC, ACR/LTV), key risks and mitigants, "
        "DSCR adequacy, and the overall credit recommendation.\n"
        "This section is read first by senior management — be concise and analytical."
    ),
    3: (
        "Write Section 3: Credit Risk Assessment.\n"
        "Cover: probability of default assessment, internal rating rationale, "
        "MAS 612 regulatory considerations, ESG and sanctions screening results, "
        "industry risk (marine/shipping cyclicality, tariff exposure), "
        "and borrower-specific risks and mitigants."
    ),
    4: (
        "Write Section 4: Borrower Background.\n"
        "Cover: corporate history and ownership structure, business operations, "
        "fleet composition and capacity, key management team, "
        "market position and competitive landscape, and major customers and contracts."
    ),
    5: (
        "Write Section 5: Collateral Assessment.\n"
        "Cover: collateral type (vessel mortgage, refund guarantee, etc.), "
        "valuation methodology, LTC/ACR ratios with calculations, "
        "LTV/ACR schedule over the loan tenor, and collateral adequacy conclusion."
    ),
    6: (
        "Write Section 6: Project Overview.\n"
        "Cover: vessel specifications, shipyard details, construction milestones, "
        "delivery schedule, pre-delivery financing structure, "
        "expected employment (charter party or spot), and project risk analysis."
    ),
    7: (
        "Write Section 7: Financial Analysis.\n"
        "This is the most detailed section. Cover: 3-year income statement trends, "
        "balance sheet highlights, cash flow analysis, key financial ratios table "
        "(leverage, liquidity, profitability, coverage), DSCR calculation, "
        "FX exposure, and any restatements or accounting notes.\n"
        "Include data tables with actual figures for all presented periods."
    ),
    8: (
        "Write Section 8: Legal Documentation & Charges.\n"
        "Cover: list of security documents, registered charges and their priority, "
        "lender banks and amounts, any existing encumbrances, "
        "legal opinions obtained, and governing law and jurisdiction."
    ),
    9: (
        "Write Section 9: Compliance Checklist.\n"
        "Provide a structured checklist covering: KYC/AML status, "
        "sanctions screening (OFAC, EU, MAS), PEP checks, "
        "environmental/ESG compliance, regulatory approvals required, "
        "and Banking Act Section 33-3 concentration limit check."
    ),
    10: (
        "Write Section 10: Appendix.\n"
        "Include: DSCR projection tables for the facility tenor, "
        "fleet capacity tables, financial projections if available, "
        "and any supporting data referenced in the main report."
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

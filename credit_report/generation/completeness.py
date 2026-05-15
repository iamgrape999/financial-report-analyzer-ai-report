"""
Section completeness validation and auto-fill for mandatory sub-sections.

Why this module exists:
- §2 requires exactly 5 tables (T1-T5). If the AI truncates at T1 or T2,
  the analyst sees an incomplete report with no indication of what is missing.
- With a 16 384-token budget, §2 fits comfortably — but edge cases exist
  (very large input JSON, long guarantor histories) where the AI still stops early
  without writing the continuation token.
- This module detects gaps and issues a targeted fill call for only the missing tables.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Required table markers per section ───────────────────────────────────────
# Each entry is a (marker_text, human_label) pair.
# We search for the marker_text (case-insensitive) inside the generated markdown.
# The human_label is used in log messages and fill prompts.

SECTION_REQUIRED_TABLES: dict[int, list[tuple[str, str]]] = {
    2: [
        ("**Credit Overview**",                          "T1 Credit Overview"),
        ("**Solvency**",                                 "T2 Solvency"),
        ("**The Guarantor and their Supportive",         "T3 Guarantor and Supportive Performance"),
        ("**Collateral Summary**",                       "T4 Collateral Summary"),
        ("**Risk and Mitigants**",                       "T5 Risk and Mitigants"),
    ],
}


def check_section_completeness(section_no: int, markdown: str) -> list[tuple[str, str]]:
    """
    Return a list of (marker, label) pairs for tables that are absent from *markdown*.
    Returns empty list if the section has no completeness requirements or all tables are present.
    """
    required = SECTION_REQUIRED_TABLES.get(section_no)
    if not required:
        return []

    md_lower = markdown.lower()
    missing = [
        (marker, label)
        for marker, label in required
        if marker.lower() not in md_lower
    ]
    return missing


def _build_fill_system_prompt(section_no: int) -> str:
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

    if section_no == 2:
        existing_snippet = existing_markdown[-1200:] if len(existing_markdown) > 1200 else existing_markdown
        return (
            f"The following tables are MISSING from the already-generated §2 output: {missing_labels}\n\n"
            f"TAIL OF EXISTING OUTPUT (last 1200 chars — for context only, do NOT repeat):\n"
            f"```\n{existing_snippet}\n```\n\n"
            f"INPUT DATA:\n```json\n{_json.dumps(input_json, ensure_ascii=False, indent=2)[:6000]}\n```\n\n"
            f"REQUIRED OUTPUT LANGUAGE: {output_language}\n\n"
            "Now output ONLY the missing tables in the exact two-column Markdown format. "
            "No heading, no explanation. Start directly with the first missing table."
        )

    return (
        f"Missing sections: {missing_labels}\n\n"
        f"Input JSON: {_json.dumps(input_json, ensure_ascii=False)[:4000]}\n\n"
        "Output ONLY the missing sections."
    )


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
    Call Gemini to generate only the missing tables and return
    (appended_markdown, tokens_used).

    The caller is responsible for appending the result to the existing markdown.
    """
    from credit_report.generation.claude_client import call_gemini_raw
    from credit_report.config import CR_SECTION_MAX_TOKENS

    system_prompt = _build_fill_system_prompt(section_no)
    user_prompt = _build_fill_user_prompt(
        section_no, missing, existing_markdown, input_json, output_language
    )

    # Use a generous budget: up to 8192 tokens for fill (typically much less needed)
    max_tokens = min(CR_SECTION_MAX_TOKENS, 8192)

    logger.info(
        "[Completeness] fill call section=%d missing=%s",
        section_no, [label for _, label in missing],
    )

    fill_text = await call_gemini_raw(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        api_key=api_key,
        model_id=model_id,
    )

    # Rough token estimate: call_gemini_raw doesn't return usage metadata
    # We use char-count / 4 as a conservative proxy (Gemini averages ~3.5 chars/token)
    estimated_tokens = (len(user_prompt) + len(fill_text)) // 4

    return fill_text, estimated_tokens

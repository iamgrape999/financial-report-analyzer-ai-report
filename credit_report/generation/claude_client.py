from __future__ import annotations

from typing import Optional

import anthropic

from credit_report.config import (
    ANTHROPIC_API_KEY,
    CONTINUATION_END_TOKENS,
    CONTINUATION_RESUME_TOKENS,
    CREDIT_REPORT_MODEL,
    CR_SECTION_MAX_TOKENS,
    SECTION_MAX_OUTPUT_TOKENS,
)
from credit_report.generation.prompt_builder import build_section_prompt

MAX_CONTINUATION_ROUNDS = 3


def _detect_continuation_token(text: str, section_no: int) -> bool:
    """Return True if text ends with the section's continuation marker."""
    token = CONTINUATION_END_TOKENS.get(section_no)
    return bool(token and token in text)


def _strip_continuation_token(text: str, section_no: int) -> str:
    """Remove the continuation marker from text and strip trailing whitespace."""
    token = CONTINUATION_END_TOKENS.get(section_no)
    if token:
        text = text.replace(token, "")
    return text.strip()


async def generate_section_markdown(
    section_no: int,
    input_json: dict,
    evidence_chunks: list[str],
    preceding_outputs: Optional[dict[int, str]] = None,
    api_key: Optional[str] = None,
    model_id: Optional[str] = None,
) -> tuple[str, int]:
    """
    Call Claude and assemble multi-part section Markdown with continuation support.

    Returns (full_markdown, total_tokens_used).
    """
    key = api_key or ANTHROPIC_API_KEY
    model = model_id or CREDIT_REPORT_MODEL
    max_tokens = SECTION_MAX_OUTPUT_TOKENS.get(section_no) or CR_SECTION_MAX_TOKENS

    client = anthropic.AsyncAnthropic(api_key=key)

    parts: list[str] = []
    total_tokens = 0

    for round_no in range(MAX_CONTINUATION_ROUNDS):
        is_continuation = round_no > 0
        resume_token = CONTINUATION_RESUME_TOKENS.get(section_no) if is_continuation else None

        system_prompt, user_prompt = build_section_prompt(
            section_no=section_no,
            input_json=input_json,
            evidence_chunks=evidence_chunks,
            preceding_outputs=preceding_outputs,
            is_continuation=is_continuation,
            continuation_resume_token=resume_token,
        )

        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = response.content[0].text if response.content else ""
        total_tokens += response.usage.input_tokens + response.usage.output_tokens

        needs_continuation = _detect_continuation_token(text, section_no)
        parts.append(_strip_continuation_token(text, section_no))

        if not needs_continuation or response.stop_reason != "end_turn":
            break

    return "\n\n".join(parts).strip(), total_tokens

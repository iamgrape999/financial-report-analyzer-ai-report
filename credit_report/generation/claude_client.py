from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from google import genai
from google.genai import types as genai_types

from credit_report.config import (
    CONTINUATION_END_TOKENS,
    CONTINUATION_RESUME_TOKENS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    CR_SECTION_MAX_TOKENS,
    LLM_TIMEOUT_SECONDS,
    SECTION_MAX_OUTPUT_TOKENS,
)
from credit_report.generation.prompt_builder import build_section_prompt

MAX_CONTINUATION_ROUNDS = 3

logger = logging.getLogger(__name__)

_API_KEY_RE = re.compile(r'AIza[A-Za-z0-9_\-]{35}')
_BEARER_RE = re.compile(r'Bearer\s+[A-Za-z0-9._\-]{20,}')


def _sanitize_error(msg: str) -> str:
    """Strip API keys and bearer tokens from error strings before logging or raising."""
    msg = _API_KEY_RE.sub('AIza***REDACTED***', msg)
    msg = _BEARER_RE.sub('Bearer ***REDACTED***', msg)
    return msg


class GenerationError(Exception):
    pass


class GenerationRateLimitError(GenerationError):
    """Gemini API rate limit (429) — caller should surface HTTP 429."""
    retry_after_seconds: int = 60


class GenerationConfigError(GenerationError):
    """API key missing or authentication rejected — caller should surface HTTP 503."""
    pass


async def call_gemini_raw(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2048,
    api_key: Optional[str] = None,
    model_id: Optional[str] = None,
) -> str:
    """Single-turn Gemini call for ad-hoc tasks (paragraph improvement, QA, etc.)."""
    key = api_key or GEMINI_API_KEY
    if not key:
        raise ValueError("GEMINI_API_KEY is not configured.")
    model = model_id or GEMINI_MODEL
    client = genai.Client(api_key=key)
    cfg = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=max_tokens,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )
    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=user_prompt,
                config=cfg,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("call_gemini_raw: timeout after %ds", LLM_TIMEOUT_SECONDS)
        raise TimeoutError(f"Gemini API did not respond within {LLM_TIMEOUT_SECONDS}s")
    return (response.text or "").strip()


def _detect_continuation_token(text: str, section_no: int) -> bool:
    token = CONTINUATION_END_TOKENS.get(section_no)
    return bool(token and token in text)


def _strip_continuation_token(text: str, section_no: int) -> str:
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
    output_language: str = "en",
    industry: str = "marine",
) -> tuple[str, int]:
    """
    Call Gemini and assemble multi-part section Markdown with continuation support.

    Returns (full_markdown, total_tokens_used).
    """
    key = api_key or GEMINI_API_KEY
    if not key:
        raise ValueError("GEMINI_API_KEY is not configured. Set it in Render environment variables to enable AI generation.")
    model = model_id or GEMINI_MODEL
    max_tokens = SECTION_MAX_OUTPUT_TOKENS.get(section_no) or CR_SECTION_MAX_TOKENS

    client = genai.Client(api_key=key)

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
            output_language=output_language,
            industry=industry,
        )

        cfg = genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=cfg,
                ),
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "generate_section_markdown: timeout section=%d round=%d after %ds",
                section_no, round_no, LLM_TIMEOUT_SECONDS,
            )
            raise
        except Exception as exc:
            safe = _sanitize_error(str(exc))
            exc_type = type(exc).__name__
            if "ResourceExhausted" in exc_type or "429" in safe or "quota" in safe.lower():
                logger.warning(
                    "generate_section_markdown: rate limited section=%d round=%d: %s",
                    section_no, round_no, safe,
                )
                raise GenerationRateLimitError(
                    f"Gemini API rate limit hit for section {section_no}: {safe}"
                ) from exc
            if any(k in exc_type for k in ("Unauthenticated", "PermissionDenied")) or \
               any(k in safe for k in ("401", "403", "API key", "api_key")):
                logger.error(
                    "generate_section_markdown: auth error section=%d: %s",
                    section_no, safe,
                )
                raise GenerationConfigError(
                    f"Gemini API authentication error for section {section_no}: {safe}"
                ) from exc
            logger.error(
                "generate_section_markdown: unexpected error section=%d round=%d: %s",
                section_no, round_no, safe,
            )
            raise

        text = response.text or ""
        usage = response.usage_metadata
        if usage:
            total_tokens += (usage.prompt_token_count or 0) + (usage.candidates_token_count or 0)

        needs_continuation = _detect_continuation_token(text, section_no)
        parts.append(_strip_continuation_token(text, section_no))

        finish_reason = str(
            response.candidates[0].finish_reason if response.candidates else ""
        )
        logger.debug(
            "generate_section_markdown: section=%d round=%d finish=%s continuation=%s",
            section_no, round_no, finish_reason, needs_continuation,
        )
        # Continue if the AI explicitly wrote the continuation token.
        # Previously broke when finish_reason == MAX_TOKENS even if the token was present.
        if not needs_continuation:
            break

    return "\n\n".join(parts).strip(), total_tokens

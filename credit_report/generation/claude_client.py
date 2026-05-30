from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from google import genai
from google.genai import types as genai_types

from credit_report.config import (
    CONTINUATION_END_TOKENS,
    CONTINUATION_RESUME_TOKENS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    CR_SECTION_MAX_TOKENS,
    LLM_MAX_RETRIES,
    LLM_RETRY_BASE_DELAY,
    LLM_TIMEOUT_SECONDS,
    SECTION_MAX_OUTPUT_TOKENS,
)
from credit_report.generation.prompt_builder import build_section_prompt

MAX_CONTINUATION_ROUNDS = 3

logger = logging.getLogger(__name__)

# HTTP status codes that are safe to retry (transient server-side conditions).
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


async def _call_with_retry(
    coro_fn: Callable[[], Any],
    *,
    max_retries: int = LLM_MAX_RETRIES,
    base_delay: float = LLM_RETRY_BASE_DELAY,
    label: str = "LLM call",
) -> Any:
    """Run coro_fn() with exponential backoff on transient errors.

    Retries on: asyncio.TimeoutError, HTTP 429/500/502/503/504.
    Does NOT retry on: ValueError (bad key/prompt), HTTP 400/401/403.
    """
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except asyncio.TimeoutError as exc:
            if attempt == max_retries:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "%s: timeout (attempt %d/%d), retrying in %.1fs",
                label, attempt + 1, max_retries, delay,
            )
            await asyncio.sleep(delay)
        except Exception as exc:
            # Inspect status code from google-genai or httpx exceptions.
            status = (
                getattr(exc, "status_code", None)
                or getattr(exc, "code", None)
                or getattr(getattr(exc, "response", None), "status_code", None)
            )
            if status in _RETRYABLE_STATUS_CODES and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "%s: HTTP %s (attempt %d/%d), retrying in %.1fs: %s",
                    label, status, attempt + 1, max_retries, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                raise


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
    )

    async def _call() -> str:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=user_prompt,
                config=cfg,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
        return (response.text or "").strip()

    try:
        return await _call_with_retry(_call, label="call_gemini_raw")
    except asyncio.TimeoutError:
        logger.error("call_gemini_raw: timeout after %ds (all retries exhausted)", LLM_TIMEOUT_SECONDS)
        raise TimeoutError(f"Gemini API did not respond within {LLM_TIMEOUT_SECONDS}s")


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
    industry: str = "tw_shipping",
    institution_name: str = "the Bank",
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
            institution_name=institution_name,
        )

        cfg = genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens,
        )

        _round = round_no  # capture for closure

        async def _call() -> Any:
            return await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=cfg,
                ),
                timeout=LLM_TIMEOUT_SECONDS,
            )

        try:
            response = await _call_with_retry(
                _call,
                label=f"generate_section_markdown section={section_no} round={_round}",
            )
        except asyncio.TimeoutError:
            logger.error(
                "generate_section_markdown: timeout section=%d round=%d after %ds (all retries exhausted)",
                section_no, round_no, LLM_TIMEOUT_SECONDS,
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
        if not needs_continuation:
            break

    return "\n\n".join(parts).strip(), total_tokens

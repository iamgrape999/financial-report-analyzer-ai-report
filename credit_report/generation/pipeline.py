from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from credit_report.audit.events import write_event


def _strip_qa_output(markdown: str) -> str:
    """
    Remove AI-generated QA gate output from section markdown before saving.

    Sections §1/§3/§7/§8/§9/§10 instruct the AI to PRINT QA results at the end
    of the output. These are internal self-checks (not part of the final credit
    report) and must be stripped before the analyst sees the text.

    Formats stripped:
    - Verbose block:  **QA Gate Results:** ...  (§3 style bullet list)
    - Compact inline: [QA] G1:✅ G2:✅ ...      (§7/§8/§9/§10 style)
    - Bracket items:  [QA-J1: PASS/FAIL] ...    (§1/§3 alternate style)
    """
    original = markdown
    # Verbose block starting with optional bold "QA Gate Results:" heading
    markdown = re.sub(
        r"\*?\*?QA[\s_]Gate[\s_]Results?:?\*?\*?\s*\n.*",
        "",
        markdown,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Compact single-line: [QA] or [QA-Jx: ...] — remove the whole line
    markdown = re.sub(
        r"^\[QA[^\]]*\][^\n]*\n?",
        "",
        markdown,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    # Only rstrip trailing whitespace when QA content was actually removed;
    # leave the original trailing whitespace intact when there is nothing to strip.
    return markdown.rstrip() if markdown != original else markdown


def _deduplicate_section1(markdown: str) -> str:
    """
    Safety net for §1: Gemini sometimes self-corrects by re-appending T&C + Account
    Strategy after the QA Gate fails.  If 'Account Strategy' (the last major §1
    sub-section) appears as a heading/bold line more than once, truncate before the
    second occurrence to eliminate the duplicate tail.
    """
    pattern = re.compile(
        r"^[ \t]*(?:#{1,3}[ \t]+)?(?:\*{1,2})?account\s+strategy(?:\*{1,2})?[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(markdown))
    if len(matches) >= 2:
        cut = matches[1].start()
        logger.info("[Dedup] §1 duplicate 'Account Strategy' removed at char pos=%d", cut)
        return markdown[:cut].rstrip()
    return markdown


def _deduplicate_section2(markdown: str) -> str:
    """
    Safety net for §2: 5-table structure. If 'Risk and Mitigants' (last table T5)
    appears as a bold cell/heading more than once, truncate before the second occurrence.
    """
    pattern = re.compile(
        r"(?:^\|[ \t]*\*?\*?Risk and Mitigants\*?\*?[ \t]*\||"
        r"^[ \t]*(?:#{1,3}[ \t]+)?(?:\*{1,2})?Risk and Mitigants(?:\*{1,2})?[ \t]*$)",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(markdown))
    if len(matches) >= 2:
        cut = matches[1].start()
        logger.info("[Dedup] §2 duplicate 'Risk and Mitigants' removed at char pos=%d", cut)
        return markdown[:cut].rstrip()
    return markdown


def _deduplicate_section3(markdown: str) -> str:
    """
    Safety net for §3: ESG is the last mandatory sub-section. If 'ESG' appears as a
    standalone heading/bold marker more than once, truncate before the second occurrence.
    """
    pattern = re.compile(
        r"^[ \t]*(?:#{1,3}[ \t]+)?(?:\*{1,2})?ESG(?:\*{1,2})?[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(markdown))
    if len(matches) >= 2:
        cut = matches[1].start()
        logger.info("[Dedup] §3 duplicate 'ESG' heading removed at char pos=%d", cut)
        return markdown[:cut].rstrip()
    return markdown


from credit_report.config import (
    CR_MAX_CONCURRENT_GENERATIONS,
    GEMINI_MODEL,
    GENERATION_ORDER,
    LLM_TIMEOUT_SECONDS,
    SECTION_HARD_DEPENDENCIES,
)
from credit_report.generation.claude_client import (
    generate_section_markdown,
    GenerationConfigError,
    GenerationRateLimitError,
)
from credit_report.generation.evidence import retrieve_evidence
from credit_report.generation.quota import check_quota, reserve_and_record_tokens
from credit_report.models import Report, SectionInput, SectionOutput

_generation_semaphore = asyncio.Semaphore(CR_MAX_CONCURRENT_GENERATIONS)
_MIN_SECTION_CHARS = 150


async def get_section_output(
    db: AsyncSession, report_id: str, section_no: int
) -> Optional[SectionOutput]:
    result = await db.execute(
        select(SectionOutput).where(
            SectionOutput.report_id == report_id,
            SectionOutput.section_no == section_no,
        ).order_by(SectionOutput.id)
    )
    return result.scalars().first()


async def check_hard_dependencies(
    db: AsyncSession, report_id: str, section_no: int
) -> list[int]:
    """Return list of hard-dependency section numbers that are not yet done."""
    deps = SECTION_HARD_DEPENDENCIES.get(section_no, [])
    missing: list[int] = []
    for dep_no in deps:
        output = await get_section_output(db, report_id, dep_no)
        if not output or output.status != "done":
            missing.append(dep_no)
    return missing


async def run_section_generation(
    db: AsyncSession,
    report_id: str,
    section_no: int,
    actor_user_id: str,
    actor_role: str = "analyst",
    preceding_outputs: Optional[dict[int, str]] = None,
    output_language: str = "en",
) -> SectionOutput:
    """
    Run the full generation pipeline for a single section.

    Steps:
      1. Enforce per-user daily token quota (raises 429 if exhausted).
      2. Load the analyst JSON input for this section.
      3. Retrieve keyword-matched evidence chunks from uploaded PDFs.
      4. Mark the SectionOutput record as "generating" (upsert).
      5. Call Gemini (rate-limited by _generation_semaphore).
      6. Record tokens consumed against the user's daily quota.
      7. Persist the result and write an audit event.
    """
    # Step 1: quota gate — fail fast before any expensive work
    await check_quota(db, actor_user_id, role=actor_role)

    si_result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        ).order_by(SectionInput.id.desc())
    )
    si = si_result.scalars().first()
    input_json: dict = json.loads(si.input_json) if si and si.input_json else {}

    if not input_json:
        logger.info(
            "run_section_generation: no structured input section=%d report=%s — generating from evidence only",
            section_no, report_id,
        )

    # Inject Report.report_type into input_json["metadata"] so build_section_prompt()
    # can surface the correct analytical context hint (YoY, risk flags, etc.) without
    # requiring the analyst to manually embed the type in each section's input JSON.
    report_result = await db.execute(select(Report).where(Report.id == report_id))
    _report = report_result.scalar_one_or_none()
    if _report and _report.report_type:
        meta = input_json.setdefault("metadata", {})
        if "report_type" not in meta:
            meta["report_type"] = _report.report_type

    # §7 Financial Analysis: enrich with pre-computed ratios from the calculation engine.
    # This prevents the AI from re-deriving DSCR/LTV/ACR and introduces hallucination risk.
    if section_no == 7:
        try:
            from credit_report.api.calculations import get_calc_results_for_prompt
            calc_context = await get_calc_results_for_prompt(db, report_id)
            if calc_context:
                input_json = {**input_json, "__calc_results": calc_context}
                logger.info(
                    "[Calc] injected %d calc results into §7 prompt report=%s",
                    len(calc_context), report_id,
                )
        except Exception as _ce:
            logger.warning("[Calc] failed to load calc results for §7 report=%s: %s", report_id, _ce)

    evidence_chunks = retrieve_evidence(report_id, section_no)
    logger.info("run_section_generation: starting section=%d report=%s user=%s evidence_chunks=%d preceding=%s", section_no, report_id, actor_user_id, len(evidence_chunks), list(preceding_outputs.keys()) if preceding_outputs else [])

    existing = await get_section_output(db, report_id, section_no)
    if existing:
        existing.status = "generating"
        output = existing
    else:
        output = SectionOutput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            status="generating",
        )
        db.add(output)
    await db.flush()

    try:
        async with _generation_semaphore:
            markdown, tokens_used = await generate_section_markdown(
                section_no=section_no,
                input_json=input_json,
                evidence_chunks=evidence_chunks,
                preceding_outputs=preceding_outputs,
                output_language=output_language,
            )

        markdown = _strip_qa_output(markdown)
        if section_no == 1:
            markdown = _deduplicate_section1(markdown)
        elif section_no == 2:
            markdown = _deduplicate_section2(markdown)
        elif section_no == 3:
            markdown = _deduplicate_section3(markdown)

        # Quality gate — guard against empty/trivial Gemini responses
        if not markdown or not markdown.strip():
            logger.error(
                "run_section_generation: EMPTY output section=%d report=%s tokens=%d — "
                "marking as error to prevent blank section being saved",
                section_no, report_id, tokens_used,
            )
            output.status = "error"
            output.model_id = GEMINI_MODEL
            output.tokens_used = tokens_used
            raise RuntimeError(
                f"Gemini returned empty markdown for §{section_no} — will not save blank section"
            )
        if len(markdown.strip()) < _MIN_SECTION_CHARS:
            logger.warning(
                "run_section_generation: SHORT output section=%d report=%s chars=%d "
                "(< %d minimum) — saving but flagging",
                section_no, report_id, len(markdown.strip()), _MIN_SECTION_CHARS,
            )

        output.markdown = markdown
        output.status = "done"
        output.model_id = GEMINI_MODEL
        output.tokens_used = tokens_used
        output.generated_at = datetime.now(timezone.utc)
        logger.info("run_section_generation: done section=%d report=%s tokens=%d model=%s chars=%d", section_no, report_id, tokens_used, GEMINI_MODEL, len(markdown))

        # ── Completeness check + auto-fill for sections with mandatory sub-tables ──
        try:
            from credit_report.generation.completeness import (
                check_section_completeness,
                fill_missing_tables,
            )
            missing = check_section_completeness(section_no, markdown, input_json)
            if missing:
                missing_labels = [label for _, label in missing]
                logger.warning(
                    "[Completeness] section=%d report=%s missing=%s — triggering fill",
                    section_no, report_id, missing_labels,
                )
                fill_text, fill_tokens = await fill_missing_tables(
                    section_no=section_no,
                    existing_markdown=markdown,
                    missing=missing,
                    input_json=input_json,
                    output_language=output_language,
                )
                if fill_text:
                    # §3 MSR empty-table: replace the broken partial block inline
                    # so section order (External→Internal→MAS612→ESG) is preserved.
                    # Falls back to normal append when no broken block is found.
                    if section_no == 3:
                        from credit_report.generation.completeness import replace_empty_msr_block
                        replaced = replace_empty_msr_block(markdown, fill_text)
                        markdown = replaced if replaced is not None else markdown.rstrip() + "\n\n" + fill_text
                    else:
                        markdown = markdown.rstrip() + "\n\n" + fill_text
                    markdown = _strip_qa_output(markdown)
                    if section_no == 1:
                        markdown = _deduplicate_section1(markdown)
                    elif section_no == 2:
                        markdown = _deduplicate_section2(markdown)
                    elif section_no == 3:
                        markdown = _deduplicate_section3(markdown)
                    tokens_used += fill_tokens
                    output.markdown = markdown
                    output.tokens_used = tokens_used
                    logger.info(
                        "[Completeness] fill done section=%d report=%s fill_chars=%d total_tokens=%d",
                        section_no, report_id, len(fill_text), tokens_used,
                    )
                    # Verify fill resolved all gaps; warn if still incomplete
                    still_missing = check_section_completeness(section_no, markdown, input_json)
                    if still_missing:
                        logger.warning(
                            "[Completeness] still missing after fill section=%d report=%s missing=%s",
                            section_no, report_id, [l for _, l in still_missing],
                        )
        except Exception as _comp_err:
            logger.warning(
                "[Completeness] check/fill failed section=%d report=%s: %s",
                section_no, report_id, _comp_err,
            )
        # ─────────────────────────────────────────────────────────────────────

        # ── Block AST parsing (savepoint — failure rolls back AST only, not main tx) ──
        try:
            from credit_report.block_ast.builder import build_blocks
            from credit_report.block_ast.repository import save_blocks
            from credit_report.fact_store.repository import get_facts_for_report

            raw_facts = await get_facts_for_report(db, report_id)
            facts_payload = [
                {"fact_id": f.id, "value": f.value, "value_text": f.value_text}
                for f in raw_facts
                if f.value is not None
            ]
            blocks, cells = build_blocks(report_id, section_no, markdown, facts_payload)
            async with db.begin_nested():
                await save_blocks(db, blocks, cells)
            logger.info("[AST] section=%d report=%s blocks=%d cells=%d",
                        section_no, report_id, len(blocks), len(cells))
        except Exception as _ast_err:
            logger.warning("[AST] build_blocks failed section=%d report=%s: %s",
                           section_no, report_id, _ast_err)
        # ─────────────────────────────────────────────────────────────────────

        # Record consumption atomically — raises 429 if limit exceeded post-generation
        await reserve_and_record_tokens(db, actor_user_id, tokens_used, role=actor_role)

        await write_event(
            db,
            action="section.generated",
            actor_user_id=actor_user_id,
            actor_role="system",
            report_id=report_id,
            target_type="section_output",
            target_id=f"{report_id}/{section_no}",
            after=f"tokens={tokens_used} model={GEMINI_MODEL}",
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        output.status = "error"
        timeout_msg = f"LLM timeout after {LLM_TIMEOUT_SECONDS}s — please retry or contact support"
        logger.error("run_section_generation: timeout section=%d report=%s", section_no, report_id)
        await write_event(
            db,
            action="section.generation_error",
            actor_user_id=actor_user_id,
            actor_role="system",
            report_id=report_id,
            target_type="section_output",
            target_id=f"{report_id}/{section_no}",
            after=timeout_msg,
        )
        raise TimeoutError(timeout_msg) from exc
    except GenerationRateLimitError as exc:
        output.status = "error"
        logger.warning(
            "run_section_generation: Gemini rate limited section=%d report=%s",
            section_no, report_id,
        )
        await write_event(
            db,
            action="section.generation_error",
            actor_user_id=actor_user_id,
            actor_role="system",
            report_id=report_id,
            target_type="section_output",
            target_id=f"{report_id}/{section_no}",
            after="rate_limited",
        )
        from fastapi import HTTPException as _HTTPEx
        raise _HTTPEx(status_code=429, detail=str(exc)) from exc
    except GenerationConfigError as exc:
        output.status = "error"
        logger.error(
            "run_section_generation: Gemini config/auth error section=%d report=%s",
            section_no, report_id,
        )
        await write_event(
            db,
            action="section.generation_error",
            actor_user_id=actor_user_id,
            actor_role="system",
            report_id=report_id,
            target_type="section_output",
            target_id=f"{report_id}/{section_no}",
            after="config_error",
        )
        from fastapi import HTTPException as _HTTPEx
        raise _HTTPEx(
            status_code=503,
            detail="AI service not configured correctly — contact your administrator",
        ) from exc
    except Exception as exc:
        output.status = "error"
        logger.exception("run_section_generation: error section=%d report=%s: %s", section_no, report_id, exc)
        await write_event(
            db,
            action="section.generation_error",
            actor_user_id=actor_user_id,
            actor_role="system",
            report_id=report_id,
            target_type="section_output",
            target_id=f"{report_id}/{section_no}",
            after=str(exc)[:500],
        )
        raise

    await db.flush()
    return output


async def run_full_report_generation(
    db: AsyncSession,
    report_id: str,
    actor_user_id: str,
    actor_role: str = "analyst",
    output_language: str = "en",
) -> dict[int, str]:
    """
    Generate all sections in GENERATION_ORDER, skipping any whose hard
    dependencies were not satisfied at the time they are reached.

    Returns {section_no: status_string}.
    """
    results: dict[int, str] = {}
    generated_outputs: dict[int, str] = {}
    logger.info("run_full_report_generation: starting report=%s user=%s order=%s", report_id, actor_user_id, GENERATION_ORDER)

    for section_no in GENERATION_ORDER:
        missing_deps = await check_hard_dependencies(db, report_id, section_no)
        if missing_deps:
            logger.warning("run_full_report_generation: skipping section=%d missing_deps=%s report=%s", section_no, missing_deps, report_id)
            results[section_no] = f"skipped_missing_deps:{missing_deps}"
            continue

        try:
            output = await run_section_generation(
                db=db,
                report_id=report_id,
                section_no=section_no,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                preceding_outputs=generated_outputs,
                output_language=output_language,
            )
            results[section_no] = output.status
            if output.markdown:
                generated_outputs[section_no] = output.markdown
        except Exception as exc:
            logger.error("run_full_report_generation: section=%d failed report=%s: %s", section_no, report_id, exc)
            results[section_no] = f"error:{exc}"

    done = sum(1 for v in results.values() if v == "done")
    logger.info("run_full_report_generation: complete report=%s done=%d/%d results=%s", report_id, done, len(GENERATION_ORDER), results)
    return results

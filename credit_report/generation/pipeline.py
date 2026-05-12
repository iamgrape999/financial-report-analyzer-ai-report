from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from credit_report.audit.events import write_event
from credit_report.config import (
    CR_MAX_CONCURRENT_GENERATIONS,
    GEMINI_MODEL,
    GENERATION_ORDER,
    LLM_TIMEOUT_SECONDS,
    SECTION_HARD_DEPENDENCIES,
)
from credit_report.generation.claude_client import generate_section_markdown
from credit_report.generation.evidence import retrieve_evidence
from credit_report.generation.quota import check_quota, record_tokens
from credit_report.models import SectionInput, SectionOutput

_generation_semaphore = asyncio.Semaphore(CR_MAX_CONCURRENT_GENERATIONS)


async def get_section_output(
    db: AsyncSession, report_id: str, section_no: int
) -> Optional[SectionOutput]:
    result = await db.execute(
        select(SectionOutput).where(
            SectionOutput.report_id == report_id,
            SectionOutput.section_no == section_no,
        )
    )
    return result.scalar_one_or_none()


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
        )
    )
    si = si_result.scalar_one_or_none()
    input_json: dict = json.loads(si.input_json) if si and si.input_json else {}

    if not input_json:
        logger.error("run_section_generation: no input_json section=%d report=%s", section_no, report_id)
        raise ValueError(
            f"Section {section_no} has no analyst input data. "
            "Save section input JSON before triggering AI generation."
        )

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
            )

        output.markdown = markdown
        output.status = "done"
        output.model_id = GEMINI_MODEL
        output.tokens_used = tokens_used
        output.generated_at = datetime.now(timezone.utc)
        logger.info("run_section_generation: done section=%d report=%s tokens=%d model=%s chars=%d", section_no, report_id, tokens_used, GEMINI_MODEL, len(markdown))

        # ── Block AST parsing (non-blocking) ─────────────────────────────────
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
            await save_blocks(db, blocks, cells)
            logger.info("[AST] section=%d report=%s blocks=%d cells=%d",
                        section_no, report_id, len(blocks), len(cells))
        except Exception as _ast_err:
            logger.warning("[AST] build_blocks failed section=%d report=%s: %s",
                           section_no, report_id, _ast_err)
        # ─────────────────────────────────────────────────────────────────────

        # Record consumption against the user's daily quota
        await record_tokens(db, actor_user_id, tokens_used)

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

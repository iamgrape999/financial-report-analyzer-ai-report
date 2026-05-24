from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.fact_store import repository as repo
from credit_report.fact_store.models import CanonicalFact, FactConflict
from credit_report.fact_store.models import SOURCE_PRIORITY
from credit_report.generation.claude_client import call_gemini_raw
from credit_report.models import Report
from credit_report.schemas import (
    AutoResolvePriorityResponse,
    ConflictAISuggestion,
    ConflictResponse,
    ResolveConflictRequest,
)
from credit_report.security.auth import get_current_user, require_analyst
from credit_report.security.models import User

_JSON_RE = re.compile(r'\{[^{}]+\}', re.DOTALL)


class MarkUnresolvedResponse(BaseModel):
    status: str
    conflict_id: str

router = APIRouter(prefix="/reports/{report_id}/facts/conflicts", tags=["conflicts"])


async def _assert_conflict_report_access(
    db: AsyncSession, report_id: str, current_user: User
) -> None:
    """Raise 404/403 if the caller does not own the report (admin exempt)."""
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if not report or report.is_deleted:
        raise HTTPException(status_code=404, detail="Report not found")
    if current_user.role != "admin" and report.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")


@router.post("/auto-resolve-priority", response_model=AutoResolvePriorityResponse)
async def auto_resolve_by_priority(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Bulk-resolve C-level conflicts where source priority unambiguously picks the winner.

    Only processes conflicts where the two facts come from *different* source types
    (e.g., analyst_input_json vs pdf_extraction).  Same-source conflicts are skipped
    and require human review or an /ai-suggest call.
    """
    await _assert_conflict_report_access(db, report_id, current_user)

    conflicts = await repo.get_open_conflicts(db, report_id)
    resolved_ids: list[str] = []
    skipped = 0

    for conflict in conflicts:
        pri_a = SOURCE_PRIORITY.get(conflict.source_a or "", 99)
        pri_b = SOURCE_PRIORITY.get(conflict.source_b or "", 99)

        if pri_a == pri_b:
            skipped += 1
            continue

        if pri_a < pri_b:
            chosen, rejected = conflict.fact_a_id, [conflict.fact_b_id]
            reason = (
                f"Auto-resolved by source priority: "
                f"{conflict.source_a} (p{pri_a}) supersedes {conflict.source_b} (p{pri_b})"
            )
        else:
            chosen, rejected = conflict.fact_b_id, [conflict.fact_a_id]
            reason = (
                f"Auto-resolved by source priority: "
                f"{conflict.source_b} (p{pri_b}) supersedes {conflict.source_a} (p{pri_a})"
            )

        await repo.resolve_conflict(
            db,
            conflict_id=conflict.id,
            chosen_fact_id=chosen,
            rejected_fact_ids=rejected,
            resolution_reason=reason,
            resolved_by=current_user.id,
        )
        await write_event(
            db,
            action="conflict.auto_resolve_priority",
            actor_user_id=current_user.id,
            actor_role=current_user.role,
            report_id=report_id,
            target_type="conflict",
            target_id=conflict.id,
            after=f"chosen={chosen}",
            reason=reason,
        )
        resolved_ids.append(conflict.id)

    return AutoResolvePriorityResponse(
        resolved_count=len(resolved_ids),
        skipped_count=skipped,
        resolved_conflict_ids=resolved_ids,
    )


@router.get("", response_model=list[ConflictResponse])
async def list_conflicts(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_conflict_report_access(db, report_id, current_user)
    return await repo.get_open_conflicts(db, report_id)


@router.get("/{conflict_id}", response_model=ConflictResponse)
async def get_conflict(
    report_id: str,
    conflict_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_conflict_report_access(db, report_id, current_user)
    result = await db.execute(
        select(FactConflict).where(
            FactConflict.id == conflict_id,
            FactConflict.report_id == report_id,
        )
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")
    return conflict


@router.post("/{conflict_id}/resolve", response_model=ConflictResponse)
async def resolve_conflict(
    report_id: str,
    conflict_id: str,
    payload: ResolveConflictRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    await _assert_conflict_report_access(db, report_id, current_user)
    result = await db.execute(
        select(FactConflict).where(
            FactConflict.id == conflict_id,
            FactConflict.report_id == report_id,
        )
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")
    if conflict.status != "open":
        raise HTTPException(status_code=400, detail=f"Conflict is already '{conflict.status}'")

    resolved = await repo.resolve_conflict(
        db,
        conflict_id=conflict_id,
        chosen_fact_id=payload.chosen_fact_id,
        rejected_fact_ids=payload.rejected_fact_ids,
        resolution_reason=payload.resolution_reason,
        resolved_by=current_user.id,
    )

    await write_event(
        db,
        action="conflict.resolve",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="conflict",
        target_id=conflict_id,
        after=f"chosen={payload.chosen_fact_id}",
        reason=payload.resolution_reason,
    )
    return resolved


@router.post("/{conflict_id}/mark-unresolved", response_model=MarkUnresolvedResponse)
async def mark_unresolved(
    report_id: str,
    conflict_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    await _assert_conflict_report_access(db, report_id, current_user)
    result = await db.execute(
        select(FactConflict).where(
            FactConflict.id == conflict_id,
            FactConflict.report_id == report_id,
        )
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")

    # When unresolving a previously resolved conflict, restore the involved facts
    # back to "conflicted" so the conflict can be re-resolved. Without this,
    # the chosen fact stays "approved" (can only → deprecated) and rejected facts
    # stay "deprecated" (terminal), making the conflict unresolvable again.
    if conflict.status == "resolved":
        for fid in (conflict.fact_a_id, conflict.fact_b_id):
            if not fid:
                continue
            fr = await db.execute(select(CanonicalFact).where(CanonicalFact.id == fid))
            fact = fr.scalar_one_or_none()
            if fact and fact.state in ("approved", "deprecated"):
                fact.state = "conflicted"
                fact.version += 1

    conflict.status = "open"
    conflict.chosen_fact_id = None
    conflict.resolution_reason = None
    conflict.resolved_by = None
    conflict.resolved_at = None
    await write_event(
        db,
        action="conflict.mark_unresolved",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="conflict",
        target_id=conflict_id,
    )
    return {"status": "open", "conflict_id": conflict_id}


@router.post("/{conflict_id}/ai-suggest", response_model=ConflictAISuggestion)
async def ai_suggest_resolution(
    report_id: str,
    conflict_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ask Gemini to recommend which conflicting fact to accept.

    NEVER auto-resolves — returns a suggestion with reasoning that the analyst
    reviews and then confirms via POST /{conflict_id}/resolve.

    Triage logic:
      C-level (source types differ): returns deterministic rule-based suggestion,
        confidence=95, auto_resolvable=True — no Gemini call needed.
      B-level (same source, diff < 15%): Gemini analyses context, returns
        suggested_winner with 50–94 confidence.
      A-level (same source, diff ≥ 15%): Gemini marks as uncertain / high risk.
    """
    await _assert_conflict_report_access(db, report_id, current_user)

    result = await db.execute(
        select(FactConflict).where(
            FactConflict.id == conflict_id,
            FactConflict.report_id == report_id,
        )
    )
    conflict = result.scalar_one_or_none()
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")
    if conflict.status != "open":
        raise HTTPException(status_code=400, detail=f"Conflict is already '{conflict.status}'")

    pri_a = SOURCE_PRIORITY.get(conflict.source_a or "", 99)
    pri_b = SOURCE_PRIORITY.get(conflict.source_b or "", 99)

    # ── C-level: cross-source, rule determines winner without AI ─────────────
    if pri_a != pri_b:
        if pri_a < pri_b:
            winner, winner_id = "fact_a", conflict.fact_a_id
            reason = (
                f"{conflict.source_a} (priority {pri_a}) has higher data authority "
                f"than {conflict.source_b} (priority {pri_b}). "
                f"Analyst input always supersedes PDF extraction."
            )
        else:
            winner, winner_id = "fact_b", conflict.fact_b_id
            reason = (
                f"{conflict.source_b} (priority {pri_b}) has higher data authority "
                f"than {conflict.source_a} (priority {pri_a}). "
                f"Analyst input always supersedes PDF extraction."
            )
        return ConflictAISuggestion(
            conflict_id=conflict_id,
            suggested_winner=winner,
            suggested_fact_id=winner_id,
            confidence=95,
            risk_level="low",
            auto_resolvable=True,
            reason=reason,
            resolution_suggestion=reason,
        )

    # ── B/A-level: same source type — ask Gemini ─────────────────────────────
    val_a = conflict.value_a or "N/A"
    val_b = conflict.value_b or "N/A"

    try:
        num_a = float(conflict.value_a or "0")
        num_b = float(conflict.value_b or "0")
        diff_pct = abs(num_a - num_b) / max(abs(num_a), abs(num_b), 1) * 100
    except (ValueError, TypeError):
        diff_pct = 100.0  # text mismatch: treat as maximum divergence

    system = (
        "You are a senior credit analyst. Reply ONLY with the JSON object requested. "
        "Set risk_level=high when confidence < 70 or numeric difference > 10%."
    )
    user_prompt = (
        f"Two documents report different values for the same financial metric:\n"
        f"  Metric : {conflict.metric_name}\n"
        f"  Entity : {conflict.entity}\n"
        f"  Period : {conflict.period}\n\n"
        f"  Fact A : {val_a}  (source: {conflict.source_a})\n"
        f"  Fact B : {val_b}  (source: {conflict.source_b})\n"
        f"  Difference: {diff_pct:.1f}%\n\n"
        "Which value is more likely to be correct for a bank credit report?\n"
        "Common causes: different fiscal year definitions, consolidated vs parent-only, "
        "audited vs preliminary, currency conversion differences.\n\n"
        'Reply with JSON only: {"choice":"fact_a"|"fact_b"|"uncertain",'
        '"confidence":0-100,"reason":"one sentence","risk_level":"low"|"medium"|"high"}'
    )

    choice, confidence, reason, risk_level = "uncertain", 0, "AI analysis unavailable.", "high"
    try:
        raw = await call_gemini_raw(system_prompt=system, user_prompt=user_prompt, max_tokens=200)
        m = _JSON_RE.search(raw)
        if m:
            data = json.loads(m.group())
            choice     = data.get("choice", "uncertain")
            confidence = max(0, min(100, int(data.get("confidence", 50))))
            reason     = data.get("reason", "")
            risk_level = data.get("risk_level", "medium")
    except Exception:
        pass  # defaults already set above

    winner_id = (
        conflict.fact_a_id if choice == "fact_a"
        else conflict.fact_b_id if choice == "fact_b"
        else None
    )
    resolution_suggestion = (
        f"AI suggestion ({confidence}% confidence, {risk_level} risk): {reason}"
        if choice != "uncertain"
        else "Manual review required: AI could not determine a clear winner."
    )

    return ConflictAISuggestion(
        conflict_id=conflict_id,
        suggested_winner=choice,
        suggested_fact_id=winner_id,
        confidence=confidence,
        risk_level=risk_level,
        auto_resolvable=False,
        reason=reason,
        resolution_suggestion=resolution_suggestion,
    )

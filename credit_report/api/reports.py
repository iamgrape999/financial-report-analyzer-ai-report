from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.fact_store.input_extractor import CONFIG_DIR, InputFactExtractor
from credit_report.fact_store.repository import get_facts_for_report, upsert_facts
from credit_report.models import Report, SectionInput
from credit_report.schemas import (
    ApplyFieldSuggestionItem,
    ApplySuggestionsRequest,
    ApplySuggestionsResponse,
    CreateReportRequest,
    FieldSuggestion,
    FieldSuggestionsResponse,
    ReportResponse,
    SectionInputPayload,
    SectionInputResponse,
    UpdateReportStatusRequest,
)
from credit_report.security.auth import get_current_user, require_analyst
from credit_report.security.models import User

router = APIRouter(prefix="/reports", tags=["reports"])

VALID_STATUSES = ("draft", "validated", "review_in_progress", "approved")


def _can_view_report(report: Report, current_user: User) -> bool:
    """Return whether a user can read a report in the current coarse RBAC model."""
    return current_user.role in {"admin", "reviewer", "approver"} or report.created_by == current_user.id


def _assert_can_view_report(report: Report, current_user: User) -> None:
    if not _can_view_report(report, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this report.",
        )


def _assert_owner_or_admin(report: Report, current_user: User) -> None:
    """Raise 403 if the user is not the report creator and not an admin."""
    if current_user.role == "admin":
        return
    if report.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this report.",
        )


def _strip_instruction_keys(obj):
    """Recursively remove keys starting with '_' from the input JSON."""
    if isinstance(obj, dict):
        return {k: _strip_instruction_keys(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_instruction_keys(i) for i in obj]
    return obj


@router.post("", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    payload: CreateReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    report = Report(
        id=str(uuid.uuid4()),
        industry=payload.industry,
        report_type=payload.report_type,
        borrower_name=payload.borrower_name,
        booking_branch=payload.booking_branch,
        created_by=current_user.id,
    )
    db.add(report)
    logger.info("create_report: id=%s borrower=%r industry=%r user=%s", report.id, report.borrower_name, report.industry, current_user.id)

    await write_event(
        db,
        action="report.created",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report.id,
        target_type="report",
        target_id=report.id,
        after=f"industry={report.industry}",
    )
    await db.flush()
    return report


@router.get("", response_model=list[ReportResponse])
async def list_reports(
    skip: int = Query(default=0, ge=0, le=2_147_483_647),
    limit: int = Query(default=20, ge=0, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Report).where(Report.is_deleted == False)
    if current_user.role not in {"admin", "reviewer", "approver"}:
        query = query.where(Report.created_by == current_user.id)
    result = await db.execute(
        query.order_by(Report.created_at.desc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all())


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_can_view_report(report, current_user)
    return report


@router.patch("/{report_id}/status", response_model=ReportResponse)
async def update_status(
    report_id: str,
    payload: UpdateReportStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID_STATUSES}")

    # Only Approver/Admin can transition to "approved"
    if payload.status == "approved" and current_user.role not in ("approver", "admin"):
        raise HTTPException(status_code=403, detail="Only approvers and admins can approve reports")

    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if payload.status != "approved":
        _assert_owner_or_admin(report, current_user)

    old_status = report.status
    report.status = payload.status
    logger.info("update_status: report=%s %r → %r user=%s", report_id, old_status, payload.status, current_user.id)

    await write_event(
        db,
        action="report.status_change",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="report",
        target_id=report_id,
        before=old_status,
        after=payload.status,
    )
    return report


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_owner_or_admin(report, current_user)

    report.is_deleted = True
    await write_event(
        db,
        action="report.deleted",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="report",
        target_id=report_id,
    )


# ── Section Inputs ────────────────────────────────────────────────────────────────────────────────────────────

@router.put("/{report_id}/inputs/{section_no}", response_model=SectionInputResponse)
async def save_section_input(
    report_id: str,
    section_no: int,
    payload: SectionInputPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    if section_no < 1 or section_no > 11:
        raise HTTPException(status_code=400, detail="section_no must be 1-11")

    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_owner_or_admin(report, current_user)

    # Upsert section input — always read newest row (DESC) so subsequent saves are idempotent
    si_result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        ).order_by(SectionInput.id.desc())
    )
    si = si_result.scalars().first()
    if si:
        si.input_json = json.dumps(payload.input_json, ensure_ascii=False)
        si.saved_by = current_user.id
        si.saved_at = datetime.now(timezone.utc)  # server_default only fires on INSERT; force update
    else:
        si = SectionInput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            input_json=json.dumps(payload.input_json, ensure_ascii=False),
            saved_by=current_user.id,
        )
        db.add(si)

    # Flush the INSERT/UPDATE immediately so concurrent writers get 409 rather than
    # having the INSERT fail mid-savepoint (which would leave the session in a broken state).
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Concurrent write conflict — another request saved this section. Please retry.",
        )

    # Auto-extract facts from analyst JSON.
    # IMPORTANT: wrapped in begin_nested() (SAVEPOINT) so that any DB error raised by
    # upsert_facts() or the inner write_event() is rolled back to the savepoint — NOT
    # the outer transaction.  On PostgreSQL, an unrolled exception leaves the connection
    # in InFailedSQLTransaction state, causing every subsequent statement to also fail.
    try:
        async with db.begin_nested():
            extractor = InputFactExtractor(section_no)
            cleaned = _strip_instruction_keys(payload.input_json)
            facts_data = extractor.extract(report_id, cleaned)
            if facts_data:
                await upsert_facts(db, facts_data)
                logger.debug("save_section_input: extracted %d facts section=%d report=%s", len(facts_data), section_no, report_id)
                await write_event(
                    db,
                    action="facts.extracted_from_input",
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    report_id=report_id,
                    target_type="section_input",
                    target_id=f"{report_id}/{section_no}",
                    after=f"{len(facts_data)} facts extracted",
                )
    except Exception:
        logger.warning("save_section_input: fact extraction failed (non-blocking) section=%d report=%s", section_no, report_id, exc_info=True)

    # Auto-recalculate derived ratios from accumulated facts (non-blocking).
    # Also wrapped in a savepoint so a calculation failure cannot corrupt the outer session.
    try:
        async with db.begin_nested():
            from credit_report.api.calculations import _run_recalculate_core
            n_calcs, _ = await _run_recalculate_core(db, report_id)
            if n_calcs:
                logger.debug("save_section_input: recalculated %d calcs section=%d report=%s", n_calcs, section_no, report_id)
    except Exception:
        logger.warning("save_section_input: recalculate failed (non-blocking) section=%d report=%s", section_no, report_id, exc_info=True)

    await write_event(
        db,
        action="section_input.saved",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="section_input",
        target_id=f"{report_id}/{section_no}",
    )
    await db.flush()
    # Refresh to load server-generated timestamp (server_default=func.now() is not
    # reflected on the Python object after flush() on PostgreSQL with asyncpg).
    await db.refresh(si)
    logger.info("save_section_input: saved section=%d report=%s user=%s", section_no, report_id, current_user.id)

    return SectionInputResponse(
        section_no=section_no,
        input_json=payload.input_json,
        saved_at=si.saved_at,
    )


@router.get("/{report_id}/inputs/{section_no}", response_model=SectionInputResponse)
async def get_section_input(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report_result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = report_result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_can_view_report(report, current_user)

    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        ).order_by(SectionInput.id.desc())
    )
    si = result.scalars().first()
    if not si:
        raise HTTPException(status_code=404, detail="Section input not found")

    try:
        parsed = json.loads(si.input_json) if si.input_json else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Stored section input contains invalid JSON — data may be corrupted: {exc}",
        )
    return SectionInputResponse(
        section_no=si.section_no,
        input_json=parsed,
        saved_at=si.saved_at,
    )


@router.get("/{report_id}/inputs")
async def list_section_inputs(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report_result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = report_result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_can_view_report(report, current_user)

    result = await db.execute(
        select(SectionInput).where(SectionInput.report_id == report_id)
    )
    inputs = result.scalars().all()
    return [
        {"section_no": si.section_no, "saved_at": si.saved_at}
        for si in inputs
    ]


# ── Semantic status transition endpoints ──────────────────────────────────────

async def _get_live_report(db: AsyncSession, report_id: str) -> Report:
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.post("/{report_id}/submit-for-review", response_model=ReportResponse)
async def submit_for_review(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Transition report from draft/validated → review_in_progress.

    Requires at least one section with status='done'. Only the report owner or admin may submit.
    """
    from credit_report.models import SectionOutput

    report = await _get_live_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    if report.status not in ("draft", "validated"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot submit for review from status '{report.status}'. Report must be in draft or validated state.",
        )

    done_result = await db.execute(
        select(SectionOutput).where(
            SectionOutput.report_id == report_id,
            SectionOutput.status == "done",
        )
    )
    done_sections = done_result.scalars().all()
    if not done_sections:
        raise HTTPException(
            status_code=422,
            detail="Cannot submit for review — no sections have been generated yet.",
        )

    old_status = report.status
    report.status = "review_in_progress"
    await write_event(
        db,
        action="report.submitted_for_review",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="report",
        target_id=report_id,
        before=old_status,
        after="review_in_progress",
    )
    logger.info(
        "submit_for_review: report=%s %r → review_in_progress user=%s sections_done=%d",
        report_id, old_status, current_user.id, len(done_sections),
    )
    return report


@router.post("/{report_id}/approve", response_model=ReportResponse)
async def approve_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Transition report to approved. Only approvers and admins may approve."""
    if current_user.role not in ("approver", "admin"):
        raise HTTPException(status_code=403, detail="Only approvers and admins can approve reports")

    report = await _get_live_report(db, report_id)

    if report.status not in ("review_in_progress", "validated"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve from status '{report.status}'. Report must be under review.",
        )

    old_status = report.status
    report.status = "approved"
    await write_event(
        db,
        action="report.approved",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="report",
        target_id=report_id,
        before=old_status,
        after="approved",
    )
    logger.info("approve_report: report=%s %r → approved user=%s", report_id, old_status, current_user.id)
    return report


@router.post("/{report_id}/recall", response_model=ReportResponse)
async def recall_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Recall a report from review back to draft. Owner or admin only."""
    report = await _get_live_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    if report.status not in ("review_in_progress",):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot recall from status '{report.status}'. Only reports under review can be recalled.",
        )

    old_status = report.status
    report.status = "draft"
    await write_event(
        db,
        action="report.recalled",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="report",
        target_id=report_id,
        before=old_status,
        after="draft",
    )
    logger.info("recall_report: report=%s %r → draft user=%s", report_id, old_status, current_user.id)
    return report


@router.get("/{report_id}/review-progress")
async def get_review_progress(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return section generation counts and block validation progress."""
    from credit_report.models import SectionOutput
    from credit_report.block_ast.models import ReportBlock

    report = await _get_live_report(db, report_id)
    _assert_can_view_report(report, current_user)

    output_result = await db.execute(
        select(SectionOutput).where(SectionOutput.report_id == report_id)
    )
    outputs = output_result.scalars().all()
    sections_done = sum(1 for o in outputs if o.status == "done")
    sections_error = sum(1 for o in outputs if o.status == "error")

    block_result = await db.execute(
        select(ReportBlock).where(ReportBlock.report_id == report_id, ReportBlock.is_stale == False)
    )
    blocks = block_result.scalars().all()
    blocks_total = len(blocks)
    blocks_passed = sum(1 for b in blocks if b.validation_status == "passed")
    blocks_conflict = sum(1 for b in blocks if b.validation_status == "conflict")

    return {
        "report_id": report_id,
        "report_status": report.status,
        "sections_total": 10,
        "sections_done": sections_done,
        "sections_error": sections_error,
        "blocks_total": blocks_total,
        "blocks_passed": blocks_passed,
        "blocks_conflict": blocks_conflict,
        "ready_for_review": sections_done > 0 and report.status in ("draft", "validated"),
        "ready_to_approve": report.status == "review_in_progress",
    }


# ── Field Suggestion helpers ──────────────────────────────────────────────────

def _resolve_path_safe(obj: Any, path: str) -> Any:
    """Walk dot-notation path (with optional bracket indices) into a nested dict/list.

    Supports 'rows[0].field' notation so entity_path entries from YAML configs
    that reference list elements (Section 3, 7) resolve correctly.
    """
    cur = obj
    for part in re.split(r"\.", path.lstrip("$.")):
        if cur is None:
            return None
        idx_match = re.match(r"^(.+)\[(\d+)\]$", part)
        if idx_match:
            key, idx = idx_match.group(1), int(idx_match.group(2))
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
            if isinstance(cur, list) and 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _values_equal(a: Any, b: Any) -> bool:
    """Numerically-aware equality: '12,345' == 12345.0; '12.30' == 12.3."""
    if a is None:
        return False
    try:
        fa = float(str(a).replace(",", "").replace("%", "").strip())
        fb = float(str(b).replace(",", "").replace("%", "").strip())
        return abs(fa - fb) < 1e-6
    except (ValueError, TypeError):
        return str(a).strip() == str(b).strip()


def _make_suggestion_id(report_id: str, section_no: int, field_path: str, fact_id: str) -> str:
    raw = f"{report_id}:{section_no}:{field_path}:{fact_id}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _humanize_path(path: str) -> str:
    """Convert 7A_borrower_financials.income_statement.FY2024.revenue → readable label."""
    parts = path.split(".")
    cleaned = []
    for p in parts:
        # Strip leading section prefix like "7A_", "2B_", "7a_" (case-insensitive)
        p = re.sub(r"^\d+[A-Za-z]_", "", p)
        p = p.replace("_", " ").title()
        cleaned.append(p)
    return " › ".join(cleaned)


def _compute_confidence(fact: Any, facts_for_report: list[Any]) -> tuple[str, float, list[str], Optional[str], bool]:
    """Return (level, score, reasons, conflict_warning, selectable)."""
    score = 60.0
    reasons: list[str] = []
    conflict_warning: Optional[str] = None

    # Source reliability
    if fact.source_priority == 1:
        score += 20
        reasons.append("Analyst-input source (highest priority)")
    elif fact.source_priority == 2:
        score += 15
        reasons.append("Manual override source")
    elif fact.source_priority == 3:
        score += 8
        reasons.append("Document extraction source")
    else:
        reasons.append("Derived calculation source")

    # Extraction state
    if fact.state == "approved":
        score += 12
        reasons.append("Fact approved by reviewer")
    elif fact.state == "validated":
        score += 8
        reasons.append("Fact validated")
    elif fact.state == "conflicted":
        score -= 35
        conflict_warning = "This fact has conflicting values from multiple sources — verify manually"
        reasons.append("⚠ Conflicting sources detected")
    elif fact.state == "deprecated":
        score -= 50
        reasons.append("⚠ Deprecated fact")

    # Has a clean display value
    if fact.display:
        score += 5
        reasons.append("Formatted display value available")

    # Check if value is numeric (more reliable)
    if fact.value is not None:
        score += 5
        reasons.append("Numeric value extracted")

    # Consistency check: same metric across sources
    same_metric = [f for f in facts_for_report if f.metric_name == fact.metric_name
                   and f.entity == fact.entity and f.period == fact.period
                   and f.id != fact.id and f.state != "deprecated"]
    if same_metric:
        values = [f.value for f in same_metric if f.value is not None]
        if values and fact.value is not None:
            max_v, min_v = max(values + [fact.value]), min(values + [fact.value])
            if max_v > 0 and (max_v - min_v) / max_v < 0.02:
                score += 8
                reasons.append(f"Consistent across {len(same_metric)+1} sources")
            elif (max_v - min_v) / max(max_v, 1) > 0.10:
                score -= 15
                if not conflict_warning:
                    conflict_warning = f"Value differs >10% from another source"
                reasons.append("⚠ Material discrepancy with another source")

    score = max(0.0, min(100.0, score))

    if score >= 85:
        level = "high"
    elif score >= 65:
        level = "medium"
    else:
        level = "low"

    selectable = fact.state != "conflicted" and score >= 50

    return level, round(score, 1), reasons, conflict_warning, selectable


# ── GET field-suggestions ─────────────────────────────────────────────────────

@router.get("/{report_id}/sections/{section_no}/field-suggestions",
            response_model=FieldSuggestionsResponse)
async def get_field_suggestions(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return per-field CanonicalFact-backed suggestions for the given section."""
    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1–10")

    report_result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = report_result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_can_view_report(report, current_user)

    # Load all non-deprecated facts and build a metric+entity index
    all_facts = await get_facts_for_report(db, report_id)
    active_facts = [f for f in all_facts if f.state != "deprecated"]

    # O(1) lookup: (metric.lower(), entity.upper(), period) → best fact
    # Among duplicates keep the lowest source_priority (most authoritative)
    full_index: dict[tuple, Any] = {}
    for f in active_facts:
        key = (f.metric_name.lower(), (f.entity or "").upper(), f.period)
        existing = full_index.get(key)
        if existing is None or f.source_priority < existing.source_priority:
            full_index[key] = f

    # Fallback index by (metric, entity) ignoring period — for static mappings
    me_index: dict[tuple, Any] = {}
    for f in active_facts:
        key = (f.metric_name.lower(), (f.entity or "").upper())
        existing = me_index.get(key)
        if existing is None or f.source_priority < existing.source_priority:
            me_index[key] = f

    # Load YAML config for this section using the report's industry
    industry = report.industry or "marine"
    config_path = CONFIG_DIR / industry / f"section_{section_no}.yaml"
    if not config_path.exists():
        logger.warning(
            "get_field_suggestions: no YAML config for section=%d industry=%r path=%s — suggestions empty",
            section_no, industry, config_path,
        )
        return FieldSuggestionsResponse(
            report_id=report_id, section_no=section_no,
            total_facts_checked=len(active_facts), suggestions=[],
        )

    if not _YAML_AVAILABLE:
        logger.warning("get_field_suggestions: yaml module not available")
        return FieldSuggestionsResponse(
            report_id=report_id, section_no=section_no,
            total_facts_checked=len(active_facts), suggestions=[],
        )
    try:
        with config_path.open(encoding="utf-8") as fh:
            yaml_config = _yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("get_field_suggestions: YAML load failed section=%d: %s", section_no, exc)
        return FieldSuggestionsResponse(
            report_id=report_id, section_no=section_no,
            total_facts_checked=len(active_facts), suggestions=[],
        )

    # Load current section input for comparison
    si_result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        ).order_by(SectionInput.id.desc())
    )
    si = si_result.scalars().first()
    current_input: dict = json.loads(si.input_json) if si and si.input_json else {}

    suggestions: list[FieldSuggestion] = []

    for mapping in yaml_config.get("facts", []):
        metric = mapping.get("metric", "").lower()
        if not metric:
            continue

        # Resolve entity: static 'entity:' key takes precedence;
        # 'entity_path:' reads the entity name from the current section input
        # (used in Section 3 ratings and Section 7 guarantor mappings).
        if "entity_path" in mapping:
            entity_raw = _resolve_path_safe(current_input, mapping["entity_path"])
            entity_key = str(entity_raw or "").upper()
        else:
            entity_key = (mapping.get("entity") or "").upper()

        if "iterate_path" in mapping:
            # Iterate mapping: one suggestion per fiscal-year period
            iterate_path = mapping["iterate_path"]
            field = mapping.get("field", "")
            # Collect all facts for this metric+entity across all periods
            candidates = [
                f for f in active_facts
                if f.metric_name.lower() == metric
                and (not entity_key or (f.entity or "").upper() == entity_key)
            ]
            # When entity_key is the abstract BORROWER and no exact match found,
            # ETL facts may carry the real company name instead of the abstract key.
            # Fall back to metric-only to surface these facts as suggestions.
            if not candidates and entity_key == "BORROWER":
                candidates = [f for f in active_facts if f.metric_name.lower() == metric
                              and (f.entity or "").upper() not in {"FACILITY", "MARKET"}]
            for fact in candidates:
                if not fact.period:
                    logger.debug(
                        "field-suggestions: skipping fact with no period metric=%s entity=%s fact_id=%s",
                        fact.metric_name, fact.entity, fact.id,
                    )
                    continue
                full_path = f"{iterate_path}.{fact.period}.{field}"
                suggested_val = fact.value if fact.value is not None else fact.value_text
                if suggested_val is None:
                    continue
                current_val = _resolve_path_safe(current_input, full_path)
                if _values_equal(current_val, suggested_val):
                    continue
                level, score, reasons, conflict_warning, selectable = _compute_confidence(fact, active_facts)
                suggestions.append(FieldSuggestion(
                    suggestion_id=_make_suggestion_id(report_id, section_no, full_path, fact.id),
                    field_path=full_path,
                    field_label=_humanize_path(full_path),
                    metric_name=fact.metric_name,
                    entity=fact.entity or "",
                    period=fact.period,
                    current_value=current_val,
                    suggested_value=suggested_val,
                    display=fact.display,
                    currency=fact.currency,
                    unit=fact.unit,
                    confidence=level,
                    confidence_score=score,
                    confidence_reasons=reasons,
                    source_type=fact.source_type,
                    source_priority=fact.source_priority,
                    fact_id=fact.id,
                    fact_state=fact.state,
                    conflict_warning=conflict_warning,
                    selectable=selectable,
                ))
        else:
            # Static mapping: single field
            path = mapping.get("path", "")
            if not path:
                continue
            # Resolve period: 'period:' is static; 'period_path:' reads from current input.
            # Section 4 financial fields use period_path so the period comes from whatever
            # fiscal_year the analyst typed — without resolving this, full_index always misses.
            if "period_path" in mapping:
                period_raw = _resolve_path_safe(current_input, mapping["period_path"])
                lookup_period = str(period_raw or "").upper()
            else:
                lookup_period = (mapping.get("period") or "").upper()

            # Prefer exact (metric, entity, period) match; then (metric, entity); then metric-only
            fact = full_index.get((metric, entity_key, lookup_period))
            if fact is None:
                fact = me_index.get((metric, entity_key))
            if fact is None:
                # Entity mismatch fallback: ETL stores facts with the actual company name
                # while YAML uses the abstract "BORROWER" entity.  Pick the best-priority
                # fact for this metric, preferring the resolved period when available.
                candidates = [f for f in active_facts if f.metric_name.lower() == metric]
                if lookup_period:
                    period_match = [f for f in candidates
                                    if (f.period or "").upper() == lookup_period]
                    candidates = period_match or candidates
                fact = min(candidates, key=lambda f: f.source_priority, default=None)
            if fact is None:
                continue
            suggested_val = fact.value if fact.value is not None else fact.value_text
            if suggested_val is None:
                continue
            current_val = _resolve_path_safe(current_input, path)
            if _values_equal(current_val, suggested_val):
                continue
            level, score, reasons, conflict_warning, selectable = _compute_confidence(fact, active_facts)
            suggestions.append(FieldSuggestion(
                suggestion_id=_make_suggestion_id(report_id, section_no, path, fact.id),
                field_path=path,
                field_label=_humanize_path(path),
                metric_name=fact.metric_name,
                entity=fact.entity or "",
                period=fact.period,
                current_value=current_val,
                suggested_value=suggested_val,
                display=fact.display,
                currency=fact.currency,
                unit=fact.unit,
                confidence=level,
                confidence_score=score,
                confidence_reasons=reasons,
                source_type=fact.source_type,
                source_priority=fact.source_priority,
                fact_id=fact.id,
                fact_state=fact.state,
                conflict_warning=conflict_warning,
                selectable=selectable,
            ))

    suggestions.sort(key=lambda s: s.field_path)
    logger.info(
        "get_field_suggestions: report=%s section=%d facts_checked=%d suggestions=%d user=%s",
        report_id, section_no, len(active_facts), len(suggestions), current_user.id,
    )
    return FieldSuggestionsResponse(
        report_id=report_id,
        section_no=section_no,
        total_facts_checked=len(active_facts),
        suggestions=suggestions,
    )


# ── POST field-suggestions/apply ─────────────────────────────────────────────

@router.post("/{report_id}/sections/{section_no}/field-suggestions/apply",
             response_model=ApplySuggestionsResponse)
async def apply_field_suggestions(
    report_id: str,
    section_no: int,
    payload: ApplySuggestionsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Server-side apply of analyst-selected field suggestions.

    apply_mode="only_empty"  — skips fields that already have a value (safe default)
    apply_mode="overwrite"   — overwrites existing values (requires explicit choice)
    """
    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1–10")
    if payload.apply_mode not in ("only_empty", "overwrite"):
        raise HTTPException(status_code=400, detail="apply_mode must be 'only_empty' or 'overwrite'")
    if not payload.items:
        raise HTTPException(status_code=400, detail="No items to apply")
    if len(payload.items) > 500:
        raise HTTPException(status_code=400, detail="Too many items: max 500 per apply request")

    report_result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = report_result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    # Apply requires edit rights (not just view)
    _assert_owner_or_admin(report, current_user)

    # Load active facts for re-validation
    all_facts = await get_facts_for_report(db, report_id)
    fact_by_id = {f.id: f for f in all_facts if f.state != "deprecated"}

    # Load current section input
    si_result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        ).order_by(SectionInput.id.desc())
    )
    si = si_result.scalars().first()
    current_input: dict = json.loads(si.input_json) if si and si.input_json else {}

    applied_paths: list[str] = []
    skipped_paths: list[str] = []
    conflict_paths: list[str] = []

    for item in payload.items:
        fact = fact_by_id.get(item.fact_id)
        if not fact:
            skipped_paths.append(item.field_path)
            continue

        # Re-validate suggestion_id to prevent tampered field paths
        expected_id = _make_suggestion_id(report_id, section_no, item.field_path, item.fact_id)
        if item.suggestion_id != expected_id:
            logger.warning(
                "apply_field_suggestions: tampered suggestion_id report=%s path=%s user=%s",
                report_id, item.field_path, current_user.id,
            )
            skipped_paths.append(item.field_path)
            continue

        # Reject conflicted facts
        if fact.state == "conflicted":
            conflict_paths.append(item.field_path)
            continue

        current_val = _resolve_path_safe(current_input, item.field_path)

        # only_empty mode: skip if already has a value
        if payload.apply_mode == "only_empty" and current_val is not None and str(current_val).strip():
            skipped_paths.append(item.field_path)
            continue

        # Set value at path (build intermediate dicts as needed)
        parts = item.field_path.split(".")
        cur = current_input
        for p in parts[:-1]:
            if not isinstance(cur.get(p), dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = item.suggested_value
        applied_paths.append(item.field_path)

    if not applied_paths:
        return ApplySuggestionsResponse(
            applied_count=0,
            skipped_count=len(skipped_paths),
            conflict_count=len(conflict_paths),
            applied_paths=[],
            skipped_paths=skipped_paths,
            conflict_paths=conflict_paths,
        )

    # Persist the merged input
    if si:
        si.input_json = json.dumps(current_input, ensure_ascii=False)
        si.saved_by = current_user.id
        si.saved_at = datetime.now(timezone.utc)
    else:
        si = SectionInput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            input_json=json.dumps(current_input, ensure_ascii=False),
            saved_by=current_user.id,
        )
        db.add(si)

    await db.flush()

    await write_event(
        db,
        action="section_input.facts_applied",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="section_input",
        target_id=f"{report_id}/{section_no}",
        after=(
            f"applied={len(applied_paths)} mode={payload.apply_mode} "
            f"paths={','.join(applied_paths[:5])}{'...' if len(applied_paths)>5 else ''}"
        ),
    )

    logger.info(
        "apply_field_suggestions: report=%s section=%d applied=%d skipped=%d conflicts=%d mode=%s user=%s",
        report_id, section_no, len(applied_paths), len(skipped_paths),
        len(conflict_paths), payload.apply_mode, current_user.id,
    )

    return ApplySuggestionsResponse(
        applied_count=len(applied_paths),
        skipped_count=len(skipped_paths),
        conflict_count=len(conflict_paths),
        applied_paths=applied_paths,
        skipped_paths=skipped_paths,
        conflict_paths=conflict_paths,
    )

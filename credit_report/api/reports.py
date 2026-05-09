from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.fact_store.input_extractor import InputFactExtractor
from credit_report.fact_store.repository import upsert_facts
from credit_report.models import Report, SectionInput
from credit_report.schemas import (
    CreateReportRequest,
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
    skip: int = 0,
    limit: int = 20,
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
    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1-10")

    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_owner_or_admin(report, current_user)

    # Upsert section input
    si_result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        )
    )
    si = si_result.scalar_one_or_none()
    if si:
        si.input_json = json.dumps(payload.input_json, ensure_ascii=False)
        si.saved_by = current_user.id
    else:
        si = SectionInput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            input_json=json.dumps(payload.input_json, ensure_ascii=False),
            saved_by=current_user.id,
        )
        db.add(si)

    # Auto-extract facts from analyst JSON. Extraction failures never block input save.
    try:
        extractor = InputFactExtractor(section_no)
        cleaned = _strip_instruction_keys(payload.input_json)
        facts_data = extractor.extract(report_id, cleaned)
        if facts_data:
            await upsert_facts(db, facts_data)
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
        pass  # Extraction failure never blocks input save

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
        )
    )
    si = result.scalar_one_or_none()
    if not si:
        raise HTTPException(status_code=404, detail="Section input not found")

    return SectionInputResponse(
        section_no=si.section_no,
        input_json=json.loads(si.input_json),
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

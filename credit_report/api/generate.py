from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.config import CREDIT_REPORT_MAX_UPLOAD_MB
from credit_report.database import get_db
from credit_report.generation.evidence import extract_text_from_pdf, save_document_text
from credit_report.generation.models import SectionDocument
from credit_report.generation.pipeline import (
    check_hard_dependencies,
    get_section_output,
    run_full_report_generation,
    run_section_generation,
)
from credit_report.models import Report, SectionOutput
from credit_report.security.auth import get_current_user, require_analyst
from credit_report.security.models import User

router = APIRouter(prefix="/reports/{report_id}", tags=["generation"])

_MAX_UPLOAD_BYTES = CREDIT_REPORT_MAX_UPLOAD_MB * 1024 * 1024


# ── Schemas ─────────────────────────────────────────────────────────────────────────────

class DocumentOut(BaseModel):
    id: str
    original_filename: str
    file_size_bytes: int

    model_config = {"from_attributes": True}


class SectionOutputOut(BaseModel):
    section_no: int
    status: str
    model_id: Optional[str] = None
    tokens_used: Optional[int] = None
    generated_at: Optional[str] = None
    markdown: Optional[str] = None

    model_config = {"from_attributes": True}


class GenerateOneResult(BaseModel):
    section_no: int
    status: str
    tokens_used: Optional[int] = None


class GenerateAllResult(BaseModel):
    sections: dict[str, str]


# ── Helpers ───────────────────────────────────────────────────────────────────────────────────

async def _require_report(db: AsyncSession, report_id: str) -> Report:
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def _assert_owner_or_admin(report: Report, current_user: User) -> None:
    """Raise 403 if the user is not the report creator and not an admin."""
    if current_user.role == "admin":
        return
    if report.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this report.",
        )


def _output_to_schema(o: SectionOutput) -> SectionOutputOut:
    return SectionOutputOut(
        section_no=o.section_no,
        status=o.status,
        model_id=o.model_id,
        tokens_used=o.tokens_used,
        generated_at=o.generated_at.isoformat() if o.generated_at else None,
        markdown=o.markdown,
    )


# ── Document management ───────────────────────────────────────────────────────────────────────────────────

@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    report_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Upload a PDF evidence document for a report."""
    await _require_report(db, report_id)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {CREDIT_REPORT_MAX_UPLOAD_MB} MB upload limit",
        )

    doc_id = str(uuid.uuid4())
    text = extract_text_from_pdf(pdf_bytes)
    save_document_text(report_id, doc_id, text)

    doc = SectionDocument(
        id=doc_id,
        report_id=report_id,
        original_filename=file.filename,
        file_size_bytes=len(pdf_bytes),
        uploaded_by=current_user.id,
    )
    db.add(doc)
    await db.flush()
    return doc


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_report(db, report_id)
    result = await db.execute(
        select(SectionDocument)
        .where(SectionDocument.report_id == report_id, SectionDocument.is_deleted == False)
        .order_by(SectionDocument.uploaded_at.desc())
    )
    return list(result.scalars().all())


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    report_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    result = await db.execute(
        select(SectionDocument).where(
            SectionDocument.id == doc_id,
            SectionDocument.report_id == report_id,
            SectionDocument.is_deleted == False,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    doc.is_deleted = True


# ── Section generation ───────────────────────────────────────────────────────────────────────────────────

@router.post("/generate/{section_no}", response_model=GenerateOneResult)
async def generate_section(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Trigger AI generation for a single section. Returns 409 if hard deps are unmet."""
    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1–10")

    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    missing = await check_hard_dependencies(db, report_id, section_no)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Hard dependencies not yet generated: sections {missing}",
        )

    try:
        output = await run_section_generation(
            db=db,
            report_id=report_id,
            section_no=section_no,
            actor_user_id=current_user.id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Generation failed: {exc}")

    return GenerateOneResult(
        section_no=section_no,
        status=output.status,
        tokens_used=output.tokens_used,
    )


@router.post("/generate", response_model=GenerateAllResult)
async def generate_full_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Trigger AI generation for all sections in dependency order."""
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)
    results = await run_full_report_generation(
        db=db,
        report_id=report_id,
        actor_user_id=current_user.id,
    )
    return GenerateAllResult(sections={str(k): v for k, v in results.items()})


# ── Section output retrieval ──────────────────────────────────────────────────────────────────────────────────

@router.get("/sections/{section_no}/output", response_model=SectionOutputOut)
async def get_section_output_endpoint(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_report(db, report_id)
    output = await get_section_output(db, report_id, section_no)
    if not output:
        raise HTTPException(status_code=404, detail="Section output not found")
    return _output_to_schema(output)


@router.get("/outputs", response_model=list[SectionOutputOut])
async def list_outputs(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_report(db, report_id)
    result = await db.execute(
        select(SectionOutput)
        .where(SectionOutput.report_id == report_id)
        .order_by(SectionOutput.section_no)
    )
    return [_output_to_schema(o) for o in result.scalars().all()]

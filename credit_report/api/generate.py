from __future__ import annotations

import asyncio
import json
import logging
import uuid
from functools import partial
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Form

from credit_report.config import CREDIT_REPORT_MAX_UPLOAD_MB
from credit_report.database import get_db
from credit_report.generation.evidence import extract_text_from_file, save_document_text
from credit_report.generation.etl import DOCUMENT_SECTION_MAP, etl_document
from credit_report.generation.models import SectionDocument
from credit_report.generation.pipeline import (
    check_hard_dependencies,
    get_section_output,
    run_full_report_generation,
    run_section_generation,
)
from credit_report.models import Report, SectionInput, SectionOutput
from credit_report.security.auth import get_current_user, require_analyst
from credit_report.security.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/{report_id}", tags=["generation"])

_MAX_UPLOAD_BYTES = CREDIT_REPORT_MAX_UPLOAD_MB * 1024 * 1024


# ── Schemas ──────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {
    "pdf", "docx", "doc", "pptx", "ppt", "txt", "csv", "md",
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
}

DOCUMENT_TYPES = [
    "annual_report", "financial_statement", "analyst_presentation",
    "interim_report", "valuation_report", "charter_agreement",
    "shipbuilding_contract", "kyc_document", "legal_document",
    "external_report", "other",
]


class DocumentOut(BaseModel):
    id: str
    original_filename: str
    file_size_bytes: int
    document_type: Optional[str] = None
    file_format: Optional[str] = None
    etl_status: Optional[str] = None

    model_config = {"from_attributes": True}


class ETLResult(BaseModel):
    doc_id: str
    document_type: str
    sections_extracted: list[int]
    data: dict[str, dict]  # {str(section_no): {field: value}}


class SectionJsonImportResult(BaseModel):
    section_no: int
    fields_imported: int
    message: str


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


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _assert_can_view(report: Report, current_user: User) -> None:  # noqa: F811
    if current_user.role in {"admin", "reviewer", "approver"}:
        return
    if report.created_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You do not have permission to view this report.")


def _output_to_schema(o: SectionOutput) -> SectionOutputOut:
    return SectionOutputOut(
        section_no=o.section_no,
        status=o.status,
        model_id=o.model_id,
        tokens_used=o.tokens_used,
        generated_at=o.generated_at.isoformat() if o.generated_at else None,
        markdown=o.markdown,
    )


async def _load_section_input(db: AsyncSession, report_id: str, section_no: int) -> dict:
    """Return parsed input_json for a section, or empty dict if not saved."""
    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        )
    )
    si = result.scalar_one_or_none()
    if not si or not si.input_json:
        return {}
    try:
        return json.loads(si.input_json)
    except Exception:
        return {}


# ── Document management ───────────────────────────────────────────────────────

@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    report_id: str,
    file: UploadFile = File(...),
    document_type: str = Form(default="other"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Upload a document (PDF, DOCX, PPTX, TXT, JPG, PNG, etc.) for a report."""
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    if document_type not in DOCUMENT_TYPES:
        document_type = "other"

    fname = (file.filename or "upload").strip()
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        logger.warning("upload_document: file too large bytes=%d limit=%dMB report=%s", len(file_bytes), CREDIT_REPORT_MAX_UPLOAD_MB, report_id)
        raise HTTPException(status_code=413, detail=f"File exceeds the {CREDIT_REPORT_MAX_UPLOAD_MB} MB upload limit")

    doc_id = str(uuid.uuid4())
    logger.info("upload_document: extracting text file=%r type=%s bytes=%d doc=%s report=%s", fname, document_type, len(file_bytes), doc_id, report_id)
    loop = asyncio.get_event_loop()
    text, detected_fmt = await loop.run_in_executor(None, partial(extract_text_from_file, file_bytes, fname))
    save_document_text(report_id, doc_id, text)
    logger.info("upload_document: saved doc=%s fmt=%s chars=%d report=%s user=%s", doc_id, detected_fmt, len(text), report_id, current_user.id)

    doc = SectionDocument(
        id=doc_id,
        report_id=report_id,
        original_filename=fname,
        file_size_bytes=len(file_bytes),
        document_type=document_type,
        file_format=detected_fmt,
        etl_status="pending",
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
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
    result = await db.execute(
        select(SectionDocument)
        .where(SectionDocument.report_id == report_id, SectionDocument.is_deleted == False)
        .order_by(SectionDocument.uploaded_at.desc())
    )
    return list(result.scalars().all())


@router.post("/documents/{doc_id}/etl", response_model=ETLResult)
async def etl_document_endpoint(
    report_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Run AI ETL on an uploaded document — extracts structured data for each relevant section.

    Returns extracted field values per section so the UI can show them and let the
    analyst review/save them as section inputs.
    """
    from credit_report.generation.evidence import load_document_texts
    from pathlib import Path
    from credit_report.config import CREDIT_REPORTS_ROOT

    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)

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

    # Load extracted text from filesystem
    txt_path = CREDIT_REPORTS_ROOT / report_id / f"{doc_id}.txt"
    if not txt_path.exists():
        raise HTTPException(status_code=422, detail="Document text not found — re-upload the file")

    text = txt_path.read_text(encoding="utf-8")
    if not text.strip():
        raise HTTPException(status_code=422, detail="Document appears to have no extractable text")

    doc_type = doc.document_type or "other"
    logger.info("etl_document_endpoint: doc=%s type=%s chars=%d report=%s user=%s", doc_id, doc_type, len(text), report_id, current_user.id)

    try:
        extracted = await etl_document(text=text, document_type=doc_type)
    except Exception as exc:
        logger.exception("etl_document_endpoint: ETL failed doc=%s: %s", doc_id, exc)
        doc.etl_status = "error"
        raise HTTPException(status_code=500, detail=f"ETL extraction failed: {exc}")

    doc.etl_status = "done"

    return ETLResult(
        doc_id=doc_id,
        document_type=doc_type,
        sections_extracted=sorted(extracted.keys()),
        data={str(k): v for k, v in extracted.items()},
    )


@router.post("/import-section-json", response_model=SectionJsonImportResult)
async def import_section_json(
    report_id: str,
    section_no: int = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Import a structured JSON file directly as section input data.

    Accepts a JSON file (e.g. financial-analysis.json) with field-value pairs and
    saves them as the SectionInput for the given section_no.  Existing input is
    merged (JSON-merged, file wins on conflict).
    """
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)

    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1-10")

    raw = await file.read()
    try:
        payload: dict = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON root must be an object")

    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        try:
            current_data: dict = json.loads(existing.data or "{}")
        except Exception:
            current_data = {}
        current_data.update(payload)
        existing.data = json.dumps(current_data, ensure_ascii=False)
        await db.flush()
        logger.info(
            "import_section_json: merged section=%d fields=%d report=%s user=%s",
            section_no, len(payload), report_id, current_user.id,
        )
    else:
        db.add(SectionInput(
            report_id=report_id,
            section_no=section_no,
            data=json.dumps(payload, ensure_ascii=False),
        ))
        await db.flush()
        logger.info(
            "import_section_json: created section=%d fields=%d report=%s user=%s",
            section_no, len(payload), report_id, current_user.id,
        )

    return SectionJsonImportResult(
        section_no=section_no,
        fields_imported=len(payload),
        message=f"Imported {len(payload)} fields into section {section_no}",
    )


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    report_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)
    result = await db.execute(
        select(SectionDocument).where(
            SectionDocument.id == doc_id,
            SectionDocument.report_id == report_id,
            SectionDocument.is_deleted == False,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        logger.warning("delete_document: not found doc=%s report=%s user=%s", doc_id, report_id, current_user.id)
        raise HTTPException(status_code=404, detail="Document not found")
    doc.is_deleted = True
    await db.flush()
    logger.info("delete_document: soft-deleted doc=%s report=%s user=%s", doc_id, report_id, current_user.id)


# ── Section generation ────────────────────────────────────────────────────────

@router.post("/generate/{section_no}", response_model=GenerateOneResult)
async def generate_section(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Trigger AI generation for a single section.

    Requires section input data to be saved first (422 if missing).
    Returns 409 if hard dependencies are not yet generated.
    """
    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1–10")

    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    # Require analyst input data before allowing generation
    input_data = await _load_section_input(db, report_id, section_no)
    if not input_data:
        logger.warning("generate_section: no input data section=%d report=%s user=%s", section_no, report_id, current_user.id)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Section {section_no} has no analyst input data. "
                "Fill in and save the section input JSON before generating."
            ),
        )

    missing = await check_hard_dependencies(db, report_id, section_no)
    if missing:
        logger.info("generate_section: blocked on hard deps=%s section=%d report=%s", missing, section_no, report_id)
        raise HTTPException(
            status_code=409,
            detail=f"Hard dependencies not yet generated: sections {missing}",
        )

    # Pass all already-generated sections as context (critical for §2 Overall Comments)
    preceding: dict[int, str] = {}
    for n in range(1, 11):
        if n == section_no:
            continue
        ctx = await get_section_output(db, report_id, n)
        if ctx and ctx.status == "done" and ctx.markdown:
            preceding[n] = ctx.markdown

    logger.info("generate_section: starting section=%d report=%s user=%s preceding_sections=%s", section_no, report_id, current_user.id, list(preceding.keys()))
    try:
        output = await run_section_generation(
            db=db,
            report_id=report_id,
            section_no=section_no,
            actor_user_id=current_user.id,
            actor_role=current_user.role,
            preceding_outputs=preceding or None,
        )
    except Exception as exc:
        logger.exception("generate_section: generation failed section=%d report=%s: %s", section_no, report_id, exc)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Generation failed: {exc}")

    logger.info("generate_section: done section=%d report=%s status=%s tokens=%s", section_no, report_id, output.status, output.tokens_used)
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
    """Trigger AI generation for all 10 sections in dependency order.

    All 10 sections must have analyst input data saved before this can run.
    Returns 422 with a list of sections that are still missing input data.
    """
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    # Pre-flight: all 10 sections must have non-empty input data
    missing_inputs: list[int] = []
    for sec_no in range(1, 11):
        data = await _load_section_input(db, report_id, sec_no)
        if not data:
            missing_inputs.append(sec_no)

    if missing_inputs:
        logger.warning("generate_full_report: preflight failed missing_sections=%s report=%s user=%s", missing_inputs, report_id, current_user.id)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot generate: sections {missing_inputs} have no analyst input data. "
                "Save input JSON for every section before running the full report."
            ),
        )

    logger.info("generate_full_report: starting full pipeline report=%s user=%s", report_id, current_user.id)
    results = await run_full_report_generation(
        db=db,
        report_id=report_id,
        actor_user_id=current_user.id,
        actor_role=current_user.role,
    )
    logger.info("generate_full_report: complete report=%s results=%s", report_id, results)
    return GenerateAllResult(sections={str(k): v for k, v in results.items()})


# ── Section output retrieval ──────────────────────────────────────────────────

@router.get("/sections/{section_no}/output", response_model=SectionOutputOut)
async def get_section_output_endpoint(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
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
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
    result = await db.execute(
        select(SectionOutput)
        .where(SectionOutput.report_id == report_id)
        .order_by(SectionOutput.section_no)
    )
    return [_output_to_schema(o) for o in result.scalars().all()]

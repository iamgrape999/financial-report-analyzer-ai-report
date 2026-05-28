from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from functools import partial
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.config import CREDIT_REPORT_MAX_UPLOAD_MB, GEMINI_API_KEY
from credit_report.database import get_db
from credit_report.generation.evidence import extract_text_from_file, save_document_binary, save_document_text
from credit_report.generation.etl import DOCUMENT_SECTION_MAP, etl_document
from credit_report.generation.models import SectionDocument
from credit_report.integrations.twse import TWSEOpenAPIClient, build_section7_input
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
    "xlsx", "xls",
}

DOCUMENT_TYPES = [
    "annual_report", "financial_statement", "analyst_presentation",
    "interim_report", "valuation_report", "charter_agreement",
    "shipbuilding_contract", "kyc_document", "legal_document",
    "external_report", "other",
]

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "text/plain": "txt",
    "text/csv": "csv",
    "text/markdown": "md",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
}


def _clean_document_type(document_type: Optional[str]) -> str:
    return document_type if document_type in DOCUMENT_TYPES else "other"


def _extension_from_filename(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _safe_filename(filename: str, fallback: str = "document") -> str:
    basename = unquote(filename.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]).strip()
    basename = re.sub(r"[\\/\x00-\x1f\x7f]+", "_", basename).strip(" ._")
    return basename[:180] or fallback


def _filename_from_url(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    filename = _safe_filename(parsed.path, "downloaded_document")
    ext = _extension_from_filename(filename)
    if ext in ALLOWED_EXTENSIONS:
        return filename
    mapped_ext = CONTENT_TYPE_EXTENSIONS.get(content_type.split(";", 1)[0].strip().lower())
    if mapped_ext:
        return f"{filename}.{mapped_ext}"
    return filename


def _ensure_supported_extension(filename: str) -> str:
    ext = _extension_from_filename(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    return ext


def _extract_and_save_document_text(report_id: str, doc_id: str, file_bytes: bytes, fname: str) -> None:
    try:
        text, detected_fmt = extract_text_from_file(file_bytes, fname)
        save_document_text(report_id, doc_id, text)
        logger.info(
            "document_text_extraction: saved text doc=%s fmt=%s chars=%d report=%s",
            doc_id, detected_fmt, len(text), report_id,
        )
    except Exception as exc:
        logger.exception("document_text_extraction: failed doc=%s file=%r report=%s: %s", doc_id, fname, report_id, exc)


async def _download_document_url(url: str) -> tuple[bytes, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL must be an http(s) document link")

    max_bytes = _MAX_UPLOAD_BYTES
    data = bytearray()
    content_type = ""
    filename = ""
    try:
        timeout = httpx.Timeout(30.0, connect=10.0, read=30.0)
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream("GET", url, headers={"User-Agent": "financial-report-analyzer/1.0"}) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=400, detail=f"URL download failed with HTTP {resp.status_code}")
                content_type = resp.headers.get("content-type", "")
                disposition = resp.headers.get("content-disposition", "")
                match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, re.IGNORECASE)
                if match:
                    filename = _safe_filename(match.group(1), "downloaded_document")
                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        exceeds_limit = int(content_length) > max_bytes
                    except ValueError:
                        exceeds_limit = False
                    if exceeds_limit:
                        raise HTTPException(status_code=413, detail=f"File exceeds the {CREDIT_REPORT_MAX_UPLOAD_MB} MB upload limit")
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise HTTPException(status_code=413, detail=f"File exceeds the {CREDIT_REPORT_MAX_UPLOAD_MB} MB upload limit")
    except HTTPException:
        raise
    except httpx.RequestError as exc:
        raise HTTPException(status_code=400, detail=f"URL download failed: {exc}") from exc

    if not data:
        raise HTTPException(status_code=400, detail="URL returned an empty file")
    filename = filename or _filename_from_url(url, content_type)
    _ensure_supported_extension(filename)
    return bytes(data), filename


class DocumentOut(BaseModel):
    id: str
    original_filename: str
    file_size_bytes: int
    document_type: Optional[str] = None
    file_format: Optional[str] = None
    etl_status: Optional[str] = None

    model_config = {"from_attributes": True}


class DocumentUrlIn(BaseModel):
    url: str
    document_type: Optional[str] = "other"


class TWSEImportRequest(BaseModel):
    stock_code: str
    role: str = "guarantor"  # borrower | guarantor
    section_no: int = 7
    merge_existing: bool = True


class TWSEImportResult(BaseModel):
    section_no: int
    stock_code: str
    fields_imported: int
    input_json: dict


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


class GenerateTaskResult(BaseModel):
    task_id: str
    status: str           # "running" | "done" | "error"
    section_no: Optional[int] = None
    tokens_used: Optional[int] = None
    sections: Optional[dict[str, str]] = None  # for full-report tasks
    detail: Optional[str] = None


# In-memory task registry — acceptable for single-instance deployments.
# Entries are never evicted; memory usage is bounded by restart cadence.
_generation_tasks: dict[str, dict] = {}


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
        ).order_by(SectionInput.id.desc())
    )
    si = result.scalars().first()
    if not si or not si.input_json:
        return {}
    try:
        return json.loads(si.input_json)
    except Exception:
        return {}


def _deep_merge(base: dict, incoming: dict) -> dict:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _count_leaf_fields(obj) -> int:
    if isinstance(obj, dict):
        return sum(_count_leaf_fields(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_leaf_fields(v) for v in obj)
    return 1 if obj not in (None, "") else 0


async def _upsert_section_input_json(
    db: AsyncSession,
    report_id: str,
    section_no: int,
    input_json: dict,
    user_id: str,
) -> SectionInput:
    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        ).order_by(SectionInput.id)
    )
    si = result.scalars().first()
    if si:
        si.input_json = json.dumps(input_json, ensure_ascii=False)
        si.saved_by = user_id
    else:
        si = SectionInput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            input_json=json.dumps(input_json, ensure_ascii=False),
            saved_by=user_id,
        )
        db.add(si)
    await db.flush()
    return si



@router.post("/twse/import", response_model=TWSEImportResult)
async def import_twse_openapi_data(
    report_id: str,
    payload: TWSEImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Import high-coverage TWSE OpenAPI company and financial-statement data into §7."""
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    stock_code = re.sub(r"\D", "", payload.stock_code or "")
    if len(stock_code) != 4:
        raise HTTPException(status_code=400, detail="stock_code must be a 4-digit TWSE listed company code")
    if payload.role not in {"borrower", "guarantor"}:
        raise HTTPException(status_code=400, detail="role must be 'borrower' or 'guarantor'")
    if payload.section_no != 7:
        raise HTTPException(status_code=400, detail="TWSE import currently targets section 7")

    client = TWSEOpenAPIClient()
    bundle = await client.fetch_company_bundle(stock_code)
    imported = build_section7_input(stock_code, bundle, role=payload.role)
    if not imported.get("twse_import", {}).get("row_counts") or not any(imported["twse_import"]["row_counts"].values()):
        raise HTTPException(status_code=404, detail=f"No TWSE OpenAPI rows found for stock code {stock_code}")

    existing = await _load_section_input(db, report_id, payload.section_no) if payload.merge_existing else {}
    merged = _deep_merge(existing, imported) if payload.merge_existing else imported
    await _upsert_section_input_json(db, report_id, payload.section_no, merged, current_user.id)
    logger.info(
        "import_twse_openapi_data: report=%s stock=%s role=%s fields=%d user=%s",
        report_id, stock_code, payload.role, _count_leaf_fields(imported), current_user.id,
    )

    return TWSEImportResult(
        section_no=payload.section_no,
        stock_code=stock_code,
        fields_imported=_count_leaf_fields(imported),
        input_json=merged,
    )


# ── Document management ───────────────────────────────────────────────────────

@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    report_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    document_type: str = Form(default="other"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Upload a document and make it visible immediately; text extraction runs after the response."""
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    document_type = _clean_document_type(document_type)
    fname = _safe_filename(file.filename or "upload", "upload")
    ext = _ensure_supported_extension(fname)

    file_bytes = await file.read()
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        logger.warning("upload_document: file too large bytes=%d limit=%dMB report=%s", len(file_bytes), CREDIT_REPORT_MAX_UPLOAD_MB, report_id)
        raise HTTPException(status_code=413, detail=f"File exceeds the {CREDIT_REPORT_MAX_UPLOAD_MB} MB upload limit")

    doc_id = str(uuid.uuid4())
    save_document_binary(report_id, doc_id, file_bytes, fname)
    logger.info("upload_document: accepted file=%r type=%s bytes=%d doc=%s report=%s user=%s", fname, document_type, len(file_bytes), doc_id, report_id, current_user.id)

    doc = SectionDocument(
        id=doc_id,
        report_id=report_id,
        original_filename=fname,
        file_size_bytes=len(file_bytes),
        document_type=document_type,
        file_format=ext,
        etl_status="pending",
        uploaded_by=current_user.id,
    )
    db.add(doc)
    await db.flush()
    background_tasks.add_task(_extract_and_save_document_text, report_id, doc_id, file_bytes, fname)
    return doc


@router.post("/documents/url", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document_url(
    report_id: str,
    payload: DocumentUrlIn,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Download a document from a URL, store it on the report, and extract text in the background."""
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    document_type = _clean_document_type(payload.document_type)
    file_bytes, fname = await _download_document_url(payload.url)
    ext = _extension_from_filename(fname)

    doc_id = str(uuid.uuid4())
    save_document_binary(report_id, doc_id, file_bytes, fname)
    logger.info("upload_document_url: accepted url=%r file=%r type=%s bytes=%d doc=%s report=%s user=%s", payload.url, fname, document_type, len(file_bytes), doc_id, report_id, current_user.id)

    doc = SectionDocument(
        id=doc_id,
        report_id=report_id,
        original_filename=fname,
        file_size_bytes=len(file_bytes),
        document_type=document_type,
        file_format=ext,
        etl_status="pending",
        uploaded_by=current_user.id,
    )
    db.add(doc)
    await db.flush()
    background_tasks.add_task(_extract_and_save_document_text, report_id, doc_id, file_bytes, fname)
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
    from credit_report.generation.evidence import load_document_texts, save_document_text
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

    # Load extracted text — if .txt is missing, re-extract from stored binary (server restart recovery)
    doc_dir = CREDIT_REPORTS_ROOT / report_id
    txt_path = doc_dir / f"{doc_id}.txt"
    if not txt_path.exists():
        bin_path = doc_dir / f"{doc_id}.bin"
        if bin_path.exists():
            fname_path = doc_dir / f"{doc_id}.fname"
            stored_fname = (
                fname_path.read_text(encoding="utf-8")
                if fname_path.exists()
                else (doc.original_filename or "upload.pdf")
            )
            logger.info(
                "etl_document_endpoint: .txt missing, re-extracting from binary doc=%s file=%r report=%s",
                doc_id, stored_fname, report_id,
            )
            loop = asyncio.get_event_loop()
            reextracted_text, _ = await loop.run_in_executor(
                None, partial(extract_text_from_file, bin_path.read_bytes(), stored_fname)
            )
            save_document_text(report_id, doc_id, reextracted_text)
        else:
            raise HTTPException(
                status_code=422,
                detail="Document file not found on server — please delete and re-upload this document",
            )

    text = txt_path.read_text(encoding="utf-8")
    if not text.strip():
        raise HTTPException(status_code=422, detail="Document appears to have no extractable text")

    doc_type = doc.document_type or "other"
    logger.info("etl_document_endpoint: doc=%s type=%s chars=%d report=%s user=%s", doc_id, doc_type, len(text), report_id, current_user.id)

    try:
        extracted = await etl_document(text=text, document_type=doc_type)
    except ValueError as exc:
        logger.warning("etl_document_endpoint: config error doc=%s: %s", doc_id, exc)
        doc.etl_status = "error"
        await db.commit()  # commit before raise — get_db rolls back on exception
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("etl_document_endpoint: ETL failed doc=%s: %s", doc_id, exc)
        doc.etl_status = "error"
        await db.commit()  # commit before raise — get_db rolls back on exception
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

    if section_no < 1 or section_no > 11:
        raise HTTPException(status_code=400, detail="section_no must be 1-11")

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
            current_data: dict = json.loads(existing.input_json or "{}")
        except Exception:
            current_data = {}
        current_data.update(payload)
        existing.input_json = json.dumps(current_data, ensure_ascii=False)
        await db.flush()
        logger.info(
            "import_section_json: merged section=%d fields=%d report=%s user=%s",
            section_no, len(payload), report_id, current_user.id,
        )
    else:
        db.add(SectionInput(
            report_id=report_id,
            section_no=section_no,
            input_json=json.dumps(payload, ensure_ascii=False),
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

@router.get("/generate/status/{task_id}", response_model=GenerateTaskResult)
async def generation_task_status(
    report_id: str,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Poll the status of a background generation task."""
    task = _generation_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or server was restarted")
    return GenerateTaskResult(task_id=task_id, **task)


@router.post("/generate/{section_no}", status_code=202, response_model=GenerateTaskResult)
async def generate_section(
    report_id: str,
    section_no: int,
    background_tasks: BackgroundTasks,
    gen_language: str = Query(default="en", description="Output language: 'en' for English, 'zh' for Traditional Chinese"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Trigger AI generation for a single section (runs as background task).

    Returns 202 immediately with a task_id. Poll GET /generate/status/{task_id}
    for completion. Returns 409 if hard dependencies are not yet generated.
    """
    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1–10")

    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    # Fast preflight: dependency check (uses request session for immediate 409 response)
    missing = await check_hard_dependencies(db, report_id, section_no)
    if missing:
        logger.info("generate_section: blocked on hard deps=%s section=%d report=%s", missing, section_no, report_id)
        raise HTTPException(
            status_code=409,
            detail=f"Hard dependencies not yet generated: sections {missing}",
        )

    task_id = str(uuid.uuid4())
    _generation_tasks[task_id] = {"status": "running", "section_no": section_no}
    user_id, user_role = current_user.id, current_user.role
    output_lang = gen_language if gen_language in ("en", "zh") else "en"

    async def _bg_generate_section():
        from credit_report.database import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_db:
            try:
                # Reload preceding outputs in background context for freshest data
                preceding: dict[int, str] = {}
                for n in range(1, 11):
                    if n == section_no:
                        continue
                    ctx = await get_section_output(bg_db, report_id, n)
                    if ctx and ctx.status == "done" and ctx.markdown:
                        preceding[n] = ctx.markdown

                logger.info(
                    "generate_section[bg]: starting section=%d report=%s user=%s preceding=%s lang=%s",
                    section_no, report_id, user_id, list(preceding.keys()), output_lang,
                )
                output = await run_section_generation(
                    db=bg_db,
                    report_id=report_id,
                    section_no=section_no,
                    actor_user_id=user_id,
                    actor_role=user_role,
                    preceding_outputs=preceding or None,
                    output_language=output_lang,
                )
                await bg_db.commit()
                _generation_tasks[task_id].update({
                    "status": output.status,
                    "tokens_used": output.tokens_used,
                })
                logger.info(
                    "generate_section[bg]: done section=%d report=%s status=%s tokens=%s",
                    section_no, report_id, output.status, output.tokens_used,
                )
            except Exception as exc:
                try:
                    await bg_db.rollback()
                except Exception:
                    pass
                _generation_tasks[task_id].update({
                    "status": "error",
                    "detail": str(exc)[:500],
                })
                logger.exception(
                    "generate_section[bg]: error section=%d report=%s: %s",
                    section_no, report_id, exc,
                )

    background_tasks.add_task(_bg_generate_section)
    logger.info("generate_section: queued task=%s section=%d report=%s user=%s", task_id, section_no, report_id, user_id)
    return GenerateTaskResult(task_id=task_id, status="running", section_no=section_no)


@router.post("/generate", status_code=202, response_model=GenerateTaskResult)
async def generate_full_report(
    report_id: str,
    background_tasks: BackgroundTasks,
    gen_language: str = Query(default="en", description="Output language: 'en' for English, 'zh' for Traditional Chinese"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Trigger AI generation for all 10 sections in dependency order (background task).

    Returns 202 immediately with a task_id. Poll GET /generate/status/{task_id} for completion.
    Sections without saved input data are skipped; returns 422 only if NO sections have data.
    """
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    # Preflight data check — collect which sections have structured input (informational only)
    sections_with_data: list[int] = []
    for sec_no in range(1, 11):
        data = await _load_section_input(db, report_id, sec_no)
        if data:
            sections_with_data.append(sec_no)

    logger.info(
        "generate_full_report: sections_with_data=%s report=%s user=%s",
        sections_with_data, report_id, current_user.id,
    )
    # Note: sections without structured JSON can still generate from uploaded evidence (ETL chunks).
    # We do NOT block here — the pipeline handles missing input_json gracefully.

    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not configured. Set it in Render environment variables to enable AI generation.",
        )

    task_id = str(uuid.uuid4())
    _generation_tasks[task_id] = {"status": "running"}
    user_id, user_role = current_user.id, current_user.role
    full_output_lang = gen_language if gen_language in ("en", "zh") else "en"

    async def _bg_generate_full_report():
        from credit_report.database import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_db:
            try:
                logger.info(
                    "generate_full_report[bg]: starting report=%s user=%s task=%s lang=%s",
                    report_id, user_id, task_id, full_output_lang,
                )
                results = await run_full_report_generation(
                    db=bg_db,
                    report_id=report_id,
                    actor_user_id=user_id,
                    actor_role=user_role,
                    output_language=full_output_lang,
                )
                await bg_db.commit()
                done = sum(1 for v in results.values() if v == "done")
                _generation_tasks[task_id].update({
                    "status": "done",
                    "sections": {str(k): v for k, v in results.items()},
                })
                logger.info(
                    "generate_full_report[bg]: complete report=%s task=%s done=%d/%d",
                    report_id, task_id, done, len(results),
                )
            except Exception as exc:
                try:
                    await bg_db.rollback()
                except Exception:
                    pass
                _generation_tasks[task_id].update({
                    "status": "error",
                    "detail": str(exc)[:500],
                })
                logger.exception(
                    "generate_full_report[bg]: error report=%s task=%s: %s",
                    report_id, task_id, exc,
                )

    background_tasks.add_task(_bg_generate_full_report)
    logger.info(
        "generate_full_report: queued task=%s report=%s user=%s sections_with_data=%s",
        task_id, report_id, user_id, sections_with_data,
    )
    return GenerateTaskResult(task_id=task_id, status="running")


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

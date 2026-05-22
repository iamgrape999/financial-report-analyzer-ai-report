from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from functools import partial
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.config import CREDIT_REPORT_MAX_UPLOAD_MB, GEMINI_API_KEY
from credit_report.database import get_db
from credit_report.generation.evidence import extract_text_from_file, save_document_binary, save_document_text
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
    "xlsx", "xls",
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
    text_chars: Optional[int] = None
    extraction_quality: Optional[str] = None  # "good" | "low" | "empty"

    model_config = {"from_attributes": True}


class ETLResult(BaseModel):
    doc_id: str
    document_type: str
    sections_extracted: list[int]
    data: dict[str, dict]  # {str(section_no): {field: value}}
    facts_registered: int = 0  # CanonicalFact records auto-created from ETL output


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


_TASK_TTL = 3600        # 1 hour
_TASK_MAXSIZE = 10_000  # cap to prevent unbounded growth on high-traffic instances


class _TaskStore:
    """Bounded in-memory task registry with TTL eviction."""

    def __init__(self, ttl: int = _TASK_TTL, maxsize: int = _TASK_MAXSIZE) -> None:
        self._data: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self._ttl = ttl
        self._maxsize = maxsize

    def set(self, task_id: str, value: dict) -> None:
        self._evict()
        self._data[task_id] = (dict(value), time.monotonic())
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def get(self, task_id: str) -> dict | None:
        entry = self._data.get(task_id)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._data[task_id]
            return None
        return dict(value)

    def update(self, task_id: str, updates: dict) -> None:
        entry = self._data.get(task_id)
        if entry is None:
            return
        value, ts = entry
        value.update(updates)
        self._data[task_id] = (value, ts)

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, ts) in self._data.items() if now - ts > self._ttl]
        for k in expired:
            del self._data[k]


_generation_tasks = _TaskStore()


# ── SSE Progress Bus ──────────────────────────────────────────────────────────

def _sse(event_type: str, data: dict) -> str:
    """Format a single SSE event."""
    import json as _json
    return f"event: {event_type}\ndata: {_json.dumps(data)}\n\n"


class _ProgressBus:
    """In-process pub-sub for SSE progress events keyed by task_id."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}

    def create(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._queues[task_id] = q
        return q

    def push(self, task_id: str, event: dict) -> None:
        q = self._queues.get(task_id)
        if q:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def close(self, task_id: str) -> None:
        q = self._queues.pop(task_id, None)
        if q:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def stream(self, task_id: str) -> AsyncGenerator[str, None]:
        """Yield SSE strings until None sentinel is received."""
        q = self._queues.get(task_id)
        if not q:
            yield _sse("error", {"detail": "task not found"})
            return
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=60.0)
            except asyncio.TimeoutError:
                yield _sse("heartbeat", {"ts": time.monotonic()})
                continue
            if event is None:
                break
            yield _sse(event.get("type", "progress"), event)


_progress_bus = _ProgressBus()


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


def _strip_nulls(obj: object) -> object:
    """Recursively remove None values from dicts/lists to avoid wasting tokens on null fields."""
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        cleaned = [_strip_nulls(i) for i in obj if i is not None]
        # Drop list items that became empty dicts after stripping
        return [i for i in cleaned if not (isinstance(i, dict) and not i)]
    return obj


def _deep_merge_section_input(base: dict, overlay: dict) -> dict:
    """Merge ETL overlay into existing section input — analyst data (base) wins on non-null values."""
    result = dict(base)
    for k, v in overlay.items():
        if k not in result or result[k] is None or result[k] == "" or result[k] == []:
            result[k] = v
        elif isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge_section_input(result[k], v)
    return result


async def _auto_populate_section_inputs(
    db: AsyncSession,
    report_id: str,
    doc_id: str,
    extracted: dict[int, dict],
    actor_user_id: str,
) -> dict[int, str]:
    """Save ETL extraction results as SectionInput rows, merging with existing analyst data.

    Existing analyst-entered values always win (ETL only fills empty fields).
    Returns {section_no: "new" | "merged"} for logging.
    """
    results: dict[int, str] = {}
    for sec_no, sec_data in extracted.items():
        if not isinstance(sec_data, dict) or not sec_data:
            continue
        try:
            existing_res = await db.execute(
                select(SectionInput).where(
                    SectionInput.report_id == report_id,
                    SectionInput.section_no == sec_no,
                ).order_by(SectionInput.id.desc())
            )
            si = existing_res.scalars().first()
            if si and si.input_json:
                try:
                    current_data = json.loads(si.input_json)
                except Exception:
                    current_data = {}
                merged = _strip_nulls(_deep_merge_section_input(current_data, sec_data))
                si.input_json = json.dumps(merged, ensure_ascii=False)
                si.saved_by = actor_user_id
                results[sec_no] = "merged"
            else:
                new_si = SectionInput(
                    id=str(uuid.uuid4()),
                    report_id=report_id,
                    section_no=sec_no,
                    input_json=json.dumps(_strip_nulls(sec_data), ensure_ascii=False),
                    saved_by=actor_user_id,
                )
                db.add(new_si)
                results[sec_no] = "new"
        except Exception as _e:
            logger.warning("_auto_populate_section_inputs: section=%d error: %s", sec_no, _e)
    await db.flush()
    logger.info(
        "_auto_populate_section_inputs: report=%s doc=%s sections=%s",
        report_id, doc_id, results,
    )
    return results


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
    try:
        text, detected_fmt = await asyncio.wait_for(
            loop.run_in_executor(None, partial(extract_text_from_file, file_bytes, fname)),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        logger.error("upload_document: text extraction timed out file=%r bytes=%d doc=%s", fname, len(file_bytes), doc_id)
        raise HTTPException(
            status_code=408,
            detail="Document text extraction timed out. Try compressing the PDF or uploading a smaller portion.",
        )
    save_document_text(report_id, doc_id, text)
    save_document_binary(report_id, doc_id, file_bytes, fname)

    text_chars = len(text.strip())
    from credit_report.generation.evidence import _text_quality_ok
    if text_chars == 0:
        extraction_quality = "empty"
        logger.warning(
            "upload_document: EMPTY text after extraction file=%r doc=%s report=%s "
            "— ETL will receive no data; try a different file format or re-scan",
            fname, doc_id, report_id,
        )
    elif not _text_quality_ok(text):
        extraction_quality = "low"
        logger.warning(
            "upload_document: LOW QUALITY text file=%r chars=%d doc=%s report=%s "
            "— Vision OCR was attempted but content may be degraded",
            fname, text_chars, doc_id, report_id,
        )
    else:
        extraction_quality = "good"

    logger.info(
        "upload_document: saved doc=%s fmt=%s chars=%d quality=%s report=%s user=%s",
        doc_id, detected_fmt, text_chars, extraction_quality, report_id, current_user.id,
    )

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
    return DocumentOut(
        id=doc.id,
        original_filename=doc.original_filename,
        file_size_bytes=doc.file_size_bytes,
        document_type=doc.document_type,
        file_format=doc.file_format,
        etl_status=doc.etl_status,
        text_chars=text_chars,
        extraction_quality=extraction_quality,
    )


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

    # Auto-register key financial metrics extracted by ETL as CanonicalFact records.
    # This enables downstream fact-conflict detection and report generation without
    # requiring analysts to manually import each field.
    facts_registered = 0
    try:
        from credit_report.generation.etl import build_canonical_facts_from_etl
        from credit_report.fact_store import repository as fact_repo

        facts_data = build_canonical_facts_from_etl(
            report_id=report_id,
            doc_id=doc_id,
            extracted=extracted,
        )
        if facts_data:
            await fact_repo.upsert_facts(db, facts_data)
            facts_registered = len(facts_data)
            logger.info(
                "etl_document_endpoint: auto-registered %d facts doc=%s report=%s",
                facts_registered, doc_id, report_id,
            )
    except Exception as _freg_err:
        logger.warning(
            "etl_document_endpoint: fact auto-registration failed doc=%s report=%s: %s",
            doc_id, report_id, _freg_err,
        )

    # Auto-populate SectionInput rows from ETL data so generation can proceed immediately
    sections_populated: dict[int, str] = {}
    try:
        sections_populated = await _auto_populate_section_inputs(
            db=db,
            report_id=report_id,
            doc_id=doc_id,
            extracted=extracted,
            actor_user_id=current_user.id,
        )
    except Exception as _pop_err:
        logger.warning(
            "etl_document_endpoint: section auto-populate failed doc=%s report=%s: %s",
            doc_id, report_id, _pop_err,
        )

    await db.commit()
    logger.info(
        "etl_document_endpoint: done doc=%s report=%s facts=%d sections_populated=%s",
        doc_id, report_id, facts_registered, sections_populated,
    )

    return ETLResult(
        doc_id=doc_id,
        document_type=doc_type,
        sections_extracted=sorted(extracted.keys()),
        data={str(k): v for k, v in extracted.items()},
        facts_registered=facts_registered,
    )


@router.post("/documents/{doc_id}/etl/stream")
async def etl_document_stream(
    report_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """
    Streaming ETL: runs OCR + extraction as a background task and returns
    a task_id.  Connect to GET .../etl/stream/{task_id} for SSE progress.
    """
    from credit_report.config import CREDIT_REPORTS_ROOT
    from pathlib import Path

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

    task_id = str(uuid.uuid4())
    _progress_bus.create(task_id)

    # Capture values needed in background task
    user_id = current_user.id
    doc_type = doc.document_type or "other"
    doc_dir = CREDIT_REPORTS_ROOT / report_id
    txt_path = doc_dir / f"{doc_id}.txt"
    bin_path = doc_dir / f"{doc_id}.bin"
    fname_path = doc_dir / f"{doc_id}.fname"

    async def _run_streaming_etl() -> None:
        from credit_report.database import AsyncSessionLocal

        async with AsyncSessionLocal() as bg_db:
            try:
                _progress_bus.push(task_id, {"type": "start", "stage": "ocr", "message": "Starting document processing…"})

                # Load text or re-extract
                if txt_path.exists():
                    text = txt_path.read_text(encoding="utf-8")
                    _progress_bus.push(task_id, {
                        "type": "ocr_done", "chars": len(text),
                        "message": f"Document text loaded ({len(text):,} chars)",
                    })
                elif bin_path.exists():
                    stored_fname = fname_path.read_text(encoding="utf-8") if fname_path.exists() else "upload.pdf"
                    ext = stored_fname.rsplit(".", 1)[-1].lower() if "." in stored_fname else ""

                    _progress_bus.push(task_id, {"type": "ocr_start", "message": "Extracting text from document…"})

                    if ext == "pdf":
                        from credit_report.generation.evidence import (
                            extract_text_from_pdf,
                            extract_text_from_scanned_pdf_vision_async,
                            _quality_stats,
                        )
                        raw = bin_path.read_bytes()
                        loop = asyncio.get_event_loop()
                        text = await loop.run_in_executor(None, extract_text_from_pdf, raw)
                        stats = _quality_stats(text)

                        if not text.strip() or stats["ratio_pct"] < 5:
                            # Scanned PDF — use async VLM OCR with progress
                            _progress_bus.push(task_id, {
                                "type": "ocr_vlm_start",
                                "message": "Scanned PDF detected — using Gemini Vision OCR…",
                            })
                            try:
                                from pypdf import PdfReader
                                import io as _io
                                n_pages = len(PdfReader(_io.BytesIO(raw)).pages)
                            except Exception:
                                n_pages = 0

                            _progress_bus.push(task_id, {
                                "type": "ocr_pages_total",
                                "total_pages": n_pages,
                                "message": f"Processing {n_pages} pages…",
                            })

                            def _on_page(page_idx, total, chars):
                                _progress_bus.push(task_id, {
                                    "type": "ocr_page",
                                    "page": page_idx,
                                    "total": total,
                                    "chars": chars,
                                    "pct": round(page_idx / max(total, 1) * 100),
                                    "message": f"OCR page {page_idx}/{total}…",
                                })

                            text = await extract_text_from_scanned_pdf_vision_async(
                                raw,
                                on_progress=_on_page,
                                max_pages=200,
                            )
                        save_document_text(report_id, doc_id, text)
                    else:
                        raw = bin_path.read_bytes()
                        from credit_report.generation.evidence import extract_text_from_file
                        loop = asyncio.get_event_loop()
                        text, _ = await loop.run_in_executor(None, extract_text_from_file, raw, stored_fname)
                        save_document_text(report_id, doc_id, text)
                else:
                    _progress_bus.push(task_id, {"type": "error", "message": "Document file not found — please re-upload"})
                    _progress_bus.close(task_id)
                    return

                if not text.strip():
                    _progress_bus.push(task_id, {"type": "error", "message": "No extractable text found in document"})
                    _progress_bus.close(task_id)
                    return

                _progress_bus.push(task_id, {
                    "type": "ocr_done",
                    "chars": len(text),
                    "message": f"Text extraction complete — {len(text):,} characters",
                })

                # ETL extraction phase
                from credit_report.generation.etl import (
                    DOCUMENT_SECTION_MAP, _ETL_CHUNK_SIZE, _ETL_CHUNK_OVERLAP,
                    _call_gemini_etl_once, _deep_merge_etl, _has_any_value,
                )
                target_sections = DOCUMENT_SECTION_MAP.get(doc_type, [4, 7])
                n_chunks = max(1, (len(text) - 1) // (_ETL_CHUNK_SIZE - _ETL_CHUNK_OVERLAP) + 1) if len(text) > _ETL_CHUNK_SIZE else 1

                _progress_bus.push(task_id, {
                    "type": "etl_start",
                    "sections": target_sections,
                    "chunks": n_chunks,
                    "message": f"Extracting {len(target_sections)} sections in {n_chunks} chunk(s)…",
                })

                # Run ETL with per-chunk progress
                merged: dict[int, dict] = {}
                if len(text) > _ETL_CHUNK_SIZE:
                    chunks = []
                    i = 0
                    while i < len(text):
                        chunk = text[i: i + _ETL_CHUNK_SIZE]
                        if chunk.strip():
                            chunks.append(chunk)
                        i += _ETL_CHUNK_SIZE - _ETL_CHUNK_OVERLAP

                    for c_idx, chunk in enumerate(chunks):
                        _progress_bus.push(task_id, {
                            "type": "etl_chunk",
                            "chunk": c_idx + 1,
                            "total_chunks": len(chunks),
                            "pct": round((c_idx + 1) / len(chunks) * 100),
                            "message": f"Analysing text chunk {c_idx + 1}/{len(chunks)}…",
                        })
                        chunk_result = await _call_gemini_etl_once(
                            document_type=doc_type,
                            text_chunk=chunk,
                            target_sections=target_sections,
                            chunk_info=f"{c_idx + 1}/{len(chunks)}",
                        )
                        for sec_no, sec_data in chunk_result.items():
                            if sec_no in merged:
                                merged[sec_no] = _deep_merge_etl(merged[sec_no], sec_data)
                            else:
                                merged[sec_no] = sec_data
                else:
                    _progress_bus.push(task_id, {
                        "type": "etl_chunk", "chunk": 1, "total_chunks": 1, "pct": 50,
                        "message": "Analysing document…",
                    })
                    merged = await _call_gemini_etl_once(
                        document_type=doc_type,
                        text_chunk=text,
                        target_sections=target_sections,
                    )

                _progress_bus.push(task_id, {
                    "type": "etl_done",
                    "sections_extracted": sorted(merged.keys()),
                    "message": f"Extraction complete — {len(merged)} section(s) found",
                })

                # Apply section-specific flatten transforms so downstream code sees
                # the same flat FIELD_DEFS-compatible structure as etl_document() produces.
                from credit_report.generation.etl import (
                    _flatten_section3 as _fs3,
                    _flatten_section5 as _fs5,
                    _flatten_section6 as _fs6,
                    _flatten_section8 as _fs8,
                )
                if 3 in merged and isinstance(merged[3], dict):
                    merged[3] = _fs3(merged[3])
                if 5 in merged and isinstance(merged[5], dict):
                    merged[5] = _fs5(merged[5])
                if 6 in merged and isinstance(merged[6], dict):
                    merged[6] = _fs6(merged[6])
                if 8 in merged and isinstance(merged[8], dict):
                    merged[8] = _fs8(merged[8])

                # Auto-register CanonicalFacts
                facts_registered = 0
                if merged:
                    _progress_bus.push(task_id, {
                        "type": "facts_start",
                        "message": "Registering extracted facts…",
                    })
                    try:
                        from credit_report.generation.etl import build_canonical_facts_from_etl
                        from credit_report.fact_store import repository as fact_repo

                        facts_data = build_canonical_facts_from_etl(
                            report_id=report_id,
                            doc_id=doc_id,
                            extracted=merged,
                        )
                        if facts_data:
                            await fact_repo.upsert_facts(bg_db, facts_data)
                            facts_registered = len(facts_data)

                        # Auto-populate SectionInput rows so generation can proceed immediately
                        try:
                            await _auto_populate_section_inputs(
                                db=bg_db,
                                report_id=report_id,
                                doc_id=doc_id,
                                extracted=merged,
                                actor_user_id=user_id,
                            )
                        except Exception as _pop_err:
                            logger.warning("[ETL-STREAM] section populate error: %s", _pop_err)

                        # Update document ETL status
                        doc_res = await bg_db.execute(
                            select(SectionDocument).where(SectionDocument.id == doc_id)
                        )
                        bg_doc = doc_res.scalar_one_or_none()
                        if bg_doc:
                            bg_doc.etl_status = "done"
                        await bg_db.commit()

                    except Exception as _fe:
                        logger.warning("[ETL-STREAM] fact registration error: %s", _fe)

                _progress_bus.push(task_id, {
                    "type": "complete",
                    "sections_extracted": sorted(merged.keys()),
                    "facts_registered": facts_registered,
                    "doc_type": doc_type,
                    "message": f"Done! {len(merged)} section(s), {facts_registered} fact(s) registered",
                    "data": {str(k): v for k, v in merged.items()},
                })

            except Exception as exc:
                logger.exception("[ETL-STREAM] task=%s error: %s", task_id, exc)
                _progress_bus.push(task_id, {
                    "type": "error",
                    "message": f"ETL failed: {exc}",
                })
            finally:
                _progress_bus.close(task_id)

    background_tasks.add_task(_run_streaming_etl)
    return {"task_id": task_id, "status": "running"}


async def _sse_user(
    request: Request,
    token: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve current user from Bearer header OR ?token= query param (needed for EventSource)."""
    from credit_report.security.auth import decode_token
    from credit_report.security.models import User as _User
    bearer = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:].strip()
    if not bearer and token:
        bearer = token
    if not bearer:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(bearer)
        user_id = payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    result = await db.execute(select(_User).where(_User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


@router.get("/documents/etl/stream/{task_id}")
async def etl_stream_events(
    request: Request,
    report_id: str,
    task_id: str,
    token: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """SSE endpoint — stream ETL progress events for a given task_id (EventSource-compatible)."""
    await _sse_user(request, token=token, db=db)
    return StreamingResponse(
        _progress_bus.stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
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
        ).order_by(SectionInput.id.desc())
    )
    existing = result.scalars().first()

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
    _generation_tasks.set(task_id, {"status": "running", "section_no": section_no})
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
                _generation_tasks.update(task_id, {
                    "status": output.status,
                    "tokens_used": output.tokens_used,
                })
                _progress_bus.push(task_id, {
                    "type": "section_done",
                    "section_no": section_no,
                    "tokens_used": output.tokens_used,
                    "message": f"§{section_no} generation complete ({output.tokens_used} tokens)",
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
                _generation_tasks.update(task_id, {
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
    _generation_tasks.set(task_id, {"status": "running"})
    _progress_bus.create(task_id)
    user_id, user_role = current_user.id, current_user.role
    full_output_lang = gen_language if gen_language in ("en", "zh") else "en"

    async def _bg_generate_full_report():
        from credit_report.database import AsyncSessionLocal
        from credit_report.config import GENERATION_ORDER
        from credit_report.generation.pipeline import (
            run_section_generation,
            check_hard_dependencies,
        )
        async with AsyncSessionLocal() as bg_db:
            try:
                logger.info(
                    "generate_full_report[bg]: starting report=%s user=%s task=%s lang=%s",
                    report_id, user_id, task_id, full_output_lang,
                )
                results: dict = {}
                generated_outputs: dict = {}
                total = len(GENERATION_ORDER)

                for section_no in GENERATION_ORDER:
                    done_count = len(results)
                    _progress_bus.push(task_id, {
                        "type": "section_progress",
                        "section_no": section_no,
                        "status": "generating",
                        "done_count": done_count,
                        "total": total,
                        "pct": round(done_count / total * 100),
                        "message": f"§{section_no} generating… ({done_count}/{total})",
                    })

                    missing_deps = await check_hard_dependencies(bg_db, report_id, section_no)
                    if missing_deps:
                        logger.warning(
                            "generate_full_report[bg]: skipping section=%d missing_deps=%s report=%s",
                            section_no, missing_deps, report_id,
                        )
                        results[section_no] = f"skipped_missing_deps:{missing_deps}"
                    else:
                        try:
                            output = await run_section_generation(
                                db=bg_db,
                                report_id=report_id,
                                section_no=section_no,
                                actor_user_id=user_id,
                                actor_role=user_role,
                                preceding_outputs=generated_outputs,
                                output_language=full_output_lang,
                            )
                            results[section_no] = output.status
                            if output.markdown:
                                generated_outputs[section_no] = output.markdown
                        except Exception as exc:
                            logger.error(
                                "generate_full_report[bg]: section=%d failed report=%s: %s",
                                section_no, report_id, exc,
                            )
                            results[section_no] = f"error:{exc}"

                    done_count = len(results)
                    sec_status = results[section_no]
                    _progress_bus.push(task_id, {
                        "type": "section_done",
                        "section_no": section_no,
                        "status": sec_status,
                        "done_count": done_count,
                        "total": total,
                        "pct": round(done_count / total * 100),
                        "message": f"§{section_no} {sec_status} ({done_count}/{total} sections)",
                    })

                await bg_db.commit()
                done = sum(1 for v in results.values() if v == "done")
                _progress_bus.push(task_id, {
                    "type": "complete",
                    "sections": {str(k): v for k, v in results.items()},
                    "done_count": done,
                    "total": total,
                    "message": f"Report generation complete — {done}/{len(results)} sections done",
                })
                _generation_tasks.update(task_id, {
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
                _generation_tasks.update(task_id, {
                    "status": "error",
                    "detail": str(exc)[:500],
                })
                logger.exception(
                    "generate_full_report[bg]: error report=%s task=%s: %s",
                    report_id, task_id, exc,
                )
            finally:
                _progress_bus.close(task_id)

    background_tasks.add_task(_bg_generate_full_report)
    logger.info(
        "generate_full_report: queued task=%s report=%s user=%s sections_with_data=%s",
        task_id, report_id, user_id, sections_with_data,
    )
    return GenerateTaskResult(task_id=task_id, status="running")


@router.get("/generate/stream/{task_id}")
async def generation_stream_events(
    request: Request,
    report_id: str,
    task_id: str,
    token: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """SSE endpoint — stream section generation progress (EventSource-compatible)."""
    await _sse_user(request, token=token, db=db)
    return StreamingResponse(
        _progress_bus.stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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

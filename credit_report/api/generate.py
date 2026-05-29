from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import time
import uuid
from collections import OrderedDict
from functools import partial
from pathlib import Path as _Path
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.config import CREDIT_REPORT_MAX_UPLOAD_MB, GEMINI_API_KEY
from credit_report.schemas import GapFillRequest, GapFillResponse, GapFillSectionResult
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
from credit_report.security.rate_limit import rate_limit_check

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/{report_id}", tags=["generation"])
diagnostics_router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

_MAX_UPLOAD_BYTES = CREDIT_REPORT_MAX_UPLOAD_MB * 1024 * 1024


# ── Schemas ──────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {
    "pdf", "docx", "doc", "pptx", "ppt", "txt", "csv", "md",
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
    "xlsx", "xls",
}

# Magic-number prefixes for security-critical binary formats.
# Checked against the first 8 bytes of every uploaded file.
_MAGIC_SIGNATURES: dict[bytes, str] = {
    b"%PDF":          "pdf",
    b"PK\x03\x04":   "zip-office",   # .docx/.xlsx/.pptx are zip-based
    b"\xd0\xcf\x11\xe0": "ole",      # .doc/.xls/.ppt legacy Office
    b"\xff\xd8\xff":  "jpeg",
    b"\x89PNG":       "png",
    b"GIF8":          "gif",
    b"RIFF":          "riff",        # .webp uses RIFF container
}

def _check_magic(data: bytes, ext: str) -> bool:
    """Return False if the file's magic bytes contradict its declared extension."""
    head = data[:8]
    for sig, kind in _MAGIC_SIGNATURES.items():
        if not head.startswith(sig):
            continue
        if kind == "pdf" and ext == "pdf":
            return True
        if kind == "zip-office" and ext in ("docx", "xlsx", "pptx"):
            return True
        if kind == "ole" and ext in ("doc", "xls", "ppt"):
            return True
        if kind == "jpeg" and ext in ("jpg", "jpeg"):
            return True
        if kind == "png" and ext == "png":
            return True
        if kind == "gif" and ext == "gif":
            return True
        if kind == "riff" and ext == "webp":
            return True
        # Magic matched a known format but extension disagrees → reject
        return False
    # No magic match for this extension — pass through (text/csv/md have no magic)
    return True

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
_url_import_tasks = _TaskStore(ttl=3600, maxsize=500)  # longer TTL for background downloads

# Per-(report_id, section_no) lock — prevents duplicate concurrent generation of
# the same section.  Entries are added just before the BackgroundTask is queued
# and removed when the background task finishes (success or error).
_generating_sections: set[tuple[str, int]] = set()


# ── SSE Progress Bus ──────────────────────────────────────────────────────────

def _sse(event_type: str, data: dict) -> str:
    """Format a single SSE event."""
    import json as _json
    return f"event: {event_type}\ndata: {_json.dumps(data)}\n\n"


class _ProgressBus:
    """In-process pub-sub for SSE progress events keyed by task_id.

    Improvements over the naive dict approach:
    - LRU capacity cap (_MAX_QUEUES) evicts oldest queue when full
    - TTL eviction (_QUEUE_TTL) prevents orphaned queues from zombified tasks
    - Cancellation flag: stream() sets it on client disconnect so the background
      ETL coroutine can check is_cancelled() and abort between chunks
    """

    _MAX_QUEUES = 1_000
    _QUEUE_TTL = 3_600  # seconds — orphan cleanup

    def __init__(self) -> None:
        self._queues: OrderedDict[str, asyncio.Queue] = OrderedDict()
        self._created_at: dict[str, float] = {}
        self._cancelled: set[str] = set()

    def _evict_stale(self) -> None:
        now = time.monotonic()
        stale = [k for k, ts in self._created_at.items() if now - ts > self._QUEUE_TTL]
        for k in stale:
            self._queues.pop(k, None)
            self._created_at.pop(k, None)
            self._cancelled.discard(k)

    def create(self, task_id: str) -> asyncio.Queue:
        self._evict_stale()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._queues[task_id] = q
        self._created_at[task_id] = time.monotonic()
        # LRU cap: drop the oldest entry when over budget
        if len(self._queues) > self._MAX_QUEUES:
            oldest_id, _ = next(iter(self._queues.items()))
            self._queues.pop(oldest_id, None)
            self._created_at.pop(oldest_id, None)
            self._cancelled.discard(oldest_id)
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
        self._created_at.pop(task_id, None)
        if q:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def cancel(self, task_id: str) -> None:
        """Signal the background ETL/generation task to stop cleanly."""
        self._cancelled.add(task_id)

    def is_cancelled(self, task_id: str) -> bool:
        return task_id in self._cancelled

    async def stream(self, task_id: str) -> AsyncGenerator[str, None]:
        """Yield SSE strings until None sentinel is received.

        The finally block removes the queue AND sets the cancellation flag so
        the background task stops between chunks when the client disconnects.
        """
        q = self._queues.get(task_id)
        if not q:
            yield _sse("error", {"detail": "task not found"})
            return
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    yield _sse("heartbeat", {"ts": time.monotonic()})
                    continue
                if event is None:
                    break
                yield _sse(event.get("type", "progress"), event)
        finally:
            self._queues.pop(task_id, None)
            self._created_at.pop(task_id, None)
            # Signal cancellation so the background task aborts between chunks
            self._cancelled.add(task_id)


_progress_bus = _ProgressBus()

# Maps ETL task_id → report_id so etl_stream_events can verify report ownership.
# Bounded to 10 000 entries (same order-of-magnitude as _ProgressBus._MAX_QUEUES).
_ETL_TASK_REPORT: "OrderedDict[str, str]" = OrderedDict()
_ETL_TASK_REPORT_MAX = 10_000


def _register_etl_task(task_id: str, report_id: str) -> None:
    _ETL_TASK_REPORT[task_id] = report_id
    _ETL_TASK_REPORT.move_to_end(task_id)
    if len(_ETL_TASK_REPORT) > _ETL_TASK_REPORT_MAX:
        _ETL_TASK_REPORT.popitem(last=False)


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

async def _extract_and_save_text_bg(report_id: str, doc_id: str) -> None:
    """Background task: read binary from disk, extract text, persist.

    Reads from the already-saved .bin file so that file_bytes are NOT held in memory
    from request time until this task runs (prevents OOM when extractions queue up).
    If extraction fails, ETL recovers from the .bin on the next run.
    """
    from credit_report.config import CREDIT_REPORTS_ROOT as _CR_ROOT
    loop = asyncio.get_running_loop()
    try:
        bin_path = _CR_ROOT / report_id / f"{doc_id}.bin"
        fname_path = _CR_ROOT / report_id / f"{doc_id}.fname"
        if not bin_path.exists():
            logger.error("upload bg: binary not found doc=%s report=%s — skipping extraction", doc_id, report_id)
            return
        file_bytes = bin_path.read_bytes()
        fname = fname_path.read_text(encoding="utf-8") if fname_path.exists() else f"{doc_id}.bin"

        text, detected_fmt = await loop.run_in_executor(
            None, partial(extract_text_from_file, file_bytes, fname)
        )
        del file_bytes  # free before writing text (save_document_text is fast)
        save_document_text(report_id, doc_id, text)
        logger.info(
            "upload_document bg: extraction done doc=%s fmt=%s chars=%d report=%s",
            doc_id, detected_fmt, len(text.strip()), report_id,
        )
    except Exception as exc:
        logger.error(
            "upload_document bg: extraction failed doc=%s report=%s: %s — "
            "ETL will re-extract from stored binary when triggered",
            doc_id, report_id, exc,
        )


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    report_id: str,
    file: UploadFile = File(...),
    document_type: str = Form(default="other"),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Upload a document (PDF, DOCX, PPTX, TXT, JPG, PNG, etc.) for a report."""
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)
    rate_limit_check(f"upload:{current_user.id}", max_requests=10, window_seconds=3600)

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

    if not _check_magic(file_bytes, ext):
        logger.warning("upload_document: magic-byte mismatch file=%r ext=%s report=%s", fname, ext, report_id)
        raise HTTPException(
            status_code=400,
            detail=f"File content does not match its declared extension '.{ext}'. Upload rejected.",
        )

    doc_id = str(uuid.uuid4())

    # Save binary BEFORE returning — document is always recoverable even if
    # text extraction is slow or fails (ETL re-extracts from .bin when .txt missing).
    save_document_binary(report_id, doc_id, file_bytes, fname)

    doc = SectionDocument(
        id=doc_id,
        report_id=report_id,
        original_filename=fname,
        file_size_bytes=len(file_bytes),
        document_type=document_type,
        file_format=ext,  # refined to detected format once background task completes
        etl_status="pending",
        uploaded_by=current_user.id,
    )
    db.add(doc)
    await db.flush()

    logger.info(
        "upload_document: registered doc=%s file=%r bytes=%d report=%s user=%s — text extraction queued",
        doc_id, fname, len(file_bytes), report_id, current_user.id,
    )

    # Kick off text extraction after the 201 response is sent.
    # This eliminates the 95 %-progress stall for large/scanned PDFs that need
    # Gemini Vision OCR (60-180 s): the HTTP connection closes immediately.
    if background_tasks is not None:
        background_tasks.add_task(_extract_and_save_text_bg, report_id, doc_id)

    return DocumentOut(
        id=doc.id,
        original_filename=doc.original_filename,
        file_size_bytes=doc.file_size_bytes,
        document_type=doc.document_type,
        file_format=doc.file_format,
        etl_status=doc.etl_status,
        text_chars=None,
        extraction_quality=None,
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
    from credit_report.generation.evidence import load_document_texts, save_document_text, _safe_report_dir
    from pathlib import Path

    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
    rate_limit_check(f"etl:{current_user.id}", max_requests=5, window_seconds=1800)

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
    doc_dir = _safe_report_dir(report_id)
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
            loop = asyncio.get_running_loop()
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
        await write_event(db, action="etl.failed", actor_user_id=current_user.id,
                          actor_role=current_user.role, report_id=report_id,
                          target_type="document", target_id=doc_id, reason=str(exc))
        await db.commit()  # commit before raise — get_db rolls back on exception
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("etl_document_endpoint: ETL failed doc=%s: %s", doc_id, exc)
        doc.etl_status = "error"
        await write_event(db, action="etl.failed", actor_user_id=current_user.id,
                          actor_role=current_user.role, report_id=report_id,
                          target_type="document", target_id=doc_id, reason=str(exc))
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
        else:
            logger.warning("etl_document_endpoint: zero facts extracted doc=%s report=%s", doc_id, report_id)
            await write_event(db, action="etl.zero_facts", actor_user_id=current_user.id,
                              actor_role=current_user.role, report_id=report_id,
                              target_type="document", target_id=doc_id,
                              reason="ETL succeeded but extracted zero mappable facts")
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
    from credit_report.generation.evidence import _safe_report_dir
    from pathlib import Path

    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
    rate_limit_check(f"etl:{current_user.id}", max_requests=5, window_seconds=1800)

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
    _register_etl_task(task_id, report_id)

    # Capture values needed in background task
    user_id = current_user.id
    user_role = current_user.role
    doc_type = doc.document_type or "other"
    doc_dir = _safe_report_dir(report_id)
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
                        loop = asyncio.get_running_loop()
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
                        loop = asyncio.get_running_loop()
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
                        # Abort if client disconnected — prevents burning Gemini tokens
                        if _progress_bus.is_cancelled(task_id):
                            logger.info("[ETL-STREAM] task=%s cancelled by client disconnect at chunk %d", task_id, c_idx + 1)
                            return
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
                    _flatten_section4 as _fs4,
                    _flatten_section5 as _fs5,
                    _flatten_section6 as _fs6,
                    _flatten_section8 as _fs8,
                    _flatten_section10 as _fs10,
                )
                if 3 in merged and isinstance(merged[3], dict):
                    merged[3] = _fs3(merged[3])
                if 4 in merged and isinstance(merged[4], dict):
                    merged[4] = _fs4(merged[4])
                if 5 in merged and isinstance(merged[5], dict):
                    merged[5] = _fs5(merged[5])
                if 6 in merged and isinstance(merged[6], dict):
                    merged[6] = _fs6(merged[6])
                if 8 in merged and isinstance(merged[8], dict):
                    merged[8] = _fs8(merged[8])
                if 10 in merged and isinstance(merged[10], dict):
                    merged[10] = _fs10(merged[10])

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
                        else:
                            logger.warning("[ETL-STREAM] zero facts extracted doc=%s report=%s", doc_id, report_id)
                            await write_event(
                                bg_db,
                                action="etl.zero_facts",
                                actor_user_id=user_id,
                                actor_role=user_role,
                                report_id=report_id,
                                target_type="document",
                                target_id=doc_id,
                                reason="ETL succeeded but extracted zero mappable facts",
                            )

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
                try:
                    await write_event(
                        bg_db,
                        action="etl.failed",
                        actor_user_id=user_id,
                        actor_role=user_role,
                        report_id=report_id,
                        target_type="document",
                        target_id=doc_id,
                        reason=str(exc),
                    )
                    await bg_db.commit()
                except Exception:
                    logger.warning("[ETL-STREAM] audit write failed for task=%s", task_id)
            finally:
                _progress_bus.close(task_id)

    background_tasks.add_task(_run_streaming_etl)
    return {"task_id": task_id, "status": "running"}


async def _sse_user(
    request: Request,
    token: Optional[str] = Query(default=None),
    ticket: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve current user for SSE endpoints.

    Preferred: ?ticket=<one-time-ticket> obtained from POST /auth/sse-ticket.
    The ticket is consumed immediately so it never replays even if captured in logs.
    Legacy fallback: ?token=<jwt> or Authorization: Bearer <jwt> (deprecated for SSE).
    """
    from credit_report.security.auth import decode_token
    from credit_report.security.models import User as _User

    user_id: Optional[str] = None

    if ticket:
        from credit_report.security.sse_ticket import consume_ticket
        user_id = consume_ticket(ticket)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or expired SSE ticket")
    else:
        bearer = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:].strip()
        if not bearer and token:
            bearer = token
            logger.warning("[SSE] ?token= query param is deprecated; use POST /auth/sse-ticket")
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
    ticket: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """SSE endpoint — stream ETL progress events for a given task_id (EventSource-compatible)."""
    user = await _sse_user(request, token=token, ticket=ticket, db=db)
    # IDOR guard: verify this task was issued for the report in the URL path.
    # Unknown task_id (not in map) falls through — the stream will close immediately
    # with an empty result, which is safe (task may have expired or been evicted).
    mapped_report_id = _ETL_TASK_REPORT.get(task_id)
    if mapped_report_id and mapped_report_id != report_id:
        raise HTTPException(status_code=404, detail="Task not found")
    # Also verify the caller can access this report.
    report = await _require_report(db, report_id)
    _assert_can_view(report, user)
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
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {CREDIT_REPORT_MAX_UPLOAD_MB} MB upload limit")
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Poll the status of a background generation task."""
    task = _generation_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or server was restarted")
    # Verify the task belongs to the report in the URL to prevent IDOR.
    if task.get("report_id") and task["report_id"] != report_id:
        raise HTTPException(status_code=404, detail="Task not found or server was restarted")
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
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
    if report.status == "approved":
        raise HTTPException(status_code=409, detail="Approved reports cannot be regenerated")

    # Fast preflight: dependency check (uses request session for immediate 409 response)
    missing = await check_hard_dependencies(db, report_id, section_no)
    if missing:
        logger.info("generate_section: blocked on hard deps=%s section=%d report=%s", missing, section_no, report_id)
        raise HTTPException(
            status_code=409,
            detail=f"Hard dependencies not yet generated: sections {missing}",
        )

    gen_key = (report_id, section_no)
    if gen_key in _generating_sections:
        raise HTTPException(
            status_code=409,
            detail=f"Section {section_no} is already being generated for this report",
        )
    _generating_sections.add(gen_key)

    task_id = str(uuid.uuid4())
    _generation_tasks.set(task_id, {"status": "running", "section_no": section_no, "report_id": report_id})
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
            finally:
                _generating_sections.discard(gen_key)

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
    _generation_tasks.set(task_id, {"status": "running", "report_id": report_id})
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


# ── Gap-fill helpers ──────────────────────────────────────────────────────────

def _path_get(obj: dict, path: str) -> Any:
    """Walk dot-notation path into nested dict; return None if missing."""
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _path_set(obj: dict, path: str, value: Any) -> None:
    """Write value at dot-notation path, creating intermediate dicts."""
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


_GAPFILL_YAML_BASE = _Path(__file__).parent.parent / "fact_store" / "fact_mapping_config"

_SAFE_INDUSTRY_RE = _re.compile(r'^[a-z_]{1,40}$')


def _load_section_yaml_paths(industry: str, section_no: int) -> list[str]:
    """Return all field `path` values from the YAML config for a section."""
    if not _SAFE_INDUSTRY_RE.match(industry):
        return []
    yaml_path = _GAPFILL_YAML_BASE / industry / f"section_{section_no}.yaml"
    if not yaml_path.exists():
        return []
    try:
        import yaml as _yaml
        raw = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        return [entry["path"] for entry in raw.get("facts", []) if "path" in entry]
    except Exception:
        return []


# ── Gap-fill endpoint ─────────────────────────────────────────────────────────

@router.post("/gap-fill", response_model=GapFillResponse)
async def gap_fill_report(
    report_id: str,
    payload: GapFillRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Server-proxied Gemini gap-fill.

    Fills empty section-input fields using Gemini's training-knowledge estimate
    for the named company.  All filled values are flagged as UNVERIFIED and must
    be verified against primary sources before the report is approved.
    No client-side Gemini API key is required.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not configured on the server.",
        )

    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)

    if report.status == "approved":
        raise HTTPException(status_code=409, detail="Approved reports cannot be modified")

    industry = (report.industry or "marine").lower()
    target_sections = payload.sections if payload.sections else list(range(1, 11))
    target_sections = [s for s in target_sections if 1 <= s <= 10]

    from credit_report.generation.claude_client import call_gemini_raw

    section_results: list[GapFillSectionResult] = []
    total_filled = 0

    for sec_no in target_sections:
        paths = _load_section_yaml_paths(industry, sec_no)
        if not paths:
            section_results.append(GapFillSectionResult(section_no=sec_no, filled_count=0, skipped_count=0))
            continue

        # Load current input
        current = await _load_section_input(db, report_id, sec_no)

        # Only ask Gemini to fill truly empty paths (None or missing)
        empty_paths = [p for p in paths if _path_get(current, p) is None]
        if not empty_paths:
            section_results.append(GapFillSectionResult(section_no=sec_no, filled_count=0, skipped_count=len(paths)))
            continue

        field_list = "\n".join(f"- {p}" for p in empty_paths[:20])
        system_prompt = (
            "You are a financial data assistant. Return ONLY valid JSON — no markdown fences, "
            "no explanations. Use null for fields you cannot estimate with reasonable confidence."
        )
        user_prompt = (
            f"Company: {payload.company_name}\n"
            f"Section {sec_no} of a bank credit report is missing values for these fields:\n"
            f"{field_list}\n\n"
            "Return a flat JSON object mapping each field path to its best estimated value. "
            "Flag uncertain values by appending _UNVERIFIED to string values. "
            "Use null for fields you have no basis to estimate."
        )

        try:
            raw_text = await call_gemini_raw(system_prompt, user_prompt, max_tokens=600)
        except Exception as exc:
            logger.warning("gap_fill_report: Gemini call failed section=%d: %s", sec_no, exc)
            section_results.append(GapFillSectionResult(section_no=sec_no, filled_count=0, skipped_count=len(empty_paths)))
            continue

        # Parse JSON from response (Gemini may include preamble text)
        gap_data: dict = {}
        m = _re.search(r'\{[\s\S]+\}', raw_text)
        if m:
            try:
                gap_data = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        filled_count = 0
        for path, val in gap_data.items():
            if val is None:
                continue
            # Only fill if path is one we asked about and the field is still empty
            if path in empty_paths and _path_get(current, path) is None:
                _path_set(current, path, val)
                filled_count += 1

        if filled_count > 0:
            # Persist updated section input
            result = await db.execute(
                select(SectionInput).where(
                    SectionInput.report_id == report_id,
                    SectionInput.section_no == sec_no,
                ).order_by(SectionInput.id.desc())
            )
            si = result.scalars().first()
            if si:
                si.input_json = json.dumps(current, ensure_ascii=False)
                si.saved_by = current_user.id
            else:
                db.add(SectionInput(
                    id=str(uuid.uuid4()),
                    report_id=report_id,
                    section_no=sec_no,
                    input_json=json.dumps(current, ensure_ascii=False),
                    saved_by=current_user.id,
                ))
            await db.flush()

        total_filled += filled_count
        section_results.append(GapFillSectionResult(
            section_no=sec_no,
            filled_count=filled_count,
            skipped_count=len(empty_paths) - filled_count,
        ))

    await write_event(
        db,
        action="section_input.gap_filled",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        report_id=report_id,
        target_type="report",
        target_id=report_id,
        reason=f"gap-fill company={payload.company_name!r} total_filled={total_filled}",
    )
    await db.commit()

    logger.info(
        "gap_fill_report: report=%s company=%r sections=%s total_filled=%d user=%s",
        report_id, payload.company_name, target_sections, total_filled, current_user.id,
    )

    return GapFillResponse(
        company_name=payload.company_name,
        total_filled=total_filled,
        sections=section_results,
    )


class AutoFetchRequest(BaseModel):
    sources: list[str] = ["mops", "edgar"]
    stock_code: Optional[str] = None
    company_name: Optional[str] = None
    direct_urls: list[str] = []


@router.post("/fetch-documents", status_code=202)
async def auto_fetch_documents(
    report_id: str,
    payload: AutoFetchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Auto-fetch documents from MOPS (Taiwan), SEC EDGAR, or direct URLs.

    Downloads matching PDFs/filings and registers them as SectionDocuments
    ready for ETL — equivalent to a manual upload for each file.

    Supported sources:
      - "mops"   → Taiwan annual reports via TWSE (requires stock_code)
      - "edgar"  → SEC 10-K / 20-F via EDGAR (requires company_name)
      - "direct" → any publicly reachable URL (requires direct_urls list)
    """
    from credit_report.api.doc_fetcher import run_auto_fetch
    from functools import partial as _partial

    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    if report.status == "approved":
        raise HTTPException(status_code=409, detail="Approved reports cannot be modified")

    valid_sources = {"mops", "edgar", "direct"}
    sources = [s for s in (payload.sources or []) if s in valid_sources]
    if not sources:
        raise HTTPException(status_code=422, detail="At least one valid source required: mops, edgar, direct")

    fetched_docs, fetch_errors = await run_auto_fetch(
        sources=sources,
        stock_code=payload.stock_code or None,
        company_name=payload.company_name or None,
        direct_urls=payload.direct_urls or [],
    )

    registered: list[dict] = []
    upload_errors: list[dict] = []

    for fdoc in fetched_docs:
        fname = fdoc.filename
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if ext not in ALLOWED_EXTENSIONS:
            upload_errors.append({"filename": fname, "error": f"Unsupported extension .{ext}"})
            continue
        if len(fdoc.data) > _MAX_UPLOAD_BYTES:
            upload_errors.append({"filename": fname, "error": "File exceeds 50 MB upload limit"})
            continue
        if not _check_magic(fdoc.data, ext):
            upload_errors.append({"filename": fname, "error": "Magic-byte mismatch — file rejected"})
            continue

        doc_id = str(uuid.uuid4())
        # Save binary first — document is always registered regardless of extraction outcome.
        # ETL re-extracts from .bin when .txt is missing, so a failed extraction here is recoverable.
        save_document_binary(report_id, doc_id, fdoc.data, fname)

        loop = asyncio.get_running_loop()
        text_chars = 0
        detected_fmt = ext
        try:
            text, detected_fmt = await asyncio.wait_for(
                loop.run_in_executor(None, _partial(extract_text_from_file, fdoc.data, fname)),
                timeout=180.0,
            )
            save_document_text(report_id, doc_id, text)
            text_chars = len(text.strip())
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning(
                "auto_fetch: extraction failed for %r — binary saved, ETL will re-extract: %s",
                fname, exc,
            )
            upload_errors.append({"filename": fname, "error": f"Text extraction deferred (ETL will retry): {exc}"})

        doc = SectionDocument(
            id=doc_id,
            report_id=report_id,
            original_filename=fname,
            file_size_bytes=len(fdoc.data),
            document_type=fdoc.document_type,
            file_format=detected_fmt,
            etl_status="pending",
            uploaded_by=current_user.id,
        )
        db.add(doc)
        await db.flush()
        logger.info(
            "auto_fetch: registered doc=%s source=%s file=%r bytes=%d report=%s",
            doc_id, fdoc.source, fname, len(fdoc.data), report_id,
        )
        registered.append({
            "id": doc_id,
            "filename": fname,
            "source": fdoc.source,
            "document_type": fdoc.document_type,
            "file_size_bytes": len(fdoc.data),
            "text_chars": text_chars,
        })

    all_errors = [{"source": e.source, "message": e.message} for e in fetch_errors]
    all_errors.extend(upload_errors)

    return {
        "fetched": len(registered),
        "documents": registered,
        "errors": all_errors,
    }


# ── URL Import background job ─────────────────────────────────────────────────

class UrlImportRequest(BaseModel):
    url: str
    filename: Optional[str] = None  # override inferred filename


async def _url_import_bg(task_id: str, report_id: str, url: str, filename: Optional[str], user_id: str) -> None:
    """Background task: download URL → save binary → extract text → register document."""
    from credit_report.api.doc_fetcher import FetchError, check_ssrf_safe, fetch_direct_url
    from credit_report.database import AsyncSessionLocal
    from functools import partial as _partial

    _url_import_tasks.update(task_id, {"status": "FETCHING_URL"})
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(follow_redirects=True, max_redirects=5) as client:
            fdoc, err = await fetch_direct_url(client, url, filename=filename)
    except Exception as exc:
        logger.warning("url_import_bg: download error task=%s: %s", task_id, exc)
        _url_import_tasks.update(task_id, {"status": "FETCH_FAILED", "error": str(exc)[:300]})
        return

    if err or not fdoc:
        msg = err.message if err else "No data returned"
        _url_import_tasks.update(task_id, {"status": "FETCH_FAILED", "error": msg})
        return

    fname = fdoc.filename
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in ALLOWED_EXTENSIONS:
        _url_import_tasks.update(task_id, {"status": "FETCH_FAILED", "error": f"Unsupported file type .{ext}"})
        return
    if len(fdoc.data) > _MAX_UPLOAD_BYTES:
        _url_import_tasks.update(task_id, {"status": "FETCH_FAILED", "error": "File exceeds 50 MB limit"})
        return
    if not _check_magic(fdoc.data, ext):
        _url_import_tasks.update(task_id, {"status": "FETCH_FAILED", "error": "File content does not match its extension"})
        return

    doc_id = str(uuid.uuid4())
    _url_import_tasks.update(task_id, {"status": "BINARY_SAVED", "document_id": doc_id, "filename": fname})

    save_document_binary(report_id, doc_id, fdoc.data, fname)

    async with AsyncSessionLocal() as db:
        doc = SectionDocument(
            id=doc_id,
            report_id=report_id,
            original_filename=fname,
            file_size_bytes=len(fdoc.data),
            document_type=fdoc.document_type,
            file_format=ext,
            etl_status="pending",
            uploaded_by=user_id,
        )
        db.add(doc)
        await db.commit()

    _url_import_tasks.update(task_id, {"status": "TEXT_EXTRACTING"})
    loop = asyncio.get_running_loop()
    try:
        text, _ = await asyncio.wait_for(
            loop.run_in_executor(None, _partial(extract_text_from_file, fdoc.data, fname)),
            timeout=300.0,
        )
        del fdoc
        save_document_text(report_id, doc_id, text)
        _url_import_tasks.update(task_id, {"status": "READY", "text_chars": len(text.strip())})
        logger.info("url_import_bg: done task=%s doc=%s chars=%d", task_id, doc_id, len(text.strip()))
    except Exception as exc:
        del fdoc
        logger.warning("url_import_bg: extraction failed task=%s doc=%s: %s — binary saved", task_id, doc_id, exc)
        _url_import_tasks.update(task_id, {
            "status": "EXTRACT_FAILED",
            "error": f"Text extraction failed (binary saved — run ETL to retry): {str(exc)[:200]}",
        })


@router.post("/url-imports", status_code=202)
async def create_url_import(
    report_id: str,
    payload: UrlImportRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Submit a URL for background download and document registration.

    Returns immediately with a task_id. Poll GET /url-imports/{task_id} for status.
    Status progression: QUEUED → FETCHING_URL → BINARY_SAVED → TEXT_EXTRACTING → READY
    On failure: FETCH_FAILED | EXTRACT_FAILED
    """
    from credit_report.api.doc_fetcher import check_ssrf_safe

    if not payload.url.strip():
        raise HTTPException(status_code=422, detail="url is required")
    ssrf_err = check_ssrf_safe(payload.url)
    if ssrf_err:
        raise HTTPException(status_code=422, detail=f"URL blocked for security: {ssrf_err}")

    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)
    if report.status == "approved":
        raise HTTPException(status_code=409, detail="Approved reports cannot be modified")

    task_id = str(uuid.uuid4())
    _url_import_tasks.set(task_id, {
        "task_id": task_id,
        "report_id": report_id,
        "url": payload.url,
        "status": "QUEUED",
        "document_id": None,
        "filename": None,
        "text_chars": None,
        "error": None,
    })
    background_tasks.add_task(
        _url_import_bg, task_id, report_id, payload.url, payload.filename, current_user.id
    )
    return {"task_id": task_id, "status": "QUEUED", "document_id": None}


@router.get("/url-imports/{task_id}")
async def get_url_import_status(
    report_id: str,
    task_id: str,
    current_user: User = Depends(require_analyst),
):
    """Poll the status of a URL import background job."""
    task = _url_import_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or expired (TTL=1h)")
    if task.get("report_id") != report_id:
        raise HTTPException(status_code=403, detail="Task does not belong to this report")
    return task


# ── TWSE import ───────────────────────────────────────────────────────────────

class TWSEImportRequest(BaseModel):
    stock_code: str
    apply_mode: str = "only_empty"       # "only_empty" | "overwrite"
    sections: list[int] = [4, 7]         # §1, §3, §4, §5, §7, §9 supported


class TWSEImportResult(BaseModel):
    stock_code: str
    company_name: str
    sections_updated: list[int]
    fields_written: int
    fields_skipped: int
    not_found: bool = False
    p1_available: Optional[bool] = None  # True=financial stmts fetched; False=blocked/403; None=not checked


@router.post("/import-twse", response_model=TWSEImportResult)
async def import_twse_data(
    report_id: str,
    payload: TWSEImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Fetch company data from TWSE OpenAPI and merge into SectionInputs for §1/§3/§4/§5/§7/§9."""
    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone

    from credit_report.api.twse_importer import (
        SUPPORTED_SECTIONS,
        apply_field_mapping,
        fetch_twse_company,
        map_to_section,
    )

    if payload.apply_mode not in ("only_empty", "overwrite"):
        raise HTTPException(status_code=422, detail="apply_mode must be 'only_empty' or 'overwrite'")
    if not payload.stock_code.strip():
        raise HTTPException(status_code=422, detail="stock_code is required")
    invalid_secs = [s for s in payload.sections if s not in SUPPORTED_SECTIONS]
    if invalid_secs:
        raise HTTPException(
            status_code=422,
            detail=f"Sections {invalid_secs} have no TWSE data. "
                   f"Supported: {sorted(SUPPORTED_SECTIONS)}",
        )

    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    _assert_owner_or_admin(report, current_user)
    if report.status == "approved":
        raise HTTPException(status_code=409, detail="Approved reports are immutable")

    twse_data = await fetch_twse_company(payload.stock_code.strip())
    if twse_data is None:
        return TWSEImportResult(
            stock_code=payload.stock_code,
            company_name="",
            sections_updated=[],
            fields_written=0,
            fields_skipped=0,
            not_found=True,
        )

    # Determine if P1 financial statement data was actually returned
    _p1_available: Optional[bool] = (
        bool(twse_data.income_statements or twse_data.balance_sheets or twse_data.cash_flows)
        if twse_data is not None else None
    )

    section_maps: dict[int, dict] = {
        sec_no: map_to_section(sec_no, twse_data)
        for sec_no in payload.sections
    }

    total_written = 0
    total_skipped = 0
    updated_sections: list[int] = []

    for sec_no, field_map in section_maps.items():
        if not field_map:
            continue

        si_result = await db.execute(
            select(SectionInput).where(
                SectionInput.report_id == report_id,
                SectionInput.section_no == sec_no,
            ).order_by(SectionInput.id.desc())
        )
        si = si_result.scalars().first()
        existing_json: dict = {}
        if si and si.input_json:
            try:
                existing_json = _json.loads(si.input_json)
            except (ValueError, TypeError):
                existing_json = {}

        merged, written, skipped = apply_field_mapping(existing_json, field_map, payload.apply_mode)
        total_written += written
        total_skipped += skipped

        if written == 0:
            continue

        updated_sections.append(sec_no)
        merged_str = _json.dumps(merged, ensure_ascii=False)
        if si:
            si.input_json = merged_str
            si.saved_by = current_user.id
            si.saved_at = datetime.now(timezone.utc)
        else:
            si = SectionInput(
                id=str(_uuid.uuid4()),
                report_id=report_id,
                section_no=sec_no,
                input_json=merged_str,
                saved_by=current_user.id,
            )
            db.add(si)

    await db.flush()

    company_name = twse_data.company_name_zh or twse_data.company_name_en or payload.stock_code
    logger.info(
        "twse_import: stock=%r company=%r sections=%r written=%d skipped=%d report=%s",
        payload.stock_code, company_name, updated_sections, total_written, total_skipped, report_id,
    )
    return TWSEImportResult(
        stock_code=payload.stock_code,
        company_name=company_name,
        sections_updated=updated_sections,
        fields_written=total_written,
        fields_skipped=total_skipped,
        p1_available=_p1_available,
    )


# ── TWSE runtime diagnostics ──────────────────────────────────────────────────

@diagnostics_router.get("/twse")
async def get_twse_diagnostics(
    current_user: User = Depends(require_analyst),
):
    """
    Probe all TWSE OpenAPI endpoints and return per-endpoint runtime status.
    P0 endpoints should always succeed; P1 returns 403 from non-Taiwan IPs.
    Use this to determine why financial statement fields are empty after TWSE import.
    """
    from credit_report.api.twse_importer import EndpointProbeResult, probe_twse_endpoints

    results: list[EndpointProbeResult] = await probe_twse_endpoints()

    p0_ok    = all(r.usable for r in results if r.tier == "P0")
    is_ok    = any(r.usable for r in results if r.name == "t163sb03_1")
    bs_ok    = any(r.usable for r in results if r.name == "t163sb04_1")
    cf_ok    = any(r.usable for r in results if r.name == "t163sb05_1")
    div_ok   = any(r.usable for r in results if r.name == "t187ap14_L")
    p1_ok    = is_ok and bs_ok and cf_ok

    if p0_ok and p1_ok:
        diagnosis = "All endpoints available — TWSE import should populate §1/§3/§4/§5/§7/§9 including financial statements."
    elif p0_ok and not p1_ok:
        blocked = [r.name for r in results if r.tier == "P1" and not r.usable]
        diagnosis = (
            f"P0 endpoints OK (company profile, news, revenue). "
            f"P1 financial statement endpoints blocked: {blocked}. "
            f"Financial data (§7A IS/BS/CF, §7B ratios) cannot be imported from this network. "
            f"Deploy to Taiwan-based server or use production Render to access P1."
        )
    else:
        failed = [r.name for r in results if r.tier == "P0" and not r.usable]
        diagnosis = f"P0 endpoints failing: {failed}. Check network connectivity to openapi.twse.com.tw."

    return {
        "probed_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "summary": {
            "company_profile_available": p0_ok,
            "income_statement_available": is_ok,
            "balance_sheet_available": bs_ok,
            "cash_flow_available": cf_ok,
            "dividend_available": div_ok,
            "p1_financial_stmts_available": p1_ok,
            "diagnosis": diagnosis,
        },
        "endpoints": [
            {
                "name": r.name,
                "url": r.url,
                "tier": r.tier,
                "desc": r.desc,
                "status_code": r.status_code,
                "latency_ms": r.latency_ms,
                "row_count": r.row_count,
                "sample_keys": r.sample_keys,
                "error_type": r.error_type,
                "usable": r.usable,
            }
            for r in results
        ],
    }


@router.get("/generate/stream/{task_id}")
async def generation_stream_events(
    request: Request,
    report_id: str,
    task_id: str,
    token: Optional[str] = Query(default=None),
    ticket: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """SSE endpoint — stream section generation progress (EventSource-compatible)."""
    await _sse_user(request, token=token, ticket=ticket, db=db)
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

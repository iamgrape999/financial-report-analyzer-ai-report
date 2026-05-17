from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from credit_report.config import CREDIT_REPORTS_ROOT, CR_MAX_CHUNKS_PER_SECTION, SECTION_RETRIEVAL_KEYWORDS

logger = logging.getLogger(__name__)

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks of approximately CHUNK_SIZE characters."""
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunk = text[i : i + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _score_chunk(chunk: str, keywords: list[str]) -> int:
    """Count how many keywords appear in the chunk (case-insensitive)."""
    lower = chunk.lower()
    return sum(1 for kw in keywords if kw.lower() in lower)


def save_document_text(report_id: str, doc_id: str, text: str) -> None:
    """Persist extracted document text to CREDIT_REPORTS_ROOT/{report_id}/{doc_id}.txt."""
    doc_dir = CREDIT_REPORTS_ROOT / report_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / f"{doc_id}.txt").write_text(text, encoding="utf-8")


def save_document_binary(report_id: str, doc_id: str, file_bytes: bytes, filename: str) -> None:
    """Persist the original uploaded file bytes so ETL can be re-run without re-uploading."""
    doc_dir = CREDIT_REPORTS_ROOT / report_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / f"{doc_id}.bin").write_bytes(file_bytes)
    (doc_dir / f"{doc_id}.fname").write_text(filename, encoding="utf-8")


def load_document_texts(report_id: str) -> list[str]:
    """Load all extracted document texts for a report from the filesystem."""
    doc_dir = CREDIT_REPORTS_ROOT / report_id
    if not doc_dir.exists():
        return []
    texts: list[str] = []
    for txt_file in sorted(doc_dir.glob("*.txt")):
        try:
            texts.append(txt_file.read_text(encoding="utf-8"))
        except Exception as _e:
            logger.warning("load_document_texts: failed to read %s report=%s: %s", txt_file.name, report_id, _e)
    return texts


def retrieve_evidence(
    report_id: str,
    section_no: int,
    max_chunks: int = CR_MAX_CHUNKS_PER_SECTION,
) -> list[str]:
    """
    Return the most keyword-relevant text chunks for a section.

    Chunks are scored by how many of the section's retrieval keywords they contain,
    then the top-scoring chunks (up to max_chunks) are returned.
    """
    keywords = SECTION_RETRIEVAL_KEYWORDS.get(section_no, [])
    if not keywords:
        return []

    all_texts = load_document_texts(report_id)
    if not all_texts:
        return []

    all_chunks: list[str] = []
    for text in all_texts:
        all_chunks.extend(_chunk_text(text))

    scored = [(chunk, _score_chunk(chunk, keywords)) for chunk in all_chunks]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [chunk for chunk, score in scored[:max_chunks] if score > 0]


# ── Text quality check ────────────────────────────────────────────────────────

def _text_quality_ok(text: str, min_meaningful_ratio: float = 0.05, min_chars: int = 80) -> bool:
    """Return True if text contains enough real content (letters, digits, CJK characters).

    Chinese brokerage PDFs often use CID fonts that pdfminer extracts as mostly whitespace
    or unmapped glyphs even though the byte count is large. This check prevents passing
    that garbage to ETL and ensures Vision OCR is used as fallback.
    """
    stripped = text.strip()
    if not stripped or len(stripped) < min_chars:
        return False
    meaningful = sum(
        1 for c in stripped
        if ('一' <= c <= '鿿')          # CJK Unified Ideographs
        or ('㐀' <= c <= '䶿')          # CJK Extension A
        or ('豈' <= c <= '﫿')          # CJK Compatibility Ideographs
        or c.isascii() and (c.isalpha() or c.isdigit())
    )
    ratio = meaningful / len(stripped)
    return ratio >= min_meaningful_ratio


def _quality_stats(text: str) -> dict:
    """Return quality metrics dict for logging."""
    stripped = text.strip()
    if not stripped:
        return {"chars": 0, "meaningful": 0, "ratio_pct": 0.0, "sample": ""}
    meaningful = sum(
        1 for c in stripped
        if ('一' <= c <= '鿿') or ('㐀' <= c <= '䶿') or ('豈' <= c <= '﫿')
        or c.isascii() and (c.isalpha() or c.isdigit())
    )
    return {
        "chars": len(stripped),
        "meaningful": meaningful,
        "ratio_pct": round(meaningful / len(stripped) * 100, 1),
        "sample": stripped[:120].replace("\n", " "),
    }


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract plain text from PDF bytes. Tries pdfminer first, then pypdf.

    Returns empty string if extraction fails or text quality is too low
    (e.g. CID-font Chinese PDFs where characters are unmapped).
    """
    pdf_kb = len(pdf_bytes) // 1024
    logger.info("[OCR] extract_text_from_pdf: start bytes=%dKB", pdf_kb)

    # pdfminer — best for native-text PDFs
    t0 = time.perf_counter()
    try:
        import io
        from pdfminer.high_level import extract_text

        result = extract_text(io.BytesIO(pdf_bytes))
        elapsed = (time.perf_counter() - t0) * 1000
        stats = _quality_stats(result or "")
        logger.info(
            "[OCR] pdfminer: elapsed=%.0fms chars=%d meaningful=%d ratio=%.1f%% sample=%r",
            elapsed, stats["chars"], stats["meaningful"], stats["ratio_pct"], stats["sample"],
        )
        if result and _text_quality_ok(result):
            logger.info("[OCR] pdfminer: quality OK → using this text")
            return result
        if result and result.strip():
            logger.warning(
                "[OCR] pdfminer: LOW QUALITY text (ratio=%.1f%% < 5%% or chars<%d) "
                "— likely CID-font encoding issue, falling back to pypdf",
                stats["ratio_pct"], 80,
            )
        else:
            logger.info("[OCR] pdfminer: returned empty text")
    except Exception as e:
        logger.warning("[OCR] pdfminer: exception: %s", e)

    # pypdf fallback
    t0 = time.perf_counter()
    try:
        import io
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        pages = [page.extract_text() or "" for page in reader.pages]
        non_empty_pages = sum(1 for p in pages if p.strip())
        result = "\n\n".join(p for p in pages if p.strip())
        elapsed = (time.perf_counter() - t0) * 1000
        stats = _quality_stats(result)
        logger.info(
            "[OCR] pypdf: elapsed=%.0fms pages=%d non_empty_pages=%d chars=%d "
            "meaningful=%d ratio=%.1f%% sample=%r",
            elapsed, page_count, non_empty_pages,
            stats["chars"], stats["meaningful"], stats["ratio_pct"], stats["sample"],
        )
        if result and _text_quality_ok(result):
            logger.info("[OCR] pypdf: quality OK → using this text")
            return result
        if result and result.strip():
            logger.warning(
                "[OCR] pypdf: LOW QUALITY text (ratio=%.1f%%) — will try Vision OCR",
                stats["ratio_pct"],
            )
        else:
            logger.info("[OCR] pypdf: returned empty text")
    except Exception as e:
        logger.warning("[OCR] pypdf: exception: %s", e)

    logger.warning(
        "[OCR] extract_text_from_pdf: all parsers failed or produced low-quality text "
        "— returning empty so caller triggers Vision OCR"
    )
    return ""


# ── Office format extraction ──────────────────────────────────────────────────

def extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract plain text from DOCX bytes using python-docx."""
    logger.info("[OCR] extract_text_from_docx: start bytes=%dKB", len(file_bytes) // 1024)
    try:
        import io
        from docx import Document

        t0 = time.perf_counter()
        doc = Document(io.BytesIO(file_bytes))
        parts: list[str] = []
        para_count = 0
        for para in doc.paragraphs:
            if para.text.strip():
                parts.append(para.text.strip())
                para_count += 1
        table_rows = 0
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    parts.append(row_text)
                    table_rows += 1
        result = "\n".join(parts)
        elapsed = (time.perf_counter() - t0) * 1000
        stats = _quality_stats(result)
        logger.info(
            "[OCR] extract_text_from_docx: elapsed=%.0fms paragraphs=%d table_rows=%d "
            "chars=%d ratio=%.1f%% sample=%r",
            elapsed, para_count, table_rows, stats["chars"], stats["ratio_pct"], stats["sample"],
        )
        return result
    except Exception as e:
        logger.warning("[OCR] extract_text_from_docx: failed: %s", e)
        return ""


def extract_text_from_pptx(file_bytes: bytes) -> str:
    """Extract plain text from PPTX bytes using python-pptx."""
    logger.info("[OCR] extract_text_from_pptx: start bytes=%dKB", len(file_bytes) // 1024)
    try:
        import io
        from pptx import Presentation

        t0 = time.perf_counter()
        prs = Presentation(io.BytesIO(file_bytes))
        parts: list[str] = []
        slide_count = len(prs.slides)
        shape_count = 0
        for slide_no, slide in enumerate(prs.slides, 1):
            slide_parts = [f"[Slide {slide_no}]"]
            for shape in slide.shapes:
                shape_count += 1
                if hasattr(shape, "text") and shape.text.strip():
                    slide_parts.append(shape.text.strip())
                if shape.has_table:
                    for row in shape.table.rows:
                        row_text = " | ".join(
                            cell.text.strip() for cell in row.cells if cell.text.strip()
                        )
                        if row_text:
                            slide_parts.append(row_text)
            if len(slide_parts) > 1:
                parts.append("\n".join(slide_parts))
        result = "\n\n".join(parts)
        elapsed = (time.perf_counter() - t0) * 1000
        stats = _quality_stats(result)
        logger.info(
            "[OCR] extract_text_from_pptx: elapsed=%.0fms slides=%d shapes=%d "
            "non_empty_slides=%d chars=%d ratio=%.1f%% sample=%r",
            elapsed, slide_count, shape_count, len(parts),
            stats["chars"], stats["ratio_pct"], stats["sample"],
        )
        return result
    except Exception as e:
        logger.warning("[OCR] extract_text_from_pptx: failed: %s", e)
        return ""


# ── Vision OCR ────────────────────────────────────────────────────────────────

_OCR_PROMPT = (
    "Extract ALL text, numbers, tables, and structured data from this document. "
    "The document may be in Traditional Chinese (繁體中文), Simplified Chinese (简体中文), "
    "or English — extract all text faithfully in its original language. "
    "For tables, preserve the structure using | separators between columns and "
    "new lines between rows. Include all financial figures, dates, company names, "
    "key metrics, percentages, ratios, and headers exactly as shown. "
    "Preserve original currency symbols and units (NTD, TWD, USD, HKD, etc.). "
    "Output as plain text maintaining the original layout and page structure."
)


def extract_text_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Use Gemini Vision to OCR and extract text+tables from an image."""
    logger.info("[OCR] extract_text_from_image: start bytes=%dKB mime=%s", len(image_bytes) // 1024, mime_type)
    try:
        from google import genai
        from google.genai import types as genai_types
        from credit_report.config import GEMINI_API_KEY, GEMINI_OCR_MODEL

        if not GEMINI_API_KEY:
            logger.warning("[OCR] extract_text_from_image: GEMINI_API_KEY not set — cannot OCR")
            return ""

        t0 = time.perf_counter()
        client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("[OCR] extract_text_from_image: calling Gemini model=%s max_tokens=32768", GEMINI_OCR_MODEL)
        response = client.models.generate_content(
            model=GEMINI_OCR_MODEL,
            contents=[
                genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                genai_types.Part.from_text(_OCR_PROMPT),
            ],
            config=genai_types.GenerateContentConfig(max_output_tokens=32768),
        )
        elapsed = (time.perf_counter() - t0) * 1000
        result = response.text or ""
        finish_reason = getattr(getattr(response, "candidates", [None])[0], "finish_reason", "unknown") if response.candidates else "no_candidates"
        stats = _quality_stats(result)
        logger.info(
            "[OCR] extract_text_from_image: elapsed=%.0fms chars=%d meaningful=%d "
            "ratio=%.1f%% finish_reason=%s sample=%r",
            elapsed, stats["chars"], stats["meaningful"],
            stats["ratio_pct"], finish_reason, stats["sample"],
        )
        if not result:
            logger.warning("[OCR] extract_text_from_image: Gemini returned empty response")
        return result
    except Exception as e:
        logger.warning("[OCR] extract_text_from_image: Gemini Vision failed: %s", e)
        return ""


def _split_pdf_pages(pdf_bytes: bytes) -> list[bytes]:
    """Return a list of single-page PDF byte blobs using pypdf."""
    try:
        import io
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages: list[bytes] = []
        for page in reader.pages:
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            pages.append(buf.getvalue())
        return pages
    except Exception as e:
        logger.warning("[OCR] _split_pdf_pages failed: %s — will send full PDF", e)
        return []


# Gemini inline PDF upload limit per request
_GEMINI_PDF_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB
# Page count threshold: PDFs larger than this go through page-by-page VLM
_PDF_PAGE_THRESHOLD = 30


def extract_text_from_scanned_pdf_vision(pdf_bytes: bytes, max_pages: int = 60) -> str:
    """
    For scanned/image PDFs where text extraction yields nothing:
    send the PDF to Gemini Vision for OCR.

    Strategy:
    - Small PDFs (≤ 20 MB): send whole file in one call.
    - Large PDFs (> 20 MB) or those with many pages: split into single-page chunks,
      OCR each page separately, concatenate.  This prevents silent truncation and
      ensures every page is processed.
    """
    pdf_kb = len(pdf_bytes) // 1024
    logger.info("[OCR] extract_text_from_scanned_pdf_vision: start pdf_kb=%d", pdf_kb)
    try:
        from google import genai
        from google.genai import types as genai_types
        from credit_report.config import GEMINI_API_KEY, GEMINI_OCR_MODEL

        if not GEMINI_API_KEY:
            logger.warning("[OCR] extract_text_from_scanned_pdf_vision: GEMINI_API_KEY not set")
            return ""

        client = genai.Client(api_key=GEMINI_API_KEY)
        t0 = time.perf_counter()

        # Decide strategy: whole-PDF vs page-by-page
        use_page_split = len(pdf_bytes) > _GEMINI_PDF_SIZE_LIMIT
        if not use_page_split:
            # Try to count pages; switch to page-split if > _PDF_PAGE_THRESHOLD
            try:
                import io
                from pypdf import PdfReader
                n_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
                use_page_split = n_pages > _PDF_PAGE_THRESHOLD
                logger.info("[OCR] PDF pages=%d use_page_split=%s", n_pages, use_page_split)
            except Exception:
                pass

        if not use_page_split:
            # Single-call path (fast for short PDFs)
            logger.info("[OCR] calling Gemini Vision PDF OCR (single-call) model=%s", GEMINI_OCR_MODEL)
            response = client.models.generate_content(
                model=GEMINI_OCR_MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    genai_types.Part.from_text(_OCR_PROMPT),
                ],
                config=genai_types.GenerateContentConfig(max_output_tokens=32768),
            )
            result = response.text or ""
            elapsed = (time.perf_counter() - t0) * 1000
            stats = _quality_stats(result)
            finish_reason = (
                str(response.candidates[0].finish_reason) if response.candidates else "no_candidates"
            )
            logger.info(
                "[OCR] vision_ocr single-call: elapsed=%.0fms chars=%d "
                "meaningful=%d ratio=%.1f%% finish_reason=%s sample=%r",
                elapsed, stats["chars"], stats["meaningful"],
                stats["ratio_pct"], finish_reason, stats["sample"][:200],
            )
            if stats["ratio_pct"] < 5:
                logger.warning(
                    "[OCR] vision_ocr: low quality ratio=%.1f%% — escalating to page-by-page",
                    stats["ratio_pct"],
                )
                use_page_split = True  # fall through to page split below
            else:
                return result

        # Page-by-page path: split into single-page PDFs, OCR each, concatenate
        page_blobs = _split_pdf_pages(pdf_bytes)
        if not page_blobs:
            # pypdf split failed — fall back to capped single-call
            data = pdf_bytes[:_GEMINI_PDF_SIZE_LIMIT]
            logger.info("[OCR] page split failed — fallback single-call sending_kb=%d", len(data) // 1024)
            response = client.models.generate_content(
                model=GEMINI_OCR_MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=data, mime_type="application/pdf"),
                    genai_types.Part.from_text(_OCR_PROMPT),
                ],
                config=genai_types.GenerateContentConfig(max_output_tokens=32768),
            )
            return response.text or ""

        n_pages = len(page_blobs)
        capped_pages = page_blobs[:max_pages]
        logger.info(
            "[OCR] page-by-page OCR: total_pages=%d processing=%d model=%s",
            n_pages, len(capped_pages), GEMINI_OCR_MODEL,
        )
        page_texts: list[str] = []
        for page_idx, page_blob in enumerate(capped_pages):
            try:
                page_response = client.models.generate_content(
                    model=GEMINI_OCR_MODEL,
                    contents=[
                        genai_types.Part.from_bytes(data=page_blob, mime_type="application/pdf"),
                        genai_types.Part.from_text(_OCR_PROMPT),
                    ],
                    config=genai_types.GenerateContentConfig(max_output_tokens=8192),
                )
                page_text = page_response.text or ""
                page_texts.append(page_text)
                if page_idx % 10 == 0:
                    logger.info("[OCR] page-by-page: processed %d/%d pages", page_idx + 1, len(capped_pages))
            except Exception as page_err:
                logger.warning("[OCR] page-by-page: page %d failed: %s", page_idx + 1, page_err)
                page_texts.append("")  # preserve page numbering

        result = "\n\n".join(t for t in page_texts if t.strip())
        elapsed = (time.perf_counter() - t0) * 1000
        stats = _quality_stats(result)
        logger.info(
            "[OCR] page-by-page OCR complete: total_pages=%d elapsed=%.0fms chars=%d "
            "meaningful=%d ratio=%.1f%% sample=%r",
            len(capped_pages), elapsed, stats["chars"], stats["meaningful"],
            stats["ratio_pct"], stats["sample"][:200],
        )
        if n_pages > max_pages:
            logger.warning(
                "[OCR] page-by-page: PDF has %d pages but only first %d were processed",
                n_pages, max_pages,
            )
        return result

    except Exception as e:
        logger.warning("[OCR] extract_text_from_scanned_pdf_vision: Gemini Vision failed: %s", e)
        return ""


# ── xlsx / xls ────────────────────────────────────────────────────────────────

def _extract_text_from_xlsx(file_bytes: bytes) -> str:
    """Convert Excel workbook sheets to Markdown tables for ETL processing."""
    try:
        import openpyxl
        from io import BytesIO

        wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
        parts: list[str] = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            parts.append(f"\n## Sheet: {sheet_name}\n")
            headers = [str(c) if c is not None else "" for c in rows[0]]
            n_cols = len(headers)
            parts.append("| " + " | ".join(headers) + " |")
            parts.append("| " + " | ".join(["---"] * n_cols) + " |")
            for row in rows[1:51]:   # cap at 50 data rows per sheet
                raw = [str(c) if c is not None else "" for c in row]
                # Pad/truncate to match header width so the markdown table stays valid.
                padded = (raw + [""] * n_cols)[:n_cols]
                parts.append("| " + " | ".join(padded) + " |")
        wb.close()
        return "\n".join(parts)
    except Exception as e:
        logger.warning("[OCR] xlsx extraction failed: %s", e)
        return ""


# ── Entry point ───────────────────────────────────────────────────────────────

def extract_text_from_file(file_bytes: bytes, filename: str) -> tuple[str, str]:
    """
    Detect file format and extract text accordingly.

    Returns (extracted_text, detected_format).
    For scanned PDFs where text extraction fails, attempts Gemini Vision OCR.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_kb = len(file_bytes) // 1024
    logger.info("[OCR] extract_text_from_file: file=%r ext=%s size=%dKB", filename, ext, file_kb)
    t_start = time.perf_counter()

    if ext == "pdf":
        text = extract_text_from_pdf(file_bytes)
        if not text.strip():
            logger.info(
                "[OCR] PDF text extraction returned nothing (empty or low-quality) "
                "— escalating to Gemini Vision OCR: %r", filename,
            )
            text = extract_text_from_scanned_pdf_vision(file_bytes)
            if not text.strip():
                logger.warning("[OCR] Vision OCR also returned empty for %r", filename)
        fmt = "pdf"
    elif ext in ("docx",):
        text = extract_text_from_docx(file_bytes)
        fmt = "docx"
    elif ext in ("doc",):
        text = extract_text_from_docx(file_bytes)
        fmt = "doc"
    elif ext in ("pptx",):
        text = extract_text_from_pptx(file_bytes)
        fmt = "pptx"
    elif ext in ("ppt",):
        text = extract_text_from_pptx(file_bytes)
        fmt = "ppt"
    elif ext in ("txt", "csv", "md"):
        text = file_bytes.decode("utf-8", errors="replace")
        fmt = ext
    elif ext in ("jpg", "jpeg"):
        text = extract_text_from_image(file_bytes, "image/jpeg")
        fmt = "jpg"
    elif ext in ("png",):
        text = extract_text_from_image(file_bytes, "image/png")
        fmt = "png"
    elif ext in ("gif",):
        text = extract_text_from_image(file_bytes, "image/gif")
        fmt = "gif"
    elif ext in ("webp",):
        text = extract_text_from_image(file_bytes, "image/webp")
        fmt = "webp"
    elif ext in ("bmp",):
        text = extract_text_from_image(file_bytes, "image/bmp")
        fmt = "bmp"
    elif ext in ("tiff", "tif"):
        text = extract_text_from_image(file_bytes, "image/tiff")
        fmt = "tiff"
    elif ext == "xlsx":
        text = _extract_text_from_xlsx(file_bytes)
        fmt = "xlsx"
    elif ext == "xls":
        # openpyxl only supports .xlsx (Office Open XML); legacy .xls is unsupported.
        # Return a diagnostic string so ETL knows the file was received but unreadable.
        logger.warning("[OCR] .xls file uploaded (%r) — openpyxl only reads .xlsx; extraction skipped", filename)
        text = "[XLS_UNSUPPORTED: re-save as .xlsx to enable extraction]"
        fmt = "xls"
    else:
        logger.info("[OCR] unknown extension %r — trying pdf then docx", ext)
        text = extract_text_from_pdf(file_bytes)
        if not text:
            text = extract_text_from_docx(file_bytes)
        fmt = ext or "unknown"

    total_ms = (time.perf_counter() - t_start) * 1000
    stats = _quality_stats(text)
    logger.info(
        "[OCR] extract_text_from_file: DONE file=%r fmt=%s total_elapsed=%.0fms "
        "final_chars=%d meaningful=%d ratio=%.1f%%",
        filename, fmt, total_ms,
        stats["chars"], stats["meaningful"], stats["ratio_pct"],
    )
    if not text.strip():
        logger.warning(
            "[OCR] extract_text_from_file: FINAL TEXT IS EMPTY for %r — "
            "ETL will receive no text and return no data", filename,
        )
    return text, fmt

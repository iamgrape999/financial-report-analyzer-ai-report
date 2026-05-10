from __future__ import annotations

import logging
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


def load_document_texts(report_id: str) -> list[str]:
    """Load all extracted document texts for a report from the filesystem."""
    doc_dir = CREDIT_REPORTS_ROOT / report_id
    if not doc_dir.exists():
        return []
    texts: list[str] = []
    for txt_file in sorted(doc_dir.glob("*.txt")):
        try:
            texts.append(txt_file.read_text(encoding="utf-8"))
        except Exception:
            pass
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


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract plain text from PDF bytes. Tries pdfminer first, then pypdf."""
    # pdfminer — best for native-text PDFs
    try:
        import io
        from pdfminer.high_level import extract_text

        result = extract_text(io.BytesIO(pdf_bytes))
        if result and result.strip():
            logger.debug("extract_text_from_pdf: pdfminer succeeded, chars=%d", len(result))
            return result
    except Exception as e:
        logger.debug("extract_text_from_pdf: pdfminer failed: %s", e)

    # pypdf fallback
    try:
        import io
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        result = "\n\n".join(p for p in pages if p.strip())
        if result.strip():
            logger.debug("extract_text_from_pdf: pypdf succeeded, chars=%d", len(result))
            return result
    except Exception as e:
        logger.debug("extract_text_from_pdf: pypdf failed: %s", e)

    logger.warning("extract_text_from_pdf: all parsers returned empty text — may be a scanned PDF")
    return ""


def extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract plain text from DOCX bytes using python-docx."""
    try:
        import io
        from docx import Document

        doc = Document(io.BytesIO(file_bytes))
        parts: list[str] = []
        for para in doc.paragraphs:
            if para.text.strip():
                parts.append(para.text.strip())
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    parts.append(row_text)
        return "\n".join(parts)
    except Exception as e:
        logger.warning("extract_text_from_docx: failed: %s", e)
        return ""


def extract_text_from_pptx(file_bytes: bytes) -> str:
    """Extract plain text from PPTX bytes using python-pptx."""
    try:
        import io
        from pptx import Presentation

        prs = Presentation(io.BytesIO(file_bytes))
        parts: list[str] = []
        for slide_no, slide in enumerate(prs.slides, 1):
            slide_parts = [f"[Slide {slide_no}]"]
            for shape in slide.shapes:
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
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning("extract_text_from_pptx: failed: %s", e)
        return ""


def extract_text_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Use Anthropic Claude Vision to OCR and extract text+tables from an image."""
    try:
        import anthropic
        import base64
        from credit_report.config import ANTHROPIC_API_KEY, CREDIT_REPORT_MODEL

        if not ANTHROPIC_API_KEY:
            logger.warning("extract_text_from_image: ANTHROPIC_API_KEY not set")
            return ""

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CREDIT_REPORT_MODEL,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode(),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract ALL text, numbers, tables, and structured data from this image. "
                            "For tables, preserve the structure using | separators between columns and "
                            "new lines between rows. Include all financial figures, dates, company names, "
                            "key metrics, percentages, and ratios exactly as shown. "
                            "Output as plain text maintaining original layout structure."
                        ),
                    },
                ],
            }],
        )
        result = response.content[0].text or ""
        logger.debug("extract_text_from_image: extracted chars=%d mime=%s", len(result), mime_type)
        return result
    except Exception as e:
        logger.warning("extract_text_from_image: Claude Vision failed: %s", e)
        return ""


def extract_text_from_scanned_pdf_vision(pdf_bytes: bytes, max_pages: int = 20) -> str:
    """
    For scanned/image PDFs where text extraction yields nothing:
    convert each page to an image and OCR with Claude Vision.
    Requires Pillow.
    """
    try:
        import io
        import base64
        import anthropic
        from credit_report.config import ANTHROPIC_API_KEY, CREDIT_REPORT_MODEL

        if not ANTHROPIC_API_KEY:
            return ""

        # Try to get page images using pypdf + Pillow
        import pypdf
        from PIL import Image

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page_count = min(len(reader.pages), max_pages)
        if page_count == 0:
            return ""

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        all_text: list[str] = []

        for page_no in range(page_count):
            try:
                page = reader.pages[page_no]
                # Extract images embedded in the PDF page
                images = list(page.images)
                if not images:
                    continue
                for img_obj in images[:3]:  # Up to 3 images per page
                    img_bytes = img_obj.data
                    response = client.messages.create(
                        model=CREDIT_REPORT_MODEL,
                        max_tokens=2048,
                        messages=[{
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": base64.b64encode(img_bytes).decode(),
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": f"[Page {page_no + 1}] Extract all text and table data from this page image. Preserve table structure with | separators.",
                                },
                            ],
                        }],
                    )
                    all_text.append(response.content[0].text or "")
            except Exception as e:
                logger.debug("extract_text_from_scanned_pdf_vision: page %d failed: %s", page_no + 1, e)
                continue

        return "\n\n".join(t for t in all_text if t.strip())
    except Exception as e:
        logger.warning("extract_text_from_scanned_pdf_vision: failed: %s", e)
        return ""


def extract_text_from_file(file_bytes: bytes, filename: str) -> tuple[str, str]:
    """
    Detect file format and extract text accordingly.

    Returns (extracted_text, detected_format).
    For scanned PDFs where text extraction fails, attempts Claude Vision OCR.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        text = extract_text_from_pdf(file_bytes)
        if not text.strip():
            logger.info("extract_text_from_file: PDF has no extractable text — trying vision OCR: %s", filename)
            text = extract_text_from_scanned_pdf_vision(file_bytes)
        return text, "pdf"
    elif ext in ("docx",):
        return extract_text_from_docx(file_bytes), "docx"
    elif ext in ("doc",):
        text = extract_text_from_docx(file_bytes)
        return text, "doc"
    elif ext in ("pptx",):
        return extract_text_from_pptx(file_bytes), "pptx"
    elif ext in ("ppt",):
        return extract_text_from_pptx(file_bytes), "ppt"
    elif ext in ("txt", "csv", "md"):
        return file_bytes.decode("utf-8", errors="replace"), ext
    elif ext in ("jpg", "jpeg"):
        return extract_text_from_image(file_bytes, "image/jpeg"), "jpg"
    elif ext in ("png",):
        return extract_text_from_image(file_bytes, "image/png"), "png"
    elif ext in ("gif",):
        return extract_text_from_image(file_bytes, "image/gif"), "gif"
    elif ext in ("webp",):
        return extract_text_from_image(file_bytes, "image/webp"), "webp"
    else:
        text = extract_text_from_pdf(file_bytes)
        if not text:
            text = extract_text_from_docx(file_bytes)
        return text, ext or "unknown"

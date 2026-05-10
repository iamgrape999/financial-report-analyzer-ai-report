from __future__ import annotations

from pathlib import Path
from typing import Optional

from credit_report.config import CREDIT_REPORTS_ROOT, CR_MAX_CHUNKS_PER_SECTION, SECTION_RETRIEVAL_KEYWORDS

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
    try:
        import io
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams

        output = io.StringIO()
        extract_text_to_fp(io.BytesIO(pdf_bytes), output, laparams=LAParams(), output_type="text", codec="utf-8")
        result = output.getvalue()
        if result.strip():
            return result
    except Exception:
        pass

    try:
        import io
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(p for p in pages if p.strip())
    except Exception:
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
        # Also extract tables
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    parts.append(row_text)
        return "\n".join(parts)
    except Exception:
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
                # Extract table data
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
    except Exception:
        return ""


def extract_text_from_image_gemini(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Use Gemini vision to extract text and data from an image (OCR + VLM)."""
    try:
        from google import genai
        from google.genai import types as genai_types
        import base64
        from credit_report.config import GEMINI_API_KEY, GEMINI_MODEL

        client = genai.Client(api_key=GEMINI_API_KEY)
        image_b64 = base64.b64encode(image_bytes).decode()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                "Extract ALL text, numbers, tables, and data from this image. "
                "Preserve table structure with | separators. Include all financial figures, "
                "dates, company names, and key metrics. Output as plain text.",
            ],
        )
        return response.text or ""
    except Exception:
        return ""


def extract_text_from_file(file_bytes: bytes, filename: str) -> tuple[str, str]:
    """
    Detect file format and extract text accordingly.

    Returns (extracted_text, detected_format).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        return extract_text_from_pdf(file_bytes), "pdf"
    elif ext in ("docx",):
        return extract_text_from_docx(file_bytes), "docx"
    elif ext in ("doc",):
        # Try docx parser (may work for some .doc files)
        text = extract_text_from_docx(file_bytes)
        return text, "doc"
    elif ext in ("pptx",):
        return extract_text_from_pptx(file_bytes), "pptx"
    elif ext in ("ppt",):
        text = extract_text_from_pptx(file_bytes)
        return text, "ppt"
    elif ext in ("txt", "csv", "md"):
        return file_bytes.decode("utf-8", errors="replace"), ext
    elif ext in ("jpg", "jpeg"):
        return extract_text_from_image_gemini(file_bytes, "image/jpeg"), "jpg"
    elif ext in ("png",):
        return extract_text_from_image_gemini(file_bytes, "image/png"), "png"
    elif ext in ("gif",):
        return extract_text_from_image_gemini(file_bytes, "image/gif"), "gif"
    elif ext in ("webp",):
        return extract_text_from_image_gemini(file_bytes, "image/webp"), "webp"
    else:
        # Unknown — try PDF then DOCX
        text = extract_text_from_pdf(file_bytes)
        if not text:
            text = extract_text_from_docx(file_bytes)
        return text, ext or "unknown"

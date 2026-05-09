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
    """
    Extract plain text from PDF bytes.

    Tries pdfminer.six first (better layout), falls back to pypdf.
    Returns empty string on failure rather than raising.
    """
    try:
        import io

        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams

        output = io.StringIO()
        extract_text_to_fp(
            io.BytesIO(pdf_bytes),
            output,
            laparams=LAParams(),
            output_type="text",
            codec="utf-8",
        )
        return output.getvalue()
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

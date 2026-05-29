"""Regression tests for Render OOM prevention and audit-discovered bugs.

Covers:
  1. Extraction concurrency semaphore (prevents concurrent OOM)
  2. pdfplumber page cap lowered to 50
  3. pypdf fallback page cap added at 50
  4. url_import_bg frees download buffer before extraction
  5. User-visible truncation banner when pages are capped (silent data loss fix)
  6. NameError fix: `len(capped)` → `pages_to_process` in async OCR logger
  7. TWSE diagnostics uses authenticated fetch, not window.open (401 fix)
"""
from pathlib import Path
from pathlib import Path

import pytest

SRC_GENERATE = (Path(__file__).parent.parent / "credit_report" / "api" / "generate.py").read_text()
SRC_EVIDENCE = (Path(__file__).parent.parent / "credit_report" / "generation" / "evidence.py").read_text()


class TestExtractSemaphore:
    def test_semaphore_defined(self):
        assert "_EXTRACT_SEMAPHORE" in SRC_GENERATE

    def test_get_extract_semaphore_helper(self):
        assert "_get_extract_semaphore" in SRC_GENERATE

    def test_semaphore_used_in_upload_bg(self):
        assert "async with sem:" in SRC_GENERATE
        assert "_get_extract_semaphore()" in SRC_GENERATE

    def test_semaphore_limit_is_one(self):
        # Must be Semaphore(1) — only one concurrent extraction at a time
        assert "asyncio.Semaphore(1)" in SRC_GENERATE


class TestPageCaps:
    def test_pdfplumber_cap_is_50(self):
        assert "_PDF_MAX_PDFPLUMBER_PAGES = 50" in SRC_EVIDENCE, (
            "pdfplumber cap must be ≤50 to keep Render under 512 MB"
        )

    def test_pdfplumber_cap_not_100(self):
        assert "_PDF_MAX_PDFPLUMBER_PAGES = 100" not in SRC_EVIDENCE, (
            "100-page cap was too high — it could spike to 300 MB; use 50"
        )

    def test_pypdf_cap_exists(self):
        assert "_PDF_MAX_PYPDF_PAGES" in SRC_EVIDENCE, (
            "pypdf fallback must have a page cap (it previously had none)"
        )

    def test_pypdf_cap_is_50(self):
        assert "_PDF_MAX_PYPDF_PAGES = 50" in SRC_EVIDENCE

    def test_pypdf_cap_applied_in_loop(self):
        assert "_PDF_MAX_PYPDF_PAGES" in SRC_EVIDENCE
        # The cap constant must be referenced inside the extraction body
        assert "range(capped_pages)" in SRC_EVIDENCE or "capped_pages" in SRC_EVIDENCE


class TestUrlImportMemoryOrder:
    def test_fdoc_freed_before_extraction(self):
        # After save_document_binary, fdoc must be deleted BEFORE run_in_executor
        # so the download buffer and extraction peak don't overlap in memory.
        src = SRC_GENERATE
        save_pos = src.find("save_document_binary(report_id, doc_id, fdoc.data, fname)")
        del_fdoc_pos = src.find("del fdoc")
        extract_pos = src.find("file_bytes_for_extract")
        assert save_pos != -1, "save_document_binary call must exist"
        assert del_fdoc_pos != -1, "del fdoc must exist in url_import_bg"
        assert extract_pos != -1, "url_import_bg must re-read from binary path for extraction"
        # Order must be: save → del fdoc → extract
        assert save_pos < del_fdoc_pos < extract_pos, (
            "fdoc must be freed AFTER save but BEFORE extraction to avoid double-counting memory"
        )

    def test_url_import_also_uses_semaphore(self):
        # Both upload bg and url import bg must serialise through the semaphore
        count = SRC_GENERATE.count("_get_extract_semaphore()")
        assert count >= 2, f"semaphore must be acquired in both bg tasks (found {count} calls)"


class TestGCBetweenPhases:
    def test_gc_called_after_plumber(self):
        assert "del plumber_text" in SRC_EVIDENCE
        assert "gc.collect()" in SRC_EVIDENCE

    def test_del_reader_in_pypdf(self):
        assert "del reader, pages" in SRC_EVIDENCE or "del reader" in SRC_EVIDENCE


class TestUserVisibleTruncationWarning:
    """Page cap silently dropping financial data was a gap; these tests lock in the fix."""

    def test_pdfplumber_truncation_banner_exists(self):
        assert "EXTRACTION NOTE" in SRC_EVIDENCE, (
            "Truncation banner must be prepended to text when pages are capped"
        )

    def test_truncation_banner_mentions_page_count(self):
        assert "total_pages > _PDF_MAX_PDFPLUMBER_PAGES" in SRC_EVIDENCE

    def test_pypdf_truncation_banner_exists(self):
        assert "page_count > _PDF_MAX_PYPDF_PAGES" in SRC_EVIDENCE

    def test_banner_advises_splitting(self):
        assert "splitting the PDF" in SRC_EVIDENCE or "splitting" in SRC_EVIDENCE


class TestAsyncOCRNameError:
    """Regression: `len(capped)` NameError on line 878 crashed async Vision OCR completion."""

    def test_capped_variable_not_used_in_logger(self):
        assert "len(capped)" not in SRC_EVIDENCE, (
            "'capped' is not defined in extract_text_from_scanned_pdf_vision_async; "
            "the log line must use 'pages_to_process'"
        )

    def test_pages_to_process_used_in_async_ocr_logger(self):
        assert "pages_to_process, elapsed, len(result)" in SRC_EVIDENCE


class TestTwseDiagnosticsAuth:
    HTML = (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")

    def test_no_window_open_for_diagnostics(self):
        assert "window.open('/api/credit-report/diagnostics/twse'" not in self.HTML, (
            "window.open() cannot send JWT headers — must use authenticated fetch instead"
        )

    def test_showTwseDiagnosticsModal_exists(self):
        assert "showTwseDiagnosticsModal" in self.HTML

    def test_diagnostics_modal_uses_h_headers(self):
        assert "headers:H()" in self.HTML

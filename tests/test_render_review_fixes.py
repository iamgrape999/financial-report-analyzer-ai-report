from __future__ import annotations

import io
import json
import time

from openpyxl import Workbook


def test_xlsx_extraction_preserves_merged_cell_values() -> None:
    from credit_report.generation.evidence import _extract_text_from_xlsx

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Financials"
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "Revenue"
    sheet["A2"] = "FY2025"
    sheet["B2"] = 123

    data = io.BytesIO()
    workbook.save(data)

    text = _extract_text_from_xlsx(data.getvalue())

    assert "## Sheet: Financials" in text
    assert "| Revenue | Revenue |" in text
    assert "| FY2025 | 123 |" in text


def test_restored_running_generation_tasks_are_marked_error(tmp_path, monkeypatch) -> None:
    from credit_report.api import generate

    tasks_file = tmp_path / "generation_tasks.json"
    now = time.time()
    tasks_file.write_text(
        json.dumps(
            {
                "running-task": {"status": "running", "created_at": now, "updated_at": now},
                "done-task": {"status": "done", "created_at": now, "updated_at": now},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(generate, "_TASKS_FILE", tasks_file)
    generate._generation_tasks.clear()

    generate._load_tasks_from_disk()

    assert generate._generation_tasks["running-task"]["status"] == "error"
    assert "server restart" in generate._generation_tasks["running-task"]["detail"]
    assert generate._generation_tasks["done-task"]["status"] == "done"


def test_prepare_database_exits_on_unversioned_app_schema(monkeypatch) -> None:
    """Render's `prepare_database.py && uvicorn` startup chain must stop on legacy schemas."""
    from scripts import prepare_database

    async def fake_table_state() -> tuple[bool, bool]:
        return False, True

    monkeypatch.setattr(prepare_database, "_is_sqlite_url", lambda _url: False)
    monkeypatch.setattr(prepare_database, "_table_state", fake_table_state)

    try:
        prepare_database.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:  # pragma: no cover - assertion message is clearer than pytest.raises here
        raise AssertionError("prepare_database.main() should abort startup for unversioned app schemas")


async def test_section_11_page_selection_uses_keywords_and_falls_back_to_document_pages() -> None:
    from credit_report.generation.document_pipeline import select_pages_for_section
    from credit_report.generation.models import DocumentPage

    class FakeScalarResult:
        def __init__(self, pages: list[DocumentPage]):
            self._pages = pages

        def all(self) -> list[DocumentPage]:
            return self._pages

    class FakeResult:
        def __init__(self, pages: list[DocumentPage]):
            self._pages = pages

        def scalars(self) -> FakeScalarResult:
            return FakeScalarResult(self._pages)

    class FakeDB:
        def __init__(self, pages: list[DocumentPage]):
            self._pages = pages

        async def execute(self, _query):
            return FakeResult(self._pages)

    pages = [
        DocumentPage(id="p1", document_id="doc", report_id="r", pdf_page_no=1, merged_text="cover page"),
        DocumentPage(
            id="p2",
            document_id="doc",
            report_id="r",
            pdf_page_no=2,
            merged_text="Rating: BUY 目標價 EPS forecast",
        ),
        DocumentPage(id="p3", document_id="doc", report_id="r", pdf_page_no=3, merged_text="appendix"),
    ]

    keyword_pages = await select_pages_for_section(FakeDB(pages), "doc", 11)
    assert [page.pdf_page_no for page in keyword_pages] == [2]

    fallback_pages = [
        DocumentPage(id="f1", document_id="doc", report_id="r", pdf_page_no=1, merged_text="cover page"),
        DocumentPage(id="f2", document_id="doc", report_id="r", pdf_page_no=2, merged_text="company overview"),
    ]
    selected_fallback_pages = await select_pages_for_section(FakeDB(fallback_pages), "doc", 11)
    assert [page.pdf_page_no for page in selected_fallback_pages] == [1, 2]

    false_positive_pages = [
        DocumentPage(
            id="h1",
            document_id="doc",
            report_id="r",
            pdf_page_no=1,
            merged_text="shareholders and holding company",
        ),
        DocumentPage(
            id="h2",
            document_id="doc",
            report_id="r",
            pdf_page_no=2,
            merged_text="ordinary appendix",
        ),
        DocumentPage(
            id="h3",
            document_id="doc",
            report_id="r",
            pdf_page_no=3,
            merged_text="Analyst Report Rating: HOLD Target Price",
        ),
    ]
    selected_false_positive_pages = await select_pages_for_section(
        FakeDB(false_positive_pages),
        "doc",
        11,
    )
    assert [page.pdf_page_no for page in selected_false_positive_pages] == [3]

    fallback_with_generic_words = [
        DocumentPage(
            id="g1",
            document_id="doc",
            report_id="r",
            pdf_page_no=1,
            merged_text="shareholders and holding company",
        ),
        DocumentPage(
            id="g2",
            document_id="doc",
            report_id="r",
            pdf_page_no=2,
            merged_text="ordinary appendix",
        ),
    ]
    selected_generic_fallback_pages = await select_pages_for_section(
        FakeDB(fallback_with_generic_words),
        "doc",
        11,
    )
    assert [page.pdf_page_no for page in selected_generic_fallback_pages] == [1, 2]

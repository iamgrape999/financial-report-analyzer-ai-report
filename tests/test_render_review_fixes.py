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

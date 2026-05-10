"""DOCX export endpoint for generated credit report sections."""
from __future__ import annotations

import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.database import get_db
from credit_report.models import Report, SectionOutput
from credit_report.security.auth import get_current_user
from credit_report.security.models import User

router = APIRouter(prefix="/reports/{report_id}", tags=["export"])

SECTION_NAMES = {
    1: "Facility Structure & Key Terms",
    2: "Overall Comments",
    3: "Credit Risk Assessment",
    4: "Borrower Background",
    5: "Collateral Assessment",
    6: "Project Overview (Ship Finance)",
    7: "Financial Analysis",
    8: "Legal Documentation & Charges",
    9: "Compliance Checklist",
    10: "Appendix",
}

# Cathay Financial green
_GREEN = (0, 112, 60)


async def _require_report(db: AsyncSession, report_id: str) -> Report:
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def _assert_can_view(report: Report, user: User) -> None:
    if user.role in {"admin", "reviewer", "approver"}:
        return
    if report.created_by != user.id:
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/export/docx")
async def export_docx(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate and stream a DOCX file for all completed sections."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="python-docx not available on this server — use client-side fallback",
        )

    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)

    result = await db.execute(
        select(SectionOutput)
        .where(SectionOutput.report_id == report_id, SectionOutput.status == "done")
        .order_by(SectionOutput.section_no)
    )
    outputs = list(result.scalars().all())

    if not outputs:
        raise HTTPException(status_code=404, detail="No completed sections to export")

    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin = Cm(2.5)
        section.bottom_margin = Cm(2.5)
        section.left_margin = Cm(2.8)
        section.right_margin = Cm(2.8)

    # Cover title
    title_para = doc.add_heading("Credit Analysis Report", level=0)
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if title_para.runs:
        title_para.runs[0].font.color.rgb = RGBColor(*_GREEN)

    subtitle = report.borrower_name or report_id
    if report.report_type:
        subtitle += f" — {report.report_type}"
    sub = doc.add_paragraph(subtitle)
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph()  # spacer

    for output in outputs:
        sec_no = output.section_no
        sec_name = SECTION_NAMES.get(sec_no, f"Section {sec_no}")

        h = doc.add_heading(f"§{sec_no}  {sec_name}", level=1)
        if h.runs:
            h.runs[0].font.color.rgb = RGBColor(*_GREEN)

        _render_markdown(doc, output.markdown or "", Pt)

        if output != outputs[-1]:
            doc.add_page_break()

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    safe_name = re.sub(r"[^\w\-]", "_", report.borrower_name or report_id)[:40]
    filename = f"credit_report_{safe_name}.docx"

    return StreamingResponse(
        buf,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Markdown → python-docx renderer ──────────────────────────────────────────

_TABLE_ROW_RE = re.compile(r"^\|.+\|$")
_TABLE_SEP_RE = re.compile(r"^\|[-:| ]+\|$")
_HEADING_RE = re.compile(r"^(#{2,5})\s+(.+)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$")
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(.+)$")


def _strip_inline(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def _add_run_with_inline(para, text: str):
    """Add a run, applying bold/italic inline markdown."""
    # Split on **bold** and *italic* markers and apply formatting
    parts = re.split(r"(\*\*.*?\*\*|\*.*?\*|`[^`]+`)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            run = para.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("*") and part.endswith("*"):
            run = para.add_run(part[1:-1])
            run.italic = True
        elif part.startswith("`") and part.endswith("`"):
            run = para.add_run(part[1:-1])
            run.font.name = "Courier New"
        else:
            if part:
                para.add_run(part)


def _render_table(doc, lines: list[str]):
    data_lines = [l for l in lines if "|" in l and not _TABLE_SEP_RE.match(l)]
    if not data_lines:
        return
    headers = [h.strip() for h in data_lines[0].split("|")[1:-1]]
    rows = []
    for line in data_lines[1:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        rows.append(cells)

    if not headers:
        return

    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"

    # Header row — bold + light green shade
    for i, h in enumerate(headers):
        if i < len(table.rows[0].cells):
            cell = table.rows[0].cells[i]
            cell.text = ""
            run = cell.paragraphs[0].add_run(_strip_inline(h))
            run.bold = True
            # Light green cell shading
            from docx.oxml.ns import qn
            from docx.oxml import OxmlElement
            tc = cell._tc
            tcPr = tc.get_or_add_tcPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), "E8F5EE")
            tcPr.append(shd)

    for r_idx, row_vals in enumerate(rows):
        row_cells = table.rows[r_idx + 1].cells
        for c_idx, val in enumerate(row_vals):
            if c_idx < len(row_cells):
                row_cells[c_idx].text = _strip_inline(val)

    doc.add_paragraph()  # spacing after table


def _render_markdown(doc, markdown: str, Pt):
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        if m := _HEADING_RE.match(line):
            level = min(len(m.group(1)), 4)
            doc.add_heading(_strip_inline(m.group(2)), level=level)

        elif _TABLE_SEP_RE.match(line) or _TABLE_ROW_RE.match(line):
            table_lines = []
            while i < len(lines) and (
                _TABLE_ROW_RE.match(lines[i]) or _TABLE_SEP_RE.match(lines[i])
            ):
                table_lines.append(lines[i])
                i += 1
            _render_table(doc, table_lines)
            continue

        elif m := _BULLET_RE.match(line):
            p = doc.add_paragraph(style="List Bullet")
            _add_run_with_inline(p, m.group(1))

        elif m := _NUMBERED_RE.match(line):
            p = doc.add_paragraph(style="List Number")
            _add_run_with_inline(p, m.group(1))

        elif not line.strip():
            pass  # blank lines = natural paragraph separation

        else:
            stripped = _strip_inline(line)
            if stripped:
                p = doc.add_paragraph()
                _add_run_with_inline(p, line)  # keep inline markup

        i += 1

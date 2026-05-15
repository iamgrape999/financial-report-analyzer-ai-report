"""Convert Markdown section output into Block AST (ReportBlock + TableCell records).

Called after Stage 1 (Claude → Markdown) to produce the editable Block AST layer.
Numeric values in table cells are matched against CanonicalFacts by value (±0.5% tolerance).
Unmatched cells are marked binding_status="unbound" for human review.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
_TABLE_ROW_RE = re.compile(r"^\|.+\|$")
_TABLE_SEP_RE = re.compile(r"^\|[-:| ]+\|$")
_LIST_RE = re.compile(r"^(\s*[-*]|\s*\d+\.)\s+")
_NUMERIC_RE = re.compile(r"(?<![A-Za-z%])([\d]{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)(?![A-Za-z%])")

CHART_PLACEHOLDER_RE = re.compile(
    r"\[(?:Org Chart|Share Price Chart|Alliance Chart|Debt Maturity Chart|"
    r"System-generated ESG rating image|Group Limit chart|[^\]]*(?:Chart|Image|Figure|Graph|Chart)[^\]]*)\]",
    re.IGNORECASE,
)


def _make_block_id(section_no: int, block_type: str, index: int) -> str:
    return f"{section_no}.{block_type}.{index:03d}"


def _parse_markdown_table(lines: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Return (headers, rows) from Markdown table lines."""
    data_lines = [l for l in lines if "|" in l and not _TABLE_SEP_RE.match(l)]
    if not data_lines:
        return [], []
    headers = [h.strip() for h in data_lines[0].split("|")[1:-1]]
    rows = []
    for line in data_lines[1:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        row = {}
        for i, h in enumerate(headers):
            row[h] = cells[i] if i < len(cells) else ""
        rows.append(row)
    return headers, rows


def _find_fact_for_value(display: str, facts: list[dict[str, Any]]) -> Optional[str]:
    """Match a cell display value to a fact by numeric value (±0.5% tolerance)."""
    # Try parsing the full cell value as a number first (handles "2,791.0", "2791.0", etc.)
    cleaned = display.replace(",", "").replace("%", "").strip()
    cell_val: Optional[float] = None
    try:
        cell_val = float(cleaned)
    except (ValueError, TypeError):
        # Fall back to extracting the first number from text
        m = re.search(r"-?\d+\.?\d*", cleaned)
        if m:
            try:
                cell_val = float(m.group())
            except ValueError:
                pass

    if cell_val is None or cell_val == 0:
        return None

    for fact in facts:
        fv = fact.get("value")
        if fv is None:
            continue
        tol = max(abs(float(fv)) * 0.005, 0.01)
        if abs(float(fv) - cell_val) <= tol:
            return fact.get("fact_id") or fact.get("id")
    return None


def segment_markdown(markdown: str) -> list[dict[str, Any]]:
    """Split Markdown into typed segment dicts: {type, lines}."""
    segments: list[dict[str, Any]] = []
    lines = markdown.split("\n")
    buffer: list[str] = []
    buf_type = "paragraph"

    def flush():
        nonlocal buffer, buf_type
        if any(l.strip() for l in buffer):
            segments.append({"type": buf_type, "lines": list(buffer)})
        buffer, buf_type = [], "paragraph"

    for line in lines:
        if _HEADING_RE.match(line):
            flush()
            segments.append({"type": "heading", "lines": [line]})
        elif _TABLE_SEP_RE.match(line):
            if buf_type != "table":
                flush()
                buf_type = "table"
            buffer.append(line)
        elif _TABLE_ROW_RE.match(line):
            if buf_type != "table":
                flush()
                buf_type = "table"
            buffer.append(line)
        elif _LIST_RE.match(line):
            if buf_type not in ("list",):
                flush()
                buf_type = "list"
            buffer.append(line)
        elif CHART_PLACEHOLDER_RE.search(line):
            flush()
            segments.append({"type": "chart_image", "lines": [line]})
        else:
            if buf_type == "table":
                flush()
            buffer.append(line)

    flush()
    return segments


def build_blocks(
    report_id: str,
    section_no: int,
    markdown: str,
    facts: list[dict[str, Any]],
) -> tuple[list[dict], list[dict]]:
    """
    Convert Markdown + Fact list into (blocks, cells) dicts ready to persist.

    Returns:
      blocks: list of dicts matching ReportBlock columns
      cells:  list of dicts matching TableCell columns
    """
    segments = segment_markdown(markdown)
    blocks: list[dict] = []
    cells: list[dict] = []
    idx = 0

    for seg in segments:
        bid = _make_block_id(section_no, seg["type"], idx)
        content = "\n".join(seg["lines"])

        if seg["type"] == "table":
            headers, rows = _parse_markdown_table(seg["lines"])
            # Use stable short keys (col_000…) — header text is unlimited, cannot be a DB key
            col_id_map = {h: f"col_{i:03d}" for i, h in enumerate(headers)}
            columns = [
                {"column_id": f"col_{i:03d}", "label": h, "col_type": "string"}
                for i, h in enumerate(headers)
            ]
            block_fact_ids: list[str] = []

            for r_idx, row in enumerate(rows):
                row_id = f"row_{r_idx:03d}"
                for header_text, val in row.items():
                    col_id = col_id_map.get(header_text, f"col_{list(row.keys()).index(header_text):03d}")
                    fid = _find_fact_for_value(val, facts)
                    if fid:
                        block_fact_ids.append(fid)
                    try:
                        nums = _NUMERIC_RE.findall(val.replace(",", ""))
                        num_val = float(nums[0]) if nums else None
                    except (ValueError, IndexError):
                        num_val = None
                    cells.append({
                        "id": str(uuid.uuid4()),
                        "block_id": bid,
                        "row_id": row_id,
                        "column_id": col_id,
                        "display_value": val,
                        "numeric_value": num_val,
                        "fact_id": fid,
                        "binding_status": "bound" if fid else "unbound",
                        "version": 1,
                    })

            blocks.append({
                "id": bid,
                "report_id": report_id,
                "section_no": section_no,
                "block_type": "table",
                "content": content,
                "columns_json": json.dumps(columns),
                "source_fact_ids": json.dumps(list(set(block_fact_ids))),
                "validation_status": "pending",
                "is_stale": False,
                "version": 1,
            })

        else:
            # paragraph / heading / list / chart_image
            numeric_hits = _NUMERIC_RE.findall(content.replace(",", ""))
            bound_fact_ids = [
                fid for n in numeric_hits
                if (fid := _find_fact_for_value(n, facts)) is not None
            ]
            blocks.append({
                "id": bid,
                "report_id": report_id,
                "section_no": section_no,
                "block_type": seg["type"],
                "content": content,
                "columns_json": None,
                "source_fact_ids": json.dumps(list(set(bound_fact_ids))),
                "validation_status": "pending",
                "is_stale": False,
                "version": 1,
            })

        idx += 1

    return blocks, cells

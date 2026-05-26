#!/usr/bin/env python3
"""
etl_pipeline_v2.py — 4-Layer Annual Report ETL (Gemini + pymupdf)

Layers:
  L0: Page classification (cover/table/chart/kpi_card/mixed)
  L1: Text + Table extraction (pdfplumber)
  L2: VLM chart/image extraction (Gemini Vision via pymupdf page rendering)
  L3: Field mapping → structured JSON (593 fields × 10 sections)
  L4: Paragraph generation → 繁體中文 credit narrative (Gemini LLM)

Usage:
  python etl_pipeline_v2.py --pdf report.pdf --out ./output --paragraphs all
  python etl_pipeline_v2.py --pdf a.pdf b.pdf --out ./output --paragraphs P1,P3,P7
  python etl_pipeline_v2.py --pdf report.pdf --out ./output --mode fields-only
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False
    print("[WARN] pip install pdfplumber", file=sys.stderr)

try:
    import fitz  # pymupdf
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False
    print("[WARN] pip install pymupdf  (needed for VLM chart extraction)", file=sys.stderr)

try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False
    print("[WARN] pip install google-genai", file=sys.stderr)

sys.path.insert(0, str(Path(__file__).parent))
from field_mapper import map_fields
from paragraph_writer import write_paragraphs, format_markdown_output, PARAGRAPH_CONFIG

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_VLM_MODEL = os.getenv("GEMINI_OCR_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"))

# ── Page Classifier ───────────────────────────────────────────────────────────
SKIP_KW = ["免責聲明", "legal disclaimer", "謝謝", "thank you", "forward-looking"]


def classify_page(page, pn: int, total: int) -> str:
    if pn == 1:
        return "cover"
    text = (page.extract_text() or "").strip()
    tl = text.lower()
    wc = len(text.split())
    tabs = page.extract_tables() or []
    imgs = page.images or []
    has_img = len(imgs) > 0
    if any(k in tl for k in SKIP_KW):
        return "disclaimer"
    if wc < 30 and not tabs:
        return "divider"
    if has_img and wc < 80:
        nt = len(re.findall(r"[\d,\.]+[%億萬KTEU]?", text))
        return "kpi_card" if nt >= 4 else "chart"
    if tabs and not has_img:
        return "table"
    if tabs and has_img:
        return "mixed"
    if wc >= 200 and not has_img:
        return "text_heavy"
    return "chart" if has_img else "text_heavy"


def infer_chart_type(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["損益", "毛利", "淨利", "ebitda"]):
        return "financial_table"
    if re.search(r"q[1-4].*%|季.*%", t):
        return "bar_line_combo"
    if re.search(r"20\d{2}", t) and "%" in t:
        return "time_series"
    if any(k in t for k in ["fleet", "capacity", "throughput", "供需"]):
        return "supply_demand"
    return "auto"


# ── VLM Prompts ───────────────────────────────────────────────────────────────
VLM_PROMPTS = {
    "bar_line_combo":  (
        '分析此圖表，僅輸出純JSON：{"chart_title":"","x_axis_labels":[],'
        '"series":[{"name":"","unit":"","type":"bar|line","values":[]}],'
        '"source":"","confidence":0.0}'
    ),
    "kpi_card": (
        '提取所有KPI指標，僅輸出純JSON：{"kpis":[{"label":"","value":"","unit":"","context":""}],'
        '"confidence":0.0}'
    ),
    "financial_table": (
        '提取財務表格，僅輸出純JSON：{"table_title":"","currency_unit":"","period":"",'
        '"rows":[{"item":""}],"confidence":0.0}'
    ),
    "time_series": (
        '提取時序數據，僅輸出純JSON：{"chart_title":"","time_points":[],'
        '"series":[{"name":"","unit":"","values":[]}],"confidence":0.0}'
    ),
    "supply_demand": (
        '分析供需圖，僅輸出純JSON：{"chart_title":"","years":[],"capacity_growth_pct":[],'
        '"throughput_growth_pct":[],"fleet_capacity_mteu":[],"source":"","confidence":0.0}'
    ),
    "auto": (
        '分析此頁財務數據，僅輸出純JSON：{"page_type":"","title":"","data":{},'
        '"source":"","confidence":0.0,"notes":""}'
    ),
}


# ── Layer 1: Text + Table (pdfplumber) ────────────────────────────────────────
def extract_text_table(pdf_path: str, page_nums: list[int]) -> dict:
    results = {}
    if not HAS_PDFPLUMBER:
        return results
    with pdfplumber.open(pdf_path) as pdf:
        for pn in page_nums:
            if pn < 1 or pn > len(pdf.pages):
                continue
            pg = pdf.pages[pn - 1]
            text = (pg.extract_text() or "").strip()
            tabs = pg.extract_tables() or []
            results[pn] = {
                "extractor": "pdfplumber",
                "text": text,
                "tables": [_normalize_table(t) for t in tabs if t],
                "confidence": 0.95 if (text or tabs) else 0.3,
            }
    return results


def _normalize_table(raw: list) -> dict:
    if not raw or len(raw) < 2:
        return {"headers": [], "rows": []}
    headers = [str(x).strip() if x else f"col_{i}" for i, x in enumerate(raw[0])]
    rows = [
        {headers[i]: str(v).strip() if v else "" for i, v in enumerate(r) if i < len(headers)}
        for r in raw[1:] if any(r)
    ]
    return {"headers": headers, "rows": rows}


# ── Layer 2: VLM Chart Extraction (Gemini Vision + pymupdf) ──────────────────
def render_page_to_base64(pdf_path: str, pn: int, dpi: int = 150) -> str | None:
    """Render a PDF page to PNG base64 using pymupdf (no poppler required)."""
    if not HAS_FITZ:
        return None
    try:
        doc = fitz.open(pdf_path)
        page = doc[pn - 1]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        doc.close()
        return base64.b64encode(img_bytes).decode()
    except Exception as e:
        print(f"    [L2] render p{pn} failed: {e}", file=sys.stderr)
        return None


def extract_chart_vlm(pdf_path: str, pn: int, chart_type: str = "auto") -> dict:
    if not HAS_GENAI or not GEMINI_API_KEY:
        return {"error": "Gemini not available", "page": pn, "confidence": 0.0}
    b64 = render_page_to_base64(pdf_path, pn)
    if not b64:
        return {"error": "Page render failed", "page": pn, "confidence": 0.0}
    prompt = VLM_PROMPTS.get(chart_type, VLM_PROMPTS["auto"])
    client = genai.Client(api_key=GEMINI_API_KEY)
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_VLM_MODEL,
                contents=[
                    genai_types.Part.from_bytes(
                        data=base64.b64decode(b64),
                        mime_type="image/png",
                    ),
                    genai_types.Part.from_text(text=prompt),
                ],
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=1000,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            raw = (response.text or "").strip()
            clean = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
            try:
                parsed = json.loads(clean)
            except Exception:
                m = re.search(r"\{.*\}", clean, re.DOTALL)
                parsed = json.loads(m.group()) if m else {"raw": raw, "parse_error": True}
            parsed.update({"page": pn, "extractor": "vlm", "chart_type_used": chart_type})
            return parsed
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                return {"error": str(e), "page": pn, "confidence": 0.0}
    return {"error": "VLM failed after 3 attempts", "page": pn, "confidence": 0.0}


# ── Single PDF Extraction (L0-L2) ─────────────────────────────────────────────
def _extract_single(pdf_path: str, selective_pages: list[int] | None = None) -> dict:
    raw = {
        "meta": {
            "source_file": Path(pdf_path).name,
            "extraction_date": datetime.now().strftime("%Y-%m-%d"),
            "company": "", "ticker": "", "total_pages": 0,
            "pages_extracted": 0, "pages_skipped": [],
        },
        "income_statement": {}, "financial_ratios": {}, "operational_kpis": {},
        "quarterly": {"revenue": {}, "gross_margin": {}, "net_margin": {}},
        "freight_volume": {}, "fleet_data": {}, "raw_pages": {},
    }
    if not HAS_PDFPLUMBER:
        return raw
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        raw["meta"]["total_pages"] = total
        target = selective_pages or list(range(1, total + 1))
        for pn in target:
            if pn < 1 or pn > total:
                continue
            pg = pdf.pages[pn - 1]
            ptype = classify_page(pg, pn, total)
            if ptype in ("cover", "disclaimer", "divider"):
                raw["meta"]["pages_skipped"].append(f"p{pn}:{ptype}")
                continue
            pt = pg.extract_text() or ""
            result: dict = {"classification": ptype}
            if ptype in ("text_heavy", "table"):
                result.update(extract_text_table(pdf_path, [pn]).get(pn, {}))
            elif ptype in ("chart", "kpi_card", "map"):
                result.update(extract_chart_vlm(pdf_path, pn, infer_chart_type(pt)))
            elif ptype == "mixed":
                tr = extract_text_table(pdf_path, [pn]).get(pn, {})
                vr = extract_chart_vlm(pdf_path, pn, infer_chart_type(pt))
                result = {
                    "classification": "mixed",
                    "text_extraction": tr,
                    "vlm_extraction": vr,
                    **vr,
                    "tables": tr.get("tables", []),
                    "text": tr.get("text", ""),
                }
            raw["raw_pages"][str(pn)] = result
            raw["meta"]["pages_extracted"] += 1
            conf = result.get("confidence", 0.9)
            print(f"    P{pn:3d} {ptype:12s} conf={conf:.2f}")
    return raw


# ── Coverage Report ───────────────────────────────────────────────────────────
def _write_coverage_report(uf: dict, out_dir: str) -> None:
    cov = uf["coverage"]
    lines = [
        "# Field Coverage Report",
        f"**Overall:** {cov['coverage_pct']:.1f}%  "
        f"({cov['extracted'] + cov['inferred']}/{cov['total_fields']})",
        "",
        "| Section | Total | Extracted | Inferred | Low Conf | Missing | Manual | Coverage |",
        "|---------|-------|-----------|----------|----------|---------|--------|----------|",
    ]
    for sk, sd in uf.get("sections", {}).items():
        f = sd.get("fields", {})
        t = len(f)
        ex = sum(1 for x in f.values() if x["status"] == "extracted")
        inf = sum(1 for x in f.values() if x["status"] == "inferred")
        lc = sum(1 for x in f.values() if x["status"] == "low_conf")
        mi = sum(1 for x in f.values() if x["status"] == "missing")
        ma = sum(1 for x in f.values() if x["status"] == "manual_req")
        pct = round((ex + inf + lc) / max(t, 1) * 100, 0)
        bar = "🟢" if pct >= 75 else ("🟡" if pct >= 50 else "🔴")
        lines.append(f"| {sk} | {t} | {ex} | {inf} | {lc} | {mi} | {ma} | {bar} {pct:.0f}% |")
    Path(out_dir, "field_coverage_report.md").write_text("\n".join(lines), encoding="utf-8")


# ── Main Pipeline ─────────────────────────────────────────────────────────────
def run_pipeline(
    pdf_paths: list[str],
    out_dir: str,
    mode: str = "full",
    paragraph_ids: list[str] | None = None,
    selective_pages: list[int] | None = None,
) -> dict:
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # Merge all PDFs
    merged: dict = {
        "meta": {
            "source_file": "|".join(Path(p).name for p in pdf_paths),
            "extraction_date": datetime.now().strftime("%Y-%m-%d"),
            "company": "", "ticker": "", "total_pages": 0,
            "pages_extracted": 0, "pages_skipped": [],
        },
        "income_statement": {}, "financial_ratios": {}, "operational_kpis": {},
        "quarterly": {"revenue": {}, "gross_margin": {}, "net_margin": {}},
        "freight_volume": {}, "fleet_data": {}, "raw_pages": {},
    }
    offset = 0
    for pp in pdf_paths:
        print(f"\n[L0-L2] Extracting: {Path(pp).name}")
        raw = _extract_single(pp, selective_pages)
        for pn_s, d in raw.get("raw_pages", {}).items():
            merged["raw_pages"][str(int(pn_s) + offset)] = d
        for k in ["income_statement", "financial_ratios", "operational_kpis",
                  "quarterly", "freight_volume", "fleet_data"]:
            if raw.get(k):
                merged[k] = {**merged.get(k, {}), **raw[k]}
        for fld in ["company", "ticker"]:
            if not merged["meta"][fld] and raw["meta"].get(fld):
                merged["meta"][fld] = raw["meta"][fld]
        merged["meta"]["total_pages"] += raw["meta"]["total_pages"]
        merged["meta"]["pages_extracted"] += raw["meta"]["pages_extracted"]
        offset += raw["meta"]["total_pages"]

    raw_path = Path(out_dir) / "raw_extraction.json"
    raw_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[L0-L2] ✓ Saved raw → {raw_path}")
    print(f"         Pages: {merged['meta']['pages_extracted']} extracted, "
          f"{len(merged['meta']['pages_skipped'])} skipped")
    if mode == "raw-only":
        return merged

    print(f"\n[L3] Field mapping ({len(__import__('field_mapper').FIELD_REGISTRY)} fields)...")
    uf = map_fields(merged)
    cov = uf["coverage"]
    print(
        f"     Coverage {cov['coverage_pct']:.1f}% | "
        f"Ext:{cov['extracted']} Inf:{cov['inferred']} "
        f"Low:{cov['low_conf']} Miss:{cov['missing']} Manual:{cov['manual_req']}"
    )
    fp = Path(out_dir) / "unified_fields.json"
    fp.write_text(json.dumps(uf, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_coverage_report(uf, out_dir)
    print(f"     ✓ Saved → {fp}")
    if mode == "fields-only":
        return uf

    pids = paragraph_ids or list(PARAGRAPH_CONFIG.keys())
    print(f"\n[L4] Generating paragraphs: {', '.join(pids)}")
    if not GEMINI_API_KEY:
        print("     ⚠️  GEMINI_API_KEY not set — skipping paragraph generation", file=sys.stderr)
        return uf
    results = write_paragraphs(uf, pids)
    md = format_markdown_output(results, uf, merged["meta"]["source_file"])
    pp2 = Path(out_dir) / "credit_paragraphs.md"
    pp2.write_text(md, encoding="utf-8")
    (Path(out_dir) / "credit_paragraphs.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total_missing = sum(r["missing_field_count"] for r in results.values())
    print(f"\n✅ Done  → {fp}")
    print(f"        → {pp2}")
    print(f"        → field_coverage_report.md")
    print(f"   Total [待填] placeholders across all paragraphs: {total_missing}")
    return uf


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="4-Layer Annual Report ETL — PDF → 593 fields → 10 credit paragraphs"
    )
    ap.add_argument("--pdf", nargs="+", required=True, help="Input PDF file(s)")
    ap.add_argument("--out", default="./output", help="Output directory")
    ap.add_argument(
        "--mode",
        choices=["full", "fields-only", "raw-only"],
        default="full",
        help="Pipeline stop point: full=all layers, fields-only=skip L4, raw-only=skip L3+L4",
    )
    ap.add_argument(
        "--paragraphs",
        default="all",
        help="Paragraph IDs to generate: 'all' or comma-separated e.g. 'P1,P3,P7'",
    )
    ap.add_argument(
        "--pages",
        default=None,
        help="Comma-separated page numbers to process (default: all pages)",
    )
    args = ap.parse_args()
    pages = [int(x.strip()) for x in args.pages.split(",")] if args.pages else None
    pids = (
        list(PARAGRAPH_CONFIG.keys())
        if args.paragraphs.lower() == "all"
        else [x.strip().upper() for x in args.paragraphs.split(",")]
    )
    run_pipeline(args.pdf, args.out, args.mode, pids, pages)

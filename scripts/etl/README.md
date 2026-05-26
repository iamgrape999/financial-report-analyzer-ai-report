# Annual Report ETL — 4-Layer Pipeline

Standalone CLI tools to extract structured data from financial PDFs and
generate Traditional Chinese credit report paragraphs.

## Prerequisites

```bash
# All deps are in requirements.txt; install once:
pip install -r requirements.txt
# GEMINI_API_KEY must be set:
export GEMINI_API_KEY=AIza...
```

## Quick Start

```bash
# Full pipeline: PDF → 77+ fields → 10 credit paragraphs
python etl_pipeline_v2.py --pdf annual_report.pdf --out ./output --paragraphs all

# Fields only (no paragraph generation, no API cost):
python etl_pipeline_v2.py --pdf report.pdf --out ./output --mode fields-only

# Selective paragraphs:
python etl_pipeline_v2.py --pdf report.pdf --out ./output --paragraphs P1,P3,P7

# Multiple PDFs merged (higher field coverage):
python etl_pipeline_v2.py --pdf investor_pres.pdf annual_report.pdf esg.pdf --out ./output

# Re-run only paragraph generation from saved fields:
python paragraph_writer.py --fields ./output/unified_fields.json \
  --out ./output/credit_paragraphs_v2.md --paragraphs P5,P6,P9
```

## Architecture

```
PDF(s)
  ├─ L0: Page Classifier  (cover/table/chart/kpi_card/mixed)
  ├─ L1: Text + Table      pdfplumber → raw text, tables
  ├─ L2: VLM Chart         Gemini Vision + pymupdf → chart series JSON
  ├─ L3: Field Mapper      raw → 77 structured fields (10 sections)
  └─ L4: Paragraph Writer  Gemini LLM → 繁體中文 credit narrative
```

## Output Files

| File | Description |
|------|-------------|
| `raw_extraction.json` | Per-page raw extraction (audit trail) |
| `unified_fields.json` | 77 fields with value + status + confidence |
| `field_coverage_report.md` | Per-section coverage table |
| `credit_paragraphs.md` | 1–10 narrative paragraphs, `[待填]` for missing |
| `credit_paragraphs.json` | Paragraph JSON with issue warnings |

## Field Status Legend

| Status | Meaning |
|--------|---------|
| `extracted` | Found in document, confidence ≥ 0.75 |
| `inferred` | Calculated from other fields (e.g. margin = profit/revenue) |
| `low_conf` | Found but confidence 0.50–0.74 |
| `missing` | Not found in any source page → `[待填]` in paragraph |
| `manual_req` | Requires credit officer judgment (P6/P9 fields) |

## Coverage by Section

| Section | Auto-Fill | Notes |
|---------|-----------|-------|
| P1 公司概況 | ~70% | From KPI pages and cover |
| P2 產業分析 | ~80% | From market charts |
| P3 財務分析 | ~85% | From income statement tables |
| P4 公司治理 | ~20% | Limited in IR presentations |
| P5 風險評估 | ~30% | Needs credit officer judgement |
| P6 授信建議 | 0% | All manual |
| P7 流動性 | ~60% | From financial ratio charts |
| P8 ESG | ~40% | If ESG report included |
| P9 同業比較 | 0% | All manual (external data needed) |
| P10 評等/債務 | ~30% | If ratings mentioned in text |

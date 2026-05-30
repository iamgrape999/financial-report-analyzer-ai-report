"""Page-first document ETL pipeline primitives.

This module intentionally keeps deterministic document coverage, page manifests,
section planning, and Smart Import proposals separate from the LLM extractor.  The
LLM should see small page-bound chunks, never an arbitrary leading slice of an
annual report.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from credit_report.generation.models import (
    CandidateFact,
    DocumentBlock,
    DocumentPage,
    ExtractedTable,
    SectionDocument,
    SectionImportProposal,
)
from credit_report.config import CR_OCR_TIMEOUT_SECONDS
from credit_report.models import SectionInput

FINANCIAL_PAGE_TERMS = (
    "財務狀況及經營結果",
    "財務狀況分析",
    "財務績效分析",
    "現金流量",
    "營業收入淨額",
    "營業毛利",
    "營業淨利",
    "本年度淨利",
    "資產總額",
    "負債總額",
    "權益總額",
)

SECTION_KEYWORDS: dict[int, tuple[str, ...]] = {
    1: ("公司簡介", "股票代號", "公司債", "股本", "資本結構", "發言人", "會計師"),
    2: ("致股東報告書", "營收", "每股盈餘", "毛利率", "營業利益率", "AI", "展望"),
    3: ("風險", "公司治理", "內部控制", "法規遵循", "資安", "出口管制", "氣候"),
    4: ("公司簡介", "市場概況", "董事", "主要經理人", "全球據點", "產品", "專業積體電路製造"),
    5: ("擔保", "抵押", "collateral", "guarantee", "valuation"),
    6: ("亞利桑那", "熊本", "Dresden", "德國", "2奈米", "2 奈米", "CoWoS", "資本支出", "擴產"),
    7: FINANCIAL_PAGE_TERMS + ("每股盈餘", "EPS"),
    8: ("公司債", "會計師", "法規遵循", "重大契約", "訴訟", "關係企業"),
    9: ("內部控制", "法規遵循", "出口管制", "反托拉斯", "勞動", "資安", "風險管理"),
    10: ("目錄", "財務概況", "附錄", "關係企業", "重要契約"),
    11: (
        "rating",
        "ratings",
        "buy",
        "hold",
        "sell",
        "neutral",
        "target price",
        "price target",
        "analyst",
        "broker",
        "research report",
        "investment recommendation",
        "forecast",
        "estimate",
        "valuation",
        "upside",
        "downside",
        "PBR",
        "PER",
        "P/E",
        "ROE",
        "EPS",
        "目標價",
        "投資建議",
        "個股報告",
        "年度預測",
        "季度預測",
        "每股盈餘",
        "殖利率",
        "比率分析",
    ),
}

# Section 11 can include short English analyst ratings.  Match them as tokens
# instead of substrings so ordinary words such as "shareholder" or "holding"
# do not suppress the fallback page window with false positives.
SECTION_WORD_BOUNDARY_KEYWORDS: dict[int, tuple[str, ...]] = {
    # Section 2: "AI" would otherwise match "chairman", "sustainability", etc.
    2: ("ai",),
    # Section 11: "rating" added alongside "ratings" so plurals also match;
    # "per", "eps", "p/e" are short acronyms that substring-match common words
    # like "performance" or "percentage" without word-boundary anchoring.
    11: ("rating", "ratings", "buy", "hold", "sell", "neutral", "per", "eps", "p/e"),
}

# ── Document profile ──────────────────────────────────────────────────────────

@dataclass
class DocumentProfile:
    """Detected characteristics of an uploaded document used to select keyword sets."""
    industry: str = "generic"       # tw_semiconductor | tw_banking | tw_shipping | tw_real_estate | tw_insurance | generic
    market: str = "TW"              # TW | HK | US | JP | SG | generic
    language: str = "zh_tw"        # zh_tw | en | ja | mixed
    report_type: str = "annual_report"  # annual_report | financial_statement | analyst_report | interim_report | other
    is_scanned: bool = False        # True when native text quality is very low (image-only PDF)

    def keyword_profile_key(self) -> str:
        """Map profile to a SECTION_KEYWORDS_BY_PROFILE entry."""
        if self.report_type == "analyst_report":
            return "analyst_report"
        if self.language == "en":
            return "en_annual"
        return self.industry  # tw_semiconductor, tw_banking, etc.

    def as_dict(self) -> dict:
        return {
            "industry": self.industry,
            "market": self.market,
            "language": self.language,
            "report_type": self.report_type,
            "is_scanned": self.is_scanned,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DocumentProfile":
        return cls(
            industry=d.get("industry", "generic"),
            market=d.get("market", "TW"),
            language=d.get("language", "zh_tw"),
            report_type=d.get("report_type", "annual_report"),
            is_scanned=bool(d.get("is_scanned", False)),
        )


# Keyword sets per industry profile.  Each entry merges with SECTION_KEYWORDS as
# a fallback so only the sections that differ need to be overridden.
SECTION_KEYWORDS_BY_PROFILE: dict[str, dict[int, tuple[str, ...]]] = {
    # tw_semiconductor explicitly mirrors SECTION_KEYWORDS so all profiles have
    # a concrete mapping and the fallback chain is never relied upon for the
    # primary industry.
    "tw_semiconductor": {k: v for k, v in SECTION_KEYWORDS.items()},
    "tw_banking": {
        1: ("銀行簡介", "股票代號", "資本結構", "股本", "負債", "淨值", "信評"),
        2: ("致股東", "淨利差", "放款", "存款", "逾放比", "每股盈餘", "資本適足率", "展望"),
        3: ("信用風險", "市場風險", "流動性風險", "作業風險", "法遵", "內部控制", "公司治理"),
        4: ("授信業務", "財富管理", "投資銀行", "數位金融", "分行", "子公司", "海外據點"),
        6: ("數位轉型", "分行設置", "海外佈局", "資訊系統", "資本支出"),
        7: FINANCIAL_PAGE_TERMS + ("淨利差", "放款", "存款", "備抵呆帳", "資本適足率", "EPS"),
        8: ("公司債", "金管會", "法規遵循", "訴訟", "重大契約", "存款保險"),
    },
    "tw_shipping": {
        1: ("航運公司", "股票代號", "船隊", "資本結構", "股本"),
        2: ("致股東", "運費", "TEU", "BDI", "船隊", "每股盈餘", "展望"),
        3: ("航運風險", "市場風險", "匯率風險", "環保法規", "公司治理"),
        4: ("貨櫃", "散裝", "船隊管理", "航線", "港口", "代理"),
        6: ("新船建造", "造船", "船齡", "汰舊換新", "資本支出", "港口投資"),
        7: FINANCIAL_PAGE_TERMS + ("運費收入", "TEU", "每股盈餘", "EPS"),
        8: ("船舶抵押", "法規遵循", "訴訟", "海事法", "公約"),
    },
    "tw_real_estate": {
        1: ("建設公司", "股票代號", "資本結構", "股本", "土地"),
        2: ("致股東", "銷售", "推案", "完工", "每股盈餘", "展望"),
        3: ("市場風險", "利率風險", "法規風險", "公司治理"),
        4: ("住宅", "商辦", "廠辦", "建案", "工業用地", "土地開發"),
        6: ("建案開發", "土地取得", "容積率", "都更", "資本支出"),
        7: FINANCIAL_PAGE_TERMS + ("銷售收入", "推案金額", "每股盈餘", "EPS"),
    },
    "tw_insurance": {
        1: ("保險公司", "股票代號", "資本結構", "股本", "清償能力"),
        2: ("致股東", "保費", "理賠", "投資", "每股盈餘", "展望"),
        3: ("保險風險", "信用風險", "市場風險", "清償風險", "法規遵循"),
        4: ("壽險", "產險", "健康險", "投資型保單", "再保", "通路"),
        6: ("系統建置", "數位轉型", "資本支出"),
        7: FINANCIAL_PAGE_TERMS + ("保費收入", "理賠率", "清償能力", "每股盈餘", "EPS"),
    },
    "en_annual": {
        1: ("company overview", "ticker", "capital structure", "board of directors", "auditor"),
        2: ("letter to shareholders", "revenue", "earnings per share", "EPS", "gross margin", "outlook"),
        3: ("risk", "governance", "compliance", "internal control", "cybersecurity", "climate"),
        4: ("business overview", "market", "directors", "management", "products", "services", "subsidiaries"),
        5: ("collateral", "mortgage", "guarantee", "pledge", "security interest", "valuation"),
        6: ("capital expenditure", "capex", "expansion", "new facility", "investment", "R&D"),
        7: ("revenue", "gross profit", "operating income", "net income", "total assets", "EPS", "ROE"),
        8: ("bonds", "auditor", "compliance", "litigation", "contracts", "related party"),
        9: ("internal control", "compliance", "risk management", "audit committee"),
        10: ("table of contents", "index", "financial highlights", "appendix"),
        11: SECTION_KEYWORDS[11],
    },
    "analyst_report": {
        11: SECTION_KEYWORDS[11],
        # Analyst reports focus almost entirely on §11 content.
        # Fallback to SECTION_KEYWORDS for other sections.
    },
    "generic": {
        # Wide-net Chinese + English keywords covering any industry.
        1: ("公司簡介", "company overview", "股票代號", "ticker", "資本結構", "capital structure"),
        2: ("致股東", "letter to shareholders", "營收", "revenue", "每股盈餘", "EPS", "展望", "outlook"),
        3: ("風險", "risk", "公司治理", "governance", "內部控制", "internal control", "法規遵循", "compliance"),
        4: ("業務概況", "business overview", "產品", "products", "市場", "market", "董事", "directors"),
        5: ("擔保", "抵押", "collateral", "guarantee", "valuation", "質押"),
        6: ("資本支出", "capital expenditure", "capex", "擴產", "expansion", "投資"),
        7: FINANCIAL_PAGE_TERMS + ("revenue", "net income", "total assets", "EPS", "每股盈餘"),
        8: ("公司債", "bonds", "法規遵循", "compliance", "訴訟", "litigation", "重大契約"),
        9: ("內部控制", "internal control", "法規遵循", "compliance", "risk management"),
        10: ("目錄", "table of contents", "財務概況", "financial highlights", "附錄"),
        11: SECTION_KEYWORDS[11],
    },
}

# Industry-detection signals: (term_list, industry_key, weight)
_INDUSTRY_SIGNALS: list[tuple[tuple[str, ...], str, int]] = [
    (("半導體", "晶圓", "積體電路", "wafer", "fab", "foundry", "tsmc", "台積電", "umc", "聯電", "製程節點", "奈米"), "tw_semiconductor", 3),
    (("銀行", "授信", "存款", "放款", "資本適足", "金融機構", "金控", "逾放比", "淨利差"), "tw_banking", 3),
    (("航運", "船舶", "貨櫃", "散裝", "運費", "teu", "bdi", "造船", "港口", "航線"), "tw_shipping", 3),
    (("不動產", "房地產", "建設", "建案", "容積率", "土地開發", "都市更新", "住宅"), "tw_real_estate", 3),
    (("保險", "保費", "理賠", "壽險", "產險", "清償能力", "再保", "投資型保單"), "tw_insurance", 3),
]

_REPORT_TYPE_SIGNALS: list[tuple[tuple[str, ...], str]] = [
    (("年報", "annual report", "致股東報告書", "letter to shareholders"), "annual_report"),
    (("季報", "interim report", "半年報", "第一季", "第二季", "q1", "q2", "q3"), "interim_report"),
    (("財務報告", "財務報表", "financial statements", "balance sheet", "income statement"), "financial_statement"),
    (("目標價", "target price", "投資建議", "investment recommendation", "buy", "sell", "hold", "rating"), "analyst_report"),
]


def detect_document_profile(
    text: str,
    filename: str | None = None,
    pages: list[dict] | None = None,
) -> DocumentProfile:
    """Detect industry, market, language and report type from document text.

    Uses only heuristics (no LLM) so it is fast and zero-cost.  The result is
    stored on SectionDocument.document_profile and used by select_pages_for_section
    to pick the right keyword set.
    """
    sample = (text or "")[:8000].lower()
    fname = (filename or "").lower()

    # Language: count CJK code-points vs ASCII letters
    cjk_count = sum(1 for c in sample if "一" <= c <= "鿿")
    ascii_count = sum(1 for c in sample if c.isascii() and c.isalpha())
    total = cjk_count + ascii_count or 1
    if cjk_count / total > 0.4:
        language = "zh_tw"
    elif cjk_count / total < 0.1:
        language = "en"
    else:
        language = "mixed"

    # Report type — check filename first, then text
    report_type = "annual_report"
    for terms, rtype in _REPORT_TYPE_SIGNALS:
        if any(t in fname for t in terms) or any(t in sample for t in terms):
            report_type = rtype
            break

    # Industry — accumulate weighted votes
    scores: dict[str, int] = {}
    for terms, industry_key, weight in _INDUSTRY_SIGNALS:
        hits = sum(1 for t in terms if t in sample)
        if hits:
            scores[industry_key] = scores.get(industry_key, 0) + hits * weight

    if scores:
        industry = max(scores, key=lambda k: scores[k])
    else:
        industry = "generic"

    # Market detection
    if any(t in sample for t in ("台灣證券交易所", "twse", "台股", "上市", "上櫃")):
        market = "TW"
    elif any(t in sample for t in ("hkex", "hong kong stock", "港交所", "港股")):
        market = "HK"
    elif any(t in sample for t in ("nasdaq", "nyse", "sec filing", "10-k", "10-q")):
        market = "US"
    elif any(t in sample for t in ("東証", "tse", "tokyo stock")):
        market = "JP"
    else:
        market = "TW" if language == "zh_tw" else "generic"

    # Scanned-PDF detection: >60% of pages have quality < 0.3
    is_scanned = False
    if pages:
        low_quality = sum(1 for p in pages if (p.get("text_quality_score") or 0.0) < 0.3)
        is_scanned = len(pages) > 0 and (low_quality / len(pages)) > 0.6

    return DocumentProfile(
        industry=industry,
        market=market,
        language=language,
        report_type=report_type,
        is_scanned=is_scanned,
    )


def get_section_keywords_for_profile(profile: DocumentProfile, section_no: int) -> tuple[str, ...]:
    """Return the keyword tuple for a section, using the profile's industry keywords with SECTION_KEYWORDS fallback."""
    key = profile.keyword_profile_key()
    profile_map = SECTION_KEYWORDS_BY_PROFILE.get(key, {})
    # Profile-specific override first; fall back to tw_semiconductor defaults then generic SECTION_KEYWORDS
    return (
        profile_map.get(section_no)
        or SECTION_KEYWORDS_BY_PROFILE.get("tw_semiconductor", {}).get(section_no)
        or SECTION_KEYWORDS.get(section_no)
        or ()
    )


ANNUAL_SECTION_PLAN = {
    1: {"status": "partial", "guard": "do_not_generate_facility_terms_from_annual_report"},
    2: {"status": "supported"},
    3: {"status": "partial", "requires_internal_bank_data": ["internal_rating", "MAS_612_grade"]},
    4: {"status": "supported"},
    5: {"status": "not_supported", "required_docs": ["facility agreement", "collateral agreement", "valuation report"]},
    6: {"status": "partial", "not_applicable": ["ship_finance_fields"]},
    7: {"status": "supported", "hard_minimum": list(FINANCIAL_PAGE_TERMS)},
    8: {"status": "partial", "bank_legal_document_status": "not_available_from_annual_report"},
    9: {"status": "supported"},
    10: {"status": "supported"},
}

SECTION7_REQUIRED = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "eps",
    "gross_margin",
    "operating_margin",
    "net_margin",
)

SECTION7_TERM_MAP = {
    "revenue": ("營業收入淨額", "營收"),
    "gross_profit": ("營業毛利",),
    "operating_income": ("營業淨利", "營業利益"),
    "net_income": ("本年度淨利", "稅後淨利"),
    "total_assets": ("資產總額",),
    "total_liabilities": ("負債總額",),
    "total_equity": ("權益總額",),
    "operating_cash_flow": ("營業活動", "營業活動之淨現金流入"),
    "investing_cash_flow": ("投資活動", "投資活動之淨現金流出"),
    "financing_cash_flow": ("籌資活動", "籌資活動之淨現金"),
    "eps": ("每股盈餘", "EPS"),
    "gross_margin": ("毛利率",),
    "operating_margin": ("營業利益率", "營業淨利率"),
    "net_margin": ("純益率", "淨利率"),
}


@dataclass(frozen=True)
class PageScanSummary:
    document_id: str
    total_pages: int
    processed_pages: int
    native_text_pages: int
    table_pages_detected: int
    financial_pages_detected: int
    toc_parsed: bool
    coverage_pct: float

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "total_pages": self.total_pages,
            "processed_pages": self.processed_pages,
            "native_text_pages": self.native_text_pages,
            "table_pages_detected": self.table_pages_detected,
            "financial_pages_detected": self.financial_pages_detected,
            "toc_parsed": self.toc_parsed,
            "coverage_pct": self.coverage_pct,
        }


def _quality_score(text: str) -> float:
    if not text:
        return 0.0
    non_space = len(re.sub(r"\s+", "", text))
    if non_space >= 1200:
        return 0.96
    if non_space >= 400:
        return 0.85
    if non_space >= 80:
        return 0.55
    return 0.2


def _layout_type(text: str) -> str:
    if any(term in text for term in FINANCIAL_PAGE_TERMS):
        return "financial_table_and_narrative"
    if "目錄" in text or re.search(r"\d+\.\d+", text):
        return "toc_or_section_index"
    if len(re.sub(r"\s+", "", text)) < 120:
        return "image_or_low_text"
    return "narrative"


def _printed_page(text: str) -> tuple[str | None, str | None]:
    matches = re.findall(r"(?<!\d)(\d{1,3})(?!\d)", text[-500:])
    if not matches:
        return None, None
    return matches[-1], matches[-1]


def _section_hint(text: str) -> str | None:
    for section_no, keywords in SECTION_KEYWORDS.items():
        if _matches_any_section_keyword(text, keywords, section_no):
            return str(section_no)
    return None


def _table_type(text: str) -> str | None:
    if "財務狀況" in text or all(t in text for t in ("資產總額", "負債總額", "權益總額")):
        return "balance_sheet_summary"
    if "財務績效" in text or all(t in text for t in ("營業收入淨額", "營業毛利", "本年度淨利")):
        return "income_statement_summary"
    if "現金流量" in text or all(t in text for t in ("營業活動", "投資活動", "籌資活動")):
        return "cash_flow_summary"
    return None


def _periods(text: str) -> list[str]:
    values = []
    for year in re.findall(r"民國\s*(\d{2,3})\s*年", text):
        values.append(f"民國{year}年")
    for year in re.findall(r"\b(20\d{2})\b", text):
        values.append(f"FY{year}")
    return sorted(set(values))


def _matches_section_keyword(text: str, keyword: str, section_no: int) -> bool:
    """Return whether text contains a section keyword using safe matching.

    Page text extracted from PDFs often varies casing (for example "Rating",
    "BUY", or "Target Price"), so comparisons are normalized to lower case.
    Short §11 English ratings are matched with word boundaries to avoid broad
    substring hits on unrelated annual-report text such as "holding company".
    """
    if not text or not keyword:
        return False

    normalized_text = re.sub(r"\s+", " ", text).lower()
    normalized_keyword = re.sub(r"\s+", " ", keyword).lower()
    if normalized_keyword in SECTION_WORD_BOUNDARY_KEYWORDS.get(section_no, ()):
        return re.search(rf"(?<!\w){re.escape(normalized_keyword)}(?!\w)", normalized_text) is not None
    return normalized_keyword in normalized_text


def _matches_any_section_keyword(text: str, keywords: tuple[str, ...], section_no: int) -> bool:
    return any(_matches_section_keyword(text, keyword, section_no) for keyword in keywords)


def _docling_extract(file_path: Path, *, do_ocr: bool = False) -> list[dict]:
    """Extract pages via Docling (table structure, optional OCR).

    OCR is disabled by default because modern annual reports carry a native text
    layer — enabling it on CPU adds minutes per document without quality gain.
    Set do_ocr=True only for confirmed image-only (scanned) PDFs.

    Returns the same dict schema as _pypdf_extract so callers are backend-agnostic.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = do_ocr
    opts.do_table_structure = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    result = converter.convert(str(file_path))
    doc = result.document

    # Collect page-level text
    page_texts: dict[int, list[str]] = {}
    for item, _ in doc.iterate_items():
        if not hasattr(item, "text") or not item.text:
            continue
        for prov in getattr(item, "prov", []):
            page_no = prov.page_no
            page_texts.setdefault(page_no, []).append(item.text)

    # Collect table cells per page
    page_cells: dict[int, list[dict]] = {}
    for table in doc.tables:
        for prov in getattr(table, "prov", []):
            page_no = prov.page_no
            cells: list[dict] = []
            grid = getattr(table.data, "grid", None) or []
            for row_idx, row in enumerate(grid):
                for col_idx, cell in enumerate(row):
                    cells.append({
                        "row": cell.start_row_offset_idx,
                        "col": cell.start_col_offset_idx,
                        "row_span": max(cell.row_span, 1),
                        "col_span": max(cell.col_span, 1),
                        "text": cell.text or "",
                    })
            page_cells.setdefault(page_no, []).extend(cells)

    total_pages = len(doc.pages) if doc.pages else max(page_texts.keys(), default=0)
    pages = []
    for page_no in range(1, total_pages + 1):
        text = "\n".join(page_texts.get(page_no, []))
        printed_start, printed_end = _printed_page(text)
        pages.append({
            "pdf_page_no": page_no,
            "printed_page_start": printed_start,
            "printed_page_end": printed_end,
            "native_text": text,
            "merged_text": text,
            "text_quality_score": _quality_score(text),
            "layout_type": _layout_type(text),
            "section_hint": _section_hint(text),
            "table_type": _table_type(text),
            "periods": _periods(text),
            "raw_cells": page_cells.get(page_no, []),
            "extraction_method": "docling",
        })
    return pages


def _pypdf_extract(file_path: Path) -> list[dict]:
    """Fallback extraction using pypdf (native text layer only, no OCR)."""
    from pypdf import PdfReader

    reader = PdfReader(str(file_path))
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        printed_start, printed_end = _printed_page(text)
        pages.append({
            "pdf_page_no": idx,
            "printed_page_start": printed_start,
            "printed_page_end": printed_end,
            "native_text": text,
            "merged_text": text,
            "text_quality_score": _quality_score(text),
            "layout_type": _layout_type(text),
            "section_hint": _section_hint(text),
            "table_type": _table_type(text),
            "periods": _periods(text),
            "raw_cells": [],
            "extraction_method": "pypdf",
        })
    return pages


def extract_pdf_pages(file_path: Path, *, do_ocr: bool = False) -> list[dict]:
    """Extract pages from a PDF using Docling (table structure) with pypdf fallback.

    Pass do_ocr=True for confirmed scanned PDFs (slow on CPU, requires GPU in prod).
    """
    try:
        pages = _docling_extract(file_path, do_ocr=do_ocr)
        if pages:
            return pages
        logger.warning("Docling returned 0 pages for %s — falling back to pypdf", file_path)
    except Exception as exc:
        logger.warning("Docling extraction failed for %s (%s) — falling back to pypdf", file_path, exc)
    return _pypdf_extract(file_path)


async def scan_document_pages(db: AsyncSession, report_id: str, doc: SectionDocument, binary_path: Path) -> PageScanSummary:
    doc.etl_status = "page_scanning"
    await db.flush()

    await db.execute(delete(DocumentBlock).where(DocumentBlock.page_id.in_(select(DocumentPage.id).where(DocumentPage.document_id == doc.id))))
    await db.execute(delete(ExtractedTable).where(ExtractedTable.document_id == doc.id))
    await db.execute(delete(DocumentPage).where(DocumentPage.document_id == doc.id))

    if (doc.file_format or "").lower() != "pdf":
        raise ValueError("Page scanning currently requires a PDF document")

    # Run Docling (CPU-bound ML inference) in a thread pool to avoid blocking
    # the async event loop.  All extract_pdf_pages calls go through this helper.
    loop = asyncio.get_running_loop()

    async def _extract(do_ocr: bool, timeout: float | None = None) -> list[dict]:
        coro = loop.run_in_executor(None, functools.partial(extract_pdf_pages, binary_path, do_ocr=do_ocr))
        if timeout is not None:
            return await asyncio.wait_for(coro, timeout=timeout)
        return await coro

    # First pass: fast extraction (no OCR) to detect profile and check if scanned
    page_payloads = await _extract(do_ocr=False)

    # Detect document profile from first-pass text
    sample_text = "\n".join(p.get("merged_text", "") for p in page_payloads[:20])
    profile = detect_document_profile(sample_text, filename=doc.original_filename, pages=page_payloads)

    # If scanned PDF detected, re-extract with OCR enabled.
    # OCR is CPU-intensive: cap at 5 minutes so the API request never hangs
    # indefinitely.  On GPU hardware in production the same job takes ~30 seconds.
    if profile.is_scanned:
        logger.info("scan_document_pages: scanned PDF detected for doc=%s — re-extracting with OCR (timeout=%.0fs)", doc.id, CR_OCR_TIMEOUT_SECONDS)
        doc.etl_status = "ocr_scanning"
        await db.flush()
        try:
            page_payloads = await _extract(do_ocr=True, timeout=CR_OCR_TIMEOUT_SECONDS)
            # Re-detect profile with OCR-enriched text
            sample_text = "\n".join(p.get("merged_text", "") for p in page_payloads[:20])
            profile = detect_document_profile(sample_text, filename=doc.original_filename, pages=page_payloads)
        except asyncio.TimeoutError:
            logger.warning("OCR timed out after 300s for doc=%s — using first-pass results", doc.id)
        except Exception as exc:
            logger.warning("OCR re-extraction failed for doc=%s (%s) — using first-pass results", doc.id, exc)

    doc.document_profile = profile.as_dict()

    native_text_pages = 0
    table_pages = 0
    financial_pages = 0
    toc_parsed = False

    for payload in page_payloads:
        text = payload["merged_text"] or ""
        page = DocumentPage(
            document_id=doc.id,
            report_id=report_id,
            pdf_page_no=payload["pdf_page_no"],
            printed_page_start=payload["printed_page_start"],
            printed_page_end=payload["printed_page_end"],
            native_text=payload["native_text"],
            merged_text=payload["merged_text"],
            text_quality_score=payload["text_quality_score"],
            layout_type=payload["layout_type"],
            processing_status="processed",
        )
        db.add(page)
        await db.flush()

        extraction_method = payload.get("extraction_method", "pypdf")
        block = DocumentBlock(
            page_id=page.id,
            block_type="table" if payload["table_type"] else "paragraph",
            text=text,
            bbox=None,
            confidence=payload["text_quality_score"],
            extraction_method=extraction_method,
            section_hint=payload["section_hint"],
        )
        db.add(block)

        if text.strip():
            native_text_pages += 1
        if payload["layout_type"] == "toc_or_section_index":
            toc_parsed = toc_parsed or "財務概況" in text or "目錄" in text
        if any(term in text for term in FINANCIAL_PAGE_TERMS):
            financial_pages += 1
        raw_cells = payload.get("raw_cells") or []
        if payload["table_type"] or raw_cells:
            table_pages += 1
            db.add(
                ExtractedTable(
                    document_id=doc.id,
                    page_id=page.id,
                    table_type=payload["table_type"],
                    title=_title_for_table_type(payload["table_type"] or ""),
                    unit="新台幣仟元" if "新台幣" in text or "仟元" in text else None,
                    periods=payload["periods"],
                    raw_cells=raw_cells,
                    normalized_rows=[],
                    extraction_method=extraction_method,
                    confidence=payload["text_quality_score"],
                )
            )

    total_pages = len(page_payloads)
    processed_pages = total_pages
    coverage_pct = 100.0 if total_pages else 0.0
    doc.etl_status = "page_scan_done" if processed_pages == total_pages else "low_coverage_failed"
    await db.commit()

    return PageScanSummary(
        document_id=doc.id,
        total_pages=total_pages,
        processed_pages=processed_pages,
        native_text_pages=native_text_pages,
        table_pages_detected=table_pages,
        financial_pages_detected=financial_pages,
        toc_parsed=toc_parsed,
        coverage_pct=coverage_pct,
    )


def _title_for_table_type(table_type: str) -> str:
    return {
        "balance_sheet_summary": "財務狀況分析",
        "income_statement_summary": "財務績效分析",
        "cash_flow_summary": "現金流量分析",
    }.get(table_type, table_type)


async def plan_document_etl(db: AsyncSession, doc: SectionDocument) -> dict:
    plan = {str(k): dict(v) for k, v in ANNUAL_SECTION_PLAN.items()} if doc.document_type == "annual_report" else {}
    if not plan:
        for section_no in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            plan[str(section_no)] = {"status": "supported" if section_no in (4, 7, 10) else "partial"}
    doc.etl_status = "etl_planned"
    await db.commit()
    return {"document_id": doc.id, "target_sections": plan}


async def get_page_scan_coverage(db: AsyncSession, doc_id: str) -> dict:
    result = await db.execute(select(DocumentPage).where(DocumentPage.document_id == doc_id))
    pages = list(result.scalars().all())
    total = len(pages)
    processed = sum(1 for p in pages if p.processing_status == "processed")
    native = sum(1 for p in pages if (p.native_text or "").strip())
    table = sum(1 for p in pages if p.layout_type == "financial_table_and_narrative")
    return {
        "document_id": doc_id,
        "total_pages": total,
        "processed_pages": processed,
        "native_text_pages": native,
        "table_pages_detected": table,
        "coverage_pct": round(processed * 100 / total, 2) if total else 0.0,
    }


async def select_pages_for_section(
    db: AsyncSession,
    doc_id: str,
    section_no: int,
    limit: int = 24,
    profile: DocumentProfile | None = None,
) -> list[DocumentPage]:
    result = await db.execute(select(DocumentPage).where(DocumentPage.document_id == doc_id).order_by(DocumentPage.pdf_page_no))
    pages = list(result.scalars().all())
    if not pages:
        return []

    # Resolve profile: caller may pass it; otherwise load from the document record
    if profile is None:
        doc_result = await db.execute(
            select(SectionDocument).where(SectionDocument.id == doc_id)
        )
        doc = doc_result.scalar_one_or_none()
        if doc and doc.document_profile:
            profile = DocumentProfile.from_dict(doc.document_profile)

    keywords = (
        get_section_keywords_for_profile(profile, section_no)
        if profile is not None
        else SECTION_KEYWORDS.get(section_no, ())
    )

    selected = [
        p for p in pages
        if _matches_any_section_keyword(p.merged_text or "", keywords, section_no)
    ]
    if section_no == 10:
        selected = pages[: min(5, len(pages))] + selected
    if not selected and section_no in {4, 7, 11}:
        selected = pages
    dedup = []
    seen = set()
    for page in selected:
        if page.id in seen:
            continue
        dedup.append(page)
        seen.add(page.id)
        if len(dedup) >= limit:
            break
    return dedup


def build_page_bound_chunks(pages: Iterable[DocumentPage], max_chars: int = 24000) -> str:
    chunks = []
    used = 0
    for page in pages:
        text = (page.merged_text or "").strip()
        if not text:
            continue
        marker = f"\n\n--- PDF_PAGE {page.pdf_page_no} PRINTED_PAGE {page.printed_page_start or '?'} ---\n"
        addition = marker + text
        if used + len(addition) > max_chars and chunks:
            break
        chunks.append(addition)
        used += len(addition)
    return "".join(chunks).strip()


def validate_annual_report_gates(text: str, section_no: int | None = None) -> dict:
    if section_no not in (None, 7):
        return {"passed": True, "missing": [], "coverage_score": 1.0}
    missing = [key for key, terms in SECTION7_TERM_MAP.items() if not any(term in text for term in terms)]
    coverage = (len(SECTION7_REQUIRED) - len(missing)) / len(SECTION7_REQUIRED)
    return {"passed": coverage >= 0.8, "missing": missing, "coverage_score": round(coverage, 4)}


def _flatten_json_terms(value: object, prefix: str = "") -> list[str]:
    terms: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = f"{prefix}.{key}" if prefix else str(key)
            terms.append(key_text)
            terms.extend(_flatten_json_terms(nested, key_text))
    elif isinstance(value, list):
        for item in value:
            terms.extend(_flatten_json_terms(item, prefix))
    elif value is not None:
        terms.append(str(value))
    return terms


def _proposal_contains_any(haystack: str, terms: tuple[str, ...]) -> bool:
    lowered = haystack.lower()
    return any(term.lower() in lowered for term in terms)


def validate_section_proposal_gates(
    section_no: int,
    proposed_json: dict | None,
    source_gate: dict | None = None,
) -> dict:
    """Validate extracted proposal payloads before they become reviewable.

    Source-page coverage alone is not enough: the failure mode we are guarding
    against is an annual-report ETL run that sees the right pages but still only
    extracts identity fields such as company name and chairman.  Section 7 must
    therefore contain recognizable financial metrics in the proposed JSON before
    Smart Import can present or commit it.
    """
    source_gate = source_gate or {"passed": True, "missing": [], "coverage_score": 1.0}
    missing = list(source_gate.get("missing") or [])
    source_score = float(source_gate.get("coverage_score") or 0.0)

    payload = proposed_json if isinstance(proposed_json, dict) else {}
    if not payload:
        return {
            "passed": False,
            "missing": sorted(set(missing + ["no_extracted_data"])),
            "coverage_score": 0.0,
            "extracted_metric_score": 0.0,
            "missing_extracted_metrics": ["no_extracted_data"],
        }

    if section_no != 7:
        return {
            "passed": bool(source_gate.get("passed", True)),
            "missing": missing,
            "coverage_score": round(source_score, 4),
            "extracted_metric_score": 1.0,
            "missing_extracted_metrics": [],
        }

    haystack = "\n".join(_flatten_json_terms(payload))
    extracted_missing = [
        key for key, terms in SECTION7_TERM_MAP.items()
        if not _proposal_contains_any(haystack, (key, *terms))
    ]
    extracted_score = (len(SECTION7_REQUIRED) - len(extracted_missing)) / len(SECTION7_REQUIRED)

    # Require the source pages to pass the hard page gate and require the LLM
    # output to include a majority of Section 7 financial metric families.  This
    # deliberately rejects false-success payloads containing only corporate
    # identity/person fields even when source text is available.
    passed = bool(source_gate.get("passed", True)) and extracted_score >= 0.5
    combined_score = min(source_score, extracted_score)
    return {
        "passed": passed,
        "missing": sorted(set(missing + extracted_missing)),
        "coverage_score": round(combined_score, 4),
        "source_coverage_score": round(source_score, 4),
        "extracted_metric_score": round(extracted_score, 4),
        "missing_extracted_metrics": extracted_missing,
    }


def is_probably_annual_report(filename: str | None = None, text: str | None = None) -> bool:
    """Detect annual reports even when the upload type was selected wrongly."""
    filename_text = (filename or "").lower()
    if any(token in filename_text for token in ("annual report", "annual-report", "年報")):
        return True
    sample = (text or "")[:12000]
    annual_terms = ("年報", "annual report", "致股東報告書", "公司年報", "財務概況")
    tsmc_terms = ("台灣積體電路", "台積電", "tsmc")
    financial_terms = ("資產總額", "營業收入淨額", "每股盈餘", "現金流量")
    return (
        any(term.lower() in sample.lower() for term in annual_terms)
        and (any(term.lower() in sample.lower() for term in tsmc_terms) or sum(term in sample for term in financial_terms) >= 2)
    )


async def create_section_import_proposal(
    db: AsyncSession,
    report_id: str,
    doc_id: str,
    section_no: int,
    proposed_json: dict,
    source_pages: list[int],
    coverage_score: float,
    missing_required_fields: list[str] | None = None,
) -> SectionImportProposal:
    source_gate = {
        "passed": coverage_score >= 0.8 or section_no != 7,
        "missing": missing_required_fields or [],
        "coverage_score": coverage_score,
    }
    proposal_gate = validate_section_proposal_gates(section_no, proposed_json, source_gate)
    evidence = {
        "document_id": doc_id,
        "source_pages": source_pages,
        "source_citation_required": True,
        "coverage_gate": proposal_gate,
    }
    proposal = SectionImportProposal(
        report_id=report_id,
        section_no=section_no,
        proposed_json=proposed_json,
        evidence_map=evidence,
        coverage_score=proposal_gate["coverage_score"],
        missing_required_fields=proposal_gate["missing"],
        status="ready_for_review" if proposal_gate["passed"] else "low_coverage_failed",
    )
    db.add(proposal)
    await db.flush()
    return proposal


async def commit_section_import_proposal(db: AsyncSession, proposal: SectionImportProposal, user_id: str | None) -> SectionInput:
    result = await db.execute(
        select(SectionInput).where(SectionInput.report_id == proposal.report_id, SectionInput.section_no == proposal.section_no)
    )
    section_input = result.scalar_one_or_none()
    payload = proposal.proposed_json or {}
    if section_input:
        existing = json.loads(section_input.input_json or "{}")
        existing.update(payload)
        section_input.input_json = json.dumps(existing, ensure_ascii=False)
        section_input.saved_by = user_id
    else:
        section_input = SectionInput(
            report_id=proposal.report_id,
            section_no=proposal.section_no,
            input_json=json.dumps(payload, ensure_ascii=False),
            saved_by=user_id,
        )
        db.add(section_input)
    proposal.status = "committed"
    return section_input

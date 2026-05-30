"""Page-first document ETL pipeline primitives.

This module intentionally keeps deterministic document coverage, page manifests,
section planning, and Smart Import proposals separate from the LLM extractor.  The
LLM should see small page-bound chunks, never an arbitrary leading slice of an
annual report.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.generation.models import (
    CandidateFact,
    DocumentBlock,
    DocumentPage,
    ExtractedTable,
    SectionDocument,
    SectionImportProposal,
)
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
        if any(k in text for k in keywords):
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


def extract_pdf_pages(file_path: Path) -> list[dict]:
    reader = PdfReader(str(file_path))
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        native_text = page.extract_text() or ""
        printed_start, printed_end = _printed_page(native_text)
        pages.append(
            {
                "pdf_page_no": idx,
                "printed_page_start": printed_start,
                "printed_page_end": printed_end,
                "native_text": native_text,
                "merged_text": native_text,
                "text_quality_score": _quality_score(native_text),
                "layout_type": _layout_type(native_text),
                "section_hint": _section_hint(native_text),
                "table_type": _table_type(native_text),
                "periods": _periods(native_text),
            }
        )
    return pages


async def scan_document_pages(db: AsyncSession, report_id: str, doc: SectionDocument, binary_path: Path) -> PageScanSummary:
    doc.etl_status = "page_scanning"
    await db.flush()

    await db.execute(delete(DocumentBlock).where(DocumentBlock.page_id.in_(select(DocumentPage.id).where(DocumentPage.document_id == doc.id))))
    await db.execute(delete(ExtractedTable).where(ExtractedTable.document_id == doc.id))
    await db.execute(delete(DocumentPage).where(DocumentPage.document_id == doc.id))

    if (doc.file_format or "").lower() != "pdf":
        raise ValueError("Page scanning currently requires a PDF document")

    page_payloads = extract_pdf_pages(binary_path)
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

        block = DocumentBlock(
            page_id=page.id,
            block_type="table" if payload["table_type"] else "paragraph",
            text=text,
            bbox=None,
            confidence=payload["text_quality_score"],
            extraction_method="native_text",
            section_hint=payload["section_hint"],
        )
        db.add(block)

        if text.strip():
            native_text_pages += 1
        if payload["layout_type"] == "toc_or_section_index":
            toc_parsed = toc_parsed or "財務概況" in text or "目錄" in text
        if any(term in text for term in FINANCIAL_PAGE_TERMS):
            financial_pages += 1
        if payload["table_type"]:
            table_pages += 1
            db.add(
                ExtractedTable(
                    document_id=doc.id,
                    page_id=page.id,
                    table_type=payload["table_type"],
                    title=_title_for_table_type(payload["table_type"]),
                    unit="新台幣仟元" if "新台幣" in text or "仟元" in text else None,
                    periods=payload["periods"],
                    raw_cells=[],
                    normalized_rows=[],
                    extraction_method="native_text_heuristic",
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


async def select_pages_for_section(db: AsyncSession, doc_id: str, section_no: int, limit: int = 24) -> list[DocumentPage]:
    result = await db.execute(select(DocumentPage).where(DocumentPage.document_id == doc_id).order_by(DocumentPage.pdf_page_no))
    pages = list(result.scalars().all())
    if not pages:
        return []
    keywords = SECTION_KEYWORDS.get(section_no, ())
    selected = [p for p in pages if any(k in (p.merged_text or "") for k in keywords)]
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

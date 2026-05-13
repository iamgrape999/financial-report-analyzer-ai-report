from __future__ import annotations

import asyncio
import json
import logging
import uuid
from functools import partial
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Form

from credit_report.config import CREDIT_REPORT_MAX_UPLOAD_MB, GEMINI_API_KEY
from credit_report.database import get_db
from credit_report.generation.evidence import extract_text_from_file, save_document_binary, save_document_text
from credit_report.generation.etl import DOCUMENT_SECTION_MAP, etl_document
from credit_report.generation.models import SectionDocument
from credit_report.generation.pipeline import (
    check_hard_dependencies,
    get_section_output,
    run_full_report_generation,
    run_section_generation,
)
from credit_report.models import Report, SectionInput, SectionOutput
from credit_report.security.auth import get_current_user, require_analyst
from credit_report.security.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/{report_id}", tags=["generation"])

_MAX_UPLOAD_BYTES = CREDIT_REPORT_MAX_UPLOAD_MB * 1024 * 1024


# ── Schemas ──────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {
    "pdf", "docx", "doc", "pptx", "ppt", "txt", "csv", "md",
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
    "xlsx",
}

DOCUMENT_TYPES = [
    "annual_report", "financial_statement", "analyst_presentation",
    "interim_report", "valuation_report", "charter_agreement",
    "shipbuilding_contract", "kyc_document", "legal_document",
    "external_report", "other",
]


class DocumentOut(BaseModel):
    id: str
    original_filename: str
    file_size_bytes: int
    document_type: Optional[str] = None
    file_format: Optional[str] = None
    etl_status: Optional[str] = None

    model_config = {"from_attributes": True}


class ETLResult(BaseModel):
    doc_id: str
    document_type: str
    sections_extracted: list[int]
    data: dict[str, dict]  # {str(section_no): {field: value}}


class SectionJsonImportResult(BaseModel):
    section_no: int
    fields_imported: int
    message: str


class SectionOutputOut(BaseModel):
    section_no: int
    status: str
    model_id: Optional[str] = None
    tokens_used: Optional[int] = None
    generated_at: Optional[str] = None
    markdown: Optional[str] = None

    model_config = {"from_attributes": True}


class GenerateOneResult(BaseModel):
    section_no: int
    status: str
    tokens_used: Optional[int] = None


class GenerateAllResult(BaseModel):
    sections: dict[str, str]


class GenerateTaskResult(BaseModel):
    task_id: str
    status: str           # "running" | "done" | "error"
    section_no: Optional[int] = None
    tokens_used: Optional[int] = None
    sections: Optional[dict[str, str]] = None  # for full-report tasks
    detail: Optional[str] = None


# In-memory task registry — acceptable for single-instance deployments.
# Entries are never evicted; memory usage is bounded by restart cadence.
_generation_tasks: dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _require_report(db: AsyncSession, report_id: str) -> Report:
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.is_deleted == False)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def _assert_owner_or_admin(report: Report, current_user: User) -> None:
    """Raise 403 if the user is not the report creator and not an admin."""
    if current_user.role == "admin":
        return
    if report.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this report.",
        )


def _assert_can_view(report: Report, current_user: User) -> None:  # noqa: F811
    if current_user.role in {"admin", "reviewer", "approver"}:
        return
    if report.created_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You do not have permission to view this report.")


def _output_to_schema(o: SectionOutput) -> SectionOutputOut:
    return SectionOutputOut(
        section_no=o.section_no,
        status=o.status,
        model_id=o.model_id,
        tokens_used=o.tokens_used,
        generated_at=o.generated_at.isoformat() if o.generated_at else None,
        markdown=o.markdown,
    )


async def _load_section_input(db: AsyncSession, report_id: str, section_no: int) -> dict:
    """Return parsed input_json for a section, or empty dict if not saved."""
    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        )
    )
    si = result.scalar_one_or_none()
    if not si or not si.input_json:
        return {}
    try:
        return json.loads(si.input_json)
    except Exception:
        return {}


# ── Document management ───────────────────────────────────────────────────────

@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    report_id: str,
    file: UploadFile = File(...),
    document_type: str = Form(default="other"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Upload a document (PDF, DOCX, PPTX, TXT, JPG, PNG, etc.) for a report."""
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    if document_type not in DOCUMENT_TYPES:
        document_type = "other"

    fname = (file.filename or "upload").strip()
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        logger.warning("upload_document: file too large bytes=%d limit=%dMB report=%s", len(file_bytes), CREDIT_REPORT_MAX_UPLOAD_MB, report_id)
        raise HTTPException(status_code=413, detail=f"File exceeds the {CREDIT_REPORT_MAX_UPLOAD_MB} MB upload limit")

    doc_id = str(uuid.uuid4())
    logger.info("upload_document: extracting text file=%r type=%s bytes=%d doc=%s report=%s", fname, document_type, len(file_bytes), doc_id, report_id)
    loop = asyncio.get_event_loop()
    text, detected_fmt = await loop.run_in_executor(None, partial(extract_text_from_file, file_bytes, fname))
    save_document_text(report_id, doc_id, text)
    save_document_binary(report_id, doc_id, file_bytes, fname)
    logger.info("upload_document: saved doc=%s fmt=%s chars=%d report=%s user=%s", doc_id, detected_fmt, len(text), report_id, current_user.id)

    doc = SectionDocument(
        id=doc_id,
        report_id=report_id,
        original_filename=fname,
        file_size_bytes=len(file_bytes),
        document_type=document_type,
        file_format=detected_fmt,
        etl_status="pending",
        uploaded_by=current_user.id,
    )
    db.add(doc)
    await db.flush()
    return doc


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
    result = await db.execute(
        select(SectionDocument)
        .where(SectionDocument.report_id == report_id, SectionDocument.is_deleted == False)
        .order_by(SectionDocument.uploaded_at.desc())
    )
    return list(result.scalars().all())


@router.post("/documents/{doc_id}/etl", response_model=ETLResult)
async def etl_document_endpoint(
    report_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Run AI ETL on an uploaded document — extracts structured data for each relevant section.

    Returns extracted field values per section so the UI can show them and let the
    analyst review/save them as section inputs.
    """
    from credit_report.generation.evidence import load_document_texts, save_document_text
    from pathlib import Path
    from credit_report.config import CREDIT_REPORTS_ROOT

    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)

    result = await db.execute(
        select(SectionDocument).where(
            SectionDocument.id == doc_id,
            SectionDocument.report_id == report_id,
            SectionDocument.is_deleted == False,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Load extracted text — if .txt is missing, re-extract from stored binary (server restart recovery)
    doc_dir = CREDIT_REPORTS_ROOT / report_id
    txt_path = doc_dir / f"{doc_id}.txt"
    if not txt_path.exists():
        bin_path = doc_dir / f"{doc_id}.bin"
        if bin_path.exists():
            fname_path = doc_dir / f"{doc_id}.fname"
            stored_fname = (
                fname_path.read_text(encoding="utf-8")
                if fname_path.exists()
                else (doc.original_filename or "upload.pdf")
            )
            logger.info(
                "etl_document_endpoint: .txt missing, re-extracting from binary doc=%s file=%r report=%s",
                doc_id, stored_fname, report_id,
            )
            loop = asyncio.get_event_loop()
            reextracted_text, _ = await loop.run_in_executor(
                None, partial(extract_text_from_file, bin_path.read_bytes(), stored_fname)
            )
            save_document_text(report_id, doc_id, reextracted_text)
        else:
            raise HTTPException(
                status_code=422,
                detail="Document file not found on server — please delete and re-upload this document",
            )

    text = txt_path.read_text(encoding="utf-8")
    if not text.strip():
        raise HTTPException(status_code=422, detail="Document appears to have no extractable text")

    doc_type = doc.document_type or "other"
    logger.info("etl_document_endpoint: doc=%s type=%s chars=%d report=%s user=%s", doc_id, doc_type, len(text), report_id, current_user.id)

    try:
        extracted = await etl_document(text=text, document_type=doc_type)
    except ValueError as exc:
        logger.warning("etl_document_endpoint: config error doc=%s: %s", doc_id, exc)
        doc.etl_status = "error"
        await db.commit()  # commit before raise — get_db rolls back on exception
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("etl_document_endpoint: ETL failed doc=%s: %s", doc_id, exc)
        doc.etl_status = "error"
        await db.commit()  # commit before raise — get_db rolls back on exception
        raise HTTPException(status_code=500, detail=f"ETL extraction failed: {exc}")

    doc.etl_status = "done"

    return ETLResult(
        doc_id=doc_id,
        document_type=doc_type,
        sections_extracted=sorted(extracted.keys()),
        data={str(k): v for k, v in extracted.items()},
    )


@router.post("/import-section-json", response_model=SectionJsonImportResult)
async def import_section_json(
    report_id: str,
    section_no: int = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Import a structured JSON file directly as section input data.

    Accepts a JSON file (e.g. financial-analysis.json) with field-value pairs and
    saves them as the SectionInput for the given section_no.  Existing input is
    merged (JSON-merged, file wins on conflict).
    """
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)

    if section_no < 1 or section_no > 11:
        raise HTTPException(status_code=400, detail="section_no must be 1-11")

    raw = await file.read()
    try:
        payload: dict = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON root must be an object")

    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == report_id,
            SectionInput.section_no == section_no,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        try:
            current_data: dict = json.loads(existing.input_json or "{}")
        except Exception:
            current_data = {}
        current_data.update(payload)
        existing.input_json = json.dumps(current_data, ensure_ascii=False)
        await db.flush()
        logger.info(
            "import_section_json: merged section=%d fields=%d report=%s user=%s",
            section_no, len(payload), report_id, current_user.id,
        )
    else:
        db.add(SectionInput(
            report_id=report_id,
            section_no=section_no,
            input_json=json.dumps(payload, ensure_ascii=False),
        ))
        await db.flush()
        logger.info(
            "import_section_json: created section=%d fields=%d report=%s user=%s",
            section_no, len(payload), report_id, current_user.id,
        )

    return SectionJsonImportResult(
        section_no=section_no,
        fields_imported=len(payload),
        message=f"Imported {len(payload)} fields into section {section_no}",
    )


# ── Natural Language Input Conversion ────────────────────────────────────────

class NaturalInputConvert(BaseModel):
    natural_text: str
    section_no: int


class NaturalInputResult(BaseModel):
    section_no: int
    converted_json: dict


_SECTION_SCHEMAS: dict[int, dict] = {
    1: {
        "name": "Facility Structure & Key Terms",
        "fields": (
            "borrower (string), guarantors (string[]), "
            "all_facilities (array of {item,borrower,booking_office,proposed_facility_usd_m,ccy,tenor,facility_type,collateral,guarantor,is_new,outstanding_usd_m}), "
            "facility_type, facility_amount_usd_m (number), ltc_percent (number), tenor_years (number), "
            "tenor_structure (e.g. '4+7'), purpose, repayment_schedule, balloon_percent (number), "
            "interest_rate_basis, margin_bps (number), upfront_fee_pct (number), "
            "security_pre_delivery, security_post_delivery, "
            "value_maintenance_clause {acr_minimum_pct, ltv_maximum_pct, testing_frequency, cure_period_days, cure_mechanism}, "
            "sustainability_linked_kpi {description, max_margin_ratchet_bps}, "
            "sll_kpi_performance {kpis: array of {kpi_name, target_value, actual_value, period, on_track, ratchet_bps}}, "
            "regulatory_compliance {bank_net_worth_twd_bn, single_borrower_limit_twd_bn, usd_equivalent_usd_m, compliance_status, unsecured_drawdown_cap_usd_m}, "
            "group_limit {approved_group_limit_usd_m, total_proposed_group_utilization_usd_m, within_limit}, "
            "conditions_precedent (JSON array of {no, condition} — T&C Field #13), "
            "deal_comparison (JSON array of {term, proposed, previous} — min 11 columns: "
            "Guarantor, Facility Amount, Purpose, Vessel Type, Tenor, Margin, Upfront Fee, SLL Ratchet, Drawdowns, Availability Period, Security, FMV Maintenance), "
            "account_strategy {wallet: {bank_market, capital_market, treasury, deposit}, "
            "current_relationship, immediate_opportunities, future_opportunities, other_opportunities}, "
            "governing_law"
        ),
    },
    2: {
        "name": "Overall Comments",
        "fields": (
            "credit_decision (APPROVE or DECLINE), "
            "credit_overview_table (array of {topic, comment}), "
            "key_strengths (string[]), "
            "key_risks (array of {risk, mitigant, residual}), "
            "borrower_metrics {entity, currency, total_assets_bn, total_equity_bn, cash_bn, ebitda_bn, de_ratio, interest_coverage, revenue_bn, net_income_bn}, "
            "guarantor_metrics {entity, currency, cash_twd_bn, ebitda_twd_bn, de_ratio, interest_coverage, as_of_date}, "
            "market_index, market_index_level (number), ltc_percent, acr_min_pct, ltv_max_pct, "
            "conditions_precedent (string[])"
        ),
    },
    3: {
        "name": "Credit Risk Assessment",
        "fields": (
            "internal_rating (e.g. BB+), rating_rationale, "
            "pd_basis_points (number), pd_assessment, lgd_percent (number), el_bps (number), "
            "mas_612_applicable (bool), mas_612_classification (e.g. Performing), "
            "industry_risk_level, industry_risk_rationale, "
            "esg_category (A/B/C/D), esg_screening, poseidon_principles_aligned (bool), climate_risk, "
            "sanctions_ofac, sanctions_eu, sanctions_mas, sanctions_un, sanctions_hm_treasury, "
            "ofac_screening_date, screening_system, "
            "key_risks (string[]), mitigants (string[]), "
            "risk_matrix (array of {risk, probability, impact, rating, mitigation}), "
            "concentration_risk, country_risk"
        ),
    },
    4: {
        "name": "Borrower Background",
        "fields": (
            "company_name, legal_entity_type, registration_number, incorporation_date, incorporation_country, "
            "registered_address, group_structure, ultimate_beneficial_owner, "
            "shareholders (array of {name, stake_percent, country}), "
            "board_of_directors (array of {name, role}), "
            "key_management (array of {name, title, years_experience, background}), "
            "business_description, primary_business, years_in_operation, "
            "fleet_owned (array of {vessel, type, dwt, year_built, flag, class, charter}), "
            "annual_revenue_usd_m (number), ebitda_usd_m (number), net_income_usd_m (number), "
            "market_position, competitive_advantages (string[]), group_auditor, "
            "banking_relationships (array of {bank, product})"
        ),
    },
    5: {
        "name": "Collateral Assessment",
        "fields": (
            "collateral_type, "
            "vessel_valuations (array of {vessel, dwt, year_built_expected, flag, class, market_value_usd_m, distressed_value_usd_m, valuation_date, valuer, valuation_basis}), "
            "contract_price_usd_m (number), loan_amount_usd_m (number), ltc_percent (number), "
            "acr_at_delivery_percent (number), ltv_at_maturity_percent (number), "
            "refund_guarantee {issuer, issuer_rating, amount_usd_m, expiry, covers}, "
            "insurance {h_and_m, p_and_i, war_risk}, "
            "additional_security (string[]), "
            "ltv_schedule (array of {period, outstanding_usd_m, market_value_usd_m, ltv_pct, acr_pct}), "
            "collateral_adequacy_conclusion"
        ),
    },
    6: {
        "name": "Project Overview — Ship Finance",
        "fields": (
            "project_type, vessel_name, hull_number, vessel_type, vessel_class, "
            "dwt (number), grt (number), loa_m (number), main_engine, speed_knots (number), "
            "eco_design (bool), imo_tier, cii_rating_expected, "
            "shipyard, shipyard_country, class_society, flag_state, "
            "contract_date, contract_price_usd_m (number), "
            "payment_milestones (array of {milestone, date, pct, amount_usd_m, bank_funded}), "
            "delivery_date, construction_risk_assessment, "
            "employment_post_delivery, charterer, charterer_credit_rating, "
            "charter_rate_usd_day (number), charter_duration_years (number), "
            "charter_type, charter_governing_law"
        ),
    },
    7: {
        "name": "Financial Analysis",
        "fields": (
            "reporting_currency, unit, auditor, audit_opinion, accounting_standard, fiscal_year_end, "
            "income_statement {FY2022, FY2023, FY2024} each containing {revenue, opex, ebitda, depreciation, ebit, interest_expense, pbt, tax, net_income}, "
            "balance_sheet {FY2022, FY2023, FY2024} each containing {cash, current_assets, total_assets, short_term_debt, long_term_debt, total_debt, equity}, "
            "cash_flow {FY2022, FY2023, FY2024} each containing {cfo, capex, cfi, cff, net_change}, "
            "key_ratios {FY2022, FY2023, FY2024} each containing {dscr, debt_ebitda, debt_equity, current_ratio, net_margin_pct, roa_pct, roe_pct, interest_cover}"
        ),
    },
    8: {
        "name": "Legal Documentation & Charges",
        "fields": (
            "facility_agreement {type, date, parties (string[]), amount_usd_m}, "
            "security_documents (array of {doc, status, amount_usd_m (optional), expiry (optional)}), "
            "existing_charges (array of {charge, beneficiary, amount_usd_m, maturity}), "
            "cross_default_threshold_usd_m (number), pari_passu_clause (bool), negative_pledge (bool), "
            "legal_opinions (array of {jurisdiction, law_firm, date}), "
            "legal_counsel_borrower, legal_counsel_bank, governing_law, dispute_resolution, "
            "registration_requirements, "
            "conditions_precedent (string[]), conditions_subsequent (string[])"
        ),
    },
    9: {
        "name": "Compliance Checklist",
        "fields": (
            "customer_acceptance_date, "
            "kyc {completed (bool), completion_date, next_review_date, kyc_tier, cdd_level, refresh_cycle_months, documents_received (string[])}, "
            "aml {cleared (bool), screening_date, adverse_media}, "
            "sanctions {ofac, eu, mas, un, hm_treasury, ofsi, screening_date, screening_system}, "
            "pep {status, related_pep (bool), pep_declaration_received (bool)}, "
            "tax_compliance {country, fatca_classification, crs_status}, "
            "environmental {eexi, cii, poseidon_principles, imo_2030_readiness}, "
            "mas_regulations {banking_act_s33_3, concentration_pct}, "
            "internal_approvals_required (string[])"
        ),
    },
    10: {
        "name": "Appendix",
        "fields": (
            "dscr_projections (array of {year, period, revenue_usd_m, opex_usd_m, ebitda_usd_m, depreciation_usd_m, interest_usd_m, principal_usd_m, debt_service_usd_m, dscr, outstanding_balance_usd_m}), "
            "fleet_schedule (array of {vessel, type, dwt, year_built, flag, class, current_charter, market_value_usd_m, existing_mortgage}), "
            "sensitivity_analysis (array of {scenario, assumption, min_dscr, conclusion}), "
            "loan_repayment_schedule (array of {period, principal, interest, total, balance}), "
            "market_overview (string), "
            "glossary {DSCR, LTC, ACR, LTV} (string definitions), "
            "references (string[])"
        ),
    },
    11: {
        "name": "Analyst / External Research Report",
        "fields": (
            "analyst_firm, analyst_name, report_date, report_title, "
            "subject_company_en, subject_company_zh, subject_ticker, subject_exchange, "
            "current_rating (Buy/Hold/Sell), target_price_12m (number), target_price_currency, "
            "current_price (number), upside_pct (number), risk_rating, "
            "rating_rationale_verbatim, "
            "investment_thesis {summary_verbatim, bull_points (string[]), bear_points (string[]), key_catalysts (string[]), risks (string[])}, "
            "income_statement {currency, unit, periods (array of {year, is_forecast, revenue, ebitda, net_income, eps})}, "
            "balance_sheet {currency, unit, periods (array of {year, is_forecast, cash, total_assets, total_equity, net_debt})}, "
            "cash_flow {currency, unit, periods (array of {year, is_forecast, ocf, icf, fcf, capex})}, "
            "ratio_analysis {periods (array of {year, gross_margin_pct, ebitda_margin_pct, roe_pct, interest_coverage, per, pbr})}, "
            "valuation_metrics {target_nav_per_share_twd, per_current, pbr_current, ev_ebitda_current, peer_comparison[]}, "
            "esg {esg_overall_score, esg_risk_level, carbon_intensity_gco2_per_teu_nm, cii_rating, lng_vessels_in_fleet}, "
            "industry_context {ccfi_current, scfi_current, global_orderbook_pct_of_fleet, key_macro_risks (string[]), key_tailwinds (string[]), forward_outlook_narrative}"
        ),
    },
}


@router.post("/sections/{section_no}/convert-natural", response_model=NaturalInputResult)
async def convert_natural_input(
    report_id: str,
    section_no: int,
    payload: NaturalInputConvert,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    AI-powered conversion: natural language text → structured JSON for a section.
    Does NOT save — caller must PUT /inputs/{section_no} to persist.
    """
    import re as _re
    from credit_report.generation.claude_client import call_gemini_raw

    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)

    if section_no not in _SECTION_SCHEMAS:
        raise HTTPException(status_code=400, detail="section_no must be 1–11")

    schema = _SECTION_SCHEMAS[section_no]

    system_prompt = (
        "You are a credit analyst data extraction assistant for Cathay United Bank (國泰聯合銀行). "
        f"The user provides natural-language notes (Chinese or English) for credit report "
        f"Section {section_no}: {schema['name']}. "
        "Extract the data and return ONLY valid JSON — no markdown fences, no explanation.\n\n"
        f"Required JSON fields (use null for anything not mentioned):\n{schema['fields']}\n\n"
        "Strict rules:\n"
        "- Root must be a JSON object {{}}\n"
        "- Monetary amounts in USD millions unless user says otherwise\n"
        "- Arrays even when only one item mentioned\n"
        "- Preserve verbatim text exactly for fields ending in _verbatim\n"
        "- Booleans as true/false, dates as YYYY-MM-DD, numbers as numeric\n"
        "- Chinese inputs: extract values correctly, keep names in original script"
    )
    user_prompt = f"Section {section_no} data:\n\n{payload.natural_text}"

    try:
        raw = await call_gemini_raw(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.error(
            "convert_natural_input: AI failed section=%d report=%s: %s",
            section_no, report_id, exc,
        )
        raise HTTPException(status_code=503, detail=f"AI generation failed: {exc}")

    # Strip optional markdown fences the model may add
    cleaned = raw.strip()
    cleaned = _re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = _re.sub(r"\n?```\s*$", "", cleaned).strip()

    try:
        converted: dict = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "convert_natural_input: invalid JSON section=%d: %s — raw=%r",
            section_no, exc, cleaned[:300],
        )
        raise HTTPException(
            status_code=422,
            detail=f"AI returned invalid JSON: {exc}. Preview: {cleaned[:300]}",
        )

    if not isinstance(converted, dict):
        raise HTTPException(status_code=422, detail="AI returned non-object JSON root")

    logger.info(
        "convert_natural_input: section=%d report=%s user=%s fields=%d",
        section_no, report_id, current_user.id, len(converted),
    )
    return NaturalInputResult(section_no=section_no, converted_json=converted)


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    report_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)
    result = await db.execute(
        select(SectionDocument).where(
            SectionDocument.id == doc_id,
            SectionDocument.report_id == report_id,
            SectionDocument.is_deleted == False,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        logger.warning("delete_document: not found doc=%s report=%s user=%s", doc_id, report_id, current_user.id)
        raise HTTPException(status_code=404, detail="Document not found")
    doc.is_deleted = True
    await db.flush()
    logger.info("delete_document: soft-deleted doc=%s report=%s user=%s", doc_id, report_id, current_user.id)


# ── Section generation ────────────────────────────────────────────────────────

@router.get("/generate/status/{task_id}", response_model=GenerateTaskResult)
async def generation_task_status(
    report_id: str,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Poll the status of a background generation task."""
    task = _generation_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found or server was restarted")
    return GenerateTaskResult(task_id=task_id, **task)


@router.post("/generate/{section_no}", status_code=202, response_model=GenerateTaskResult)
async def generate_section(
    report_id: str,
    section_no: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Trigger AI generation for a single section (runs as background task).

    Returns 202 immediately with a task_id. Poll GET /generate/status/{task_id}
    for completion. Returns 409 if hard dependencies are not yet generated.
    """
    if section_no < 1 or section_no > 10:
        raise HTTPException(status_code=400, detail="section_no must be 1–10")

    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    # Fast preflight: dependency check (uses request session for immediate 409 response)
    missing = await check_hard_dependencies(db, report_id, section_no)
    if missing:
        logger.info("generate_section: blocked on hard deps=%s section=%d report=%s", missing, section_no, report_id)
        raise HTTPException(
            status_code=409,
            detail=f"Hard dependencies not yet generated: sections {missing}",
        )

    task_id = str(uuid.uuid4())
    _generation_tasks[task_id] = {"status": "running", "section_no": section_no}
    user_id, user_role = current_user.id, current_user.role

    async def _bg_generate_section():
        from credit_report.database import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_db:
            try:
                # Reload preceding outputs in background context for freshest data
                preceding: dict[int, str] = {}
                for n in range(1, 11):
                    if n == section_no:
                        continue
                    ctx = await get_section_output(bg_db, report_id, n)
                    if ctx and ctx.status == "done" and ctx.markdown:
                        preceding[n] = ctx.markdown

                logger.info(
                    "generate_section[bg]: starting section=%d report=%s user=%s preceding=%s",
                    section_no, report_id, user_id, list(preceding.keys()),
                )
                output = await run_section_generation(
                    db=bg_db,
                    report_id=report_id,
                    section_no=section_no,
                    actor_user_id=user_id,
                    actor_role=user_role,
                    preceding_outputs=preceding or None,
                )
                await bg_db.commit()
                _generation_tasks[task_id].update({
                    "status": output.status,
                    "tokens_used": output.tokens_used,
                })
                logger.info(
                    "generate_section[bg]: done section=%d report=%s status=%s tokens=%s",
                    section_no, report_id, output.status, output.tokens_used,
                )
            except Exception as exc:
                try:
                    await bg_db.rollback()
                except Exception:
                    pass
                _generation_tasks[task_id].update({
                    "status": "error",
                    "detail": str(exc)[:500],
                })
                logger.exception(
                    "generate_section[bg]: error section=%d report=%s: %s",
                    section_no, report_id, exc,
                )

    background_tasks.add_task(_bg_generate_section)
    logger.info("generate_section: queued task=%s section=%d report=%s user=%s", task_id, section_no, report_id, user_id)
    return GenerateTaskResult(task_id=task_id, status="running", section_no=section_no)


@router.post("/generate", status_code=202, response_model=GenerateTaskResult)
async def generate_full_report(
    report_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_analyst),
):
    """Trigger AI generation for all 10 sections in dependency order (background task).

    Returns 202 immediately with a task_id. Poll GET /generate/status/{task_id} for completion.
    Sections without saved input data are skipped; returns 422 only if NO sections have data.
    """
    report = await _require_report(db, report_id)
    _assert_owner_or_admin(report, current_user)

    # Preflight data check — fast, in request context before 202 is returned
    sections_with_data: list[int] = []
    for sec_no in range(1, 11):
        data = await _load_section_input(db, report_id, sec_no)
        if data:
            sections_with_data.append(sec_no)

    if not sections_with_data:
        logger.warning(
            "generate_full_report: no input data for any section report=%s user=%s",
            report_id, current_user.id,
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "No sections have saved input data. "
                "Run ETL on uploaded documents or fill in section data manually before generating."
            ),
        )

    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not configured. Set it in Render environment variables to enable AI generation.",
        )

    task_id = str(uuid.uuid4())
    _generation_tasks[task_id] = {"status": "running"}
    user_id, user_role = current_user.id, current_user.role

    async def _bg_generate_full_report():
        from credit_report.database import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_db:
            try:
                logger.info(
                    "generate_full_report[bg]: starting report=%s user=%s task=%s",
                    report_id, user_id, task_id,
                )
                results = await run_full_report_generation(
                    db=bg_db,
                    report_id=report_id,
                    actor_user_id=user_id,
                    actor_role=user_role,
                )
                await bg_db.commit()
                done = sum(1 for v in results.values() if v == "done")
                _generation_tasks[task_id].update({
                    "status": "done",
                    "sections": {str(k): v for k, v in results.items()},
                })
                logger.info(
                    "generate_full_report[bg]: complete report=%s task=%s done=%d/%d",
                    report_id, task_id, done, len(results),
                )
            except Exception as exc:
                try:
                    await bg_db.rollback()
                except Exception:
                    pass
                _generation_tasks[task_id].update({
                    "status": "error",
                    "detail": str(exc)[:500],
                })
                logger.exception(
                    "generate_full_report[bg]: error report=%s task=%s: %s",
                    report_id, task_id, exc,
                )

    background_tasks.add_task(_bg_generate_full_report)
    logger.info(
        "generate_full_report: queued task=%s report=%s user=%s sections_with_data=%s",
        task_id, report_id, user_id, sections_with_data,
    )
    return GenerateTaskResult(task_id=task_id, status="running")


# ── Section output retrieval ──────────────────────────────────────────────────

@router.get("/sections/{section_no}/output", response_model=SectionOutputOut)
async def get_section_output_endpoint(
    report_id: str,
    section_no: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
    output = await get_section_output(db, report_id, section_no)
    if not output:
        raise HTTPException(status_code=404, detail="Section output not found")
    return _output_to_schema(output)


@router.get("/outputs", response_model=list[SectionOutputOut])
async def list_outputs(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    report = await _require_report(db, report_id)
    _assert_can_view(report, current_user)
    result = await db.execute(
        select(SectionOutput)
        .where(SectionOutput.report_id == report_id)
        .order_by(SectionOutput.section_no)
    )
    return [_output_to_schema(o) for o in result.scalars().all()]

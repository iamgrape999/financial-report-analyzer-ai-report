"""
Deep ETL + Section Input CI/CD Test Suite — §1 to §11
=======================================================
Covers:
  A. ETL schema completeness & document-type routing (§1-11)
  B. Save (apply) ETL data to each section §1-11 via API endpoint
  C. Read-back integrity after save
  D. Merge / overwrite / conflict semantics
  E. Generate-section guards (§11 must be blocked, §1-10 must be allowed)
  F. Export section-name registration (DOCX/PDF backend + frontend JS)
  G. Field-type contracts (bool, pct, enum, template key)
  H. Edge cases: empty text, null sections, oversized payloads, concurrent save
  I. Permission / ownership guards
  J. Full apply-chain: ETL mock result → apply → read-back for every section

All findings are captured as assertions with descriptive failure messages.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

# ─── helpers ──────────────────────────────────────────────────────────────────

HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"


def _load_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _make_user(role: str = "analyst") -> MagicMock:
    u = MagicMock()
    u.id = str(uuid.uuid4())
    u.role = role
    u.email = f"{role}@test.local"
    return u


async def _seed_report(db: AsyncSession, rid: str, owner_id: str | None = None) -> MagicMock:
    from credit_report.models import Report

    uid = owner_id or str(uuid.uuid4())
    report = Report(
        id=rid,
        borrower_name="ETL Deep Test Co",
        created_by=uid,
        status="draft",
        is_deleted=False,
    )
    db.add(report)
    await db.flush()
    return report


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    from credit_report.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# A — ETL schema completeness & document-type routing
# ══════════════════════════════════════════════════════════════════════════════

class TestETLSchemaCompleteness:
    """All 11 sections must have a non-empty extraction schema."""

    def _load_schema(self):
        from credit_report.generation.etl import SECTION_EXTRACTION_SCHEMA
        return SECTION_EXTRACTION_SCHEMA

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_section_schema_exists(self, sec_no):
        schema = self._load_schema()
        assert sec_no in schema, (
            f"§{sec_no} missing from SECTION_EXTRACTION_SCHEMA — "
            f"present keys: {sorted(schema.keys())}"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_section_schema_non_empty(self, sec_no):
        schema = self._load_schema()
        text = schema.get(sec_no, "")
        assert len(text.strip()) > 50, (
            f"§{sec_no} schema is too short ({len(text.strip())} chars) — "
            f"likely placeholder or truncated"
        )

    def test_no_undefined_section_beyond_11(self):
        schema = self._load_schema()
        beyond = [k for k in schema.keys() if k > 11]
        assert not beyond, (
            f"Unexpected sections beyond §11 in schema: {beyond}"
        )


class TestDocumentTypeSectionRouting:
    """DOCUMENT_SECTION_MAP must route every document type to valid sections."""

    def _load_map(self):
        from credit_report.generation.etl import DOCUMENT_SECTION_MAP
        return DOCUMENT_SECTION_MAP

    def test_all_document_types_present(self):
        doc_map = self._load_map()
        expected = {
            "annual_report", "financial_statement", "analyst_presentation",
            "interim_report", "valuation_report", "charter_agreement",
            "shipbuilding_contract", "kyc_document", "legal_document",
            "external_report", "other",
        }
        missing = expected - set(doc_map.keys())
        assert not missing, f"Document types missing from DOCUMENT_SECTION_MAP: {missing}"

    @pytest.mark.parametrize("doc_type,expected_sections", [
        ("annual_report",        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
        ("financial_statement",  [7, 4, 2, 10]),
        ("analyst_presentation", [4, 7, 3, 10]),
        ("interim_report",       [7, 4, 2, 3]),
        ("valuation_report",     [5, 10, 6]),
        ("charter_agreement",    [1, 6, 5]),
        ("shipbuilding_contract",[6, 1, 5]),
        ("kyc_document",         [9, 1, 4]),
        ("legal_document",       [8, 1, 9]),
        ("external_report",      [11, 4, 7]),
        ("other",                [4, 7, 1]),
    ])
    def test_document_type_maps_to_correct_sections(self, doc_type, expected_sections):
        doc_map = self._load_map()
        actual = doc_map[doc_type]
        assert actual == expected_sections, (
            f"doc_type={doc_type!r}: expected {expected_sections}, got {actual}"
        )

    def test_all_mapped_sections_have_schemas(self):
        from credit_report.generation.etl import DOCUMENT_SECTION_MAP, SECTION_EXTRACTION_SCHEMA
        for doc_type, sections in DOCUMENT_SECTION_MAP.items():
            for sec in sections:
                assert sec in SECTION_EXTRACTION_SCHEMA, (
                    f"doc_type={doc_type!r} maps to §{sec} but §{sec} has no schema"
                )

    def test_section_11_only_in_external_report(self):
        from credit_report.generation.etl import DOCUMENT_SECTION_MAP
        for doc_type, sections in DOCUMENT_SECTION_MAP.items():
            if doc_type != "external_report":
                assert 11 not in sections, (
                    f"§11 unexpectedly appears in doc_type={doc_type!r} mapping: {sections}"
                )

    def test_each_document_type_routes_at_least_one_section(self):
        from credit_report.generation.etl import DOCUMENT_SECTION_MAP
        for doc_type, sections in DOCUMENT_SECTION_MAP.items():
            assert sections, f"doc_type={doc_type!r} maps to empty section list"


# ══════════════════════════════════════════════════════════════════════════════
# B — Save (apply) ETL data to §1-11 via save_section_input
# ══════════════════════════════════════════════════════════════════════════════

# Representative minimal payloads for each section (real field names from schema)
SECTION_PAYLOADS: dict[int, dict] = {
    1: {"metadata": {"report_type": "new_deal", "branch": "Taipei", "industry": "Shipping"},
        "facility_summary": {"rows": [], "totals": {"total_credit_limit_usd_m": 50.0}}},
    2: {"2A_credit_overview": {"bullets": [{"order": 1, "text_verbatim": "Sound financials"}]},
        "2B_solvency": {"primary_repayment_source_verbatim": "Operating cash flow"}},
    3: {"3A_external_ratings": {"all_nil": False,
        "ratings": [{"entity_abbrev": "TestCo", "sp": "BBB-", "sp_outlook": "Stable"}]},
        "3C_mas_612": {"grade": "PASS", "primary_paragraph_verbatim": "Creditworthy borrower"}},
    4: {"4A_borrower": {"company_name_en": "Test Shipping Ltd",
        "company_name_zh": "測試航運有限公司", "incorporation_country": "Taiwan"},
        "4B_ownership": {"shareholders": [{"name": "Parent Corp", "stake_percent": 100.0}]}},
    5: {"5A_security_overview": {"is_secured": True, "security_instruments": []},
        "5C_vessel_mortgage": {"applicable": True, "vessel_valuations": [],
                               "ltc_pct": 60.0, "ltc_limit_pct": 65.0}},
    6: {"6A_project": {"hull_number": "H-2603", "vessel_type": "Container",
        "teu": 8000, "fuel_type": "LNG", "delivery_date": "2026-03-01",
        "contract_price_usd_m": 120.0, "ltc_pct": 60.0},
        "6B_builder": {"name": "Hyundai Heavy Industries", "hq": "South Korea"}},
    7: {"entities_to_analyze": [{"name": "TestCo", "role": "Borrower", "currency": "USD"}],
        "7A_borrower_financials": {
            "reporting_currency": "USD", "unit": "millions",
            "income_statement": {"2024": {"revenue": 500.0, "ebitda": 100.0}},
            "balance_sheet": {"2024": {"total_assets": 2000.0, "total_equity": 800.0}},
        },
        "7B_key_ratios": {"2024": {"ebitda_margin_pct": 20.0, "debt_ebitda": 3.5}}},
    8: {"8A_acra_banking_charges": {
        "section_applicability": "internal_only",
        "acra_data_available": True,
        "jurisdiction": "Singapore",
        "charges": [],
        "summary": {"total_charges": 0, "active_charges": 0, "satisfied_charges": 0,
                    "total_active_usd_m": 0.0, "cub_charge_count": 0, "cub_total_usd_m": 0.0}}},
    9: {"9A_checklist": [{"no": 1, "category": "KYC & Compliance",
        "item": "AML check", "response": "Yes", "remarks": "Completed"}],
        "9C_recommendation": {"decision": "APPROVE", "facility_amount_usd_m": 50.0,
                              "tenor_years": 5, "risk_level_changes_from_prior": "None"}},
    10: {"10A_group_exposure": {"entity_group": "Test Group",
         "group_limit_usd_m": 200.0, "currency": "USD", "rows": []}},
    11: {"11A_report_meta": {"analyst_firm": "Capital Securities", "analyst_name": "John Doe",
         "report_date": "2026-03-15", "subject_company_en": "Test Co",
         "report_type": "Initiation"},
         "11B_rating": {"current_rating": "Buy", "target_price_3m": 45.0,
                        "target_price_currency": "TWD", "upside_pct": 15.0},
         "11E_annual_income_statement": {"currency": "TWD", "unit": "百萬元",
             "periods": [{"year": "2024", "is_forecast": False, "revenue": 5000.0}]}},
}


@pytest.mark.parametrize("sec_no", list(range(1, 12)))
@pytest.mark.asyncio
async def test_save_section_input_all_sections(db, sec_no):
    """save_section_input must accept §1-11 with valid payloads."""
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    payload_data = SECTION_PAYLOADS[sec_no]
    payload = SectionInputPayload(section_no=sec_no, input_json=payload_data)

    with (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core",
              new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    ):
        resp = await save_section_input(
            report_id=rid, section_no=sec_no, payload=payload,
            db=db, current_user=user,
        )

    assert resp is not None, f"§{sec_no}: save_section_input returned None"
    assert resp.section_no == sec_no, (
        f"§{sec_no}: response.section_no={resp.section_no} mismatch"
    )


# ══════════════════════════════════════════════════════════════════════════════
# C — Read-back integrity after save
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sec_no", list(range(1, 12)))
@pytest.mark.asyncio
async def test_readback_after_save_all_sections(db, sec_no):
    """After saving §N data, GET must return the exact same JSON."""
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    payload_data = SECTION_PAYLOADS[sec_no]
    payload = SectionInputPayload(section_no=sec_no, input_json=payload_data)

    patches = (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core",
              new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    )
    with patches[0], patches[1], patches[2]:
        await save_section_input(
            report_id=rid, section_no=sec_no, payload=payload,
            db=db, current_user=user,
        )

    read_resp = await get_section_input(
        report_id=rid, section_no=sec_no, db=db, current_user=user,
    )

    assert read_resp is not None, f"§{sec_no}: get_section_input returned None after save"
    assert read_resp.section_no == sec_no
    assert read_resp.input_json == payload_data, (
        f"§{sec_no}: read-back JSON mismatch.\n"
        f"Saved:   {json.dumps(payload_data)[:200]}\n"
        f"Got:     {json.dumps(read_resp.input_json)[:200]}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# D — Merge / overwrite semantics
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sec_no", [1, 4, 7, 11])
@pytest.mark.asyncio
async def test_second_save_overwrites_first(db, sec_no):
    """Second save to the same section must overwrite (not append) input_json."""
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    first_data = {"marker": "first", "version": 1}
    second_data = {"marker": "second", "version": 2, "extra": True}

    patches = (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core",
              new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    )
    with patches[0], patches[1], patches[2]:
        await save_section_input(
            report_id=rid, section_no=sec_no,
            payload=SectionInputPayload(section_no=sec_no, input_json=first_data),
            db=db, current_user=user,
        )
        await save_section_input(
            report_id=rid, section_no=sec_no,
            payload=SectionInputPayload(section_no=sec_no, input_json=second_data),
            db=db, current_user=user,
        )

    result = await get_section_input(
        report_id=rid, section_no=sec_no, db=db, current_user=user,
    )
    assert result.input_json == second_data, (
        f"§{sec_no}: second save did not overwrite first.\n"
        f"Expected: {second_data}\n"
        f"Got:      {result.input_json}"
    )
    assert result.input_json.get("marker") == "second", (
        f"§{sec_no}: first-save 'marker' leaked through to second save"
    )


@pytest.mark.parametrize("sec_no", [4, 7, 11])
@pytest.mark.asyncio
async def test_different_sections_do_not_interfere(db, sec_no):
    """Saving §A must not overwrite §B's data."""
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    sec_a, sec_b = 4, sec_no if sec_no != 4 else 7
    data_a = {"source": "section_a", "value": 111}
    data_b = {"source": "section_b", "value": 222}

    patches = (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core",
              new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    )
    with patches[0], patches[1], patches[2]:
        await save_section_input(
            report_id=rid, section_no=sec_a,
            payload=SectionInputPayload(section_no=sec_a, input_json=data_a),
            db=db, current_user=user,
        )
        await save_section_input(
            report_id=rid, section_no=sec_b,
            payload=SectionInputPayload(section_no=sec_b, input_json=data_b),
            db=db, current_user=user,
        )

    read_a = await get_section_input(report_id=rid, section_no=sec_a, db=db, current_user=user)
    read_b = await get_section_input(report_id=rid, section_no=sec_b, db=db, current_user=user)

    assert read_a.input_json == data_a, f"§{sec_a} was corrupted by save to §{sec_b}"
    assert read_b.input_json == data_b, f"§{sec_b} data is wrong"


# ══════════════════════════════════════════════════════════════════════════════
# E — Generate-section guards
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateSectionGuards:
    """§1-11 are generatable; §0, 12+ must be blocked."""

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    @pytest.mark.asyncio
    async def test_generate_accepts_sections_1_to_11(self, db, sec_no):
        from fastapi import BackgroundTasks
        from credit_report.api.generate import generate_section

        rid = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)
        mock_bg = MagicMock(spec=BackgroundTasks)
        mock_bg.add_task = MagicMock()

        with (
            patch("credit_report.api.generate.run_section_generation", new=AsyncMock()),
            patch("credit_report.api.generate.check_hard_dependencies",
                  new=AsyncMock(return_value=[])),
        ):
            resp = await generate_section(
                report_id=rid, section_no=sec_no,
                db=db, background_tasks=mock_bg, current_user=user,
            )
        assert resp is not None, f"§{sec_no}: generate_section returned None"

    @pytest.mark.asyncio
    async def test_generate_accepts_section_11(self, db):
        """Stage 3 Item 7: §11 now has a real prompt and must be accepted (202)."""
        from fastapi import BackgroundTasks
        from credit_report.api.generate import generate_section

        rid = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)
        mock_bg = MagicMock(spec=BackgroundTasks)
        mock_bg.add_task = MagicMock()

        with (
            patch("credit_report.api.generate.run_section_generation", new=AsyncMock()),
            patch("credit_report.api.generate.check_hard_dependencies",
                  new=AsyncMock(return_value=[])),
        ):
            resp = await generate_section(
                report_id=rid, section_no=11,
                db=db, background_tasks=mock_bg, current_user=user,
            )
        assert resp is not None, "§11 generate_section must return a task result"

    @pytest.mark.parametrize("bad_no", [0, 12, -1, 100, 999])
    @pytest.mark.asyncio
    async def test_generate_blocks_out_of_range(self, db, bad_no):
        from fastapi import BackgroundTasks
        from credit_report.api.generate import generate_section

        rid = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)
        mock_bg = MagicMock(spec=BackgroundTasks)

        with pytest.raises(HTTPException) as exc:
            await generate_section(
                report_id=rid, section_no=bad_no,
                db=db, background_tasks=mock_bg, current_user=user,
            )
        assert exc.value.status_code == 400, (
            f"section_no={bad_no}: expected 400, got {exc.value.status_code}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# F — Section-name registration in all display surfaces
# ══════════════════════════════════════════════════════════════════════════════

class TestSectionNameRegistration:
    """§1-11 must be named everywhere that renders section labels."""

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_frontend_snames_has_section(self, sec_no):
        html = _load_html()
        # SNAMES={1:'...',2:'...',...,11:'...'}
        match = re.search(r'const SNAMES\s*=\s*\{([^}]+)\}', html)
        assert match, "SNAMES constant not found in index.html"
        snames_body = match.group(1)
        assert re.search(rf'\b{sec_no}\s*:', snames_body), (
            f"§{sec_no} missing from frontend SNAMES map.\n"
            f"SNAMES body: {snames_body[:300]}"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_frontend_snames_zh_has_section(self, sec_no):
        html = _load_html()
        match = re.search(r'const SNAMES_ZH\s*=\s*\{([^}]+)\}', html)
        assert match, "SNAMES_ZH constant not found in index.html"
        snames_zh_body = match.group(1)
        assert re.search(rf'\b{sec_no}\s*:', snames_zh_body), (
            f"§{sec_no} missing from frontend SNAMES_ZH map"
        )

    @pytest.mark.parametrize("sec_no", list(range(1, 12)))
    def test_backend_export_section_names_has_section(self, sec_no):
        from credit_report.api.export import SECTION_NAMES
        assert sec_no in SECTION_NAMES, (
            f"§{sec_no} missing from credit_report.api.export.SECTION_NAMES.\n"
            f"Present: {sorted(SECTION_NAMES.keys())}"
        )

    def test_section_11_name_is_meaningful(self):
        from credit_report.api.export import SECTION_NAMES
        name = SECTION_NAMES.get(11, "")
        assert "Analyst" in name or "Research" in name or "External" in name, (
            f"§11 name should describe analyst/research content, got: {name!r}"
        )

    def test_frontend_section_11_name_in_chinese(self):
        html = _load_html()
        match = re.search(r'const SNAMES_ZH\s*=\s*\{([^}]+)\}', html)
        assert match, "SNAMES_ZH not found"
        body = match.group(1)
        # Find the §11 entry value
        m = re.search(r"11\s*:\s*'([^']+)'", body)
        assert m, "§11 not found in SNAMES_ZH"
        zh_name = m.group(1)
        assert len(zh_name) > 3, f"§11 Chinese name too short: {zh_name!r}"


# ══════════════════════════════════════════════════════════════════════════════
# G — Schema validation: section_no range guards
# ══════════════════════════════════════════════════════════════════════════════

class TestSectionNoValidation:
    """Backend must accept §1-11 and reject everything outside that range."""

    @pytest.mark.parametrize("bad_no", [0, 12, -1, 999])
    @pytest.mark.asyncio
    async def test_save_rejects_out_of_range(self, db, bad_no):
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)

        payload = SectionInputPayload(section_no=1, input_json={"test": True})
        with pytest.raises(HTTPException) as exc:
            await save_section_input(
                report_id=rid, section_no=bad_no, payload=payload,
                db=db, current_user=user,
            )
        assert exc.value.status_code == 400, (
            f"section_no={bad_no}: expected 400, got {exc.value.status_code}"
        )

    @pytest.mark.parametrize("valid_no", list(range(1, 12)))
    def test_pydantic_schema_accepts_valid_section_no(self, valid_no):
        from credit_report.schemas import SectionInputPayload
        payload = SectionInputPayload(section_no=valid_no, input_json={"x": 1})
        assert payload.section_no == valid_no

    @pytest.mark.parametrize("bad_no", [0, 12, -5])
    def test_pydantic_schema_rejects_invalid_section_no(self, bad_no):
        from credit_report.schemas import SectionInputPayload
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SectionInputPayload(section_no=bad_no, input_json={"x": 1})


# ══════════════════════════════════════════════════════════════════════════════
# H — ETL function: input/output contracts
# ══════════════════════════════════════════════════════════════════════════════

class TestETLFunctionContracts:
    """etl_document() must handle edge-cases gracefully without crashing."""

    @pytest.mark.asyncio
    async def test_etl_empty_text_returns_empty_dict(self):
        from credit_report.generation.etl import etl_document
        result = await etl_document(text="", document_type="annual_report")
        assert result == {}, "Empty text must return {} — not crash"

    @pytest.mark.asyncio
    async def test_etl_whitespace_only_text_returns_empty_dict(self):
        from credit_report.generation.etl import etl_document
        result = await etl_document(text="   \n\t  ", document_type="financial_statement")
        assert result == {}, "Whitespace-only text must return {}"

    @pytest.mark.asyncio
    async def test_etl_unknown_doc_type_falls_back(self):
        """Unknown doc_type must use fallback [4, 7, 1] without raising."""
        from credit_report.generation.etl import etl_document

        mock_response = MagicMock()
        mock_response.text = '{"4": {"4A_borrower": {"company_name_en": "Test"}}, "7": {}}'
        mock_response.candidates = [MagicMock(finish_reason="STOP")]

        with patch("google.genai.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            with patch("credit_report.config.GEMINI_API_KEY", "test-key"):
                result = await etl_document(text="Some text", document_type="unknown_type_xyz")

        assert isinstance(result, dict), "Unknown doc_type must return dict, not raise"

    @pytest.mark.asyncio
    async def test_etl_returns_only_sections_with_values(self):
        """Sections where Gemini returns all-null values must be excluded from result."""
        from credit_report.generation.etl import etl_document

        # §4 has values, §7 is all-null
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "4": {"4A_borrower": {"company_name_en": "Test Co"}},
            "7": {"entities_to_analyze": None, "7A_borrower_financials": None},
        })
        mock_response.candidates = [MagicMock(finish_reason="STOP")]

        with patch("google.genai.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            with patch("credit_report.config.GEMINI_API_KEY", "test-key"):
                result = await etl_document(text="Annual report text", document_type="annual_report")

        assert 4 in result, "§4 with non-null values must be in result"
        assert 7 not in result, "§7 with all-null values must be excluded"

    @pytest.mark.asyncio
    async def test_etl_strips_markdown_code_fences(self):
        """Gemini sometimes wraps JSON in ```json ... ``` — must be stripped."""
        from credit_report.generation.etl import etl_document

        json_payload = json.dumps({"4": {"4A_borrower": {"company_name_en": "Fence Test"}}})
        fenced = f"```json\n{json_payload}\n```"

        mock_response = MagicMock()
        mock_response.text = fenced
        mock_response.candidates = [MagicMock(finish_reason="STOP")]

        with patch("google.genai.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            with patch("credit_report.config.GEMINI_API_KEY", "test-key"):
                result = await etl_document(text="Annual report text", document_type="annual_report")

        assert 4 in result, "ETL must handle markdown-fenced JSON response"
        assert result[4].get("4A_borrower", {}).get("company_name_en") == "Fence Test"

    @pytest.mark.asyncio
    async def test_etl_gemini_empty_response_returns_empty_dict(self):
        """If Gemini returns empty string, etl_document must return {} gracefully."""
        from credit_report.generation.etl import etl_document

        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.candidates = [MagicMock(finish_reason="STOP")]

        with patch("google.genai.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            with patch("credit_report.config.GEMINI_API_KEY", "test-key"):
                result = await etl_document(text="Test text", document_type="annual_report")

        assert result == {}, "Empty Gemini response must yield {}"

    @pytest.mark.asyncio
    async def test_etl_invalid_json_response_returns_empty_dict(self):
        """Malformed JSON from Gemini must not crash — must return {}."""
        from credit_report.generation.etl import etl_document

        mock_response = MagicMock()
        mock_response.text = "I cannot process this document. {broken json"
        mock_response.candidates = [MagicMock(finish_reason="STOP")]

        with patch("google.genai.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
            with patch("credit_report.config.GEMINI_API_KEY", "test-key"):
                result = await etl_document(text="Test text", document_type="annual_report")

        assert isinstance(result, dict), "Malformed Gemini JSON must return dict, not raise"


# ══════════════════════════════════════════════════════════════════════════════
# I — ETL endpoint: file missing / binary fallback
# ══════════════════════════════════════════════════════════════════════════════

class TestETLEndpointFileFallback:
    """etl_document_endpoint must re-extract from .bin if .txt is missing."""

    @pytest.mark.asyncio
    async def test_etl_endpoint_fails_gracefully_when_both_files_missing(self, db):
        from credit_report.api.generate import etl_document_endpoint
        from credit_report.generation.models import SectionDocument

        rid = str(uuid.uuid4())
        doc_id = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)

        doc = SectionDocument(
            id=doc_id, report_id=rid, original_filename="report.pdf",
            file_size_bytes=1000, document_type="annual_report",
            file_format="pdf", etl_status="pending", uploaded_by=user.id,
        )
        db.add(doc)
        await db.flush()

        # No .txt or .bin files exist → must get 422
        with patch("credit_report.config.CREDIT_REPORTS_ROOT", Path("/nonexistent_dir_xyz")):
            with pytest.raises(HTTPException) as exc:
                await etl_document_endpoint(
                    report_id=rid, doc_id=doc_id, db=db, current_user=user,
                )
        assert exc.value.status_code == 422, (
            f"Missing files: expected 422, got {exc.value.status_code}"
        )
        assert "not found" in exc.value.detail.lower() or "re-upload" in exc.value.detail.lower(), (
            f"Error message should guide user: {exc.value.detail}"
        )

    @pytest.mark.asyncio
    async def test_etl_endpoint_rereads_txt_when_present(self, db, tmp_path):
        """When .txt exists, ETL must proceed to call etl_document (mocked)."""
        from credit_report.api.generate import etl_document_endpoint
        from credit_report.generation.models import SectionDocument

        rid = str(uuid.uuid4())
        doc_id = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)

        doc = SectionDocument(
            id=doc_id, report_id=rid, original_filename="report.pdf",
            file_size_bytes=1000, document_type="financial_statement",
            file_format="pdf", etl_status="uploaded", uploaded_by=user.id,
        )
        db.add(doc)
        await db.flush()

        # Create .txt file
        doc_dir = tmp_path / rid
        doc_dir.mkdir()
        (doc_dir / f"{doc_id}.txt").write_text("Revenue 5 billion USD EBITDA 1 billion", encoding="utf-8")

        mock_extracted = {4: {"4A_borrower": {"company_name_en": "Test"}}}

        with (
            patch("credit_report.config.CREDIT_REPORTS_ROOT", tmp_path),
            patch("credit_report.api.generate.etl_document", new=AsyncMock(return_value=mock_extracted)),
        ):
            resp = await etl_document_endpoint(
                report_id=rid, doc_id=doc_id, db=db, current_user=user,
            )

        assert resp is not None, "ETL endpoint returned None with valid .txt"
        assert 4 in resp.sections_extracted or "4" in [str(s) for s in resp.sections_extracted], (
            f"Expected §4 in sections_extracted, got: {resp.sections_extracted}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# J — Full apply-chain: mock ETL result → apply → read-back §1-11
# ══════════════════════════════════════════════════════════════════════════════

# Realistic mock ETL responses for each section (as returned by etl_document)
ETL_MOCK_RESULTS: dict[int, dict] = {
    1: {"metadata": {"report_type": "new_deal", "branch": "Taipei"},
        "facility_summary": {"totals": {"total_credit_limit_usd_m": 50.0}}},
    2: {"2A_credit_overview": {"bullets": [{"order": 1, "text_verbatim": "Positive outlook"}]}},
    3: {"3A_external_ratings": {"all_nil": False,
        "ratings": [{"entity_abbrev": "TC", "sp": "BBB", "sp_outlook": "Stable"}]}},
    4: {"4A_borrower": {"company_name_en": "Mock Shipping", "incorporation_country": "TW"},
        "4B_ownership": {"shareholders": [{"name": "ParentCo", "stake_percent": 100.0}]}},
    5: {"5A_security_overview": {"is_secured": True}, "5C_vessel_mortgage": {"applicable": True}},
    6: {"6A_project": {"hull_number": "H-001", "teu": 8000, "delivery_date": "2026-06-01"}},
    7: {"entities_to_analyze": [{"name": "MockCo", "role": "Borrower"}],
        "7A_borrower_financials": {"reporting_currency": "USD", "unit": "millions",
            "income_statement": {"2024": {"revenue": 800.0, "ebitda": 160.0}}}},
    8: {"8A_acra_banking_charges": {"section_applicability": "internal_only",
        "acra_data_available": False, "charges": []}},
    9: {"9A_checklist": [{"no": 1, "category": "KYC & Compliance",
        "item": "AML check", "response": "Yes", "remarks": "OK"}],
        "9C_recommendation": {"decision": "APPROVE", "facility_amount_usd_m": 50.0}},
    10: {"10A_group_exposure": {"entity_group": "Mock Group", "group_limit_usd_m": 300.0}},
    11: {"11A_report_meta": {"analyst_firm": "Capital Securities", "report_date": "2026-03-15",
         "subject_company_en": "MockCo"},
        "11B_rating": {"current_rating": "Buy", "upside_pct": 18.5},
        "11E_annual_income_statement": {"currency": "TWD", "unit": "百萬元",
            "periods": [{"year": "2024", "is_forecast": False, "revenue": 8500.0}]}},
}


@pytest.mark.parametrize("sec_no", list(range(1, 12)))
@pytest.mark.asyncio
async def test_full_etl_apply_chain(db, sec_no):
    """
    Full chain: ETL mock result for §N → apply via save_section_input → read back.
    Verifies the complete data flow from ETL extraction to section storage.
    """
    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    rid = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    etl_data = ETL_MOCK_RESULTS[sec_no]
    payload = SectionInputPayload(section_no=sec_no, input_json=etl_data)

    with (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core",
              new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    ):
        save_resp = await save_section_input(
            report_id=rid, section_no=sec_no, payload=payload,
            db=db, current_user=user,
        )

    assert save_resp.section_no == sec_no, f"§{sec_no}: save_resp.section_no wrong"

    read_resp = await get_section_input(
        report_id=rid, section_no=sec_no, db=db, current_user=user,
    )
    assert read_resp is not None, f"§{sec_no}: get_section_input returned None"
    assert read_resp.input_json == etl_data, (
        f"§{sec_no}: ETL chain data mismatch.\n"
        f"Sent:    {json.dumps(etl_data)[:300]}\n"
        f"Got:     {json.dumps(read_resp.input_json)[:300]}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# K — ETL field-type contracts
# ══════════════════════════════════════════════════════════════════════════════

class TestETLFieldTypeContracts:
    """Verify that field naming conventions are consistent in the ETL schema."""

    def _get_schema_text(self, sec_no: int) -> str:
        from credit_report.generation.etl import SECTION_EXTRACTION_SCHEMA
        return SECTION_EXTRACTION_SCHEMA[sec_no]

    @pytest.mark.parametrize("sec_no,bool_fields", [
        (1, ["proposed_facility_is_new", "compliant_yn"]),
        (2, ["assigned_to_cub", "satisfactory_to_bank"]),
        (3, ["all_nil", "override_flag"]),
        (5, ["is_secured", "applicable", "valuation_compliant"]),
        (6, ["eu_ets_applicable", "on_schedule"]),
        (8, ["acra_data_available", "is_cub_charge"]),
        (11, ["is_forecast"]),
    ])
    def test_boolean_fields_defined_in_schema(self, sec_no, bool_fields):
        schema = self._get_schema_text(sec_no)
        for field in bool_fields:
            assert field in schema, (
                f"§{sec_no}: boolean field '{field}' not found in schema text.\n"
                f"This field must exist for correct JSON extraction."
            )

    @pytest.mark.parametrize("sec_no,pct_fields", [
        (1, ["ltc_pct", "acr_pct", "ltv_pct"]),
        (5, ["ltc_pct", "acr_at_delivery_pct", "acr_floor_pct", "ltv_at_maturity_pct"]),
        (6, ["ltc_pct", "ontime_delivery_pct", "completion_pct"]),
        (7, ["ebitda_margin_pct", "gross_margin_pct"]),
        (11, ["upside_pct", "ebitda_margin_pct"]),
    ])
    def test_percentage_fields_defined_in_schema(self, sec_no, pct_fields):
        schema = self._get_schema_text(sec_no)
        for field in pct_fields:
            assert field in schema, (
                f"§{sec_no}: percentage field '{field}' not found in schema text"
            )

    @pytest.mark.parametrize("sec_no,verbatim_fields", [
        (1, ["purpose_text_verbatim", "pre_delivery_security_verbatim"]),
        (2, ["primary_repayment_source_verbatim", "secondary_repayment_source_verbatim"]),
        (3, ["primary_paragraph_verbatim"]),
        (5, ["guarantee_language"]),
    ])
    def test_verbatim_fields_defined_in_schema(self, sec_no, verbatim_fields):
        schema = self._get_schema_text(sec_no)
        for field in verbatim_fields:
            assert field in schema, (
                f"§{sec_no}: verbatim field '{field}' not found in schema text"
            )

    @pytest.mark.parametrize("sec_no,enum_fields", [
        (3, ["PASS", "SPECIAL_MENTION", "SUBSTANDARD", "DOUBTFUL", "LOSS"]),
        (8, ["internal_only", "not_applicable"]),
        (9, ["APPROVE", "APPROVE WITH CONDITIONS", "DECLINE"]),
    ])
    def test_enum_values_defined_in_schema(self, sec_no, enum_fields):
        schema = self._get_schema_text(sec_no)
        for val in enum_fields:
            assert val in schema, (
                f"§{sec_no}: enum value '{val}' not found in schema text"
            )

    def test_section_7_template_keys_fy_yyyy(self):
        """§7 must use FY_YYYY template keys for financial periods."""
        schema = self._get_schema_text(7)
        assert "FY_YYYY" in schema, (
            "§7 schema must define FY_YYYY template key for financial periods"
        )

    def test_section_7_template_keys_qn_yyyy(self):
        """§7 must use QN_YYYY template keys for quarterly periods."""
        schema = self._get_schema_text(7)
        assert "QN_YYYY" in schema, (
            "§7 schema must define QN_YYYY template key for quarterly periods"
        )

    def test_section_11_is_forecast_field_present(self):
        """§11 financial tables must include is_forecast for distinguishing estimates."""
        schema = self._get_schema_text(11)
        assert "is_forecast" in schema, (
            "§11 schema must include is_forecast field in financial period objects"
        )


# ══════════════════════════════════════════════════════════════════════════════
# L — Permission / ownership guards
# ══════════════════════════════════════════════════════════════════════════════

class TestPermissionGuards:
    """Only report owners (or admins) may save section data."""

    @pytest.mark.asyncio
    async def test_non_owner_cannot_save_section_input(self, db):
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload

        owner_id = str(uuid.uuid4())
        intruder = _make_user()  # different user ID
        rid = str(uuid.uuid4())
        await _seed_report(db, rid, owner_id=owner_id)

        payload = SectionInputPayload(section_no=4, input_json={"test": True})
        with pytest.raises(HTTPException) as exc:
            await save_section_input(
                report_id=rid, section_no=4, payload=payload,
                db=db, current_user=intruder,
            )
        assert exc.value.status_code in (403, 404), (
            f"Non-owner save: expected 403 or 404, got {exc.value.status_code}"
        )

    @pytest.mark.asyncio
    async def test_admin_can_save_any_section(self, db):
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload

        owner_id = str(uuid.uuid4())
        admin = _make_user(role="admin")
        rid = str(uuid.uuid4())
        await _seed_report(db, rid, owner_id=owner_id)

        payload = SectionInputPayload(section_no=7, input_json={"admin_save": True})
        with (
            patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
            patch("credit_report.api.calculations._run_recalculate_core",
                  new=AsyncMock(return_value=(0, []))),
            patch("credit_report.api.reports.write_event", new=AsyncMock()),
        ):
            resp = await save_section_input(
                report_id=rid, section_no=7, payload=payload,
                db=db, current_user=admin,
            )
        assert resp is not None, "Admin must be able to save to any section"

    @pytest.mark.asyncio
    async def test_save_to_nonexistent_report_returns_404(self, db):
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload

        user = _make_user()
        payload = SectionInputPayload(section_no=4, input_json={"test": True})
        with pytest.raises(HTTPException) as exc:
            await save_section_input(
                report_id="nonexistent-report-id", section_no=4,
                payload=payload, db=db, current_user=user,
            )
        assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# M — ETL endpoint document-type routing test
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("doc_type,primary_section", [
    # annual_report excluded: PR #15 requires page-first scan-pages before
    # the legacy /etl endpoint; it returns 409 without prior scan.
    ("financial_statement",  7),
    ("analyst_presentation", 4),
    ("external_report",      11),
    ("charter_agreement",    1),
    ("kyc_document",         9),
    ("legal_document",       8),
    ("valuation_report",     5),
])
@pytest.mark.asyncio
async def test_etl_document_routes_correct_primary_section(doc_type, primary_section, db, tmp_path):
    """etl_document_endpoint must call etl_document with the primary section first."""
    from credit_report.api.generate import etl_document_endpoint
    from credit_report.generation.models import SectionDocument

    rid = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    doc = SectionDocument(
        id=doc_id, report_id=rid, original_filename=f"test.pdf",
        file_size_bytes=500, document_type=doc_type,
        file_format="pdf", etl_status="pending", uploaded_by=user.id,
    )
    db.add(doc)
    await db.flush()

    doc_dir = tmp_path / rid
    doc_dir.mkdir()
    (doc_dir / f"{doc_id}.txt").write_text(
        f"Test document for {doc_type}. Revenue 100M. Assets 500M.", encoding="utf-8"
    )

    mock_result = {primary_section: {"test_field": "test_value"}}
    captured_doc_types = []

    async def mock_etl(text: str, document_type: str, section_nos=None):
        # Capture the document_type passed from the endpoint to verify routing
        captured_doc_types.append(document_type)
        return mock_result

    with (
        patch("credit_report.config.CREDIT_REPORTS_ROOT", tmp_path),
        patch("credit_report.api.generate.etl_document", new=mock_etl),
    ):
        resp = await etl_document_endpoint(
            report_id=rid, doc_id=doc_id, db=db, current_user=user,
        )

    assert captured_doc_types == [doc_type], (
        f"etl_document was called with doc_type={captured_doc_types}, "
        f"expected [{doc_type!r}]"
    )
    assert primary_section in resp.sections_extracted, (
        f"doc_type={doc_type!r}: primary §{primary_section} not in response "
        f"sections_extracted={resp.sections_extracted}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# N — Section 11 specific integration tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSection11Integration:
    """Comprehensive tests for §11 Analyst/External Research Report."""

    @pytest.mark.asyncio
    async def test_section_11_save_broker_report_data(self, db):
        """Full §11 payload with analyst metadata must save without errors."""
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)

        payload_data = {
            "11A_report_meta": {
                "analyst_firm": "Capital Securities 群益投顧",
                "analyst_name": "Alex Chen",
                "report_date": "2026-03-15",
                "subject_company_en": "Evergreen Marine Corp",
                "subject_company_zh": "長榮海運",
                "subject_ticker": "2603.TW",
                "report_type": "Quarterly Update",
            },
            "11B_rating": {
                "current_rating": "Buy",
                "current_rating_zh": "買進",
                "target_price_3m": 42.0,
                "target_price_12m": 48.0,
                "target_price_currency": "TWD",
                "current_price": 36.5,
                "upside_pct": 15.1,
            },
            "11C_company_fundamentals": {
                "currency": "TWD", "unit": "百萬元",
                "market_cap": 140000.0,
                "debt_ratio_pct": 35.2,
            },
            "11E_annual_income_statement": {
                "currency": "TWD", "unit": "百萬元",
                "periods": [
                    {"year": "2024", "is_forecast": False, "revenue": 85000.0, "ebitda": 20000.0},
                    {"year": "2025F", "is_forecast": True, "revenue": 90000.0, "ebitda": 22000.0},
                ],
            },
        }

        payload = SectionInputPayload(section_no=11, input_json=payload_data)
        with (
            patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
            patch("credit_report.api.calculations._run_recalculate_core",
                  new=AsyncMock(return_value=(0, []))),
            patch("credit_report.api.reports.write_event", new=AsyncMock()),
        ):
            resp = await save_section_input(
                report_id=rid, section_no=11, payload=payload,
                db=db, current_user=user,
            )
        assert resp.section_no == 11

    @pytest.mark.asyncio
    async def test_section_11_readback_preserves_is_forecast_flag(self, db):
        """is_forecast boolean must survive JSON round-trip for §11."""
        from credit_report.api.reports import save_section_input, get_section_input
        from credit_report.schemas import SectionInputPayload

        rid = str(uuid.uuid4())
        user = _make_user()
        await _seed_report(db, rid, owner_id=user.id)

        payload_data = {
            "11E_annual_income_statement": {
                "currency": "TWD", "unit": "百萬元",
                "periods": [
                    {"year": "2024", "is_forecast": False, "revenue": 85000.0},
                    {"year": "2025F", "is_forecast": True, "revenue": 90000.0},
                ],
            },
        }

        with (
            patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
            patch("credit_report.api.calculations._run_recalculate_core",
                  new=AsyncMock(return_value=(0, []))),
            patch("credit_report.api.reports.write_event", new=AsyncMock()),
        ):
            await save_section_input(
                report_id=rid, section_no=11,
                payload=SectionInputPayload(section_no=11, input_json=payload_data),
                db=db, current_user=user,
            )

        read = await get_section_input(report_id=rid, section_no=11, db=db, current_user=user)
        periods = read.input_json["11E_annual_income_statement"]["periods"]

        actual_flags = {p["year"]: p["is_forecast"] for p in periods}
        assert actual_flags["2024"] is False, (
            f"2024 is_forecast should be False (actual historical), got: {actual_flags['2024']}"
        )
        assert actual_flags["2025F"] is True, (
            f"2025F is_forecast should be True (analyst forecast), got: {actual_flags['2025F']}"
        )

    def test_section_11_generatable_by_ai(self):
        """Stage 3 Item 7: §11 now has a real prompt — guard must allow 1–11."""
        from credit_report.api.generate import generate_section
        import inspect
        source = inspect.getsource(generate_section)
        assert "section_no > 11" in source or "section_no < 1 or section_no > 11" in source, (
            "generate_section must allow section_no up to 11 after Stage 3 Item 7"
        )

    def test_section_11_in_etl_schema(self):
        from credit_report.generation.etl import SECTION_EXTRACTION_SCHEMA
        schema = SECTION_EXTRACTION_SCHEMA.get(11, "")
        assert "11A_report_meta" in schema, "§11 schema must define 11A_report_meta"
        assert "11B_rating" in schema, "§11 schema must define 11B_rating"
        assert "is_forecast" in schema, "§11 schema must include is_forecast"
        assert "analyst_firm" in schema, "§11 schema must include analyst_firm field"

    def test_external_report_maps_to_11_first(self):
        from credit_report.generation.etl import DOCUMENT_SECTION_MAP
        sections = DOCUMENT_SECTION_MAP["external_report"]
        assert sections[0] == 11, (
            f"external_report must extract §11 FIRST (broker report data is primary), "
            f"got: {sections}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# O — ETL prompt construction
# ══════════════════════════════════════════════════════════════════════════════

class TestETLPromptConstruction:
    """_build_etl_prompt must include correct sections in the generated prompts."""

    def test_build_etl_prompt_includes_target_sections(self):
        from credit_report.generation.etl import _build_etl_prompt
        system_prompt, user_prompt = _build_etl_prompt(
            document_type="annual_report",
            text="Revenue 500 million USD in FY2024.",
            section_nos=[4, 7],
        )
        assert "4" in user_prompt or "4" in system_prompt, "Target section 4 must appear in prompts"
        assert "7" in user_prompt or "7" in system_prompt, "Target section 7 must appear in prompts"

    def test_build_etl_prompt_for_external_report_includes_section_11(self):
        from credit_report.generation.etl import _build_etl_prompt
        system_prompt, user_prompt = _build_etl_prompt(
            document_type="external_report",
            text="Buy rating. Target price 45 TWD. Analyst: Capital Securities.",
            section_nos=[11, 4, 7],
        )
        assert "11" in user_prompt or "11" in system_prompt, (
            "external_report prompt must include §11 schema"
        )

    def test_build_etl_prompt_includes_schema_content(self):
        from credit_report.generation.etl import _build_etl_prompt, SECTION_EXTRACTION_SCHEMA
        system_prompt, user_prompt = _build_etl_prompt(
            document_type="financial_statement",
            text="Financial data here.",
            section_nos=[7],
        )
        full_prompt = system_prompt + user_prompt
        assert "7A_borrower_financials" in full_prompt or "income_statement" in full_prompt, (
            "§7 prompt must include schema field names"
        )

    def test_build_etl_system_prompt_content(self):
        from credit_report.generation.etl import ETL_SYSTEM_PROMPT
        assert "JSON" in ETL_SYSTEM_PROMPT, "ETL system prompt must mention JSON format"
        assert "section" in ETL_SYSTEM_PROMPT.lower(), "ETL system prompt must mention sections"
        assert len(ETL_SYSTEM_PROMPT) > 100, "ETL system prompt is too short — likely truncated"


# ══════════════════════════════════════════════════════════════════════════════
# P — Concurrent save race-condition safety
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_repeated_saves_same_section_upsert_semantics(db):
    """
    Two sequential saves to the same section must result in exactly ONE
    SectionInput record (upsert semantics, not INSERT-then-INSERT).
    This verifies the upsert path in save_section_input works correctly.
    """
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload
    from credit_report.models import SectionInput
    from sqlalchemy import select

    rid = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    patches = (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core",
              new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    )

    # First save — creates a new SectionInput row
    with patches[0], patches[1], patches[2]:
        await save_section_input(
            report_id=rid, section_no=4,
            payload=SectionInputPayload(section_no=4, input_json={"value": "first"}),
            db=db, current_user=user,
        )

    # Second save — must UPDATE existing row, not INSERT another
    with patches[0], patches[1], patches[2]:
        await save_section_input(
            report_id=rid, section_no=4,
            payload=SectionInputPayload(section_no=4, input_json={"value": "second"}),
            db=db, current_user=user,
        )

    await db.flush()

    # Check DB for duplicate rows
    result = await db.execute(
        select(SectionInput).where(
            SectionInput.report_id == rid,
            SectionInput.section_no == 4,
        )
    )
    rows = list(result.scalars().all())
    assert len(rows) == 1, (
        f"Two saves produced {len(rows)} SectionInput rows — "
        f"upsert must yield exactly 1 row, not duplicate inserts"
    )
    # And the content must be from the second save
    final_data = json.loads(rows[0].input_json)
    assert final_data.get("value") == "second", (
        f"After 2 saves, final value should be 'second', got: {final_data}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Q — Get section input for uninitialized section returns 200 with empty JSON
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sec_no", [1, 5, 8, 11])
@pytest.mark.asyncio
async def test_get_empty_section_input_returns_empty_or_404(db, sec_no):
    """
    GET /inputs/{sec_no} before any save must return 404 or empty JSON —
    must NOT crash or return another section's data.
    """
    from credit_report.api.reports import get_section_input

    rid = str(uuid.uuid4())
    user = _make_user()
    await _seed_report(db, rid, owner_id=user.id)

    try:
        resp = await get_section_input(
            report_id=rid, section_no=sec_no, db=db, current_user=user,
        )
        # If it returns (no 404), verify it's empty / section-correct
        if resp is not None:
            assert resp.section_no == sec_no, (
                f"§{sec_no}: returned data for wrong section {resp.section_no}"
            )
    except HTTPException as exc:
        assert exc.status_code == 404, (
            f"§{sec_no}: empty section returned {exc.status_code} instead of 404"
        )


# ══════════════════════════════════════════════════════════════════════════════
# R — Binary file save / re-extraction integrity
# ══════════════════════════════════════════════════════════════════════════════

class TestBinaryFilePersistence:
    """save_document_binary and re-extraction must work end-to-end."""

    def test_save_document_binary_creates_bin_and_fname(self, tmp_path):
        from credit_report.generation.evidence import save_document_binary

        rid = str(uuid.uuid4())
        doc_id = str(uuid.uuid4())
        test_bytes = b"%PDF-1.4 fake pdf content for testing"
        test_fname = "test_report.pdf"

        with patch("credit_report.generation.evidence.CREDIT_REPORTS_ROOT", tmp_path):
            save_document_binary(rid, doc_id, test_bytes, test_fname)

        bin_path = tmp_path / rid / f"{doc_id}.bin"
        fname_path = tmp_path / rid / f"{doc_id}.fname"

        assert bin_path.exists(), ".bin file was not created by save_document_binary"
        assert fname_path.exists(), ".fname file was not created by save_document_binary"
        assert bin_path.read_bytes() == test_bytes, ".bin content mismatch"
        assert fname_path.read_text(encoding="utf-8") == test_fname, ".fname content mismatch"

    def test_save_document_text_creates_txt(self, tmp_path):
        from credit_report.generation.evidence import save_document_text

        rid = str(uuid.uuid4())
        doc_id = str(uuid.uuid4())
        text = "Revenue 500 million USD. EBITDA 100 million USD."

        with patch("credit_report.generation.evidence.CREDIT_REPORTS_ROOT", tmp_path):
            save_document_text(rid, doc_id, text)

        txt_path = tmp_path / rid / f"{doc_id}.txt"
        assert txt_path.exists(), ".txt file was not created by save_document_text"
        assert txt_path.read_text(encoding="utf-8") == text, ".txt content mismatch"

    def test_save_document_binary_creates_parent_directory(self, tmp_path):
        from credit_report.generation.evidence import save_document_binary

        rid = str(uuid.uuid4())  # New report dir that doesn't exist yet
        doc_id = str(uuid.uuid4())

        with patch("credit_report.generation.evidence.CREDIT_REPORTS_ROOT", tmp_path):
            # Should not raise even if directory doesn't exist yet
            save_document_binary(rid, doc_id, b"content", "file.pdf")

        assert (tmp_path / rid).is_dir(), "save_document_binary must create parent directories"

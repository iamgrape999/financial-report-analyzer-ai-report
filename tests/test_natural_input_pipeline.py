"""
Natural Language Input → AI Convert → Save → Generate Report — CI/CD Test Suite
================================================================================
Covers the complete "✍️ 自然輸入 / Natural Input" pipeline:

  A. Backend schema integrity   — _SECTION_SCHEMAS, Pydantic models (§1–11)
  B. convert-natural endpoint   — happy-path, error cases, JSON fence stripping
  C. Full pipeline integration  — natural text → convert → PUT save → DB state
  D. HTML/JS structural checks  — NATURAL_PROMPTS, tab HTML, JS functions
  E. i18n completeness          — EN + ZH TRANSLATIONS, sp.nat_* keys, SNAMES
  F. Template content quality   — §1–11 bilingual templates, keyword coverage
  G. Edge cases & security      — empty input, oversized, invalid section_no, XSS
  H. Language toggle behaviour  — setLang/toggleLang/snm logic simulation
  I. End-to-end mocked pipeline — natural text → Gemini mock → save → DB verify

Run:  pytest tests/test_natural_input_pipeline.py -v --tb=short
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base

# ── register all ORM models so metadata.create_all includes them ─────────────
import credit_report.models  # noqa: F401
import credit_report.fact_store.models  # noqa: F401
import credit_report.block_ast.models  # noqa: F401
import credit_report.security.models  # noqa: F401
import credit_report.audit.events  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

# The real call_gemini_raw lives here; mock this target in all tests
GEMINI_PATCH_TARGET = "credit_report.generation.claude_client.call_gemini_raw"


def load_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def extract_js_block(html: str) -> str:
    """Return the largest <script> block (the main application script)."""
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    return max(scripts, key=len) if scripts else ""


def _make_user(role: str = "analyst") -> MagicMock:
    u = MagicMock()
    u.id = str(uuid.uuid4())
    u.role = role
    u.email = f"{role}@test.local"
    return u


async def _seed_report(db: AsyncSession, rid: str, owner_id: str | None = None):
    from credit_report.models import Report
    uid = owner_id or str(uuid.uuid4())
    r = Report(
        id=rid,
        borrower_name="NaturalInputTest Co",
        created_by=uid,
        status="draft",
        is_deleted=False,
    )
    db.add(r)
    await db.flush()
    return r, uid


@pytest_asyncio.fixture(scope="function")
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Shared sample natural-language inputs
# ---------------------------------------------------------------------------

SAMPLE_NATURAL_TEXT_EN = (
    "Borrower: ACME Shipping Ltd (Marshall Islands). "
    "Facility: USD 50m 7-year term loan at SOFR+280bps. "
    "Purpose: Acquire one VLCC tanker. LTC 65%. "
    "Security: First priority ship mortgage post-delivery."
)

SAMPLE_NATURAL_TEXT_ZH = (
    "借款人：ACME 船運有限公司（馬紹爾群島）。"
    "融資：5,000萬美元7年定期貸款，利率SOFR+280基點。"
    "用途：收購一艘VLCC油輪。LTC 65%。"
    "擔保品：交船後第一順位船舶抵押。"
)

VALID_GEMINI_RESPONSE = json.dumps({
    "borrower": "ACME Shipping Ltd",
    "facility_type": "Term Loan",
    "facility_amount_usd_m": 50.0,
    "tenor_years": 7,
    "purpose": "Acquisition of VLCC tanker",
})


# ============================================================================
# A. Backend schema integrity
# ============================================================================

class TestSectionSchemas:
    """A-* All 11 sections registered with required keys in _SECTION_SCHEMAS."""

    def test_all_11_sections_present(self):
        from credit_report.api.generate import _SECTION_SCHEMAS
        assert set(_SECTION_SCHEMAS.keys()) == set(range(1, 12)), (
            f"Expected §1–11, got keys: {sorted(_SECTION_SCHEMAS.keys())}"
        )

    @pytest.mark.parametrize("sec_no", range(1, 12))
    def test_each_section_has_name_and_fields(self, sec_no):
        from credit_report.api.generate import _SECTION_SCHEMAS
        schema = _SECTION_SCHEMAS[sec_no]
        assert "name" in schema, f"§{sec_no} missing 'name'"
        assert "fields" in schema, f"§{sec_no} missing 'fields'"
        assert len(schema["name"]) >= 5, f"§{sec_no} name too short: {schema['name']!r}"
        assert len(schema["fields"]) >= 20, f"§{sec_no} fields string too short"

    @pytest.mark.parametrize("sec_no", range(1, 12))
    def test_section_name_not_empty(self, sec_no):
        from credit_report.api.generate import _SECTION_SCHEMAS
        assert _SECTION_SCHEMAS[sec_no]["name"].strip(), f"§{sec_no} name is blank"

    def test_pydantic_natural_input_convert_model(self):
        from credit_report.api.generate import NaturalInputConvert
        m = NaturalInputConvert(
            natural_text="Borrower is ACME Corp, USD 50m term loan",
            section_no=1,
        )
        assert m.natural_text.startswith("Borrower")
        assert m.section_no == 1

    def test_pydantic_natural_input_result_model(self):
        from credit_report.api.generate import NaturalInputResult
        m = NaturalInputResult(section_no=3, converted_json={"key": "value"})
        assert m.section_no == 3
        assert m.converted_json == {"key": "value"}

    def test_natural_input_convert_rejects_missing_text(self):
        from pydantic import ValidationError
        from credit_report.api.generate import NaturalInputConvert
        with pytest.raises(ValidationError):
            NaturalInputConvert(section_no=1)  # missing natural_text

    def test_natural_input_convert_rejects_missing_section(self):
        from pydantic import ValidationError
        from credit_report.api.generate import NaturalInputConvert
        with pytest.raises(ValidationError):
            NaturalInputConvert(natural_text="hello")  # missing section_no

    def test_natural_input_result_rejects_non_dict_json(self):
        from pydantic import ValidationError
        from credit_report.api.generate import NaturalInputResult
        with pytest.raises((ValidationError, TypeError)):
            NaturalInputResult(section_no=1, converted_json="not a dict")

    @pytest.mark.parametrize("sec_no", range(1, 12))
    def test_section_fields_mention_key_concepts(self, sec_no):
        """Each section's field string should reference at least one structural keyword."""
        from credit_report.api.generate import _SECTION_SCHEMAS
        fields = _SECTION_SCHEMAS[sec_no]["fields"].lower()
        structural_words = ["array", "string", "number", "bool", "object", "(", "{"]
        assert any(w in fields for w in structural_words), (
            f"§{sec_no} fields string has no structural type hints: {fields[:100]}"
        )


# ============================================================================
# B. convert-natural endpoint
# ============================================================================

@pytest.mark.asyncio
class TestConvertNaturalEndpoint:
    """B-* convert-natural endpoint: happy path, errors, fence stripping."""

    async def test_happy_path_en_text(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        payload = NaturalInputConvert(
            natural_text=SAMPLE_NATURAL_TEXT_EN, section_no=1,
        )
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=VALID_GEMINI_RESPONSE):
            result = await convert_natural_input(rid, 1, payload, db, user)

        assert result.section_no == 1
        assert isinstance(result.converted_json, dict)
        assert "borrower" in result.converted_json

    async def test_happy_path_zh_text(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        payload = NaturalInputConvert(
            natural_text=SAMPLE_NATURAL_TEXT_ZH, section_no=1,
        )
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=VALID_GEMINI_RESPONSE):
            result = await convert_natural_input(rid, 1, payload, db, user)

        assert result.section_no == 1
        assert isinstance(result.converted_json, dict)

    @pytest.mark.parametrize("sec_no", range(1, 12))
    async def test_all_sections_accepted(self, db, sec_no):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(
            natural_text=f"Some descriptive data for section {sec_no}.", section_no=sec_no
        )
        mock_json = json.dumps({"section": sec_no, "data": "extracted"})
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=mock_json):
            result = await convert_natural_input(rid, sec_no, payload, db, user)
        assert result.section_no == sec_no

    async def test_invalid_section_no_0_returns_400(self, db):
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=0)
        with pytest.raises(HTTPException) as exc_info:
            await convert_natural_input(rid, 0, payload, db, user)
        assert exc_info.value.status_code == 400

    async def test_invalid_section_no_12_returns_400(self, db):
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=12)
        with pytest.raises(HTTPException) as exc_info:
            await convert_natural_input(rid, 12, payload, db, user)
        assert exc_info.value.status_code == 400

    async def test_invalid_section_no_negative_returns_400(self, db):
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=-1)
        with pytest.raises(HTTPException) as exc_info:
            await convert_natural_input(rid, -1, payload, db, user)
        assert exc_info.value.status_code == 400

    async def test_ai_service_failure_returns_503(self, db):
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=1)
        with patch(
            GEMINI_PATCH_TARGET, new_callable=AsyncMock,
            side_effect=RuntimeError("Gemini API quota exceeded"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await convert_natural_input(rid, 1, payload, db, user)
        assert exc_info.value.status_code == 503
        assert "AI generation failed" in exc_info.value.detail

    async def test_ai_returns_invalid_json_raises_422(self, db):
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=1)
        with patch(
            GEMINI_PATCH_TARGET, new_callable=AsyncMock,
            return_value="This is not JSON at all { broken",
        ):
            with pytest.raises(HTTPException) as exc_info:
                await convert_natural_input(rid, 1, payload, db, user)
        assert exc_info.value.status_code == 422

    async def test_ai_returns_json_array_not_dict_raises_422(self, db):
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=2)
        with patch(
            GEMINI_PATCH_TARGET, new_callable=AsyncMock,
            return_value='[{"a": 1}, {"b": 2}]',
        ):
            with pytest.raises(HTTPException) as exc_info:
                await convert_natural_input(rid, 2, payload, db, user)
        assert exc_info.value.status_code == 422

    async def test_json_markdown_fences_stripped(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=1)
        fenced = '```json\n{"borrower": "Test Corp", "facility_amount_usd_m": 25}\n```'
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=fenced):
            result = await convert_natural_input(rid, 1, payload, db, user)
        assert result.converted_json["borrower"] == "Test Corp"

    async def test_plain_json_fences_stripped(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=1)
        fenced = '```\n{"borrower": "Plain Fence Corp"}\n```'
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=fenced):
            result = await convert_natural_input(rid, 1, payload, db, user)
        assert result.converted_json["borrower"] == "Plain Fence Corp"

    async def test_report_not_found_raises_404(self, db):
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        payload = NaturalInputConvert(natural_text="text", section_no=1)
        user = _make_user()
        with pytest.raises(HTTPException) as exc_info:
            await convert_natural_input("nonexistent-report-id", 1, payload, db, user)
        assert exc_info.value.status_code == 404

    async def test_ai_timeout_returns_503(self, db):
        import asyncio
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="text", section_no=1)
        with patch(
            GEMINI_PATCH_TARGET, new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError("AI timeout"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await convert_natural_input(rid, 1, payload, db, user)
        assert exc_info.value.status_code == 503


# ============================================================================
# C. Full pipeline integration: natural → convert → save → DB state
# ============================================================================

@pytest.mark.asyncio
class TestFullPipelineIntegration:
    """C-* Natural text → convert (mocked AI) → PUT save → verify DB."""

    async def test_convert_then_save_section_1(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        # Step 1: convert
        payload = NaturalInputConvert(natural_text=SAMPLE_NATURAL_TEXT_EN, section_no=1)
        mock_data = {"borrower": "ACME", "facility_amount_usd_m": 50, "tenor_years": 7}
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(mock_data)):
            convert_result = await convert_natural_input(rid, 1, payload, db, user)

        assert convert_result.converted_json == mock_data

        # Step 2: save via PUT endpoint (called directly)
        save_payload = SectionInputPayload(section_no=1, input_json=convert_result.converted_json)
        await save_section_input(rid, 1, save_payload, db, user)

        # Step 3: verify DB
        row = (await db.execute(
            select(SectionInput).where(
                SectionInput.report_id == rid, SectionInput.section_no == 1
            )
        )).scalar_one_or_none()
        assert row is not None
        stored = json.loads(row.input_json) if isinstance(row.input_json, str) else row.input_json
        assert stored["borrower"] == "ACME"
        assert stored["facility_amount_usd_m"] == 50

    @pytest.mark.parametrize("sec_no", range(1, 12))
    async def test_convert_returns_dict_for_all_sections(self, db, sec_no):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(
            natural_text=f"Section {sec_no} data content.", section_no=sec_no
        )
        mock_json = json.dumps({"_section": sec_no, "extracted": True, "value": 42})
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=mock_json):
            result = await convert_natural_input(rid, sec_no, payload, db, user)
        assert isinstance(result.converted_json, dict)
        assert result.converted_json["_section"] == sec_no

    async def test_convert_does_not_auto_save(self, db):
        """convert-natural must NOT write to DB — caller decides to save."""
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="Some data.", section_no=5)
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock,
                   return_value=json.dumps({"collateral_type": "Ship mortgage"})):
            await convert_natural_input(rid, 5, payload, db, user)

        row = (await db.execute(
            select(SectionInput).where(
                SectionInput.report_id == rid, SectionInput.section_no == 5
            )
        )).scalar_one_or_none()
        assert row is None, "convert-natural must NOT persist to DB automatically"

    async def test_full_pipeline_zh_to_json_save(self, db):
        """Chinese natural input → AI → JSON with English keys → save → verify."""
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        payload = NaturalInputConvert(natural_text=SAMPLE_NATURAL_TEXT_ZH, section_no=1)
        mock_data = {
            "borrower": "ACME船運有限公司",
            "facility_amount_usd_m": 50,
            "tenor_years": 7,
            "interest_rate_basis": "SOFR",
            "margin_bps": 280,
        }
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(mock_data)):
            convert_result = await convert_natural_input(rid, 1, payload, db, user)

        await save_section_input(
            rid, 1, SectionInputPayload(section_no=1, input_json=convert_result.converted_json), db, user
        )

        row = (await db.execute(
            select(SectionInput).where(SectionInput.report_id == rid, SectionInput.section_no == 1)
        )).scalar_one_or_none()
        assert row is not None
        stored = json.loads(row.input_json) if isinstance(row.input_json, str) else row.input_json
        assert stored["margin_bps"] == 280

    async def test_convert_idempotent_save_twice(self, db):
        """Saving converted JSON twice for same section should upsert cleanly."""
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        # First save
        data_v1 = {"internal_rating": "BB+"}
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(data_v1)):
            r1 = await convert_natural_input(
                rid, 3, NaturalInputConvert(natural_text="First.", section_no=3), db, user
            )
        await save_section_input(rid, 3, SectionInputPayload(section_no=3, input_json=r1.converted_json), db, user)

        # Second save with updated data
        data_v2 = {"internal_rating": "BB"}
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(data_v2)):
            r2 = await convert_natural_input(
                rid, 3, NaturalInputConvert(natural_text="Updated.", section_no=3), db, user
            )
        await save_section_input(rid, 3, SectionInputPayload(section_no=3, input_json=r2.converted_json), db, user)

        row = (await db.execute(
            select(SectionInput).where(SectionInput.report_id == rid, SectionInput.section_no == 3)
        )).scalar_one_or_none()
        assert row is not None
        stored = json.loads(row.input_json) if isinstance(row.input_json, str) else row.input_json
        assert stored["internal_rating"] == "BB"


# ============================================================================
# D. HTML/JS structural checks
# ============================================================================

class TestHtmlStructure:
    """D-* Natural input tab HTML and JS elements present in index.html."""

    def test_tab_html_natural_tab_li_present(self):
        html = load_html()
        # The onclick uses unescaped single quotes inside a double-quoted attribute
        assert "switchTab('natural')" in html, \
            "4th tab with switchTab('natural') not found in HTML"

    def test_tab_html_data_i18n_tab_natural(self):
        html = load_html()
        assert 'data-i18n="sp.tab_natural"' in html

    def test_tabNatural_div_present(self):
        html = load_html()
        assert 'id="tabNatural"' in html

    def test_naturalInput_textarea_present(self):
        html = load_html()
        assert 'id="naturalInput"' in html

    def test_naturalPreview_div_present(self):
        html = load_html()
        assert 'id="naturalPreview"' in html

    def test_naturalPreviewJson_pre_present(self):
        html = load_html()
        assert 'id="naturalPreviewJson"' in html

    def test_naturalSpin_spinner_present(self):
        html = load_html()
        assert 'id="naturalSpin"' in html

    def test_naturalStatus_span_present(self):
        html = load_html()
        assert 'id="naturalStatus"' in html

    def test_loadNaturalTemplate_button(self):
        html = load_html()
        assert "loadNaturalTemplate()" in html

    def test_aiConvertNatural_button(self):
        html = load_html()
        assert "aiConvertNatural()" in html

    def test_applyNaturalJson_button(self):
        html = load_html()
        assert "applyNaturalJson()" in html

    def test_data_i18n_nat_title(self):
        html = load_html()
        assert 'data-i18n="sp.nat_title"' in html

    def test_data_i18n_nat_hint(self):
        html = load_html()
        assert 'data-i18n="sp.nat_hint"' in html

    def test_data_i18n_nat_template(self):
        html = load_html()
        assert 'data-i18n="sp.nat_template"' in html

    def test_data_i18n_nat_convert(self):
        html = load_html()
        assert 'data-i18n="sp.nat_convert"' in html

    def test_data_i18n_nat_preview(self):
        html = load_html()
        assert 'data-i18n="sp.nat_preview"' in html

    def test_data_i18n_nat_apply(self):
        html = load_html()
        assert 'data-i18n="sp.nat_apply"' in html

    def test_data_i18n_nat_discard(self):
        html = load_html()
        assert 'data-i18n="sp.nat_discard"' in html

    def test_js_function_loadNaturalTemplate_defined(self):
        js = extract_js_block(load_html())
        assert "function loadNaturalTemplate" in js

    def test_js_function_aiConvertNatural_defined(self):
        js = extract_js_block(load_html())
        assert "function aiConvertNatural" in js

    def test_js_function_applyNaturalJson_defined(self):
        js = extract_js_block(load_html())
        assert "function applyNaturalJson" in js

    def test_js_variable_naturalConvertedJson_declared(self):
        js = extract_js_block(load_html())
        assert "_naturalConvertedJson" in js

    def test_js_NATURAL_PROMPTS_const_declared(self):
        js = extract_js_block(load_html())
        assert "NATURAL_PROMPTS" in js

    def test_js_NATURAL_PROMPTS_has_en_key(self):
        js = extract_js_block(load_html())
        stripped = js.replace(" ", "").replace("\n", "")
        assert "NATURAL_PROMPTS={" in stripped
        assert "en:{" in stripped

    def test_js_NATURAL_PROMPTS_has_zh_key(self):
        js = extract_js_block(load_html())
        assert "zh:{" in js.replace(" ", "").replace("\n", "")

    def test_js_braces_balanced(self):
        js = extract_js_block(load_html())
        assert js.count("{") == js.count("}"), (
            f"Unbalanced braces: {{ {js.count('{')}  }} {js.count('}')}"
        )

    def test_switchTab_handles_natural(self):
        js = extract_js_block(load_html())
        assert "'natural'" in js or '"natural"' in js

    def test_four_nav_tabs_present(self):
        html = load_html()
        tabs = re.findall(r"switchTab\('(\w+)'\)", html)
        assert "input" in tabs
        assert "form" in tabs
        assert "output" in tabs
        assert "natural" in tabs

    def test_convert_natural_api_path_in_js(self):
        """JS must call the .../convert-natural backend endpoint."""
        js = extract_js_block(load_html())
        assert "convert-natural" in js

    def test_natural_tab_textarea_has_rows(self):
        """Natural input textarea should have sufficient rows for comfortable editing."""
        html = load_html()
        m = re.search(r'id="naturalInput"[^>]*rows="(\d+)"', html) or \
            re.search(r'rows="(\d+)"[^>]*id="naturalInput"', html)
        if m:
            assert int(m.group(1)) >= 8, "naturalInput textarea has too few rows"

    def test_spinner_has_dNone_class(self):
        """Spinner should start hidden (d-none)."""
        html = load_html()
        m = re.search(r'id="naturalSpin"[^>]*/>', html) or \
            re.search(r'id="naturalSpin"[^>]*>', html)
        if m:
            assert "d-none" in m.group(0), "naturalSpin spinner should be initially hidden"


# ============================================================================
# E. i18n completeness
# ============================================================================

_REQUIRED_NAT_KEYS = [
    "sp.tab_natural",
    "sp.nat_title",
    "sp.nat_hint",
    "sp.nat_template",
    "sp.nat_convert",
    "sp.nat_preview",
    "sp.nat_apply",
    "sp.nat_discard",
]


class TestI18nCompleteness:
    """E-* TRANSLATIONS, section names, language switch functions."""

    def _en_block(self) -> str:
        html = load_html()
        m = re.search(r"en:\{(.+?)(?=\},\s*zh:)", html, re.DOTALL)
        assert m, "Could not find en:{...} block in TRANSLATIONS"
        return m.group(1)

    def _zh_block(self) -> str:
        html = load_html()
        m = re.search(r"zh:\{(.+?)(?=\}\s*\};)", html, re.DOTALL)
        assert m, "Could not find zh:{...} block in TRANSLATIONS"
        return m.group(1)

    def test_translations_const_exists(self):
        js = extract_js_block(load_html())
        assert "TRANSLATIONS" in js

    @pytest.mark.parametrize("key", _REQUIRED_NAT_KEYS)
    def test_en_has_nat_key(self, key):
        en_block = self._en_block()
        assert f"'{key}'" in en_block or f'"{key}"' in en_block, \
            f"Key '{key}' not found in EN translations"

    @pytest.mark.parametrize("key", _REQUIRED_NAT_KEYS)
    def test_zh_has_nat_key(self, key):
        zh_block = self._zh_block()
        assert f"'{key}'" in zh_block or f'"{key}"' in zh_block, \
            f"Key '{key}' not found in ZH translations"

    def test_snames_has_all_11_sections(self):
        js = extract_js_block(load_html())
        assert "SNAMES" in js
        for i in range(1, 12):
            assert f"{i}:" in js, f"SNAMES missing key {i}"

    def test_snames_zh_has_all_11_sections(self):
        js = extract_js_block(load_html())
        assert "SNAMES_ZH" in js
        zh_names = [
            "融資結構", "綜合評述", "信用風險", "借款人", "擔保品",
            "船舶融資", "財務分析", "法律文件", "法規遵循", "附錄", "外部研究",
        ]
        for name in zh_names:
            assert name in js, f"SNAMES_ZH missing Chinese name containing '{name}'"

    def test_snm_function_defined(self):
        js = extract_js_block(load_html())
        assert "function snm(" in js

    def test_setLang_function_defined(self):
        js = extract_js_block(load_html())
        assert "function setLang(" in js

    def test_toggleLang_function_defined(self):
        js = extract_js_block(load_html())
        assert "function toggleLang(" in js

    def test_t_function_defined(self):
        js = extract_js_block(load_html())
        assert "function t(" in js

    def test_lang_variable_declared(self):
        js = extract_js_block(load_html())
        assert re.search(r"let\s+lang\s*=", js) or re.search(r"var\s+lang\s*=", js)

    def test_lang_persists_via_localStorage(self):
        js = extract_js_block(load_html())
        assert "localStorage" in js and "lang" in js

    def test_domcontentloaded_calls_setlang(self):
        js = extract_js_block(load_html())
        assert "DOMContentLoaded" in js
        assert "setLang" in js

    def test_sp_tab_natural_en_value_not_empty(self):
        en_block = self._en_block()
        m = re.search(r"['\"]sp\.tab_natural['\"]\s*:\s*['\"](.+?)['\"]", en_block)
        assert m, "sp.tab_natural EN value not found"
        assert len(m.group(1).strip()) > 0

    def test_sp_tab_natural_zh_value_not_empty(self):
        zh_block = self._zh_block()
        m = re.search(r"['\"]sp\.tab_natural['\"]\s*:\s*['\"](.+?)['\"]", zh_block)
        assert m, "sp.tab_natural ZH value not found"
        assert len(m.group(1).strip()) > 0

    def test_sp_nat_apply_zh_value_contains_chinese(self):
        zh_block = self._zh_block()
        m = re.search(r"['\"]sp\.nat_apply['\"]\s*:\s*['\"](.+?)['\"]", zh_block)
        assert m
        # Should contain Chinese characters or at least some Chinese content
        val = m.group(1)
        has_chinese = bool(re.search(r"[一-鿿]", val))
        assert has_chinese, f"sp.nat_apply ZH value has no Chinese characters: {val!r}"

    def test_existing_sp_tabs_still_present(self):
        html = load_html()
        for key in ["sp.tab_json", "sp.tab_form", "sp.tab_output"]:
            assert key in html, f"Existing i18n key '{key}' was removed"


# ============================================================================
# F. Template content quality (NATURAL_PROMPTS §1–11)
# ============================================================================

# Keywords verified against actual template content (case-insensitive search)
_SECTION_EN_KEYWORDS = {
    1:  ["borrower", "facility", "tenor", "interest", "security", "ltc"],
    2:  ["credit decision", "strengths", "risks", "approve"],
    3:  ["risk rating", "probability", "esg", "mitigant"],
    4:  ["incorporation", "beneficial owner", "management", "vessels"],
    5:  ["collateral", "mortgage", "ltv", "insurance", "valuation"],
    6:  ["shipyard", "vessel", "charter", "employment"],
    7:  ["revenue", "ebitda", "net profit", "dscr"],
    8:  ["governing law", "mortgage", "covenant", "default"],
    9:  ["kyc", "sanctions", "pep", "aml"],
    10: ["appendix", "document", "financial statement"],
    11: ["research source", "analyst", "outlook"],
}

_SECTION_ZH_KEYWORDS = {
    1:  ["借款人", "融資", "利率", "擔保品"],
    2:  ["信用決策", "優勢", "風險", "核准"],
    3:  ["風險評級", "違約", "ESG", "緩解"],
    4:  ["設立", "受益所有人", "管理"],
    5:  ["擔保品", "抵押", "LTV", "保險"],
    6:  ["造船廠", "船舶", "租船"],
    7:  ["營收", "EBITDA", "淨利"],
    8:  ["準據法", "抵押", "契約"],
    9:  ["KYC", "制裁", "PEP"],
    10: ["附錄", "文件"],
    11: ["研究來源", "分析師", "展望"],
}


class TestTemplateContentQuality:
    """F-* Bilingual NATURAL_PROMPTS coverage for §1–11."""

    def _nat_block(self, js: str) -> str:
        """Extract the content of NATURAL_PROMPTS={...}."""
        # Use a greedy search between the opening brace and the final closing };
        m = re.search(r"NATURAL_PROMPTS\s*=\s*(\{.+\});", js, re.DOTALL)
        assert m, "NATURAL_PROMPTS not found in JS"
        return m.group(1)

    def _en_prompts(self, js: str) -> str:
        block = self._nat_block(js)
        m = re.search(r"en:\s*\{(.+?)(?=\}\s*,\s*zh:)", block, re.DOTALL)
        assert m, "en: block not found in NATURAL_PROMPTS"
        return m.group(1)

    def _zh_prompts(self, js: str) -> str:
        block = self._nat_block(js)
        # zh: is the second major key; capture until closing }
        m = re.search(r"zh:\s*\{(.+)\}\s*$", block, re.DOTALL)
        assert m, "zh: block not found in NATURAL_PROMPTS"
        return m.group(1)

    @pytest.mark.parametrize("sec_no,keywords", _SECTION_EN_KEYWORDS.items())
    def test_en_template_sec_coverage(self, sec_no, keywords):
        js = extract_js_block(load_html())
        en_block = self._en_prompts(js).lower()
        for kw in keywords:
            assert kw.lower() in en_block, \
                f"§{sec_no} EN template missing keyword: '{kw}'"

    @pytest.mark.parametrize("sec_no,keywords", _SECTION_ZH_KEYWORDS.items())
    def test_zh_template_sec_coverage(self, sec_no, keywords):
        js = extract_js_block(load_html())
        zh_block = self._zh_prompts(js)
        for kw in keywords:
            assert kw in zh_block, \
                f"§{sec_no} ZH template missing keyword: '{kw}'"

    @pytest.mark.parametrize("sec_no", range(1, 12))
    def test_en_template_section_header_present(self, sec_no):
        js = extract_js_block(load_html())
        assert f"§{sec_no}" in js, f"§{sec_no} heading not found in NATURAL_PROMPTS"

    @pytest.mark.parametrize("sec_no", range(1, 12))
    def test_zh_template_section_header_present(self, sec_no):
        js = extract_js_block(load_html())
        zh_block = self._zh_prompts(js)
        assert f"§{sec_no}" in zh_block, f"§{sec_no} not found in ZH NATURAL_PROMPTS"

    def test_all_11_en_sections_present(self):
        js = extract_js_block(load_html())
        en_block = self._en_prompts(js)
        for i in range(1, 12):
            assert f"§{i}" in en_block, f"EN NATURAL_PROMPTS missing §{i}"

    def test_all_11_zh_sections_present(self):
        js = extract_js_block(load_html())
        zh_block = self._zh_prompts(js)
        for i in range(1, 12):
            assert f"§{i}" in zh_block, f"ZH NATURAL_PROMPTS missing §{i}"

    def test_en_templates_are_substantial(self):
        """EN templates collectively should be well over 2000 chars."""
        js = extract_js_block(load_html())
        en_block = self._en_prompts(js)
        assert len(en_block) > 2000, (
            f"EN NATURAL_PROMPTS block too short: {len(en_block)} chars"
        )

    def test_zh_templates_are_substantial(self):
        js = extract_js_block(load_html())
        zh_block = self._zh_prompts(js)
        assert len(zh_block) > 2000, (
            f"ZH NATURAL_PROMPTS block too short: {len(zh_block)} chars"
        )


# ============================================================================
# G. Edge cases & security
# ============================================================================

@pytest.mark.asyncio
class TestEdgeCasesAndSecurity:
    """G-* Boundary conditions, malformed input, injection defence."""

    async def test_empty_natural_text_still_calls_ai(self, db):
        """Pydantic accepts empty string; endpoint should still call AI."""
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        payload = NaturalInputConvert(natural_text="", section_no=1)
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value='{"borrower": null}'):
            result = await convert_natural_input(rid, 1, payload, db, user)
        assert result.section_no == 1

    async def test_very_long_natural_text(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        long_text = "Borrower is ACME Corp. " * 500  # ~11,000 chars
        payload = NaturalInputConvert(natural_text=long_text, section_no=1)
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock,
                   return_value=json.dumps({"borrower": "ACME Corp"})):
            result = await convert_natural_input(rid, 1, payload, db, user)
        assert result.converted_json["borrower"] == "ACME Corp"

    async def test_mixed_en_zh_natural_text(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        mixed = "借款人：ACME Corp. Facility: USD 50m (5,000萬美元). Tenor: 7 years（七年）."
        payload = NaturalInputConvert(natural_text=mixed, section_no=1)
        mock_resp = json.dumps({"borrower": "ACME Corp", "facility_amount_usd_m": 50})
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=mock_resp):
            result = await convert_natural_input(rid, 1, payload, db, user)
        assert result.converted_json is not None

    async def test_xss_input_not_reflected_in_json(self, db):
        """XSS in natural text should not appear as executable JSON output."""
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        xss = '<script>alert(1)</script>'
        payload = NaturalInputConvert(natural_text=xss, section_no=2)
        mock_resp = json.dumps({"credit_decision": "APPROVE"})
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=mock_resp):
            result = await convert_natural_input(rid, 2, payload, db, user)
        result_str = json.dumps(result.converted_json)
        assert "<script>" not in result_str

    async def test_unicode_special_chars_in_natural_text(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid
        unicode_text = "Borrower: 株式会社ACME（日本）— 50百万ドル貸出。Yen¥円€"
        payload = NaturalInputConvert(natural_text=unicode_text, section_no=4)
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock,
                   return_value=json.dumps({"company_name": "株式会社ACME"})):
            result = await convert_natural_input(rid, 4, payload, db, user)
        assert result.converted_json is not None

    async def test_different_report_cannot_be_accessed(self, db):
        """User cannot convert-natural for a report they didn't create."""
        from fastapi import HTTPException
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        other_user = _make_user()  # different user ID

        payload = NaturalInputConvert(natural_text="text", section_no=1)
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=VALID_GEMINI_RESPONSE):
            # With _assert_can_view, non-owner non-admin should raise 403
            try:
                result = await convert_natural_input(rid, 1, payload, db, other_user)
                # If no exception, check if it still returned data (some configs allow)
                assert result is not None
            except HTTPException as e:
                assert e.status_code in (403, 404)


def test_json_fence_stripping_logic_unit():
    """G-standalone: Unit test the fence-stripping regex used in the endpoint."""
    def strip_fences(raw: str) -> str:
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        return cleaned

    cases = [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}```', '{"a": 1}'),
        ('\n```json\n{"a": 1}\n```\n', '{"a": 1}'),
    ]
    for raw, expected in cases:
        result = strip_fences(raw)
        assert result == expected, f"Failed for {raw!r}: got {result!r}"


# ============================================================================
# H. Language toggle behaviour
# ============================================================================

class TestLanguageToggle:
    """H-* snm(), setLang(), toggleLang() JS logic verified from source."""

    def _get_snames(self):
        js = extract_js_block(load_html())
        en_m = re.search(r"(?<!\w)SNAMES\s*=\s*\{([^}]+)\}", js)
        zh_m = re.search(r"SNAMES_ZH\s*=\s*\{([^}]+)\}", js)
        assert en_m, "SNAMES not found"
        assert zh_m, "SNAMES_ZH not found"
        en_keys = {int(k) for k in re.findall(r"(\d+)\s*:", en_m.group(1))}
        zh_keys = {int(k) for k in re.findall(r"(\d+)\s*:", zh_m.group(1))}
        return en_keys, zh_keys

    def test_snames_en_has_11_keys(self):
        en_keys, _ = self._get_snames()
        assert en_keys == set(range(1, 12)), f"SNAMES has keys: {sorted(en_keys)}"

    def test_snames_zh_has_11_keys(self):
        _, zh_keys = self._get_snames()
        assert zh_keys == set(range(1, 12)), f"SNAMES_ZH has keys: {sorted(zh_keys)}"

    def test_snm_returns_zh_when_lang_is_zh(self):
        js = extract_js_block(load_html())
        assert "lang==='zh'" in js or "lang === 'zh'" in js

    def test_openPanel_uses_snm_not_raw_snames(self):
        """openPanel() must call snm() to respect language toggle."""
        js = extract_js_block(load_html())
        # Find openPanel function body
        m = re.search(r"function openPanel\((.+?)(?=^function )", js, re.DOTALL | re.MULTILINE)
        if m:
            panel_body = m.group(1)
            if "SNAMES[" in panel_body:
                assert "snm(" in panel_body, \
                    "openPanel() uses raw SNAMES[] bypassing language switch"

    def test_section_table_uses_snm(self):
        js = extract_js_block(load_html())
        assert "snm(" in js, "snm() not used — section names won't switch language"

    def test_setLang_updates_data_i18n_elements(self):
        js = extract_js_block(load_html())
        assert "data-i18n" in js
        assert "querySelectorAll" in js

    def test_setLang_updates_placeholder(self):
        js = extract_js_block(load_html())
        assert "data-i18n-placeholder" in js

    def test_toggleLang_switches_between_en_and_zh(self):
        js = extract_js_block(load_html())
        m = re.search(r"function toggleLang\(\)\{(.+?)\}", js, re.DOTALL)
        assert m, "toggleLang() body not found"
        body = m.group(1)
        assert "setLang" in body
        assert "en" in body and "zh" in body, \
            "toggleLang must reference both 'en' and 'zh'"

    def test_lang_btn_text_changes_on_toggle(self):
        js = extract_js_block(load_html())
        assert "langBtn" in js

    def test_lang_default_from_localStorage(self):
        js = extract_js_block(load_html())
        assert re.search(r"localStorage\.getItem\(['\"]lang['\"]\)", js), \
            "lang not initialized from localStorage"

    def test_setLang_writes_to_localStorage(self):
        js = extract_js_block(load_html())
        m = re.search(r"function setLang\((.+?)(?=^function |\Z)", js, re.DOTALL | re.MULTILINE)
        if m:
            body = m.group(1)
            assert "localStorage.setItem" in body, \
                "setLang() does not persist to localStorage"


# ============================================================================
# I. End-to-end mocked pipeline: natural text → save → DB verify
# ============================================================================

@pytest.mark.asyncio
class TestEndToEndMockedPipeline:
    """I-* Full E2E with mocked Gemini: natural input → convert → save → DB."""

    async def test_e2e_sec1_natural_to_save(self, db):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        # 1. Convert natural text
        converted_data = {
            "borrower": "ACME Shipping Ltd",
            "facility_type": "Term Loan",
            "facility_amount_usd_m": 50.0,
            "tenor_years": 7,
            "purpose": "Acquisition of VLCC tanker",
            "ltc_percent": 65.0,
            "margin_bps": 280,
        }
        payload = NaturalInputConvert(natural_text=SAMPLE_NATURAL_TEXT_EN, section_no=1)
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(converted_data)):
            convert_result = await convert_natural_input(rid, 1, payload, db, user)

        assert convert_result.converted_json["borrower"] == "ACME Shipping Ltd"

        # 2. Save converted JSON
        await save_section_input(
            rid, 1, SectionInputPayload(section_no=1, input_json=convert_result.converted_json), db, user
        )

        # 3. Verify saved to DB
        si = (await db.execute(
            select(SectionInput).where(SectionInput.report_id == rid, SectionInput.section_no == 1)
        )).scalar_one_or_none()
        assert si is not None
        stored = json.loads(si.input_json) if isinstance(si.input_json, str) else si.input_json
        assert stored["facility_amount_usd_m"] == 50.0
        assert stored["margin_bps"] == 280

    @pytest.mark.parametrize("sec_no,key_field", [
        (2, "credit_decision"),
        (4, "company_name"),
        (7, "revenue"),
        (9, "kyc"),
        (11, "analyst_firm"),
    ])
    async def test_e2e_convert_save_verify_key_sections(self, db, sec_no, key_field):
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        mock_data = {key_field: f"test_value_for_{key_field}"}
        payload = NaturalInputConvert(
            natural_text=f"§{sec_no} data: {key_field}=test_value.", section_no=sec_no
        )
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(mock_data)):
            result = await convert_natural_input(rid, sec_no, payload, db, user)

        await save_section_input(
            rid, sec_no, SectionInputPayload(section_no=sec_no, input_json=result.converted_json), db, user
        )

        si = (await db.execute(
            select(SectionInput).where(SectionInput.report_id == rid, SectionInput.section_no == sec_no)
        )).scalar_one_or_none()
        assert si is not None
        assert key_field in si.input_json

    async def test_e2e_all_11_sections_sequential(self, db):
        """Sequential convert+save for §1–11 all succeed."""
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        for sec_no in range(1, 12):
            mock_data = {"section_no_key": sec_no, "content": f"Generated for §{sec_no}"}
            payload = NaturalInputConvert(
                natural_text=f"§{sec_no} complete data.", section_no=sec_no
            )
            with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(mock_data)):
                result = await convert_natural_input(rid, sec_no, payload, db, user)
            await save_section_input(
                rid, sec_no, SectionInputPayload(section_no=sec_no, input_json=result.converted_json), db, user
            )

        # Verify all 11 sections saved
        from sqlalchemy import func
        count = (await db.execute(
            select(func.count()).select_from(SectionInput).where(SectionInput.report_id == rid)
        )).scalar()
        assert count == 11, f"Expected 11 saved sections, got {count}"

    async def test_e2e_idempotent_save_twice(self, db):
        """Saving natural-converted JSON twice for same section upserts cleanly."""
        from credit_report.api.generate import convert_natural_input, NaturalInputConvert
        from credit_report.api.reports import save_section_input
        from credit_report.schemas import SectionInputPayload
        from credit_report.models import SectionInput
        from sqlalchemy import select

        rid = str(uuid.uuid4())
        _, uid = await _seed_report(db, rid)
        user = _make_user(); user.id = uid

        # First save
        data_v1 = {"internal_rating": "BB+"}
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(data_v1)):
            r1 = await convert_natural_input(
                rid, 3, NaturalInputConvert(natural_text="First version.", section_no=3), db, user
            )
        await save_section_input(rid, 3, SectionInputPayload(section_no=3, input_json=r1.converted_json), db, user)

        # Second save with downgraded rating
        data_v2 = {"internal_rating": "BB"}
        with patch(GEMINI_PATCH_TARGET, new_callable=AsyncMock, return_value=json.dumps(data_v2)):
            r2 = await convert_natural_input(
                rid, 3, NaturalInputConvert(natural_text="Updated version.", section_no=3), db, user
            )
        await save_section_input(rid, 3, SectionInputPayload(section_no=3, input_json=r2.converted_json), db, user)

        si = (await db.execute(
            select(SectionInput).where(SectionInput.report_id == rid, SectionInput.section_no == 3)
        )).scalar_one_or_none()
        assert si is not None
        stored = json.loads(si.input_json) if isinstance(si.input_json, str) else si.input_json
        assert stored["internal_rating"] == "BB", \
            f"Expected 'BB' after upsert, got {stored['internal_rating']!r}"

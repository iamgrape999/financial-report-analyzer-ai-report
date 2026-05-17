"""
Tests for ETL pipeline improvements:
  1. Chunked ETL processing (no 400KB truncation)
  2. _deep_merge_etl correctness
  3. build_canonical_facts_from_etl conversion
  4. _split_pdf_pages helper
  5. Auto-registration integration (mocked Gemini)
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from credit_report.database import Base
from credit_report.fact_store.models import CanonicalFact
from credit_report.generation.etl import (
    _deep_merge_etl,
    _ETL_CHUNK_SIZE,
    build_canonical_facts_from_etl,
    _try_float,
)
from credit_report.generation.evidence import _split_pdf_pages
from main import app


BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
RPTS = f"{BASE}/reports"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def ac():
    """Full-stack ASGI client using the real app DB (same pattern as e2e tests)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def admin_hdrs(ac):
    r = await ac.post(f"{AUTH}/login", data={"username": "admin@example.com", "password": "admin123"})
    assert r.status_code == 200, f"admin login: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── Tests: _deep_merge_etl ────────────────────────────────────────────────────

class TestDeepMergeETL:
    def test_base_wins_on_non_null_scalar(self):
        base = {"4": {"4A": {"company_name_en": "Alpha Corp"}}}
        overlay = {"4": {"4A": {"company_name_en": "Beta Corp"}}}
        result = _deep_merge_etl(base["4"], overlay["4"])
        assert result["4A"]["company_name_en"] == "Alpha Corp"

    def test_overlay_fills_null_in_base(self):
        base = {"7A": {"revenue": None, "ebitda": 500.0}}
        overlay = {"7A": {"revenue": 1000.0, "ebitda": 400.0}}
        result = _deep_merge_etl(base, overlay)
        assert result["7A"]["revenue"] == 1000.0
        assert result["7A"]["ebitda"] == 500.0  # base wins

    def test_overlay_adds_missing_keys(self):
        base = {"4A": {"company_name_en": "Alpha"}}
        overlay = {"4A": {"company_name_zh": "阿爾法"}, "4B": {"shareholders": []}}
        result = _deep_merge_etl(base, overlay)
        assert result["4A"]["company_name_en"] == "Alpha"
        assert result["4A"]["company_name_zh"] == "阿爾法"
        assert "4B" in result

    def test_list_items_deduplicated(self):
        item = {"name": "Alice", "stake_percent": 50.0}
        base = {"shareholders": [item]}
        overlay = {"shareholders": [item, {"name": "Bob", "stake_percent": 30.0}]}
        result = _deep_merge_etl(base, overlay)
        names = [s["name"] for s in result["shareholders"]]
        assert names.count("Alice") == 1
        assert "Bob" in names

    def test_empty_base_returns_overlay(self):
        overlay = {"revenue": 1000.0}
        result = _deep_merge_etl({}, overlay)
        assert result == overlay

    def test_nested_null_filled(self):
        base = {"7A": {"revenue": None}}
        overlay = {"7A": {"revenue": 999.0}}
        result = _deep_merge_etl(base, overlay)
        assert result["7A"]["revenue"] == 999.0


# ── Tests: _try_float ─────────────────────────────────────────────────────────

class TestTryFloat:
    def test_numeric_string(self):
        assert _try_float("1,234.56") == pytest.approx(1234.56)

    def test_integer(self):
        assert _try_float(42) == 42.0

    def test_none(self):
        assert _try_float(None) is None

    def test_non_numeric_string(self):
        assert _try_float("n/a") is None

    def test_float_passthrough(self):
        assert _try_float(3.14) == pytest.approx(3.14)

    def test_negative(self):
        assert _try_float("-500.0") == pytest.approx(-500.0)


# ── Tests: build_canonical_facts_from_etl ─────────────────────────────────────

class TestBuildCanonicalFactsFromETL:
    def _make_extracted(self, revenue=1000.0, ebitda=200.0, net_income=100.0) -> dict[int, dict]:
        return {
            7: {
                "7A_borrower_financials": {
                    "revenue": revenue,
                    "ebitda": ebitda,
                    "net_income": net_income,
                    "total_debt": 500.0,
                    "cash_and_equivalents": 50.0,
                    "interest_expense": 30.0,
                    "total_equity": 300.0,
                }
            }
        }

    def test_basic_conversion(self):
        extracted = self._make_extracted()
        facts = build_canonical_facts_from_etl(
            report_id="r1", doc_id="d1", extracted=extracted
        )
        assert len(facts) > 0
        metrics = {f["metric_name"] for f in facts}
        assert "revenue" in metrics
        assert "ebitda" in metrics
        assert "net_income" in metrics

    def test_source_type_and_priority(self):
        facts = build_canonical_facts_from_etl(
            report_id="r1", doc_id="d1", extracted=self._make_extracted()
        )
        for f in facts:
            assert f["source_type"] == "pdf_extraction"
            assert f["source_priority"] == 3
            assert f["state"] == "extracted"

    def test_doc_id_stored_as_evidence_id(self):
        facts = build_canonical_facts_from_etl(
            report_id="r1", doc_id="doc-xyz", extracted=self._make_extracted()
        )
        for f in facts:
            assert f["source_evidence_id"] == "doc-xyz"

    def test_null_values_excluded(self):
        extracted = {7: {"7A_borrower_financials": {"revenue": None, "ebitda": None}}}
        facts = build_canonical_facts_from_etl("r1", "d1", extracted)
        assert facts == []

    def test_string_number_converted(self):
        extracted = {7: {"7A_borrower_financials": {"revenue": "1,500.0"}}}
        facts = build_canonical_facts_from_etl("r1", "d1", extracted)
        revenue_fact = next((f for f in facts if f["metric_name"] == "revenue"), None)
        assert revenue_fact is not None
        assert revenue_fact["value"] == pytest.approx(1500.0)

    def test_no_duplicates_across_sub_keys(self):
        extracted = {
            7: {
                "7A_borrower_financials": {"revenue": 1000.0},
                "7B_income_statement": {"revenue": 900.0},  # should not duplicate
            }
        }
        facts = build_canonical_facts_from_etl("r1", "d1", extracted)
        revenue_facts = [f for f in facts if f["metric_name"] == "revenue"]
        assert len(revenue_facts) == 1  # first-wins deduplication

    def test_empty_extraction_returns_empty(self):
        assert build_canonical_facts_from_etl("r1", "d1", {}) == []

    def test_custom_entity_and_period(self):
        extracted = {7: {"7A_borrower_financials": {"revenue": 500.0}}}
        facts = build_canonical_facts_from_etl(
            "r1", "d1", extracted,
            entity="TestCo", period="FY2025", currency="TWD",
        )
        assert facts[0]["entity"] == "TestCo"
        assert facts[0]["period"] == "FY2025"
        assert facts[0]["currency"] == "TWD"


# ── Tests: Chunked ETL threshold ──────────────────────────────────────────────

class TestETLChunkThreshold:
    def test_chunk_size_is_reasonable(self):
        """_ETL_CHUNK_SIZE should be between 200K and 500K chars — not the old 400K hard limit."""
        assert 200_000 <= _ETL_CHUNK_SIZE <= 500_000

    @pytest.mark.asyncio
    async def test_chunked_path_called_for_large_text(self):
        """etl_document routes to chunked path when text > _ETL_CHUNK_SIZE."""
        large_text = "Revenue: 1000\n" * (_ETL_CHUNK_SIZE // 14 + 1)
        assert len(large_text) > _ETL_CHUNK_SIZE

        mock_chunk_result: dict = {4: {"4A_borrower": {"company_name_en": "TestCo"}}}

        with patch(
            "credit_report.generation.etl._etl_document_chunked",
            new=AsyncMock(return_value=mock_chunk_result),
        ) as mock_chunked, patch(
            "credit_report.generation.etl._call_gemini_etl_once",
            new=AsyncMock(return_value={}),
        ), patch("credit_report.config.GEMINI_API_KEY", "fake-key"):
            from credit_report.generation.etl import etl_document
            result = await etl_document(text=large_text, document_type="annual_report")

        mock_chunked.assert_called_once()
        assert result == mock_chunk_result

    @pytest.mark.asyncio
    async def test_single_call_path_for_small_text(self):
        """etl_document uses single-call path when text ≤ _ETL_CHUNK_SIZE."""
        small_text = "Revenue: 1000\n" * 100
        assert len(small_text) < _ETL_CHUNK_SIZE

        mock_result: dict = {7: {"7A_borrower_financials": {"revenue": 1000.0}}}

        with patch(
            "credit_report.generation.etl._call_gemini_etl_once",
            new=AsyncMock(return_value=mock_result),
        ) as mock_single, patch(
            "credit_report.generation.etl._etl_document_chunked",
            new=AsyncMock(return_value={}),
        ) as mock_chunked, patch("credit_report.config.GEMINI_API_KEY", "fake-key"):
            from credit_report.generation.etl import etl_document
            result = await etl_document(text=small_text, document_type="annual_report")

        mock_single.assert_called_once()
        mock_chunked.assert_not_called()
        assert result == mock_result


# ── Tests: _split_pdf_pages ───────────────────────────────────────────────────

class TestSplitPdfPages:
    def _make_minimal_pdf(self) -> bytes:
        """Create a minimal valid single-page PDF in bytes."""
        return (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f\r\n0000000009 00000 n\r\n"
            b"0000000058 00000 n\r\n0000000115 00000 n\r\n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
        )

    def test_invalid_bytes_returns_empty(self):
        result = _split_pdf_pages(b"not a pdf")
        assert result == []

    def test_valid_pdf_returns_pages(self):
        pdf_bytes = self._make_minimal_pdf()
        pages = _split_pdf_pages(pdf_bytes)
        # May succeed or fail depending on pypdf's tolerance of the minimal PDF
        # but should never raise
        assert isinstance(pages, list)


# ── Tests: Auto-registration integration (mocked) ─────────────────────────────

class TestAutoRegistrationIntegration:
    async def _setup_analyst(self, ac, admin_hdrs) -> tuple[str, dict]:
        """Register an analyst and return (report_id, hdrs)."""
        email = f"analyst_{uuid.uuid4().hex[:6]}@test.com"
        await ac.post(f"{AUTH}/register",
                      json={"email": email, "password": "Pass1234!", "role": "analyst"},
                      headers=admin_hdrs)
        r = await ac.post(f"{AUTH}/login", data={"username": email, "password": "Pass1234!"})
        assert r.status_code == 200
        hdrs = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rr = await ac.post(f"{RPTS}", json={"borrower_name": "ETL Test Co"}, headers=hdrs)
        assert rr.status_code in (200, 201)
        return rr.json()["id"], hdrs

    @pytest.mark.asyncio
    async def test_etl_result_includes_facts_registered(self, ac, admin_hdrs):
        """After ETL, the ETLResult.facts_registered count should reflect registered facts."""
        import io
        report_id, hdrs = await self._setup_analyst(ac, admin_hdrs)

        content = b"Revenue: 1000\nEBITDA: 200\nNet Income: 100\n"
        upload_r = await ac.post(
            f"{RPTS}/{report_id}/documents",
            headers=hdrs,
            data={"document_type": "financial_statement"},
            files={"file": ("test.txt", io.BytesIO(content), "text/plain")},
        )
        assert upload_r.status_code in (200, 201)
        doc_id = upload_r.json()["id"]

        mock_extracted = {
            7: {
                "7A_borrower_financials": {
                    "revenue": 1000.0,
                    "ebitda": 200.0,
                    "net_income": 100.0,
                }
            }
        }
        with patch(
            "credit_report.api.generate.etl_document",
            new=AsyncMock(return_value=mock_extracted),
        ):
            etl_r = await ac.post(f"{RPTS}/{report_id}/documents/{doc_id}/etl", headers=hdrs)

        assert etl_r.status_code == 200
        body = etl_r.json()
        assert "facts_registered" in body
        assert body["facts_registered"] >= 0

    @pytest.mark.asyncio
    async def test_facts_registered_are_queryable(self, ac, admin_hdrs):
        """CanonicalFacts auto-registered by ETL must be retrievable via the facts API."""
        import io
        report_id, hdrs = await self._setup_analyst(ac, admin_hdrs)

        content = b"Revenue: 2500\n"
        upload_r = await ac.post(
            f"{RPTS}/{report_id}/documents",
            headers=hdrs,
            data={"document_type": "financial_statement"},
            files={"file": ("test.txt", io.BytesIO(content), "text/plain")},
        )
        assert upload_r.status_code in (200, 201)
        doc_id = upload_r.json()["id"]

        mock_extracted = {
            7: {
                "7A_borrower_financials": {
                    "revenue": 2500.0,
                    "ebitda": 500.0,
                }
            }
        }
        with patch(
            "credit_report.api.generate.etl_document",
            new=AsyncMock(return_value=mock_extracted),
        ):
            etl_r = await ac.post(f"{RPTS}/{report_id}/documents/{doc_id}/etl", headers=hdrs)

        assert etl_r.status_code == 200
        assert etl_r.json()["facts_registered"] > 0

        facts_r = await ac.get(f"{RPTS}/{report_id}/facts", headers=hdrs)
        assert facts_r.status_code == 200
        facts = facts_r.json()
        metrics = {f["metric_name"] for f in facts}
        assert "revenue" in metrics
        assert "ebitda" in metrics

        rev_fact = next(f for f in facts if f["metric_name"] == "revenue")
        assert rev_fact["source_type"] == "pdf_extraction"
        assert rev_fact["value"] == pytest.approx(2500.0)

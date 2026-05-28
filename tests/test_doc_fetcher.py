"""Tests for the auto-fetch document module (credit_report/api/doc_fetcher.py).

External HTTP calls are patched with unittest.mock — no live network traffic.
"""
from __future__ import annotations

import os
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from credit_report.api.doc_fetcher import (
    FetchedDoc,
    FetchError,
    _extract_file_ids,
    _extract_pdf_hrefs,
    _roc_year,
    _download_bytes,
    fetch_direct_url,
    fetch_edgar_filings,
    fetch_mops_annual_reports,
    run_auto_fetch,
)

os.environ.setdefault("ADMIN_EMAIL",    "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")

# ── Helpers ───────────────────────────────────────────────────────────────────

_FAKE_PDF = b"%PDF-1.4 fake content " + b"x" * 600   # > 512 bytes, valid magic

# Must be > 200 chars to pass the length guard in fetch_mops_annual_reports
_MOPS_HTML_WITH_CLK = (
    '<html><body>' + ' ' * 200 +
    '<a href="javascript:clkFile(\'2603001\',\'1\',\'A\')">年報下載</a>'
    '</body></html>'
)
_MOPS_HTML_WITH_HREF = (
    '<html><body>' + ' ' * 200 +
    '<a href="https://doc.twse.com.tw/files/2603_2024.pdf">PDF</a>'
    '</body></html>'
)
_MOPS_HTML_EMPTY = "<html><body>" + " " * 10 + "查無資料</body></html>"


def _mock_response(content: bytes = b"", text: str = "", status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.text = text
    r.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("", request=MagicMock(), response=r)
        if status >= 400 else None
    )
    return r


# ── Unit helpers ──────────────────────────────────────────────────────────────

def test_roc_year():
    assert _roc_year(2024) == 113
    assert _roc_year(2025) == 114
    assert _roc_year(1912) == 1


def test_extract_file_ids_standard():
    html = (
        "<a href=\"javascript:clkFile('2000001','1','A')\">dl</a>"
        "<a href=\"javascript:clkFile('2000002','2','A')\">dl</a>"
    )
    ids = _extract_file_ids(html)
    assert len(ids) == 2
    assert ids[0] == ("2000001", "1")
    assert ids[1] == ("2000002", "2")


def test_extract_file_ids_empty():
    assert _extract_file_ids("no javascript here") == []


def test_extract_pdf_hrefs_absolute():
    html = '<a href="https://doc.twse.com.tw/files/report.pdf">PDF</a>'
    hrefs = _extract_pdf_hrefs(html)
    assert hrefs == ["https://doc.twse.com.tw/files/report.pdf"]


def test_extract_pdf_hrefs_relative():
    html = '<a href="/server-java/report.pdf?id=1">PDF</a>'
    hrefs = _extract_pdf_hrefs(html)
    assert hrefs == ["https://doc.twse.com.tw/server-java/report.pdf?id=1"]


def test_extract_pdf_hrefs_ignores_non_pdf():
    html = '<a href="/files/data.xlsx">XLS</a><a href="/files/report.pdf">PDF</a>'
    hrefs = _extract_pdf_hrefs(html)
    assert len(hrefs) == 1
    assert hrefs[0].endswith("report.pdf")


# ── _download_bytes ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_download_bytes_success():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=_FAKE_PDF))
    result = await _download_bytes(client, "https://example.com/report.pdf")
    assert result == _FAKE_PDF


@pytest.mark.asyncio
async def test_download_bytes_too_small_returns_none():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=b"%PDF tiny"))
    result = await _download_bytes(client, "https://example.com/tiny.pdf")
    assert result is None


@pytest.mark.asyncio
async def test_download_bytes_http_error_returns_none():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    result = await _download_bytes(client, "https://example.com/report.pdf")
    assert result is None


@pytest.mark.asyncio
async def test_download_bytes_404_returns_none():
    client = AsyncMock()
    r = _mock_response(status=404)
    client.get = AsyncMock(return_value=r)
    result = await _download_bytes(client, "https://example.com/missing.pdf")
    assert result is None


# ── MOPS fetcher ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mops_clkfile_download():
    """clkFile IDs are resolved to download URLs and PDFs are fetched."""
    async def fake_get(url, **kw):
        if "t57sb01" in url:
            return _mock_response(text=_MOPS_HTML_WITH_CLK)
        if "t57sb02" in url:
            return _mock_response(content=_FAKE_PDF)
        return _mock_response(status=404)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    # Stub post so MOPS fallback returns empty (TWSE path should succeed above)
    client.post = AsyncMock(return_value=_mock_response(text=_MOPS_HTML_EMPTY))

    docs, errors = await fetch_mops_annual_reports(client, "2603", years_back=1)
    assert len(docs) >= 1
    assert docs[0].source == "mops"
    assert docs[0].document_type == "annual_report"
    assert docs[0].data.startswith(b"%PDF")
    assert "2603" in docs[0].filename


@pytest.mark.asyncio
async def test_mops_fallback_to_href():
    """When clkFile yields nothing, falls back to direct href PDFs."""
    async def fake_get(url, **kw):
        if "t57sb01" in url:
            return _mock_response(text=_MOPS_HTML_WITH_HREF)
        if "2603_2024.pdf" in url:
            return _mock_response(content=_FAKE_PDF)
        return _mock_response(status=404)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(return_value=_mock_response(text=_MOPS_HTML_EMPTY))

    docs, errors = await fetch_mops_annual_reports(client, "2603", years_back=1)
    assert len(docs) >= 1


@pytest.mark.asyncio
async def test_mops_no_results_returns_error():
    """Empty listing → FetchError, no docs, no crash."""
    async def fake_get(url, **kw):
        return _mock_response(text=_MOPS_HTML_EMPTY)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(return_value=_mock_response(text=_MOPS_HTML_EMPTY))

    docs, errors = await fetch_mops_annual_reports(client, "9999", years_back=1)
    assert docs == []
    assert len(errors) >= 1
    assert errors[0].source == "mops"


@pytest.mark.asyncio
async def test_mops_network_error_returns_error():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

    docs, errors = await fetch_mops_annual_reports(client, "2603", years_back=1)
    assert docs == []
    assert len(errors) >= 1


@pytest.mark.asyncio
async def test_mops_years_back_2_queries_two_years():
    """years_back=2 issues listing requests for two different ROC years."""
    calls = []

    async def fake_get(url, **kw):
        params = kw.get("params", {})
        if "year" in params:
            calls.append(int(params["year"]))
        return _mock_response(text=_MOPS_HTML_EMPTY)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(return_value=_mock_response(text=_MOPS_HTML_EMPTY))

    await fetch_mops_annual_reports(client, "2603", years_back=2)
    # Should query two different years
    assert len(set(calls)) >= 2


# ── EDGAR fetcher ─────────────────────────────────────────────────────────────

_EDGAR_HITS = {
    "hits": {"hits": [{
        "_source": {
            "accession_no": "0001234567-24-000001",
            "entity_id": "789012",
            "form_type": "20-F",
            "period_of_report": "2023-12-31",
            "file_date": "2024-04-15",
            "entity_name": "Evergreen Marine Corp",
        }
    }]}
}

_EDGAR_INDEX = {
    "directory": {"item": [
        {"name": "evergreen_20f.htm", "type": "20-F"},
        {"name": "exhibit.htm",        "type": "EX-99"},
    ]}
}

_FAKE_HTML = b"<html><body>" + b"x" * 600 + b"</body></html>"


@pytest.mark.asyncio
async def test_edgar_no_hits_returns_error():
    client = AsyncMock()
    empty = MagicMock()
    empty.status_code = 200
    empty.json = MagicMock(return_value={"hits": {"hits": []}})
    empty.raise_for_status = MagicMock()
    client.get = AsyncMock(return_value=empty)

    docs, errors = await fetch_edgar_filings(client, "NonExistentCo")
    assert docs == []
    assert len(errors) == 1
    assert "NonExistentCo" in errors[0].message


@pytest.mark.asyncio
async def test_edgar_network_error_returns_error():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("unreachable"))

    docs, errors = await fetch_edgar_filings(client, "Some Co")
    assert docs == []
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_edgar_search_json_error_returns_error():
    client = AsyncMock()
    bad = MagicMock()
    bad.status_code = 200
    bad.json = MagicMock(side_effect=ValueError("bad json"))
    bad.raise_for_status = MagicMock()
    client.get = AsyncMock(return_value=bad)

    docs, errors = await fetch_edgar_filings(client, "Some Co")
    assert docs == []
    assert len(errors) == 1


# ── Direct URL fetcher ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_direct_url_success():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=_FAKE_PDF))
    doc, err = await fetch_direct_url(client, "https://example.com/annual_report.pdf")
    assert err is None
    assert doc.source == "direct"
    assert doc.filename == "annual_report.pdf"
    assert doc.document_type == "annual_report"


@pytest.mark.asyncio
async def test_direct_url_financial_statement_type():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=_FAKE_PDF))
    doc, _ = await fetch_direct_url(client, "https://example.com/financial_statement_2024.pdf")
    assert doc.document_type == "financial_statement"


@pytest.mark.asyncio
async def test_direct_url_generic_type():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=_FAKE_PDF))
    doc, _ = await fetch_direct_url(client, "https://example.com/prospectus.pdf")
    assert doc.document_type == "other"


@pytest.mark.asyncio
async def test_direct_url_404_returns_error():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(status=404))
    doc, err = await fetch_direct_url(client, "https://example.com/missing.pdf")
    assert doc is None
    assert err is not None
    assert err.source == "direct"


@pytest.mark.asyncio
async def test_direct_url_too_small_rejected():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=b"%PDF tiny"))
    doc, err = await fetch_direct_url(client, "https://example.com/tiny.pdf")
    assert doc is None
    assert err is not None


@pytest.mark.asyncio
async def test_direct_url_custom_filename():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=_FAKE_PDF))
    doc, _ = await fetch_direct_url(client, "https://example.com/abc", filename="my_report.pdf")
    assert doc.filename == "my_report.pdf"


@pytest.mark.asyncio
async def test_direct_url_infers_filename_from_path():
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(content=_FAKE_PDF))
    doc, _ = await fetch_direct_url(client, "https://example.com/reports/doc42.pdf")
    assert doc.filename == "doc42.pdf"


# ── Orchestrator ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_auto_fetch_mops_without_stock_code_gives_error():
    docs, errors = await run_auto_fetch(sources=["mops"])
    assert docs == []
    assert any(e.source == "mops" for e in errors)


@pytest.mark.asyncio
async def test_run_auto_fetch_edgar_without_company_name_gives_error():
    docs, errors = await run_auto_fetch(sources=["edgar"])
    assert docs == []
    assert any(e.source == "edgar" for e in errors)


@pytest.mark.asyncio
async def test_run_auto_fetch_empty_sources_returns_nothing():
    docs, errors = await run_auto_fetch(sources=[])
    assert docs == []
    assert errors == []


@pytest.mark.asyncio
async def test_run_auto_fetch_ignores_invalid_source():
    """Unknown source names are silently dropped."""
    with patch("credit_report.api.doc_fetcher.fetch_direct_url",
               new=AsyncMock(return_value=(
                   FetchedDoc("x.pdf", _FAKE_PDF, "direct", "other"), None
               ))):
        docs, errors = await run_auto_fetch(
            sources=["direct", "invalid_source"],
            direct_urls=["https://example.com/x.pdf"],
        )
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_run_auto_fetch_direct_two_success_one_error():
    """Two successes and one error are all returned."""
    call_count = 0

    async def fake_fetch(client, url, **kw):
        nonlocal call_count
        call_count += 1
        if "bad" in url:
            return None, FetchError("direct", f"Failed: {url}")
        return FetchedDoc(url.split("/")[-1], _FAKE_PDF, "direct", "other"), None

    with patch("credit_report.api.doc_fetcher.fetch_direct_url", new=fake_fetch):
        docs, errors = await run_auto_fetch(
            sources=["direct"],
            direct_urls=[
                "https://a.com/good1.pdf",
                "https://b.com/good2.pdf",
                "https://c.com/bad.pdf",
            ],
        )
    assert len(docs) == 2
    assert len(errors) == 1


# ── API endpoint integration ──────────────────────────────────────────────────

BASE = "/api/credit-report"


@pytest.fixture()
def ac():
    from httpx import AsyncClient, ASGITransport
    import main
    return AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")


async def _login(ac) -> dict:
    email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
    password = os.environ.get("ADMIN_PASSWORD", "admin123")
    r = await ac.post(f"{BASE}/auth/login", data={"username": email, "password": password})
    if r.status_code != 200:
        pytest.skip(f"admin login failed (status {r.status_code})")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_fetch_endpoint_requires_auth(ac):
    r = await ac.post(f"{BASE}/reports/fake-id/fetch-documents", json={"sources": ["direct"]})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_fetch_endpoint_empty_sources_returns_422(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "FetchTest422", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]
    r = await ac.post(f"{BASE}/reports/{rid}/fetch-documents",
                      json={"sources": []}, headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_fetch_endpoint_nonexistent_report_returns_404(ac):
    hdrs = await _login(ac)
    r = await ac.post(f"{BASE}/reports/nonexistent-id/fetch-documents",
                      json={"sources": ["mops"], "stock_code": "2603"},
                      headers=hdrs)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_fetch_endpoint_mops_missing_stock_code_returns_202_with_error(ac):
    """Server-side validation: mops source without stock_code returns 202 with errors list."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "FetchTestMops", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]

    with patch("credit_report.api.doc_fetcher.run_auto_fetch",
               new=AsyncMock(return_value=(
                   [], [FetchError("mops", "stock_code is required for MOPS source")]
               ))):
        r = await ac.post(f"{BASE}/reports/{rid}/fetch-documents",
                          json={"sources": ["mops"]}, headers=hdrs)

    assert r.status_code == 202
    body = r.json()
    assert body["fetched"] == 0
    assert any("mops" in e["source"] for e in body["errors"])


@pytest.mark.asyncio
async def test_fetch_endpoint_registers_fetched_doc(ac):
    """A successfully fetched PDF is saved and returned in the documents list."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "FetchTestDoc", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]

    fake_doc = FetchedDoc(
        filename="MOPS_2603_2025_annualreport.pdf",
        data=_FAKE_PDF,
        source="mops",
        document_type="annual_report",
    )

    with patch("credit_report.api.doc_fetcher.run_auto_fetch",
               new=AsyncMock(return_value=([fake_doc], []))):
        r = await ac.post(
            f"{BASE}/reports/{rid}/fetch-documents",
            json={"sources": ["mops"], "stock_code": "2603"},
            headers=hdrs,
        )

    assert r.status_code == 202
    body = r.json()
    assert body["fetched"] == 1
    assert body["documents"][0]["source"] == "mops"
    assert body["documents"][0]["filename"] == "MOPS_2603_2025_annualreport.pdf"
    assert body["documents"][0]["document_type"] == "annual_report"

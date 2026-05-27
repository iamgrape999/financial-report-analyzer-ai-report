"""Tests for credit_report/api/twse_importer.py and the /import-twse endpoint.

External HTTP calls are patched with unittest.mock — no live network traffic.
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")

from credit_report.api.twse_importer import (
    TWSECompanyData,
    _find_by_code,
    _parse_twse_date,
    apply_field_mapping,
    deep_get,
    deep_set,
    map_to_section4,
    map_to_section7_metadata,
)

BASE = "/api/credit-report"

# ── Fixtures ──────────────────────────────────────────────────────────────────

_SAMPLE_BASIC = {
    "公司代號": "2603",
    "公司名稱": "長榮海運",
    "國際證券辨識號碼(ISIN Code)": "TW0002603004",
    "上市日期": "19870925",
    "市場別": "上市",
    "產業別": "航運業",
}

_SAMPLE_DETAILED = {
    "公司代號": "2603",
    "公司名稱": "長榮海運",
    "英文全名": "Evergreen Marine Corp",
    "英文簡稱": "EMC",
    "成立日期": "19680901",
    "實收資本額(元)": "38808000000",
    "董事長": "張衍義",
    "總經理": "謝惠全",
    "主要業務": "貨櫃輪航運",
    "簽證會計師事務所": "資誠聯合會計師事務所",
    "簽證會計師1": "陳明進",
    "簽證會計師2": "徐薇婷",
    "電話": "02-25056688",
    "住址": "台北市中山區民生東路二段166號",
    "財務報告書類型": "合併",
}

_SAMPLE_DATA = TWSECompanyData(
    stock_code="2603",
    company_name_zh="長榮海運",
    isin_code="TW0002603004",
    listing_date="1987-09-25",
    market_type="上市",
    industry_zh="航運業",
    company_name_en="Evergreen Marine Corp",
    incorporation_date="1968-09-01",
    paid_in_capital_ntd=38_808_000_000,
    chairman="張衍義",
    ceo="謝惠全",
    primary_business="貨櫃輪航運",
    auditor_firm="資誠聯合會計師事務所",
    auditor1="陳明進",
    auditor2="徐薇婷",
    phone="02-25056688",
    address="台北市中山區民生東路二段166號",
    financial_report_type="合併",
    raw={"basic": _SAMPLE_BASIC, "detailed": _SAMPLE_DETAILED},
)


# ── _parse_twse_date ──────────────────────────────────────────────────────────

def test_parse_date_yyyymmdd():
    assert _parse_twse_date("19870925") == "1987-09-25"


def test_parse_date_recent():
    assert _parse_twse_date("20241231") == "2024-12-31"


def test_parse_date_empty():
    assert _parse_twse_date("") == ""


def test_parse_date_whitespace():
    assert _parse_twse_date("  ") == ""


def test_parse_date_invalid_string():
    result = _parse_twse_date("abcdefgh")
    assert result == "abcdefgh"


def test_parse_date_too_short():
    result = _parse_twse_date("2024")
    assert result == "2024"


def test_parse_date_future_unchanged():
    result = _parse_twse_date("29991231")
    assert result == "29991231"


# ── _find_by_code ─────────────────────────────────────────────────────────────

def test_find_by_code_exact():
    rows = [{"公司代號": "2603", "val": 1}, {"公司代號": "2412", "val": 2}]
    assert _find_by_code(rows, "2603")["val"] == 1


def test_find_by_code_with_whitespace():
    rows = [{"公司代號": " 2603 ", "val": 1}]
    assert _find_by_code(rows, "2603")["val"] == 1


def test_find_by_code_not_found():
    rows = [{"公司代號": "2603"}]
    assert _find_by_code(rows, "9999") == {}


def test_find_by_code_empty_list():
    assert _find_by_code([], "2603") == {}


# ── deep_set / deep_get ───────────────────────────────────────────────────────

def test_deep_set_simple():
    obj = {}
    deep_set(obj, "a.b.c", 42)
    assert obj == {"a": {"b": {"c": 42}}}


def test_deep_set_overwrites():
    obj = {"a": {"b": {"c": 1}}}
    deep_set(obj, "a.b.c", 99)
    assert obj["a"]["b"]["c"] == 99


def test_deep_get_existing():
    obj = {"a": {"b": {"c": 7}}}
    assert deep_get(obj, "a.b.c") == 7


def test_deep_get_missing():
    assert deep_get({}, "x.y.z") is None


def test_deep_get_partial_missing():
    assert deep_get({"a": {}}, "a.b.c") is None


# ── apply_field_mapping ───────────────────────────────────────────────────────

def test_apply_only_empty_fills_missing():
    mapping = {"a.b": "new_val"}
    result, written, skipped = apply_field_mapping({}, mapping, "only_empty")
    assert result["a"]["b"] == "new_val"
    assert written == 1
    assert skipped == 0


def test_apply_only_empty_skips_existing():
    mapping = {"a.b": "new_val"}
    existing = {"a": {"b": "keep_me"}}
    result, written, skipped = apply_field_mapping(existing, mapping, "only_empty")
    assert result["a"]["b"] == "keep_me"
    assert written == 0
    assert skipped == 1


def test_apply_only_empty_treats_empty_string_as_blank():
    mapping = {"x": "filled"}
    existing = {"x": ""}
    result, written, skipped = apply_field_mapping(existing, mapping, "only_empty")
    assert result["x"] == "filled"
    assert written == 1


def test_apply_overwrite_replaces_existing():
    mapping = {"a.b": "replacement"}
    existing = {"a": {"b": "original"}}
    result, written, skipped = apply_field_mapping(existing, mapping, "overwrite")
    assert result["a"]["b"] == "replacement"
    assert written == 1
    assert skipped == 0


def test_apply_empty_mapping():
    result, written, skipped = apply_field_mapping({"x": 1}, {}, "only_empty")
    assert written == 0
    assert skipped == 0


# ── map_to_section4 ───────────────────────────────────────────────────────────

def test_map_section4_keys_present():
    m = map_to_section4(_SAMPLE_DATA)
    assert "4A_borrower.company_name_zh" in m
    assert "4A_borrower.company_name_en" in m
    assert "4A_borrower.incorporation_date" in m
    assert "4A_borrower.group_auditor" in m
    assert "4C_management.ceo_name" in m
    assert "4D_business.primary_business" in m
    assert "4E_financials.currency" in m
    assert "4E_financials.paid_in_capital_ntd_bn" in m


def test_map_section4_values():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4A_borrower.company_name_zh"] == "長榮海運"
    assert m["4A_borrower.company_name_en"] == "Evergreen Marine Corp"
    assert m["4A_borrower.incorporation_date"] == "1968-09-01"
    assert m["4A_borrower.incorporation_country"] == "Taiwan"
    assert m["4A_borrower.legal_entity_type"] == "Listed Company"
    assert m["4A_borrower.fiscal_year_end"] == "December 31"
    assert m["4C_management.ceo_name"] == "謝惠全"
    assert m["4C_management.ceo_title"] == "President"
    assert m["4D_business.primary_business"] == "貨櫃輪航運"
    assert m["4E_financials.currency"] == "NTD"


def test_map_section4_paid_in_capital_ntd_bn():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4E_financials.paid_in_capital_ntd_bn"] == pytest.approx(38.808, rel=1e-3)


def test_map_section4_empty_data_omits_fields():
    empty = TWSECompanyData(stock_code="0000")
    m = map_to_section4(empty)
    assert "4A_borrower.company_name_zh" not in m
    assert "4E_financials.paid_in_capital_ntd_bn" not in m


def test_map_section4_zero_capital_omitted():
    data = TWSECompanyData(stock_code="9999", market_type="上市", paid_in_capital_ntd=0)
    m = map_to_section4(data)
    assert "4E_financials.paid_in_capital_ntd_bn" not in m


# ── map_to_section7_metadata ─────────────────────────────────────────────────

def test_map_section7_keys_present():
    m = map_to_section7_metadata(_SAMPLE_DATA)
    assert "7A_borrower_financials.reporting_entity" in m
    assert "7A_borrower_financials.auditor" in m
    assert "7A_borrower_financials.fiscal_year_end" in m
    assert "7A_borrower_financials.reporting_currency" in m
    assert "7A_borrower_financials.unit" in m
    assert "7A_borrower_financials.accounting_standard" in m


def test_map_section7_values():
    m = map_to_section7_metadata(_SAMPLE_DATA)
    assert m["7A_borrower_financials.reporting_entity"] == "長榮海運"
    assert m["7A_borrower_financials.auditor"] == "資誠聯合會計師事務所"
    assert m["7A_borrower_financials.fiscal_year_end"] == "December 31"
    assert m["7A_borrower_financials.reporting_currency"] == "NTD"
    assert m["7A_borrower_financials.unit"] == "NTD million"
    assert m["7A_borrower_financials.accounting_standard"] == "IFRS (TIFRS)"


def test_map_section7_falls_back_to_english_name():
    data = TWSECompanyData(
        stock_code="2603",
        company_name_zh="",
        company_name_en="Evergreen Marine Corp",
    )
    m = map_to_section7_metadata(data)
    assert m["7A_borrower_financials.reporting_entity"] == "Evergreen Marine Corp"


# ── fetch_twse_company (async, HTTP mocked) ───────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_twse_company_success():
    from credit_report.api.twse_importer import fetch_twse_company

    mock_basic = MagicMock()
    mock_basic.raise_for_status = MagicMock()
    mock_basic.json = MagicMock(return_value=[_SAMPLE_BASIC])

    mock_detailed = MagicMock()
    mock_detailed.raise_for_status = MagicMock()
    mock_detailed.json = MagicMock(return_value=[_SAMPLE_DETAILED])

    async def _fake_get(url, **kw):
        if "t187ap03" in url:
            return mock_basic
        return mock_detailed

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = _fake_get
        mock_cls.return_value = mock_client

        data = await fetch_twse_company("2603")

    assert data is not None
    assert data.company_name_zh == "長榮海運"
    assert data.company_name_en == "Evergreen Marine Corp"
    assert data.paid_in_capital_ntd == 38_808_000_000
    assert data.ceo == "謝惠全"
    assert data.auditor_firm == "資誠聯合會計師事務所"


@pytest.mark.asyncio
async def test_fetch_twse_company_not_found():
    from credit_report.api.twse_importer import fetch_twse_company

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[])

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = await fetch_twse_company("9999")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_twse_company_http_error_returns_none():
    from credit_report.api.twse_importer import fetch_twse_company
    import httpx

    async def _failing_get(url, **kw):
        raise httpx.ConnectError("network unreachable")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = _failing_get
        mock_cls.return_value = mock_client

        result = await fetch_twse_company("2603")

    assert result is None


# ── /import-twse endpoint integration ─────────────────────────────────────────

@pytest.fixture()
def ac():
    from httpx import AsyncClient, ASGITransport
    import main
    return AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")


async def _login(ac) -> dict:
    r = await ac.post(f"{BASE}/auth/login", data={"username": "admin@example.com", "password": "admin123"})
    if r.status_code != 200:
        pytest.skip("admin login failed")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_import_twse_requires_auth(ac):
    r = await ac.post(f"{BASE}/reports/fake-id/import-twse",
                      json={"stock_code": "2603"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_import_twse_report_not_found(ac):
    hdrs = await _login(ac)
    r = await ac.post(f"{BASE}/reports/nonexistent-id/import-twse",
                      json={"stock_code": "2603"}, headers=hdrs)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_import_twse_invalid_apply_mode(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportTest", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]
    r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "apply_mode": "bad_mode"},
                      headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_twse_invalid_section(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportTestSec", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]
    r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "sections": [4, 99]},
                      headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_twse_stock_not_found(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportNotFound", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]

    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=None)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "9999"}, headers=hdrs)

    assert r.status_code == 200
    body = r.json()
    assert body["not_found"] is True
    assert body["fields_written"] == 0


@pytest.mark.asyncio
async def test_import_twse_success_writes_sections(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportSuccess", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]

    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603", "apply_mode": "overwrite", "sections": [4, 7]},
                          headers=hdrs)

    assert r.status_code == 200
    body = r.json()
    assert body["not_found"] is False
    assert body["company_name"] == "長榮海運"
    assert body["fields_written"] > 0
    assert 4 in body["sections_updated"]
    assert 7 in body["sections_updated"]


@pytest.mark.asyncio
async def test_import_twse_only_empty_skips_filled(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWOnlyEmpty", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]

    # Pre-fill §4 with a value
    await ac.put(
        f"{BASE}/reports/{rid}/inputs/4",
        json={"section_no": 4, "input_json": {"4A_borrower": {"company_name_zh": "已填寫"}}},
        headers=hdrs,
    )

    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603", "apply_mode": "only_empty", "sections": [4]},
                          headers=hdrs)

    assert r.status_code == 200
    body = r.json()
    # The one pre-filled field should be skipped
    assert body["fields_skipped"] >= 1

    # Verify the original value is preserved
    si_r = await ac.get(f"{BASE}/reports/{rid}/inputs/4", headers=hdrs)
    assert si_r.status_code == 200
    assert si_r.json()["input_json"]["4A_borrower"]["company_name_zh"] == "已填寫"


@pytest.mark.asyncio
async def test_import_twse_approved_report_returns_409(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWApproved409", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]

    # Force to approved state via admin status-patch endpoint
    r_upd = await ac.patch(f"{BASE}/reports/{rid}/status",
                           json={"status": "approved"}, headers=hdrs)
    if r_upd.status_code not in (200, 201):
        pytest.skip("Cannot set approved status in test environment")

    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603"}, headers=hdrs)

    assert r.status_code == 409


@pytest.mark.asyncio
async def test_import_twse_section4_only(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWSection4Only", "industry": "marine"},
                       headers=hdrs)
    assert cr.status_code in (200, 201)
    rid = cr.json()["id"]

    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603", "sections": [4]},
                          headers=hdrs)

    assert r.status_code == 200
    body = r.json()
    assert 4 in body["sections_updated"]
    assert 7 not in body["sections_updated"]

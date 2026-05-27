"""Tests for credit_report/api/twse_importer.py and the /import-twse endpoint.

Key change from earlier version: ALL company data now comes from t187ap03_L
(t187ap04_L was material news, not company detail).  Tests also cover the two
new endpoints: t187ap02_L (major shareholders) and t187ap11_L (board data).
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")

from credit_report.api.twse_importer import (
    BalanceSheetFact,
    BoardMember,
    CashFlowFact,
    DividendInfo,
    IncomeStatementFact,
    MonthlyRevenue,
    SUPPORTED_SECTIONS,
    TWSECompanyData,
    _find_all,
    _find_one,
    _latest_ytd_revenue,
    _parse_twse_date,
    apply_field_mapping,
    deep_get,
    deep_set,
    map_to_section,
    map_to_section1,
    map_to_section3,
    map_to_section4,
    map_to_section5,
    map_to_section7_metadata,
    map_to_section9,
)

BASE = "/api/credit-report"

# ── Fixtures ──────────────────────────────────────────────────────────────────

# All fields now come from a SINGLE t187ap03_L row
_SAMPLE_COMPANY = {
    "公司代號": "2603",
    "公司名稱": "長榮海運",
    "公司簡稱": "長榮",
    "英文全名": "Evergreen Marine Corp",
    "英文簡稱": "EMC",
    "國際證券辨識號碼(ISIN Code)": "TW0002603004",
    "上市日期": "19870925",
    "市場別": "上市",
    "產業別": "航運業",
    "外國企業註冊地國": "",          # empty = Taiwan
    "營利事業統一編號": "11111111",
    "成立日期": "19680901",
    "實收資本額(元)": "38808000000",
    "已發行普通股數或TDR原股發行股數": "3880800000",
    "普通股每股面額": "10",
    "董事長": "張衍義",
    "總經理": "謝惠全",
    "發言人": "蔡文瑞",
    "發言人職稱": "財務長",
    "主要業務": "貨櫃輪航運",
    "簽證會計師事務所": "資誠聯合會計師事務所",
    "簽證會計師1": "陳明進",
    "簽證會計師2": "徐薇婷",
    "電話": "02-25056688",
    "傳真電話": "02-25056000",
    "住址": "台北市中山區民生東路二段166號",
    "電子郵件信箱": "ir@evergreen-marine.com",
    "公司網址": "www.evergreen-marine.com",
    "財務報告書類型": "合併",
}

_SAMPLE_SHAREHOLDERS = [
    {"公司代號": "2603", "公司名稱": "長榮海運", "大股東名稱": "長榮國際股份有限公司"},
    {"公司代號": "2603", "公司名稱": "長榮海運", "大股東名稱": "張衍義"},
]

_SAMPLE_BOARD = [
    {
        "公司代號": "2603", "公司名稱": "長榮海運",
        "職稱": "董事長", "姓名": "張衍義",
        "目前持股": "500000000", "設質股數": "0", "設質股數佔持股比例": "0",
    },
    {
        "公司代號": "2603", "公司名稱": "長榮海運",
        "職稱": "董事", "姓名": "謝惠全",
        "目前持股": "100000", "設質股數": "0", "設質股數佔持股比例": "0",
    },
    {
        "公司代號": "2603", "公司名稱": "長榮海運",
        "職稱": "獨立董事", "姓名": "王小明",
        "目前持股": "0", "設質股數": "0", "設質股數佔持股比例": "0",
    },
]

_SAMPLE_DATA = TWSECompanyData(
    stock_code="2603",
    company_name_zh="長榮海運",
    company_name_abbrev_zh="長榮",
    company_name_en="Evergreen Marine Corp",
    company_name_abbrev_en="EMC",
    isin_code="TW0002603004",
    listing_date="1987-09-25",
    market_type="上市",
    industry_zh="航運業",
    tax_id="11111111",
    incorporation_country_raw="",
    incorporation_date="1968-09-01",
    paid_in_capital_ntd=38_808_000_000,
    shares_outstanding="3880800000",
    par_value_ntd="10",
    chairman="張衍義",
    ceo="謝惠全",
    spokesperson="蔡文瑞",
    spokesperson_title="財務長",
    primary_business="貨櫃輪航運",
    auditor_firm="資誠聯合會計師事務所",
    auditor1="陳明進",
    auditor2="徐薇婷",
    phone="02-25056688",
    fax="02-25056000",
    address="台北市中山區民生東路二段166號",
    email="ir@evergreen-marine.com",
    website="www.evergreen-marine.com",
    financial_report_type="合併",
    major_shareholders=["長榮國際股份有限公司", "張衍義"],
    board_members=[
        BoardMember(title="董事長", name="張衍義", shares_current="500000000", pledged_shares="0", pledge_pct="0"),
        BoardMember(title="董事", name="謝惠全", shares_current="100000", pledged_shares="0", pledge_pct="0"),
        BoardMember(title="獨立董事", name="王小明", shares_current="0", pledged_shares="0", pledge_pct="0"),
    ],
    raw={"company": _SAMPLE_COMPANY, "shareholders": _SAMPLE_SHAREHOLDERS, "board": _SAMPLE_BOARD},
)


# ── SUPPORTED_SECTIONS constant ───────────────────────────────────────────────

def test_supported_sections():
    assert SUPPORTED_SECTIONS == frozenset({1, 3, 4, 5, 7, 9})


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
    assert _parse_twse_date("abcdefgh") == "abcdefgh"


def test_parse_date_too_short():
    assert _parse_twse_date("2024") == "2024"


def test_parse_date_future_unchanged():
    assert _parse_twse_date("29991231") == "29991231"


# ── _find_one / _find_all ─────────────────────────────────────────────────────

def test_find_one_exact():
    rows = [{"公司代號": "2603", "v": 1}, {"公司代號": "2412", "v": 2}]
    assert _find_one(rows, "2603")["v"] == 1


def test_find_one_with_whitespace():
    rows = [{"公司代號": " 2603 "}]
    assert _find_one(rows, "2603") != {}


def test_find_one_not_found():
    assert _find_one([{"公司代號": "2603"}], "9999") == {}


def test_find_all_returns_multiple():
    rows = [
        {"公司代號": "2603", "大股東名稱": "A"},
        {"公司代號": "2603", "大股東名稱": "B"},
        {"公司代號": "2412", "大股東名稱": "C"},
    ]
    result = _find_all(rows, "2603")
    assert len(result) == 2
    assert result[0]["大股東名稱"] == "A"


def test_find_all_empty_list():
    assert _find_all([], "2603") == []


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
    assert deep_get({"a": {"b": {"c": 7}}}, "a.b.c") == 7


def test_deep_get_missing():
    assert deep_get({}, "x.y.z") is None


def test_deep_get_partial_missing():
    assert deep_get({"a": {}}, "a.b.c") is None


# ── apply_field_mapping ───────────────────────────────────────────────────────

def test_apply_only_empty_fills_missing():
    result, written, skipped = apply_field_mapping({}, {"a.b": "val"}, "only_empty")
    assert result["a"]["b"] == "val"
    assert written == 1 and skipped == 0


def test_apply_only_empty_skips_existing():
    result, written, skipped = apply_field_mapping(
        {"a": {"b": "keep"}}, {"a.b": "new"}, "only_empty"
    )
    assert result["a"]["b"] == "keep"
    assert written == 0 and skipped == 1


def test_apply_only_empty_treats_empty_string_as_blank():
    result, written, _ = apply_field_mapping({"x": ""}, {"x": "filled"}, "only_empty")
    assert result["x"] == "filled"
    assert written == 1


def test_apply_overwrite_replaces():
    result, written, _ = apply_field_mapping(
        {"a": {"b": "orig"}}, {"a.b": "new"}, "overwrite"
    )
    assert result["a"]["b"] == "new"
    assert written == 1


def test_apply_empty_mapping():
    _, written, skipped = apply_field_mapping({"x": 1}, {}, "only_empty")
    assert written == 0 and skipped == 0


# ── map_to_section1 ───────────────────────────────────────────────────────────

def test_section1_borrower_name():
    m = map_to_section1(_SAMPLE_DATA)
    assert m["terms_and_conditions.borrower"] == "長榮海運"


def test_section1_china_flag_false_for_taiwan():
    m = map_to_section1(_SAMPLE_DATA)
    assert "regulatory_compliance.china_invested_enterprise" not in m


def test_section1_china_flag_true_for_prc():
    data = TWSECompanyData(stock_code="9999", company_name_zh="某公司",
                           incorporation_country_raw="中國大陸")
    m = map_to_section1(data)
    assert m.get("regulatory_compliance.china_invested_enterprise") is True


def test_section1_china_flag_false_for_cayman():
    data = TWSECompanyData(stock_code="9999", company_name_zh="某公司",
                           incorporation_country_raw="Cayman Islands")
    m = map_to_section1(data)
    assert m.get("regulatory_compliance.china_invested_enterprise") is False


# ── map_to_section3 ───────────────────────────────────────────────────────────

def test_section3_full_name():
    m = map_to_section3(_SAMPLE_DATA)
    assert m["3B_internal_ratings.borrower_entity_full_name"] == "Evergreen Marine Corp"


def test_section3_abbrev():
    m = map_to_section3(_SAMPLE_DATA)
    assert m["3B_internal_ratings.borrower_entity_abbrev"] == "EMC"


def test_section3_falls_back_to_zh_name():
    data = TWSECompanyData(stock_code="2603", company_name_zh="長榮海運",
                           company_name_en="", company_name_abbrev_en="",
                           company_name_abbrev_zh="長榮")
    m = map_to_section3(data)
    assert m["3B_internal_ratings.borrower_entity_full_name"] == "長榮海運"
    assert m["3B_internal_ratings.borrower_entity_abbrev"] == "長榮"


# ── map_to_section4 ───────────────────────────────────────────────────────────

def test_section4_4a_fields():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4A_borrower.company_name_zh"] == "長榮海運"
    assert m["4A_borrower.company_name_en"] == "Evergreen Marine Corp"
    assert m["4A_borrower.registration_number"] == "11111111"
    assert m["4A_borrower.incorporation_country"] == "Taiwan"
    assert m["4A_borrower.incorporation_date"] == "1968-09-01"
    assert m["4A_borrower.legal_entity_type"] == "Listed Company"
    assert m["4A_borrower.fiscal_year_end"] == "December 31"
    assert m["4A_borrower.group_auditor"] == "資誠聯合會計師事務所"


def test_section4_incorporation_country_uses_raw_if_set():
    data = TWSECompanyData(stock_code="2603", market_type="上市",
                           incorporation_country_raw="Cayman Islands")
    m = map_to_section4(data)
    assert m["4A_borrower.incorporation_country"] == "Cayman Islands"


def test_section4_otc_legal_entity_type():
    data = TWSECompanyData(stock_code="2603", market_type="上櫃")
    m = map_to_section4(data)
    assert m["4A_borrower.legal_entity_type"] == "OTC Listed Company"


def test_section4_4b_shareholders():
    m = map_to_section4(_SAMPLE_DATA)
    sh = m["4B_ownership.shareholders"]
    assert isinstance(sh, list)
    assert len(sh) == 2
    assert "長榮國際股份有限公司|>10%|Taiwan" in sh
    assert m["4B_ownership.ultimate_beneficial_owner"] == "長榮國際股份有限公司"


def test_section4_no_shareholders_when_empty():
    data = TWSECompanyData(stock_code="2603")
    m = map_to_section4(data)
    assert "4B_ownership.shareholders" not in m
    assert "4B_ownership.ultimate_beneficial_owner" not in m


def test_section4_4c_management():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4C_management.ceo_name"] == "謝惠全"
    assert m["4C_management.ceo_title"] == "President"


def test_section4_4d_business():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4D_business.primary_business"] == "貨櫃輪航運"


def test_section4_4e_financials():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4E_financials.currency"] == "NTD"
    assert m["4E_financials.paid_in_capital_ntd_bn"] == pytest.approx(38.808, rel=1e-3)


def test_section4_empty_data_omits_optional_fields():
    m = map_to_section4(TWSECompanyData(stock_code="0000"))
    assert "4A_borrower.company_name_zh" not in m
    assert "4E_financials.paid_in_capital_ntd_bn" not in m


# ── map_to_section5 ───────────────────────────────────────────────────────────

def test_section5_5f_guarantor_fields():
    m = map_to_section5(_SAMPLE_DATA)
    assert m["5F_corporate_guarantee.guarantor_full_name"] == "長榮海運"
    assert m["5F_corporate_guarantee.guarantor_listed_exchange"] == "Taiwan Stock Exchange"


def test_section5_5f_otc_exchange():
    data = TWSECompanyData(stock_code="2603", company_name_zh="某公司", market_type="上櫃")
    m = map_to_section5(data)
    assert m["5F_corporate_guarantee.guarantor_listed_exchange"] == "Taipei Exchange"


def test_section5_5g_responsible_person_from_board():
    m = map_to_section5(_SAMPLE_DATA)
    # Board has 董事長=張衍義, should be preferred over basic chairman field
    assert m["5G_responsible_person.name"] == "張衍義"
    assert m["5G_responsible_person.title"] == "Chairman / 董事長"


def test_section5_5g_falls_back_to_basic_chairman():
    data = TWSECompanyData(stock_code="2603", chairman="王大明",
                           company_name_zh="某公司", market_type="上市")
    m = map_to_section5(data)
    assert m["5G_responsible_person.name"] == "王大明"


def test_section5_5g_no_chairman_omits_fields():
    data = TWSECompanyData(stock_code="2603", company_name_zh="某公司")
    m = map_to_section5(data)
    assert "5G_responsible_person.name" not in m


# ── map_to_section7_metadata ──────────────────────────────────────────────────

def test_section7_7a_fields():
    m = map_to_section7_metadata(_SAMPLE_DATA)
    assert m["7A_borrower_financials.reporting_entity"] == "長榮海運"
    assert m["7A_borrower_financials.auditor"] == "資誠聯合會計師事務所"
    assert m["7A_borrower_financials.fiscal_year_end"] == "December 31"
    assert m["7A_borrower_financials.reporting_currency"] == "NTD"
    assert m["7A_borrower_financials.unit"] == "NTD million"
    assert m["7A_borrower_financials.accounting_standard"] == "IFRS (TIFRS)"


def test_section7_entities_to_analyze():
    m = map_to_section7_metadata(_SAMPLE_DATA)
    assert m["entities_to_analyze.borrower_name"] == "Evergreen Marine Corp"
    assert m["entities_to_analyze.borrower_currency"] == "NTD"
    assert m["entities_to_analyze.borrower_unit"] == "NTD million"


def test_section7_falls_back_to_zh_name():
    data = TWSECompanyData(stock_code="2603", company_name_zh="長榮海運",
                           company_name_en="")
    m = map_to_section7_metadata(data)
    assert m["7A_borrower_financials.reporting_entity"] == "長榮海運"
    assert m["entities_to_analyze.borrower_name"] == "長榮海運"


# ── map_to_section dispatch ───────────────────────────────────────────────────

def test_map_to_section_dispatch():
    assert "terms_and_conditions.borrower" in map_to_section(1, _SAMPLE_DATA)
    assert "3B_internal_ratings.borrower_entity_full_name" in map_to_section(3, _SAMPLE_DATA)
    assert "4A_borrower.company_name_zh" in map_to_section(4, _SAMPLE_DATA)
    assert "5G_responsible_person.name" in map_to_section(5, _SAMPLE_DATA)
    assert "7A_borrower_financials.reporting_entity" in map_to_section(7, _SAMPLE_DATA)


def test_map_to_section_unsupported_returns_empty():
    assert map_to_section(2, _SAMPLE_DATA) == {}
    assert map_to_section(6, _SAMPLE_DATA) == {}
    assert map_to_section(10, _SAMPLE_DATA) == {}


# ── fetch_twse_company (HTTP mocked) ─────────────────────────────────────────

def _make_mock_resp(data):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=data)
    return r


@pytest.mark.asyncio
async def test_fetch_twse_company_success():
    from credit_report.api.twse_importer import fetch_twse_company

    async def _fake_get(url, **kw):
        if "t187ap03_L" in url:
            return _make_mock_resp([_SAMPLE_COMPANY])
        if "t187ap03_P" in url:
            return _make_mock_resp([])
        if "t187ap02_L" in url:
            return _make_mock_resp(_SAMPLE_SHAREHOLDERS)
        if "t187ap11_L" in url:
            return _make_mock_resp(_SAMPLE_BOARD)
        return _make_mock_resp([])

    with patch("httpx.AsyncClient") as mock_cls:
        mc = AsyncMock()
        mc.__aenter__ = AsyncMock(return_value=mc)
        mc.__aexit__ = AsyncMock(return_value=None)
        mc.get = _fake_get
        mock_cls.return_value = mc
        data = await fetch_twse_company("2603")

    assert data is not None
    assert data.company_name_zh == "長榮海運"
    assert data.company_name_en == "Evergreen Marine Corp"
    assert data.tax_id == "11111111"
    assert data.ceo == "謝惠全"
    assert data.chairman == "張衍義"
    assert data.auditor_firm == "資誠聯合會計師事務所"
    assert data.incorporation_date == "1968-09-01"
    assert data.paid_in_capital_ntd == 38_808_000_000
    assert len(data.major_shareholders) == 2
    assert data.major_shareholders[0] == "長榮國際股份有限公司"
    assert len(data.board_members) == 3
    assert data.board_members[0].title == "董事長"


@pytest.mark.asyncio
async def test_fetch_twse_company_falls_back_to_otc():
    from credit_report.api.twse_importer import fetch_twse_company

    async def _fake_get(url, **kw):
        if "t187ap03_P" in url:
            return _make_mock_resp([{**_SAMPLE_COMPANY, "市場別": "上櫃"}])
        return _make_mock_resp([])

    with patch("httpx.AsyncClient") as mock_cls:
        mc = AsyncMock()
        mc.__aenter__ = AsyncMock(return_value=mc)
        mc.__aexit__ = AsyncMock(return_value=None)
        mc.get = _fake_get
        mock_cls.return_value = mc
        data = await fetch_twse_company("2603")

    assert data is not None
    assert data.company_name_zh == "長榮海運"


@pytest.mark.asyncio
async def test_fetch_twse_company_not_found():
    from credit_report.api.twse_importer import fetch_twse_company

    with patch("httpx.AsyncClient") as mock_cls:
        mc = AsyncMock()
        mc.__aenter__ = AsyncMock(return_value=mc)
        mc.__aexit__ = AsyncMock(return_value=None)
        mc.get = AsyncMock(return_value=_make_mock_resp([]))
        mock_cls.return_value = mc
        assert await fetch_twse_company("9999") is None


@pytest.mark.asyncio
async def test_fetch_twse_company_http_error_returns_none():
    import httpx as _httpx
    from credit_report.api.twse_importer import fetch_twse_company

    async def _failing(*a, **kw):
        raise _httpx.ConnectError("network unreachable")

    with patch("httpx.AsyncClient") as mock_cls:
        mc = AsyncMock()
        mc.__aenter__ = AsyncMock(return_value=mc)
        mc.__aexit__ = AsyncMock(return_value=None)
        mc.get = _failing
        mock_cls.return_value = mc
        assert await fetch_twse_company("2603") is None


# ── /import-twse endpoint integration ────────────────────────────────────────

@pytest.fixture()
def ac():
    from httpx import AsyncClient, ASGITransport
    import main
    return AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")


async def _login(ac) -> dict:
    r = await ac.post(f"{BASE}/auth/login",
                      data={"username": "admin@example.com", "password": "admin123"})
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
    rid = cr.json()["id"]
    r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "apply_mode": "bad_mode"}, headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_twse_invalid_section(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportTestSec", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "sections": [4, 99]}, headers=hdrs)
    assert r.status_code == 422
    assert "99" in r.json()["detail"]


@pytest.mark.asyncio
async def test_import_twse_unsupported_section_2_rejected(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportSec2", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "sections": [2]}, headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_import_twse_stock_not_found(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportNF", "industry": "marine"},
                       headers=hdrs)
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
async def test_import_twse_sections_1_3_4_5_7_success(ac):
    """Importing all supported sections writes fields across §1/§3/§4/§5/§7."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportAll", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603", "apply_mode": "overwrite",
                                "sections": [1, 3, 4, 5, 7]},
                          headers=hdrs)
    assert r.status_code == 200
    body = r.json()
    assert body["not_found"] is False
    assert body["company_name"] == "長榮海運"
    assert body["fields_written"] > 0
    updated = set(body["sections_updated"])
    assert updated == {1, 3, 4, 5, 7}


@pytest.mark.asyncio
async def test_import_twse_default_sections_are_4_and_7(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportDef", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603", "apply_mode": "overwrite"},
                          headers=hdrs)
    assert r.status_code == 200
    body = r.json()
    assert set(body["sections_updated"]) == {4, 7}


@pytest.mark.asyncio
async def test_import_twse_section4_writes_registration_number(ac):
    """Verify the new tax_id → registration_number field is written to §4."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWTaxId", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "apply_mode": "overwrite",
                            "sections": [4]},
                      headers=hdrs)
    si = await ac.get(f"{BASE}/reports/{rid}/inputs/4", headers=hdrs)
    ij = si.json()["input_json"]
    assert ij["4A_borrower"]["registration_number"] == "11111111"


@pytest.mark.asyncio
async def test_import_twse_section4_writes_shareholders(ac):
    """Verify major shareholders from t187ap02_L populate §4B."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWShareholders", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "apply_mode": "overwrite",
                            "sections": [4]},
                      headers=hdrs)
    si = await ac.get(f"{BASE}/reports/{rid}/inputs/4", headers=hdrs)
    ij = si.json()["input_json"]
    sh = ij["4B_ownership"]["shareholders"]
    assert isinstance(sh, list) and len(sh) == 2
    assert any("長榮國際" in s for s in sh)


@pytest.mark.asyncio
async def test_import_twse_section5_writes_responsible_person(ac):
    """Verify chairman from board data populates §5G responsible person."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWResPerson", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "apply_mode": "overwrite",
                            "sections": [5]},
                      headers=hdrs)
    si = await ac.get(f"{BASE}/reports/{rid}/inputs/5", headers=hdrs)
    ij = si.json()["input_json"]
    assert ij["5G_responsible_person"]["name"] == "張衍義"
    assert "Chairman" in ij["5G_responsible_person"]["title"]


@pytest.mark.asyncio
async def test_import_twse_only_empty_skips_filled(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWOnlyEmpty", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    await ac.put(f"{BASE}/reports/{rid}/inputs/4",
                 json={"section_no": 4, "input_json": {
                     "4A_borrower": {"company_name_zh": "已填寫"}}},
                 headers=hdrs)
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603", "apply_mode": "only_empty",
                                "sections": [4]},
                          headers=hdrs)
    assert r.status_code == 200
    assert r.json()["fields_skipped"] >= 1
    si = await ac.get(f"{BASE}/reports/{rid}/inputs/4", headers=hdrs)
    assert si.json()["input_json"]["4A_borrower"]["company_name_zh"] == "已填寫"


@pytest.mark.asyncio
async def test_import_twse_approved_report_returns_409(ac):
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWApproved409", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    r_upd = await ac.patch(f"{BASE}/reports/{rid}/status",
                           json={"status": "approved"}, headers=hdrs)
    if r_upd.status_code not in (200, 201):
        pytest.skip("Cannot set approved status in test environment")
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603"}, headers=hdrs)
    assert r.status_code == 409


# ── Phase 2 tests ─────────────────────────────────────────────────────────────

# ── Phase 2: Material news classification ─────────────────────────────────────

def test_classify_news_financial_risk():
    from credit_report.api.twse_importer import _classify_news_risk
    assert _classify_news_risk("財務重大事項", "財務") == "financial"


def test_classify_news_litigation():
    from credit_report.api.twse_importer import _classify_news_risk
    assert _classify_news_risk("訴訟案件", "訴訟") == "litigation"


def test_classify_news_general():
    from credit_report.api.twse_importer import _classify_news_risk
    assert _classify_news_risk("公告事項", "其他") == "general"


def test_classify_news_guarantee():
    from credit_report.api.twse_importer import _classify_news_risk
    assert _classify_news_risk("背書保證事項", "") == "guarantee"


# ── Phase 2: ROC date parsing ─────────────────────────────────────────────────

def test_roc_ym_5digit_dec2025():
    from credit_report.api.twse_importer import _roc_year_month_to_gregorian
    assert _roc_year_month_to_gregorian("11412") == (2025, 12)


def test_roc_ym_jan2025():
    from credit_report.api.twse_importer import _roc_year_month_to_gregorian
    yr, mo = _roc_year_month_to_gregorian("11401")
    assert yr == 2025 and mo == 1


def test_roc_ym_invalid_empty():
    from credit_report.api.twse_importer import _roc_year_month_to_gregorian
    assert _roc_year_month_to_gregorian("") == (0, 0)


def test_roc_ym_invalid_str():
    from credit_report.api.twse_importer import _roc_year_month_to_gregorian
    assert _roc_year_month_to_gregorian("abc") == (0, 0)


# ── Phase 2: MonthlyRevenue dataclass ─────────────────────────────────────────

def test_monthly_revenue_defaults():
    from credit_report.api.twse_importer import MonthlyRevenue
    rev = MonthlyRevenue(year_month="11412", revenue_k_ntd=123456.0, yoy_pct=5.2)
    assert rev.year_month == "11412"
    assert rev.revenue_k_ntd == 123456.0
    assert rev.mom_pct == 0.0


# ── Phase 2: MaterialNewsItem dataclass ───────────────────────────────────────

def test_material_news_item_defaults():
    from credit_report.api.twse_importer import MaterialNewsItem
    item = MaterialNewsItem(subject="重大消息", risk_category="financial")
    assert item.subject == "重大消息"
    assert item.date == ""


# ── Phase 2: map_to_section7_financials ───────────────────────────────────────

def test_section7_financials_maps_revenue():
    from credit_report.api.twse_importer import (
        TWSECompanyData, MonthlyRevenue, map_to_section7_financials,
    )
    # 12 months of FY2024 data (ROC 113); year_month is 5 chars: "11301"–"11312"
    revs = [
        MonthlyRevenue(
            year_month=f"113{m:02d}",
            revenue_k_ntd=10_000_000.0,
            ytd_revenue_k_ntd=m * 10_000_000.0,
            ytd_yoy_pct=5.0,
        )
        for m in range(1, 13)
    ]
    data = TWSECompanyData(
        stock_code="2603",
        company_name_zh="長榮海運",
        monthly_revenues=revs,
    )
    result = map_to_section7_financials(data)
    # 12 months × 10,000,000 thousands = 120,000,000 thousands = 120,000 NTD million
    assert "7A_borrower_financials.income_statement.FY2024.revenue" in result
    assert result["7A_borrower_financials.income_statement.FY2024.revenue"] == 120_000.0


def test_section7_financials_empty_returns_empty():
    from credit_report.api.twse_importer import TWSECompanyData, map_to_section7_financials
    data = TWSECompanyData(stock_code="2603", company_name_zh="長榮海運")
    result = map_to_section7_financials(data)
    # No revenue data — should return empty dict
    assert not any("income_statement" in k for k in result)
    assert result == {}


# ── Phase 2: map_to_section4_event_risk ───────────────────────────────────────

def test_section4_event_risk_populated():
    from credit_report.api.twse_importer import (
        TWSECompanyData, MaterialNewsItem, map_to_section4_event_risk,
    )
    news = [
        MaterialNewsItem(date="20250101", subject="重大訴訟", clause="訴訟", risk_category="litigation"),
        MaterialNewsItem(date="20250110", subject="背書保證事項", clause="背書保證", risk_category="guarantee"),
    ]
    data = TWSECompanyData(stock_code="2603", company_name_zh="長榮", material_news=news)
    result = map_to_section4_event_risk(data)
    assert result["4G_risk_events.has_material_news"] is True
    assert result["4G_risk_events.news_count_recent"] == 2
    assert "litigation" in result["4G_risk_events.high_risk_categories"]


def test_section4_event_risk_empty_news():
    from credit_report.api.twse_importer import TWSECompanyData, map_to_section4_event_risk
    data = TWSECompanyData(stock_code="2603")
    assert map_to_section4_event_risk(data) == {}


# ── Phase 2: updated map_to_section dispatch ──────────────────────────────────

def test_map_to_section7_includes_financials_when_revenue_present():
    from credit_report.api.twse_importer import (
        TWSECompanyData, MonthlyRevenue, map_to_section,
    )
    revs = [MonthlyRevenue(year_month="11312", revenue_k_ntd=5_000_000.0)]
    data = TWSECompanyData(stock_code="2603", company_name_zh="長榮", monthly_revenues=revs)
    result = map_to_section(7, data)
    # Metadata keys still present
    assert "7A_borrower_financials.reporting_currency" in result
    # Revenue key added by map_to_section7_financials
    assert "7A_borrower_financials.income_statement.FY2024.revenue" in result


def test_map_to_section4_includes_event_risk():
    from credit_report.api.twse_importer import (
        TWSECompanyData, MaterialNewsItem, map_to_section,
    )
    news = [MaterialNewsItem(date="20250101", subject="訴訟", clause="訴訟", risk_category="litigation")]
    data = TWSECompanyData(stock_code="2603", company_name_zh="長榮", material_news=news)
    result = map_to_section(4, data)
    # Basic §4A key still present
    assert "4A_borrower.company_name_zh" in result
    # Event risk key added by map_to_section4_event_risk
    assert "4G_risk_events.has_material_news" in result


# ── Phase 2: fetch_twse_company integration (material news + monthly revenue) ─

@pytest.mark.asyncio
async def test_fetch_twse_company_includes_material_news():
    """fetch_twse_company() should populate material_news from t187ap04_L."""
    from credit_report.api.twse_importer import fetch_twse_company

    _NEWS = [{"公司代號": "2603", "主旨": "重大訴訟", "符合條款": "訴訟",
              "出表日期": "20250101", "事實發生日": "20250101", "說明": "訴訟詳情"}]
    _REVS = [{"公司代號": "2603", "資料年月": "11312", "當月營收": "50000000",
              "上月比較增減%": "5", "去年同月增減%": "10",
              "當月累計營收": "500000000", "前期比較增減%": "8"}]

    async def mock_get(url, **kwargs):
        class R:
            def raise_for_status(self): pass
            def json(self):
                if "t187ap03_L" in url:   return [_SAMPLE_COMPANY]
                if "t187ap03_P" in url:   return []
                if "t187ap02_L" in url:   return _SAMPLE_SHAREHOLDERS
                if "t187ap11_L" in url:   return _SAMPLE_BOARD
                if "t187ap04_L" in url:   return _NEWS
                if "t21sc03_1"  in url:   return _REVS
                if "t21sc03_2"  in url:   return []
                return []
        return R()

    with patch("httpx.AsyncClient") as mock_cls:
        mc = AsyncMock()
        mc.__aenter__ = AsyncMock(return_value=mc)
        mc.__aexit__ = AsyncMock(return_value=None)
        mc.get = mock_get
        mock_cls.return_value = mc
        result = await fetch_twse_company("2603")

    assert result is not None
    assert len(result.material_news) == 1
    assert result.material_news[0].risk_category == "litigation"
    assert len(result.monthly_revenues) == 1
    assert result.monthly_revenues[0].revenue_k_ntd == 50_000_000.0


# ── Phase 3 tests ─────────────────────────────────────────────────────────────

# ── Phase 3: _latest_ytd_revenue helper ──────────────────────────────────────

def test_latest_ytd_revenue_no_data():
    data = TWSECompanyData(stock_code="2603")
    assert _latest_ytd_revenue(data) == (0, 0.0)


def test_latest_ytd_revenue_returns_year_and_millions():
    # 120,000,000 thousands NTD → 120,000 NTD million
    revs = [MonthlyRevenue(year_month="11312", revenue_k_ntd=5_000_000.0,
                           ytd_revenue_k_ntd=120_000_000.0)]
    data = TWSECompanyData(stock_code="2603", monthly_revenues=revs)
    yr, m = _latest_ytd_revenue(data)
    assert yr == 2024
    assert m == pytest.approx(120_000.0)


def test_latest_ytd_revenue_invalid_year_month():
    revs = [MonthlyRevenue(year_month="abc", ytd_revenue_k_ntd=1_000_000.0)]
    data = TWSECompanyData(stock_code="2603", monthly_revenues=revs)
    assert _latest_ytd_revenue(data) == (0, 0.0)


# ── Phase 3: map_to_section4 new fields ──────────────────────────────────────

def _sample_with_revenue() -> TWSECompanyData:
    """_SAMPLE_DATA augmented with monthly revenue for fiscal-year snapshot tests."""
    revs = [MonthlyRevenue(year_month="11312", revenue_k_ntd=5_000_000.0,
                           ytd_revenue_k_ntd=120_000_000.0)]
    import dataclasses
    return dataclasses.replace(_SAMPLE_DATA, monthly_revenues=revs)


def test_section4_isin_code():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4A_borrower.isin_code"] == "TW0002603004"


def test_section4_listing_date():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4A_borrower.listing_date"] == "1987-09-25"


def test_section4_shares_outstanding():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4A_borrower.shares_outstanding"] == "3880800000"


def test_section4_cfo_name_from_spokesperson():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4C_management.cfo_name"] == "蔡文瑞"
    assert m["4C_management.cfo_title"] == "財務長"


def test_section4_industry_category():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4D_business.industry_category"] == "航運業"


def test_section4_reporting_type():
    m = map_to_section4(_SAMPLE_DATA)
    assert m["4D_business.reporting_type"] == "合併"


def test_section4_fiscal_year_from_monthly_revenue():
    data = _sample_with_revenue()
    m = map_to_section4(data)
    assert m["4E_financials.fiscal_year"] == "2024"


def test_section4_revenue_from_ytd_in_ntd_million():
    data = _sample_with_revenue()
    m = map_to_section4(data)
    assert m["4E_financials.revenue"] == pytest.approx(120_000.0)


def test_section4_unit_from_monthly_revenue():
    data = _sample_with_revenue()
    m = map_to_section4(data)
    assert m["4E_financials.unit"] == "NTD million"


def test_section4_no_revenue_omits_fiscal_year_and_revenue():
    m = map_to_section4(_SAMPLE_DATA)
    assert "4E_financials.fiscal_year" not in m
    assert "4E_financials.revenue" not in m


# ── Phase 3: map_to_section5 revenue_twd_bn ──────────────────────────────────

def test_section5_revenue_twd_bn():
    data = _sample_with_revenue()
    m = map_to_section5(data)
    # 120,000 NTD million = 120 NTD billion
    assert m["5F_corporate_guarantee.revenue_twd_bn"] == pytest.approx(120.0, rel=1e-3)


def test_section5_no_revenue_omits_revenue_twd_bn():
    m = map_to_section5(_SAMPLE_DATA)
    assert "5F_corporate_guarantee.revenue_twd_bn" not in m


# ── Phase 3: map_to_section9 ──────────────────────────────────────────────────

def test_section9_item16_entity_name_en():
    m = map_to_section9(_SAMPLE_DATA)
    assert m["9A_checklist.item16_entity_name"] == "Evergreen Marine Corp"


def test_section9_falls_back_to_zh():
    data = TWSECompanyData(stock_code="2603", company_name_zh="長榮海運", company_name_en="")
    m = map_to_section9(data)
    assert m["9A_checklist.item16_entity_name"] == "長榮海運"


def test_section9_no_name_returns_empty():
    m = map_to_section9(TWSECompanyData(stock_code="2603"))
    assert m == {}


# ── Phase 3: dispatch includes §9 ────────────────────────────────────────────

def test_map_to_section9_via_dispatch():
    result = map_to_section(9, _SAMPLE_DATA)
    assert "9A_checklist.item16_entity_name" in result


def test_map_to_section_unsupported_still_returns_empty():
    assert map_to_section(2, _SAMPLE_DATA) == {}
    assert map_to_section(6, _SAMPLE_DATA) == {}
    assert map_to_section(10, _SAMPLE_DATA) == {}


# ── Phase 3: integration — sections 1/3/4/5/7/9 all updated ──────────────────

@pytest.mark.asyncio
async def test_import_twse_sections_1_3_4_5_7_9_success(ac):
    """Importing all supported sections including §9 writes entity name to checklist."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWImportAll9", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        r = await ac.post(f"{BASE}/reports/{rid}/import-twse",
                          json={"stock_code": "2603", "apply_mode": "overwrite",
                                "sections": [1, 3, 4, 5, 7, 9]},
                          headers=hdrs)
    assert r.status_code == 200
    body = r.json()
    assert body["not_found"] is False
    assert set(body["sections_updated"]) == {1, 3, 4, 5, 7, 9}


# ── Phase 3 robustness: ordering-independent revenue + raw payload ───────────

def test_latest_ytd_revenue_unordered_input():
    """API may return monthly data in any order; helper must still pick the latest."""
    revs = [
        MonthlyRevenue(year_month="11301", ytd_revenue_k_ntd=10_000_000.0),
        MonthlyRevenue(year_month="11312", ytd_revenue_k_ntd=120_000_000.0),
        MonthlyRevenue(year_month="11306", ytd_revenue_k_ntd=60_000_000.0),
    ]
    data = TWSECompanyData(stock_code="2603", monthly_revenues=revs)
    yr, m = _latest_ytd_revenue(data)
    assert yr == 2024
    assert m == pytest.approx(120_000.0)


def test_latest_ytd_revenue_skips_blank_year_month():
    revs = [
        MonthlyRevenue(year_month="", ytd_revenue_k_ntd=999_000.0),
        MonthlyRevenue(year_month="11312", ytd_revenue_k_ntd=120_000_000.0),
    ]
    data = TWSECompanyData(stock_code="2603", monthly_revenues=revs)
    yr, m = _latest_ytd_revenue(data)
    assert yr == 2024 and m == pytest.approx(120_000.0)


def test_section7_financials_partial_year_uses_latest_month_yoy():
    """Partial year (jan + jun + nov, unordered) must pick Nov's YTD YoY, not insertion order."""
    from credit_report.api.twse_importer import map_to_section7_financials
    revs = [
        # Insertion order: oldest-first would have broken Phase 2 partial-year branch.
        MonthlyRevenue(year_month="11301", revenue_k_ntd=10_000_000.0,
                       ytd_revenue_k_ntd=10_000_000.0, ytd_yoy_pct=1.1),
        MonthlyRevenue(year_month="11306", revenue_k_ntd=10_000_000.0,
                       ytd_revenue_k_ntd=60_000_000.0, ytd_yoy_pct=3.3),
        MonthlyRevenue(year_month="11311", revenue_k_ntd=10_000_000.0,
                       ytd_revenue_k_ntd=110_000_000.0, ytd_yoy_pct=7.7),
    ]
    data = TWSECompanyData(stock_code="2603", monthly_revenues=revs)
    result = map_to_section7_financials(data)
    # Latest available month is Nov → ytd_yoy_pct = 7.7
    assert result["7A_borrower_financials.income_statement.FY2024.revenue_yoy_pct"] == pytest.approx(7.7)


def test_section7_financials_groups_multiple_years():
    """Multiple Gregorian years must each produce their own FY{year} key."""
    from credit_report.api.twse_importer import map_to_section7_financials
    revs = [
        MonthlyRevenue(year_month="11212", revenue_k_ntd=5_000_000.0),   # FY2023
        MonthlyRevenue(year_month="11312", revenue_k_ntd=10_000_000.0),  # FY2024
    ]
    data = TWSECompanyData(stock_code="2603", monthly_revenues=revs)
    result = map_to_section7_financials(data)
    assert "7A_borrower_financials.income_statement.FY2023.revenue" in result
    assert "7A_borrower_financials.income_statement.FY2024.revenue" in result


def test_section4_event_risk_news_count_uses_recent_label():
    """The label is news_count_recent (not _90d) because t187ap04_L is a daily snapshot."""
    from credit_report.api.twse_importer import (
        MaterialNewsItem, map_to_section4_event_risk,
    )
    news = [MaterialNewsItem(subject="訴訟", risk_category="litigation")]
    data = TWSECompanyData(stock_code="2603", material_news=news)
    result = map_to_section4_event_risk(data)
    assert "4G_risk_events.news_count_recent" in result
    assert "4G_risk_events.news_count_90d" not in result


def test_section4_event_risk_only_general_news_has_empty_categories():
    """If every news item is 'general', high_risk_categories is an empty list (not absent)."""
    from credit_report.api.twse_importer import (
        MaterialNewsItem, map_to_section4_event_risk,
    )
    news = [
        MaterialNewsItem(subject="例行公告", risk_category="general"),
        MaterialNewsItem(subject="季度公告", risk_category="general"),
    ]
    data = TWSECompanyData(stock_code="2603", material_news=news)
    result = map_to_section4_event_risk(data)
    # has_material_news is True (news exists), but no high-risk categories
    assert result["4G_risk_events.has_material_news"] is True
    assert result["4G_risk_events.high_risk_categories"] == []


@pytest.mark.asyncio
async def test_fetch_twse_company_raw_includes_news_and_revenue():
    """raw dict must capture all source rows (news + revenue) for audit trail."""
    from credit_report.api.twse_importer import fetch_twse_company

    _NEWS = [{"公司代號": "2603", "主旨": "訴訟", "符合條款": "訴訟", "出表日期": "20250101"}]
    _REVS = [{"公司代號": "2603", "資料年月": "11312", "當月營收": "50000000",
              "當月累計營收": "500000000"}]

    async def mock_get(url, **kwargs):
        class R:
            def raise_for_status(self): pass
            def json(self):
                if "t187ap03_L" in url:   return [_SAMPLE_COMPANY]
                if "t187ap04_L" in url:   return _NEWS
                if "t21sc03_1"  in url:   return _REVS
                return []
        return R()

    with patch("httpx.AsyncClient") as mock_cls:
        mc = AsyncMock()
        mc.__aenter__ = AsyncMock(return_value=mc)
        mc.__aexit__ = AsyncMock(return_value=None)
        mc.get = mock_get
        mock_cls.return_value = mc
        result = await fetch_twse_company("2603")

    assert result is not None
    assert "material_news" in result.raw
    assert "monthly_revenues" in result.raw
    assert len(result.raw["material_news"]) == 1
    assert len(result.raw["monthly_revenues"]) == 1


@pytest.mark.asyncio
async def test_import_twse_section9_writes_entity_name(ac):
    """§9 import writes item16_entity_name to the checklist section."""
    hdrs = await _login(ac)
    cr = await ac.post(f"{BASE}/reports",
                       json={"borrower_name": "TWSec9Check", "industry": "marine"},
                       headers=hdrs)
    rid = cr.json()["id"]
    with patch("credit_report.api.twse_importer.fetch_twse_company",
               new=AsyncMock(return_value=_SAMPLE_DATA)):
        await ac.post(f"{BASE}/reports/{rid}/import-twse",
                      json={"stock_code": "2603", "apply_mode": "overwrite",
                            "sections": [9]},
                      headers=hdrs)
    si = await ac.get(f"{BASE}/reports/{rid}/inputs/9", headers=hdrs)
    ij = si.json()["input_json"]
    assert ij["9A_checklist"]["item16_entity_name"] == "Evergreen Marine Corp"


# ── Phase 4 (P1 financial statements) ────────────────────────────────────────

_SAMPLE_IS = IncomeStatementFact(
    fiscal_year=2024,
    revenue_k_ntd=500_000_000.0,      # 500 billion NTD (realistic for Evergreen)
    gross_profit_k_ntd=100_000_000.0,
    op_profit_k_ntd=60_000_000.0,
    net_income_k_ntd=50_000_000.0,
)

_SAMPLE_BS = BalanceSheetFact(
    fiscal_year=2024,
    total_assets_k_ntd=1_000_000_000.0,
    total_equity_k_ntd=400_000_000.0,
    total_liabilities_k_ntd=600_000_000.0,
    current_assets_k_ntd=300_000_000.0,
    current_liabilities_k_ntd=200_000_000.0,
    cash_k_ntd=100_000_000.0,
    short_term_debt_k_ntd=150_000_000.0,
    long_term_debt_k_ntd=250_000_000.0,
)

_SAMPLE_CF = CashFlowFact(
    fiscal_year=2024,
    ocf_k_ntd=70_000_000.0,
    capex_k_ntd=20_000_000.0,
    da_k_ntd=10_000_000.0,
)


def _sample_with_financials():
    """_SAMPLE_DATA extended with P1 IS/BS/CF facts."""
    import dataclasses
    return dataclasses.replace(
        _SAMPLE_DATA,
        income_statements=[_SAMPLE_IS],
        balance_sheets=[_SAMPLE_BS],
        cash_flows=[_SAMPLE_CF],
    )


# ── _parse_roc_or_gregorian_year ──────────────────────────────────────────────

def test_parse_roc_or_gregorian_year_roc_113():
    from credit_report.api.twse_importer import _parse_roc_or_gregorian_year
    assert _parse_roc_or_gregorian_year("113") == 2024


def test_parse_roc_or_gregorian_year_roc_114():
    from credit_report.api.twse_importer import _parse_roc_or_gregorian_year
    assert _parse_roc_or_gregorian_year("114") == 2025


def test_parse_roc_or_gregorian_year_gregorian():
    from credit_report.api.twse_importer import _parse_roc_or_gregorian_year
    assert _parse_roc_or_gregorian_year("2024") == 2024


def test_parse_roc_or_gregorian_year_invalid():
    from credit_report.api.twse_importer import _parse_roc_or_gregorian_year
    assert _parse_roc_or_gregorian_year("abc") == 0


def test_parse_roc_or_gregorian_year_zero():
    from credit_report.api.twse_importer import _parse_roc_or_gregorian_year
    assert _parse_roc_or_gregorian_year("") == 0


# ── _parse_is_rows ────────────────────────────────────────────────────────────

def test_parse_is_rows_annual_only():
    from credit_report.api.twse_importer import _parse_is_rows
    rows = [
        {"公司代號": "2603", "年度": "113", "季別": "4",
         "營業收入": "500000000", "毛利（毛損）": "100000000",
         "營業利益（損失）": "60000000", "本期淨利（淨損）": "50000000"},
        {"公司代號": "2603", "年度": "113", "季別": "3", "營業收入": "350000000"},
    ]
    result = _parse_is_rows(rows, "2603")
    assert len(result) == 1
    assert result[0].fiscal_year == 2024
    assert result[0].revenue_k_ntd == 500_000_000.0
    assert result[0].gross_profit_k_ntd == 100_000_000.0
    assert result[0].op_profit_k_ntd == 60_000_000.0
    assert result[0].net_income_k_ntd == 50_000_000.0


def test_parse_is_rows_multiple_years_sorted_newest_first():
    from credit_report.api.twse_importer import _parse_is_rows
    rows = [
        {"公司代號": "2603", "年度": "112", "季別": "4", "營業收入": "400000000"},
        {"公司代號": "2603", "年度": "113", "季別": "4", "營業收入": "500000000"},
    ]
    result = _parse_is_rows(rows, "2603")
    assert result[0].fiscal_year == 2024
    assert result[1].fiscal_year == 2023


def test_parse_is_rows_wrong_company_skipped():
    from credit_report.api.twse_importer import _parse_is_rows
    rows = [{"公司代號": "2412", "年度": "113", "季別": "4", "營業收入": "1000"}]
    assert _parse_is_rows(rows, "2603") == []


# ── _parse_bs_rows ────────────────────────────────────────────────────────────

def test_parse_bs_rows_basic():
    from credit_report.api.twse_importer import _parse_bs_rows
    rows = [
        {"公司代號": "2603", "年度": "113", "季別": "4",
         "資產總計": "1000000000", "股東權益合計": "400000000",
         "流動資產": "300000000", "流動負債": "200000000",
         "現金及約當現金": "100000000",
         "短期借款": "150000000", "長期借款": "250000000"},
    ]
    result = _parse_bs_rows(rows, "2603")
    assert len(result) == 1
    bs = result[0]
    assert bs.fiscal_year == 2024
    assert bs.total_assets_k_ntd == 1_000_000_000.0
    assert bs.total_equity_k_ntd == 400_000_000.0
    assert bs.cash_k_ntd == 100_000_000.0
    assert bs.short_term_debt_k_ntd == 150_000_000.0
    assert bs.long_term_debt_k_ntd == 250_000_000.0


def test_parse_bs_rows_liabilities_derived_when_missing():
    from credit_report.api.twse_importer import _parse_bs_rows
    rows = [
        {"公司代號": "2603", "年度": "113", "季別": "4",
         "資產總計": "1000000000", "股東權益合計": "400000000"},
    ]
    result = _parse_bs_rows(rows, "2603")
    assert result[0].total_liabilities_k_ntd == pytest.approx(600_000_000.0)


# ── _parse_cf_rows ────────────────────────────────────────────────────────────

def test_parse_cf_rows_basic():
    from credit_report.api.twse_importer import _parse_cf_rows
    rows = [
        {"公司代號": "2603", "年度": "113", "季別": "4",
         "來自營運活動之現金流量": "70000000",
         "取得不動產廠房及設備": "-20000000",
         "折舊費用": "10000000"},
    ]
    result = _parse_cf_rows(rows, "2603")
    assert len(result) == 1
    cf = result[0]
    assert cf.fiscal_year == 2024
    assert cf.ocf_k_ntd == 70_000_000.0
    assert cf.capex_k_ntd == 20_000_000.0  # stored as positive abs value
    assert cf.da_k_ntd == 10_000_000.0


def test_parse_cf_rows_capex_positive_raw():
    """Capex that arrives as a positive number is stored as-is (abs is idempotent)."""
    from credit_report.api.twse_importer import _parse_cf_rows
    rows = [
        {"公司代號": "2603", "年度": "113", "季別": "4",
         "來自營運活動之現金流量": "70000000",
         "取得不動產廠房及設備": "20000000"},
    ]
    result = _parse_cf_rows(rows, "2603")
    assert result[0].capex_k_ntd == 20_000_000.0


# ── map_to_section7_income_statement ─────────────────────────────────────────

def test_section7_is_revenue():
    from credit_report.api.twse_importer import map_to_section7_income_statement
    m = map_to_section7_income_statement(_sample_with_financials())
    # 500,000,000 k NTD / 1000 = 500,000 NTD million
    assert m["7A_borrower_financials.income_statement.FY2024.revenue"] == pytest.approx(500_000.0)


def test_section7_is_gross_profit():
    from credit_report.api.twse_importer import map_to_section7_income_statement
    m = map_to_section7_income_statement(_sample_with_financials())
    assert m["7A_borrower_financials.income_statement.FY2024.gross_profit"] == pytest.approx(100_000.0)


def test_section7_is_op_profit():
    from credit_report.api.twse_importer import map_to_section7_income_statement
    m = map_to_section7_income_statement(_sample_with_financials())
    assert m["7A_borrower_financials.income_statement.FY2024.op_profit"] == pytest.approx(60_000.0)


def test_section7_is_net_income():
    from credit_report.api.twse_importer import map_to_section7_income_statement
    m = map_to_section7_income_statement(_sample_with_financials())
    assert m["7A_borrower_financials.income_statement.FY2024.net_income"] == pytest.approx(50_000.0)


def test_section7_is_empty_returns_empty():
    from credit_report.api.twse_importer import map_to_section7_income_statement
    assert map_to_section7_income_statement(TWSECompanyData(stock_code="2603")) == {}


# ── map_to_section7_balance_sheet ─────────────────────────────────────────────

def test_section7_bs_total_assets():
    from credit_report.api.twse_importer import map_to_section7_balance_sheet
    m = map_to_section7_balance_sheet(_sample_with_financials())
    assert m["7A_borrower_financials.balance_sheet.FY2024.total_assets"] == pytest.approx(1_000_000.0)


def test_section7_bs_total_equity():
    from credit_report.api.twse_importer import map_to_section7_balance_sheet
    m = map_to_section7_balance_sheet(_sample_with_financials())
    assert m["7A_borrower_financials.balance_sheet.FY2024.total_equity"] == pytest.approx(400_000.0)


def test_section7_bs_total_debt():
    from credit_report.api.twse_importer import map_to_section7_balance_sheet
    m = map_to_section7_balance_sheet(_sample_with_financials())
    # (150,000,000 + 250,000,000) k NTD / 1000 = 400,000 NTD million
    assert m["7A_borrower_financials.balance_sheet.FY2024.total_debt"] == pytest.approx(400_000.0)


def test_section7_bs_cash():
    from credit_report.api.twse_importer import map_to_section7_balance_sheet
    m = map_to_section7_balance_sheet(_sample_with_financials())
    assert m["7A_borrower_financials.balance_sheet.FY2024.cash_and_equivalents"] == pytest.approx(100_000.0)


def test_section7_bs_net_debt():
    from credit_report.api.twse_importer import map_to_section7_balance_sheet
    m = map_to_section7_balance_sheet(_sample_with_financials())
    # net_debt = 400,000 - 100,000 = 300,000 NTD million
    assert m["7A_borrower_financials.balance_sheet.FY2024.net_debt"] == pytest.approx(300_000.0)


def test_section7_bs_empty_returns_empty():
    from credit_report.api.twse_importer import map_to_section7_balance_sheet
    assert map_to_section7_balance_sheet(TWSECompanyData(stock_code="2603")) == {}


# ── map_to_section7_cash_flow ─────────────────────────────────────────────────

def test_section7_cf_ocf():
    from credit_report.api.twse_importer import map_to_section7_cash_flow
    m = map_to_section7_cash_flow(_sample_with_financials())
    assert m["7A_borrower_financials.cash_flow.FY2024.ocf"] == pytest.approx(70_000.0)


def test_section7_cf_capex():
    from credit_report.api.twse_importer import map_to_section7_cash_flow
    m = map_to_section7_cash_flow(_sample_with_financials())
    assert m["7A_borrower_financials.cash_flow.FY2024.capex"] == pytest.approx(20_000.0)


def test_section7_cf_fcf():
    from credit_report.api.twse_importer import map_to_section7_cash_flow
    m = map_to_section7_cash_flow(_sample_with_financials())
    # fcf = ocf - capex = 70,000 - 20,000 = 50,000 NTD million
    assert m["7A_borrower_financials.cash_flow.FY2024.fcf"] == pytest.approx(50_000.0)


def test_section7_cf_empty_returns_empty():
    from credit_report.api.twse_importer import map_to_section7_cash_flow
    assert map_to_section7_cash_flow(TWSECompanyData(stock_code="2603")) == {}


# ── map_to_section7_ratios ────────────────────────────────────────────────────

def test_section7_ratios_gross_margin():
    from credit_report.api.twse_importer import map_to_section7_ratios
    m = map_to_section7_ratios(_sample_with_financials())
    # 100,000 / 500,000 * 100 = 20%
    assert m["7B_key_ratios.FY2024.gross_margin_pct"] == pytest.approx(20.0)


def test_section7_ratios_ni_margin():
    from credit_report.api.twse_importer import map_to_section7_ratios
    m = map_to_section7_ratios(_sample_with_financials())
    # 50,000 / 500,000 * 100 = 10%
    assert m["7B_key_ratios.FY2024.ni_margin_pct"] == pytest.approx(10.0)


def test_section7_ratios_ebitda_margin():
    from credit_report.api.twse_importer import map_to_section7_ratios
    m = map_to_section7_ratios(_sample_with_financials())
    # ebitda = op_profit + da = 60,000,000 + 10,000,000 = 70,000,000 k NTD
    # margin = 70,000,000 / 500,000,000 * 100 = 14%
    assert m["7B_key_ratios.FY2024.ebitda_margin_pct"] == pytest.approx(14.0)


def test_section7_ratios_roe():
    from credit_report.api.twse_importer import map_to_section7_ratios
    m = map_to_section7_ratios(_sample_with_financials())
    # 50,000,000 / 400,000,000 * 100 = 12.5%
    assert m["7B_key_ratios.FY2024.roe_pct"] == pytest.approx(12.5)


def test_section7_ratios_debt_equity():
    from credit_report.api.twse_importer import map_to_section7_ratios
    m = map_to_section7_ratios(_sample_with_financials())
    # (150 + 250) M / 400 M = 1.0
    assert m["7B_key_ratios.FY2024.debt_equity"] == pytest.approx(1.0)


def test_section7_ratios_current_ratio():
    from credit_report.api.twse_importer import map_to_section7_ratios
    m = map_to_section7_ratios(_sample_with_financials())
    # 300,000,000 / 200,000,000 = 1.5
    assert m["7B_key_ratios.FY2024.current_ratio"] == pytest.approx(1.5)


def test_section7_ratios_empty_without_p1():
    from credit_report.api.twse_importer import map_to_section7_ratios
    assert map_to_section7_ratios(TWSECompanyData(stock_code="2603")) == {}


def test_section7_ratios_requires_both_is_and_bs():
    from credit_report.api.twse_importer import map_to_section7_ratios
    import dataclasses
    is_only = dataclasses.replace(_SAMPLE_DATA, income_statements=[_SAMPLE_IS])
    bs_only = dataclasses.replace(_SAMPLE_DATA, balance_sheets=[_SAMPLE_BS])
    assert map_to_section7_ratios(is_only) == {}
    assert map_to_section7_ratios(bs_only) == {}


# ── map_to_section5_from_financials ──────────────────────────────────────────

def test_section5_financials_net_worth():
    from credit_report.api.twse_importer import map_to_section5_from_financials
    m = map_to_section5_from_financials(_sample_with_financials())
    # 400,000,000 k NTD / 1,000,000 = 400 NTD billion
    assert m["5F_corporate_guarantee.net_worth_twd_bn"] == pytest.approx(400.0, rel=1e-3)


def test_section5_financials_total_debt():
    from credit_report.api.twse_importer import map_to_section5_from_financials
    m = map_to_section5_from_financials(_sample_with_financials())
    # (150 + 250) million k NTD = 400 billion
    assert m["5F_corporate_guarantee.total_debt_twd_bn"] == pytest.approx(400.0, rel=1e-3)


def test_section5_financials_cash():
    from credit_report.api.twse_importer import map_to_section5_from_financials
    m = map_to_section5_from_financials(_sample_with_financials())
    assert m["5F_corporate_guarantee.cash_twd_bn"] == pytest.approx(100.0, rel=1e-3)


def test_section5_financials_ebitda():
    from credit_report.api.twse_importer import map_to_section5_from_financials
    m = map_to_section5_from_financials(_sample_with_financials())
    # ebitda = (60,000,000 + 10,000,000) k NTD = 70 billion
    assert m["5F_corporate_guarantee.ebitda_twd_bn"] == pytest.approx(70.0, rel=1e-3)


def test_section5_financials_net_margin():
    from credit_report.api.twse_importer import map_to_section5_from_financials
    m = map_to_section5_from_financials(_sample_with_financials())
    # 50,000,000 / 500,000,000 * 100 = 10%
    assert m["5F_corporate_guarantee.net_margin_pct"] == pytest.approx(10.0)


def test_section5_financials_roe():
    from credit_report.api.twse_importer import map_to_section5_from_financials
    m = map_to_section5_from_financials(_sample_with_financials())
    # 50,000,000 / 400,000,000 * 100 = 12.5%
    assert m["5F_corporate_guarantee.roe_pct"] == pytest.approx(12.5)


def test_section5_financials_empty_without_bs():
    from credit_report.api.twse_importer import map_to_section5_from_financials
    assert map_to_section5_from_financials(TWSECompanyData(stock_code="2603")) == {}


# ── Dispatch integration: §5 and §7 with P1 data ─────────────────────────────

def test_map_to_section7_dispatch_includes_is_bs_cf_ratios():
    from credit_report.api.twse_importer import map_to_section
    m = map_to_section(7, _sample_with_financials())
    # Metadata still present
    assert "7A_borrower_financials.reporting_currency" in m
    # IS fields
    assert "7A_borrower_financials.income_statement.FY2024.gross_profit" in m
    # BS fields
    assert "7A_borrower_financials.balance_sheet.FY2024.total_assets" in m
    # CF fields
    assert "7A_borrower_financials.cash_flow.FY2024.ocf" in m
    # Ratio fields
    assert "7B_key_ratios.FY2024.gross_margin_pct" in m


def test_map_to_section5_dispatch_includes_financials():
    from credit_report.api.twse_importer import map_to_section
    m = map_to_section(5, _sample_with_financials())
    # Original §5 fields still present
    assert "5F_corporate_guarantee.guarantor_full_name" in m
    assert "5G_responsible_person.name" in m
    # P1-derived fields
    assert "5F_corporate_guarantee.net_worth_twd_bn" in m
    assert "5F_corporate_guarantee.ebitda_twd_bn" in m


def test_map_to_section7_no_p1_data_still_has_metadata():
    """§7 dispatch works even when P1 endpoints returned empty (403 in dev)."""
    from credit_report.api.twse_importer import map_to_section
    m = map_to_section(7, _SAMPLE_DATA)
    assert "7A_borrower_financials.reporting_currency" in m
    # No IS/BS/CF keys when P1 data is absent
    assert not any("gross_profit" in k for k in m)
    assert not any("total_assets" in k for k in m)


# ── DividendInfo dataclass ────────────────────────────────────────────────────

def test_dividend_info_defaults():
    d = DividendInfo(fiscal_year=2024, cash_dividend_per_share=3.5)
    assert d.fiscal_year == 2024
    assert d.cash_dividend_per_share == 3.5
    assert d.stock_dividend_per_share == 0.0

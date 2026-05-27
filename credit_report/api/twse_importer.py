"""
TWSE OpenAPI importer — fetches Taiwan Stock Exchange structured data
and maps it to SectionInput field paths across sections §1, §3, §4, §5, §7.

APIs used (no auth, public JSON):
  t187ap03_L  — all listed companies: comprehensive basic info
                (name, ISIN, listing date, industry, chairman, CEO,
                 paid-in capital, auditor, incorporation date, tax ID,
                 spokesperson, address, phone, primary business …)
  t187ap03_P  — OTC/public companies: same schema as t187ap03_L (fallback)
  t187ap02_L  — major shareholders holding >10% of listed companies
  t187ap11_L  — board / supervisor shareholding detail (title, name, shares, pledge)
  t187ap04_L  — daily material news (Phase 2: material event risk for §4G)
  t21sc03_1   — monthly revenue for listed companies (Phase 2: §7 income_statement)
  t21sc03_2   — monthly revenue for OTC companies (Phase 2: §7 fallback)

All arrays are filtered client-side by 公司代號 == stock_code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 20.0

# ── TWSE endpoint constants ───────────────────────────────────────────────────

_TWSE_COMPANY_L_URL    = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
_TWSE_COMPANY_P_URL    = "https://openapi.twse.com.tw/v1/opendata/t187ap03_P"
_TWSE_SHAREHOLDERS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap02_L"
_TWSE_BOARD_URL        = "https://openapi.twse.com.tw/v1/opendata/t187ap11_L"

# ── Phase 2 additions ─────────────────────────────────────────────────────────

# t187ap04_L: 上市公司每日重大訊息 (daily material news)
_TWSE_MATERIAL_NEWS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"

# Monthly revenue — listed companies (every month, all companies, filter client-side)
# Run scripts/twse_swagger_parser.py to verify this path:
_TWSE_MONTHLY_REVENUE_L_URL = "https://openapi.twse.com.tw/v1/opendata/t21sc03_1"
_TWSE_MONTHLY_REVENUE_P_URL = "https://openapi.twse.com.tw/v1/opendata/t21sc03_2"

# Sections for which TWSE provides meaningful auto-fill data
SUPPORTED_SECTIONS: frozenset[int] = frozenset({1, 3, 4, 5, 7, 9})

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CUB-CreditReport/1.0; "
        "+https://github.com/iamgrape999/financial-report-analyzer-ai-report)"
    ),
    "Accept": "application/json, */*",
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class MaterialNewsItem:
    """One row from t187ap04_L — material news for a listed company."""
    date: str = ""          # 出表日期
    subject: str = ""       # 主旨
    clause: str = ""        # 符合條款
    fact_date: str = ""     # 事實發生日
    description: str = ""   # 說明 (may be long)
    risk_category: str = "" # classified by _classify_news_risk()


@dataclass
class MonthlyRevenue:
    """One month row from t21sc03_1 — monthly revenue for a listed company."""
    year_month: str = ""         # 資料年月 e.g. "11412" (ROC 114 year, month 12)
    revenue_k_ntd: float = 0.0   # 當月營收 (unit: thousands NT$)
    mom_pct: float = 0.0         # 上月比較增減%
    yoy_pct: float = 0.0         # 去年同月增減%
    ytd_revenue_k_ntd: float = 0.0  # 當月累計營收 (thousands NT$)
    ytd_yoy_pct: float = 0.0     # 前期比較增減%


@dataclass
class BoardMember:
    """One director / supervisor row from t187ap11_L."""
    title: str = ""           # 職稱 (董事長, 董事, 獨立董事, 監察人 …)
    name: str = ""            # 姓名
    shares_current: str = ""  # 目前持股
    pledged_shares: str = ""  # 設質股數
    pledge_pct: str = ""      # 設質股數佔持股比例 (%)


@dataclass
class TWSECompanyData:
    """Normalised TWSE data for one company (listed or OTC)."""
    stock_code: str

    # ── From t187ap03_L / t187ap03_P ─────────────────────────────────────────
    company_name_zh: str = ""
    company_name_abbrev_zh: str = ""   # 公司簡稱
    company_name_en: str = ""          # 英文全名
    company_name_abbrev_en: str = ""   # 英文簡稱
    isin_code: str = ""
    listing_date: str = ""             # YYYY-MM-DD
    market_type: str = ""              # 上市 / 上櫃
    industry_zh: str = ""
    tax_id: str = ""                   # 營利事業統一編號
    incorporation_country_raw: str = ""  # 外國企業註冊地國 (empty = Taiwan)
    incorporation_date: str = ""       # YYYY-MM-DD
    paid_in_capital_ntd: int = 0       # raw NT$ amount
    shares_outstanding: str = ""       # 已發行普通股數
    par_value_ntd: str = ""            # 普通股每股面額
    chairman: str = ""                 # 董事長
    ceo: str = ""                      # 總經理
    spokesperson: str = ""             # 發言人
    spokesperson_title: str = ""       # 發言人職稱
    primary_business: str = ""         # 主要業務
    auditor_firm: str = ""             # 簽證會計師事務所
    auditor1: str = ""                 # 簽證會計師1
    auditor2: str = ""                 # 簽證會計師2
    phone: str = ""
    fax: str = ""                      # 傳真電話
    address: str = ""
    email: str = ""                    # 電子郵件信箱
    website: str = ""                  # 公司網址
    financial_report_type: str = ""    # 合併 / 個別

    # ── From t187ap02_L ──────────────────────────────────────────────────────
    major_shareholders: list[str] = field(default_factory=list)  # 大股東名稱 (>10%)

    # ── From t187ap11_L ──────────────────────────────────────────────────────
    board_members: list[BoardMember] = field(default_factory=list)

    # ── From t187ap04_L ──────────────────────────────────────────────────────
    material_news: list[MaterialNewsItem] = field(default_factory=list)   # newest first, up to 10

    # ── From t21sc03_1 / t21sc03_2 ───────────────────────────────────────────
    monthly_revenues: list[MonthlyRevenue] = field(default_factory=list)  # newest first, up to 24

    # ── Raw ──────────────────────────────────────────────────────────────────
    raw: dict = field(default_factory=dict, repr=False)


# ── Material news risk classifier ─────────────────────────────────────────────

_NEWS_RISK_KEYWORDS: dict[str, str] = {
    "財務": "financial",
    "訴訟": "litigation",
    "重大投資": "major_investment",
    "處分": "asset_disposal",
    "背書保證": "guarantee",
    "資金貸與": "intercompany_lending",
    "解散": "dissolution",
    "停業": "suspension",
    "破產": "bankruptcy",
    "重整": "reorganization",
    "掏空": "embezzlement",
    "內線": "insider_trading",
    "違約": "default",
}


def _classify_news_risk(subject: str, clause: str) -> str:
    combined = (subject or "") + " " + (clause or "")
    for kw, category in _NEWS_RISK_KEYWORDS.items():
        if kw in combined:
            return category
    return "general"


# ── ROC date helper ────────────────────────────────────────────────────────────

def _roc_year_month_to_gregorian(roc_ym: str) -> tuple[int, int]:
    """Convert ROC year-month string (e.g. '11412') to (2025, 12).
    ROC 114 = AD 2025; ROC year + 1911 = Gregorian year."""
    s = str(roc_ym).strip()
    try:
        if len(s) == 5:   # YYYMMM → actually YYMM? No: 3-digit ROC + 2-digit month
            roc_y = int(s[:3])
            month = int(s[3:])
        elif len(s) == 6:  # YYYYMM
            roc_y = int(s[:4])
            month = int(s[4:])
        else:
            return (0, 0)
        return (roc_y + 1911, month)
    except (ValueError, IndexError):
        return (0, 0)


# ── Date helper ───────────────────────────────────────────────────────────────

def _parse_twse_date(s: str) -> str:
    """Convert TWSE 8-digit YYYYMMDD → ISO YYYY-MM-DD; return original on failure."""
    s = s.strip()
    if not s:
        return ""
    if len(s) == 8 and s.isdigit():
        try:
            d = datetime.strptime(s, "%Y%m%d").date()
            if d.year < 1850 or d > date.today():
                return s
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


# ── API helpers ───────────────────────────────────────────────────────────────

async def _fetch_json(client: httpx.AsyncClient, url: str) -> list[dict]:
    """GET url → parsed JSON list, or [] on any error."""
    try:
        r = await client.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("twse: request failed url=%s: %s", url, exc)
        return []


def _find_one(rows: list[dict], stock_code: str) -> dict:
    """Return first row where 公司代號 matches (strip both sides)."""
    code = stock_code.strip()
    for row in rows:
        if row.get("公司代號", "").strip() == code:
            return row
    return {}


def _find_all(rows: list[dict], stock_code: str) -> list[dict]:
    """Return all rows where 公司代號 matches."""
    code = stock_code.strip()
    return [r for r in rows if r.get("公司代號", "").strip() == code]


# ── Main fetch entry-point ────────────────────────────────────────────────────

async def fetch_twse_company(stock_code: str) -> TWSECompanyData | None:
    """
    Fetch and normalise TWSE data for `stock_code`.

    Concurrently calls t187ap03_L (with t187ap03_P OTC fallback), t187ap02_L
    (major shareholders), and t187ap11_L (board shareholding).
    Returns None when the stock code is not found anywhere.
    """
    import asyncio

    async with httpx.AsyncClient(follow_redirects=True) as client:
        company_l_task    = asyncio.create_task(_fetch_json(client, _TWSE_COMPANY_L_URL))
        company_p_task    = asyncio.create_task(_fetch_json(client, _TWSE_COMPANY_P_URL))
        shareholders_task = asyncio.create_task(_fetch_json(client, _TWSE_SHAREHOLDERS_URL))
        board_task        = asyncio.create_task(_fetch_json(client, _TWSE_BOARD_URL))
        news_task         = asyncio.create_task(_fetch_json(client, _TWSE_MATERIAL_NEWS_URL))
        rev_l_task        = asyncio.create_task(_fetch_json(client, _TWSE_MONTHLY_REVENUE_L_URL))
        rev_p_task        = asyncio.create_task(_fetch_json(client, _TWSE_MONTHLY_REVENUE_P_URL))

        (
            company_l_rows, company_p_rows, sh_rows, board_rows,
            news_rows, rev_l_rows, rev_p_rows,
        ) = await asyncio.gather(
            company_l_task, company_p_task, shareholders_task, board_task,
            news_task, rev_l_task, rev_p_task,
        )

    # Prefer listed (L) over OTC (P)
    company_row = _find_one(company_l_rows, stock_code) or _find_one(company_p_rows, stock_code)
    if not company_row:
        logger.warning("twse: no data found for stock_code=%r", stock_code)
        return None

    market_type = company_row.get("市場別", "").strip()

    # Paid-in capital: numeric string possibly with commas
    raw_capital = company_row.get("實收資本額(元)", "") or company_row.get("實收資本額", "")
    try:
        paid_in_capital_ntd = int(str(raw_capital).replace(",", "").strip())
    except (ValueError, AttributeError):
        paid_in_capital_ntd = 0

    major_shareholders = [
        r.get("大股東名稱", "").strip()
        for r in _find_all(sh_rows, stock_code)
        if r.get("大股東名稱", "").strip()
    ]

    board_data = _find_all(board_rows, stock_code)
    board_members = [
        BoardMember(
            title=r.get("職稱", "").strip(),
            name=r.get("姓名", "").strip(),
            shares_current=r.get("目前持股", "").strip(),
            pledged_shares=r.get("設質股數", "").strip(),
            pledge_pct=r.get("設質股數佔持股比例", "").strip(),
        )
        for r in board_data
        if r.get("姓名", "").strip()
    ]

    # ── Phase 2: material news ────────────────────────────────────────────────
    raw_news = _find_all(news_rows, stock_code)
    material_news = []
    for row in raw_news[:10]:  # cap at 10 most recent
        material_news.append(MaterialNewsItem(
            date        = row.get("出表日期", "").strip(),
            subject     = row.get("主旨", "").strip(),
            clause      = row.get("符合條款", "").strip(),
            fact_date   = row.get("事實發生日", "").strip(),
            description = row.get("說明", "").strip(),
            risk_category = _classify_news_risk(
                row.get("主旨", ""), row.get("符合條款", "")
            ),
        ))

    # ── Phase 2: monthly revenue ──────────────────────────────────────────────
    def _safe_float(val: str) -> float:
        try:
            return float(str(val).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0.0

    rev_rows = _find_all(rev_l_rows, stock_code)
    if not rev_rows:
        rev_rows = _find_all(rev_p_rows, stock_code)  # OTC fallback
    monthly_revenues = []
    for row in rev_rows[:24]:  # up to 24 months
        monthly_revenues.append(MonthlyRevenue(
            year_month        = row.get("資料年月", "").strip(),
            revenue_k_ntd     = _safe_float(row.get("當月營收", 0)),
            mom_pct           = _safe_float(row.get("上月比較增減%", 0)),
            yoy_pct           = _safe_float(row.get("去年同月增減%", 0)),
            ytd_revenue_k_ntd = _safe_float(row.get("當月累計營收", 0)),
            ytd_yoy_pct       = _safe_float(row.get("前期比較增減%", 0)),
        ))

    return TWSECompanyData(
        stock_code=stock_code,
        company_name_zh        = company_row.get("公司名稱", "").strip(),
        company_name_abbrev_zh = company_row.get("公司簡稱", "").strip(),
        company_name_en        = (company_row.get("英文全名") or "").strip(),
        company_name_abbrev_en = (company_row.get("英文簡稱") or "").strip(),
        isin_code              = company_row.get("國際證券辨識號碼(ISIN Code)", "").strip(),
        listing_date           = _parse_twse_date(company_row.get("上市日期", "")),
        market_type            = market_type,
        industry_zh            = company_row.get("產業別", "").strip(),
        tax_id                 = company_row.get("營利事業統一編號", "").strip(),
        incorporation_country_raw = company_row.get("外國企業註冊地國", "").strip(),
        incorporation_date     = _parse_twse_date(company_row.get("成立日期", "")),
        paid_in_capital_ntd    = paid_in_capital_ntd,
        shares_outstanding     = company_row.get("已發行普通股數或TDR原股發行股數", "").strip(),
        par_value_ntd          = company_row.get("普通股每股面額", "").strip(),
        chairman               = company_row.get("董事長", "").strip(),
        ceo                    = company_row.get("總經理", "").strip(),
        spokesperson           = company_row.get("發言人", "").strip(),
        spokesperson_title     = company_row.get("發言人職稱", "").strip(),
        primary_business       = company_row.get("主要業務", "").strip(),
        auditor_firm           = company_row.get("簽證會計師事務所", "").strip(),
        auditor1               = company_row.get("簽證會計師1", "").strip(),
        auditor2               = company_row.get("簽證會計師2", "").strip(),
        phone                  = company_row.get("電話", "").strip(),
        fax                    = company_row.get("傳真電話", "").strip(),
        address                = company_row.get("住址", "").strip(),
        email                  = company_row.get("電子郵件信箱", "").strip(),
        website                = company_row.get("公司網址", "").strip(),
        financial_report_type  = company_row.get("財務報告書類型", "").strip(),
        major_shareholders     = major_shareholders,
        board_members          = board_members,
        material_news          = material_news,
        monthly_revenues       = monthly_revenues,
        raw                    = {"company": company_row, "shareholders": _find_all(sh_rows, stock_code), "board": board_data},
    )


# ── Capital helper ────────────────────────────────────────────────────────────

def _paid_in_capital_ntd_bn(ntd: int) -> float | None:
    """NT$ integer → NTD billion rounded to 3 decimal places."""
    if ntd <= 0:
        return None
    return round(ntd / 1_000_000_000, 3)


def _latest_ytd_revenue(data: TWSECompanyData) -> tuple[int, float]:
    """Return (gregorian_year, ytd_revenue_ntd_millions) from the most recent monthly row.
    monthly_revenues[0] is the newest; ytd_revenue_k_ntd / 1000 = NTD millions."""
    if not data.monthly_revenues:
        return (0, 0.0)
    latest = data.monthly_revenues[0]
    yr, _ = _roc_year_month_to_gregorian(latest.year_month)
    if yr <= 0:
        return (0, 0.0)
    return (yr, round(latest.ytd_revenue_k_ntd / 1000.0, 1))


def _incorporation_country(data: TWSECompanyData) -> str:
    """Use raw foreign-registration country if non-empty, else default to Taiwan."""
    return data.incorporation_country_raw if data.incorporation_country_raw else (
        "Taiwan" if data.stock_code else ""
    )


def _legal_entity_type(data: TWSECompanyData) -> str:
    if data.market_type == "上市":
        return "Listed Company"
    if data.market_type == "上櫃":
        return "OTC Listed Company"
    return "Listed Company" if data.stock_code else ""


def _chairman_name(data: TWSECompanyData) -> str:
    """Prefer board-member record for chairman (has pledge detail); fall back to basic field."""
    for m in data.board_members:
        if "董事長" in m.title:
            return m.name
    return data.chairman


# ── Section field mappers ─────────────────────────────────────────────────────

def map_to_section1(data: TWSECompanyData) -> dict[str, Any]:
    """
    §1 — Facility Summary / T&C fields auto-fillable from TWSE.

    - 1D: terms_and_conditions.borrower (company name)
    - 1B: regulatory_compliance.china_invested_enterprise (PRC detection)
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "":
            mapping[path] = value

    _put("terms_and_conditions.borrower", data.company_name_zh or data.company_name_en)

    country_lower = data.incorporation_country_raw.lower()
    if country_lower:
        is_china = any(kw in country_lower for kw in ("china", "prc", "中國", "大陸", "中华人民"))
        _put("regulatory_compliance.china_invested_enterprise", is_china)

    return mapping


def map_to_section3(data: TWSECompanyData) -> dict[str, Any]:
    """
    §3 — MSR rating table: borrower entity name fields.

    Fills the header rows so analysts don't need to retype the company
    name before entering internal rating scores.
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "":
            mapping[path] = value

    full_name = data.company_name_en or data.company_name_zh
    abbrev    = data.company_name_abbrev_en or data.company_name_abbrev_zh

    _put("3B_internal_ratings.borrower_entity_full_name", full_name)
    _put("3B_internal_ratings.borrower_entity_abbrev",   abbrev or full_name)

    return mapping


def map_to_section4(data: TWSECompanyData) -> dict[str, Any]:
    """
    §4 — Corporate Background.

    §4A borrower identification (name, tax ID, country, date, legal type,
    fiscal year, auditor), §4B ownership (major shareholders from t187ap02_L),
    §4C management (CEO), §4D business profile, §4E financials header.
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "" and value != 0:
            mapping[path] = value

    # §4A
    _put("4A_borrower.company_name_zh",       data.company_name_zh)
    _put("4A_borrower.company_name_en",       data.company_name_en or data.company_name_abbrev_en)
    _put("4A_borrower.registration_number",   data.tax_id)
    _put("4A_borrower.isin_code",             data.isin_code)
    _put("4A_borrower.listing_date",          data.listing_date)
    _put("4A_borrower.shares_outstanding",    data.shares_outstanding)
    _put("4A_borrower.incorporation_country", _incorporation_country(data))
    _put("4A_borrower.incorporation_date",    data.incorporation_date)
    _put("4A_borrower.legal_entity_type",     _legal_entity_type(data))
    _put("4A_borrower.fiscal_year_end",       "December 31")
    _put("4A_borrower.group_auditor",         data.auditor_firm)

    # §4B — major shareholders (>10%) from t187ap02_L
    if data.major_shareholders:
        mapping["4B_ownership.shareholders"] = [
            f"{name}|>10%|Taiwan" for name in data.major_shareholders
        ]
        _put("4B_ownership.ultimate_beneficial_owner", data.major_shareholders[0])

    # §4C — management; spokesperson (發言人) is typically the CFO / IR contact
    _put("4C_management.ceo_name",  data.ceo)
    _put("4C_management.ceo_title", "President" if data.ceo else "")
    _put("4C_management.cfo_name",  data.spokesperson)
    _put("4C_management.cfo_title", data.spokesperson_title)

    # §4D — business profile, industry category, and financial-report type
    _put("4D_business.primary_business",  data.primary_business)
    _put("4D_business.industry_category", data.industry_zh)
    _put("4D_business.reporting_type",    data.financial_report_type)

    # §4E — financials header + YTD revenue snapshot from monthly revenue data
    _put("4E_financials.currency", "NTD")
    cap_bn = _paid_in_capital_ntd_bn(data.paid_in_capital_ntd)
    if cap_bn is not None:
        mapping["4E_financials.paid_in_capital_ntd_bn"] = cap_bn

    yr, ytd_m = _latest_ytd_revenue(data)
    if yr > 0 and ytd_m > 0:
        _put("4E_financials.fiscal_year", str(yr))
        _put("4E_financials.revenue",     ytd_m)
        _put("4E_financials.unit",        "NTD million")

    return mapping


def map_to_section5(data: TWSECompanyData) -> dict[str, Any]:
    """
    §5 — Security / Guarantees / Responsible Person.

    §5F corporate guarantee identity (borrower-as-guarantor scenario),
    §5G responsible person: chairman name and title from t187ap11_L / t187ap03_L.
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "":
            mapping[path] = value

    # §5F — when the listed parent company is the guarantor
    _put("5F_corporate_guarantee.guarantor_full_name",
         data.company_name_zh or data.company_name_en)
    if data.market_type == "上市":
        _put("5F_corporate_guarantee.guarantor_listed_exchange", "Taiwan Stock Exchange")
    elif data.market_type == "上櫃":
        _put("5F_corporate_guarantee.guarantor_listed_exchange", "Taipei Exchange")

    # §5F — annual revenue for guarantor creditworthiness (NTD billions)
    _, ytd_m = _latest_ytd_revenue(data)
    if ytd_m > 0:
        _put("5F_corporate_guarantee.revenue_twd_bn", round(ytd_m / 1000.0, 3))

    # §5G — responsible person is typically the chairman
    chairman = _chairman_name(data)
    _put("5G_responsible_person.name",  chairman)
    _put("5G_responsible_person.title", "Chairman / 董事長" if chairman else "")

    return mapping


def map_to_section7_metadata(data: TWSECompanyData) -> dict[str, Any]:
    """
    §7 — Financial analysis metadata.

    7A financial table header (reporting entity, auditor, currency, unit,
    accounting standard) plus entities_to_analyze which drives LLM generation.
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "":
            mapping[path] = value

    reporting_entity = data.company_name_zh or data.company_name_en

    _put("7A_borrower_financials.reporting_entity",    reporting_entity)
    _put("7A_borrower_financials.auditor",             data.auditor_firm)
    _put("7A_borrower_financials.fiscal_year_end",     "December 31")
    _put("7A_borrower_financials.reporting_currency",  "NTD")
    _put("7A_borrower_financials.unit",                "NTD million")
    _put("7A_borrower_financials.accounting_standard", "IFRS (TIFRS)")

    # entities_to_analyze: drives which entity names the LLM uses
    _put("entities_to_analyze.borrower_name",     data.company_name_en or data.company_name_zh)
    _put("entities_to_analyze.borrower_currency", "NTD")
    _put("entities_to_analyze.borrower_unit",     "NTD million")

    return mapping


def map_to_section7_financials(data: TWSECompanyData) -> dict[str, Any]:
    """
    §7 — Map monthly revenue trend to 7A income_statement per fiscal year.

    Groups monthly_revenues by Gregorian year, sums annual revenue,
    and writes FY{year} keys compatible with section_7.yaml iterate_path format.
    Units: NTD million (input is NTD thousands, divide by 1000).
    """
    if not data.monthly_revenues:
        return {}

    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "":
            mapping[path] = value

    # Group by Gregorian year
    from collections import defaultdict
    year_months: dict[int, list[MonthlyRevenue]] = defaultdict(list)
    for rev in data.monthly_revenues:
        yr, mo = _roc_year_month_to_gregorian(rev.year_month)
        if yr > 2000:
            year_months[yr].append(rev)

    for year, months in sorted(year_months.items()):
        fy_key = f"FY{year}"
        # Annual revenue = sum of all available months for that year (in NTD millions)
        annual_revenue_m = round(sum(r.revenue_k_ntd for r in months) / 1000.0, 1)
        if annual_revenue_m > 0:
            _put(f"7A_borrower_financials.income_statement.{fy_key}.revenue", annual_revenue_m)

        # Most recent month's YoY for the full year estimate
        if len(months) == 12:
            # Full year: use the December (last month) YTD values
            dec = max(months, key=lambda r: int(r.year_month[-2:]) if r.year_month else 0)
            if dec.ytd_yoy_pct != 0:
                _put(f"7A_borrower_financials.income_statement.{fy_key}.revenue_yoy_pct",
                     round(dec.ytd_yoy_pct, 1))
        elif months:
            # Partial year: use the latest month's YTD YoY
            latest = months[-1]
            if latest.ytd_yoy_pct != 0:
                _put(f"7A_borrower_financials.income_statement.{fy_key}.revenue_yoy_pct",
                     round(latest.ytd_yoy_pct, 1))

    return mapping


def map_to_section9(data: TWSECompanyData) -> dict[str, Any]:
    """
    §9 — Credit checklist: ACRA entity name for the item-16 register lookup.

    item16_entity_name is used by the analyst to search the ACRA / TWSE
    registry for outstanding charge or lien records.
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "":
            mapping[path] = value

    _put("9A_checklist.item16_entity_name",
         data.company_name_en or data.company_name_zh)

    return mapping


def map_to_section4_event_risk(data: TWSECompanyData) -> dict[str, Any]:
    """
    §4 — Populate 4G_risk_events from material news (t187ap04_L).

    Surfaces up to 5 high-risk news items as structured event flags.
    The analyst sees pre-populated risk event summaries in §4G.
    """
    if not data.material_news:
        return {}

    mapping: dict[str, Any] = {}

    high_risk = [n for n in data.material_news if n.risk_category != "general"]
    all_news   = high_risk or data.material_news[:5]

    if all_news:
        mapping["4G_risk_events.has_material_news"] = True
        mapping["4G_risk_events.news_count_90d"] = len(data.material_news)
        mapping["4G_risk_events.high_risk_categories"] = list(
            {n.risk_category for n in high_risk}
        )
        mapping["4G_risk_events.latest_news_summary"] = "; ".join(
            f"[{n.date}] {n.subject}"
            for n in all_news[:3]
        )

    return mapping


# ── Section dispatch ──────────────────────────────────────────────────────────

def map_to_section(section_no: int, data: TWSECompanyData) -> dict[str, Any]:
    """Return the field-mapping dict for the requested section number."""
    dispatch = {
        1: map_to_section1,
        3: map_to_section3,
        4: lambda d: {**map_to_section4(d), **map_to_section4_event_risk(d)},
        5: map_to_section5,
        7: lambda d: {**map_to_section7_metadata(d), **map_to_section7_financials(d)},
        9: map_to_section9,
    }
    fn = dispatch.get(section_no)
    return fn(data) if fn else {}


# ── Deep-set / deep-get helpers ───────────────────────────────────────────────

def deep_set(obj: dict, dot_path: str, value: Any) -> None:
    """Set value at dot-notation path, creating intermediate dicts as needed."""
    parts = dot_path.split(".")
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def deep_get(obj: dict, dot_path: str) -> Any:
    """Get value at dot-notation path; return None if any key is missing."""
    cur = obj
    for part in dot_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# ── Merge helper ──────────────────────────────────────────────────────────────

def apply_field_mapping(
    input_json: dict,
    field_map: dict[str, Any],
    apply_mode: str = "only_empty",
) -> tuple[dict, int, int]:
    """
    Merge `field_map` into `input_json` according to `apply_mode`.

    apply_mode:
      "only_empty"  — only write fields that are currently None / missing / ""
      "overwrite"   — always write (overwrite existing values)

    Returns (updated_input_json, fields_written, fields_skipped).
    """
    written = 0
    skipped = 0
    for path, value in field_map.items():
        existing = deep_get(input_json, path)
        if apply_mode == "only_empty":
            if existing is None or existing == "" or existing == []:
                deep_set(input_json, path, value)
                written += 1
            else:
                skipped += 1
        else:  # overwrite
            deep_set(input_json, path, value)
            written += 1
    return input_json, written, skipped

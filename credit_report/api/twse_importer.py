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

All four return full arrays; we filter client-side by 公司代號 == stock_code.

NOTE: t187ap04_L is 上市公司每日重大訊息 (daily material news) — NOT company
data.  Earlier drafts mistakenly used it; this version does not.
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

# Sections for which TWSE provides meaningful auto-fill data
SUPPORTED_SECTIONS: frozenset[int] = frozenset({1, 3, 4, 5, 7})

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CUB-CreditReport/1.0; "
        "+https://github.com/iamgrape999/financial-report-analyzer-ai-report)"
    ),
    "Accept": "application/json, */*",
}


# ── Data classes ──────────────────────────────────────────────────────────────

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

    # ── Raw ──────────────────────────────────────────────────────────────────
    raw: dict = field(default_factory=dict, repr=False)


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

        company_l_rows, company_p_rows, sh_rows, board_rows = await asyncio.gather(
            company_l_task, company_p_task, shareholders_task, board_task
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
        raw                    = {"company": company_row, "shareholders": _find_all(sh_rows, stock_code), "board": board_data},
    )


# ── Capital helper ────────────────────────────────────────────────────────────

def _paid_in_capital_ntd_bn(ntd: int) -> float | None:
    """NT$ integer → NTD billion rounded to 3 decimal places."""
    if ntd <= 0:
        return None
    return round(ntd / 1_000_000_000, 3)


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

    # §4C — management
    _put("4C_management.ceo_name",  data.ceo)
    _put("4C_management.ceo_title", "President" if data.ceo else "")

    # §4D — business profile
    _put("4D_business.primary_business", data.primary_business)

    # §4E — financials header
    _put("4E_financials.currency", "NTD")
    cap_bn = _paid_in_capital_ntd_bn(data.paid_in_capital_ntd)
    if cap_bn is not None:
        mapping["4E_financials.paid_in_capital_ntd_bn"] = cap_bn

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


# ── Section dispatch ──────────────────────────────────────────────────────────

def map_to_section(section_no: int, data: TWSECompanyData) -> dict[str, Any]:
    """Return the field-mapping dict for the requested section number."""
    dispatch = {
        1: map_to_section1,
        3: map_to_section3,
        4: map_to_section4,
        5: map_to_section5,
        7: map_to_section7_metadata,
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

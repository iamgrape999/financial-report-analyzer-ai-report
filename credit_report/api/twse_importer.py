"""
TWSE OpenAPI importer — fetches Taiwan Stock Exchange structured data
and maps it to Section 4 (corporate background) and Section 7 (financial
analysis metadata) SectionInput field paths.

APIs used (no auth, public JSON):
  t187ap03_L — all listed companies: basic info (listing date, industry, ISIN)
  t187ap04_L — all listed companies: detailed info (chairman, CEO, capital, auditor)

Both return full arrays; we filter client-side by 公司代號 == stock_code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 20.0
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB — full array responses can be large

_TWSE_BASIC_URL    = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
_TWSE_DETAILED_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CUB-CreditReport/1.0; "
        "+https://github.com/iamgrape999/financial-report-analyzer-ai-report)"
    ),
    "Accept": "application/json, */*",
}


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class TWSECompanyData:
    """Normalised TWSE data for one listed company."""
    stock_code: str
    # From t187ap03_L
    company_name_zh: str = ""
    isin_code: str = ""
    listing_date: str = ""          # YYYY-MM-DD
    market_type: str = ""           # 上市 / 上櫃
    industry_zh: str = ""
    # From t187ap04_L
    company_name_en: str = ""
    incorporation_date: str = ""    # YYYY-MM-DD
    paid_in_capital_ntd: int = 0    # raw NT$ amount
    chairman: str = ""
    ceo: str = ""
    primary_business: str = ""
    auditor_firm: str = ""
    auditor1: str = ""
    auditor2: str = ""
    phone: str = ""
    address: str = ""
    financial_report_type: str = "" # 合併 / 個別
    raw: dict = field(default_factory=dict, repr=False)


# ── Date helper ───────────────────────────────────────────────────────────────

def _parse_twse_date(s: str) -> str:
    """Convert TWSE date string (YYYYMMDD or MMDDYYYY variants) to YYYY-MM-DD.

    Returns the original string unchanged on parse failure.
    """
    s = s.strip()
    if not s:
        return ""
    # Most TWSE dates are 8-digit YYYYMMDD
    if len(s) == 8 and s.isdigit():
        try:
            d = datetime.strptime(s, "%Y%m%d").date()
            # Sanity check: TWSE listed companies incorporated after 1850
            if d.year < 1850 or d > date.today():
                return s
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


# ── API fetchers ──────────────────────────────────────────────────────────────

async def _fetch_twse_json(client: httpx.AsyncClient, url: str) -> list[dict]:
    """GET url → parsed JSON list, or [] on any error."""
    try:
        r = await client.get(url, headers=_HEADERS, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except (httpx.HTTPError, ValueError, Exception) as exc:
        logger.warning("twse: request failed url=%s: %s", url, exc)
        return []


def _find_by_code(rows: list[dict], stock_code: str) -> dict:
    """Return the first row where 公司代號 matches stock_code (case-insensitive strip)."""
    code = stock_code.strip()
    for row in rows:
        if row.get("公司代號", "").strip() == code:
            return row
    return {}


# ── Main fetch entry-point ────────────────────────────────────────────────────

async def fetch_twse_company(stock_code: str) -> TWSECompanyData | None:
    """
    Fetch and normalise TWSE data for `stock_code`.

    Returns None when the stock code is not found in either API.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        basic_rows, detailed_rows = await _parallel_fetch(client)

    basic    = _find_by_code(basic_rows,    stock_code)
    detailed = _find_by_code(detailed_rows, stock_code)

    if not basic and not detailed:
        logger.warning("twse: no data found for stock_code=%r", stock_code)
        return None

    # Paid-in capital is stored as a string like "38808000000"
    raw_capital = detailed.get("實收資本額(元)", "") or detailed.get("實收資本額", "")
    try:
        paid_in_capital_ntd = int(raw_capital.replace(",", "").strip())
    except (ValueError, AttributeError):
        paid_in_capital_ntd = 0

    return TWSECompanyData(
        stock_code=stock_code,
        # From basic
        company_name_zh    = basic.get("公司名稱", "") or detailed.get("公司名稱", ""),
        isin_code          = basic.get("國際證券辨識號碼(ISIN Code)", ""),
        listing_date       = _parse_twse_date(basic.get("上市日期", "")),
        market_type        = basic.get("市場別", ""),
        industry_zh        = basic.get("產業別", ""),
        # From detailed
        company_name_en    = (detailed.get("英文全名") or detailed.get("英文簡稱", "")).strip(),
        incorporation_date = _parse_twse_date(detailed.get("成立日期", "")),
        paid_in_capital_ntd = paid_in_capital_ntd,
        chairman           = detailed.get("董事長", "").strip(),
        ceo                = detailed.get("總經理", "").strip(),
        primary_business   = detailed.get("主要業務", "").strip(),
        auditor_firm       = detailed.get("簽證會計師事務所", "").strip(),
        auditor1           = detailed.get("簽證會計師1", "").strip(),
        auditor2           = detailed.get("簽證會計師2", "").strip(),
        phone              = detailed.get("電話", "").strip(),
        address            = detailed.get("住址", "").strip(),
        financial_report_type = detailed.get("財務報告書類型", "").strip(),
        raw                = {"basic": basic, "detailed": detailed},
    )


async def _parallel_fetch(client: httpx.AsyncClient) -> tuple[list[dict], list[dict]]:
    """Fetch both TWSE arrays concurrently."""
    import asyncio
    basic_task    = asyncio.create_task(_fetch_twse_json(client, _TWSE_BASIC_URL))
    detailed_task = asyncio.create_task(_fetch_twse_json(client, _TWSE_DETAILED_URL))
    return await basic_task, await detailed_task


# ── Field mapping ─────────────────────────────────────────────────────────────

def _paid_in_capital_ntd_bn(ntd: int) -> float | None:
    """Convert NT$ integer → NTD billion (3 decimal places)."""
    if ntd <= 0:
        return None
    return round(ntd / 1_000_000_000, 3)


def map_to_section4(data: TWSECompanyData) -> dict[str, Any]:
    """
    Return a dict of { field_path: value } for Section 4 input_json.

    Only includes fields where data is non-empty.  Callers apply apply_mode
    (only_empty vs overwrite) before merging into the existing SectionInput.
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        """Add to mapping only if value is truthy (non-empty / non-zero)."""
        if value is not None and value != "" and value != 0:
            mapping[path] = value

    _put("4A_borrower.company_name_zh",     data.company_name_zh)
    _put("4A_borrower.company_name_en",     data.company_name_en)
    _put("4A_borrower.incorporation_date",  data.incorporation_date)
    _put("4A_borrower.incorporation_country", "Taiwan" if data.stock_code else "")
    _put("4A_borrower.legal_entity_type",   "Listed Company" if data.market_type else "")
    _put("4A_borrower.fiscal_year_end",     "December 31")   # TWSE standard
    _put("4A_borrower.group_auditor",       data.auditor_firm)
    _put("4C_management.ceo_name",          data.ceo)
    _put("4C_management.ceo_title",         "President" if data.ceo else "")
    _put("4D_business.primary_business",    data.primary_business)
    _put("4E_financials.currency",          "NTD")

    # Paid-in capital (NTD billion) — useful context in §4E
    cap_bn = _paid_in_capital_ntd_bn(data.paid_in_capital_ntd)
    if cap_bn is not None:
        mapping["4E_financials.paid_in_capital_ntd_bn"] = cap_bn

    return mapping


def map_to_section7_metadata(data: TWSECompanyData) -> dict[str, Any]:
    """
    Return §7 metadata fields derivable from TWSE data (no financial numbers).

    These are the 'header' fields of the financial table — entity name, auditor,
    currency, unit — which analysts must fill before running LLM generation.
    """
    mapping: dict[str, Any] = {}

    def _put(path: str, value: Any) -> None:
        if value is not None and value != "":
            mapping[path] = value

    _put("7A_borrower_financials.reporting_entity", data.company_name_zh or data.company_name_en)
    _put("7A_borrower_financials.auditor",          data.auditor_firm)
    _put("7A_borrower_financials.fiscal_year_end",  "December 31")
    _put("7A_borrower_financials.reporting_currency", "NTD")
    _put("7A_borrower_financials.unit",             "NTD million")
    # TWSE-listed companies report under TIFRS (Taiwan IFRS adoption since 2013)
    _put("7A_borrower_financials.accounting_standard", "IFRS (TIFRS)")

    return mapping


# ── Deep-set helper ───────────────────────────────────────────────────────────

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

from __future__ import annotations

import csv
import io
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx

from credit_report.config import TWSE_BUNDLE_CACHE_TTL, TWSE_VERIFY_SSL

logger = logging.getLogger(__name__)

TWSE_BASE_URL = "https://openapi.twse.com.tw/v1"
MOPS_OPEN_DATA_CSV_BASE_URL = "https://mopsfin.twse.com.tw/opendata"

# In-process cache: {stock_code: (fetched_at_monotonic, bundle)}
# Keyed by stock code; TTL controlled by TWSE_BUNDLE_CACHE_TTL (default 3h).
# Module-level so it is shared across all TWSEOpenAPIClient instances in a process.
_bundle_cache: dict[str, tuple[float, dict[str, list[dict[str, Any]]]]] = {}

ENDPOINTS = {
    "company_profile": "/opendata/t187ap03_L",
    "monthly_revenue": "/opendata/t187ap05_L",
    "income_statement_general": "/opendata/t187ap06_L_ci",
    "balance_sheet_general": "/opendata/t187ap07_L_ci",
    "cash_flow_general": "/opendata/t187ap08_L_ci",
    "valuation_ratios": "/exchangeReport/BWIBBU_ALL",
    "daily_trading": "/exchangeReport/STOCK_DAY_ALL",
    "monthly_average": "/exchangeReport/STOCK_DAY_AVG_ALL",
    "dividend": "/opendata/t187ap45_L",
}

REPORTING_ENTITY_ALIASES = ("公司代號", "公司代碼", "證券代號", "股票代號", "Code", "code")
PERIOD_ALIASES = ("年度", "年", "資料年度", "出表年度", "Year", "year")
QUARTER_ALIASES = ("季別", "季度", "Quarter", "quarter")
STATEMENT_LABEL_ALIASES = ("會計項目", "會計科目", "項目", "會計項目名稱", "會計科目名稱", "Account", "account")
STATEMENT_VALUE_ALIASES = ("金額", "本期金額", "本期", "當期金額", "本年度金額", "數值", "Amount", "amount")
STATEMENT_META_ALIASES = set(REPORTING_ENTITY_ALIASES + PERIOD_ALIASES + QUARTER_ALIASES + ("公司名稱", "公司簡稱", "出表日期", "出表時間", "資料日期"))

INCOME_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("營業收入", "收益", "收入", "營收"),
    "gross_profit": ("營業毛利", "毛利"),
    "operating_expense": ("營業費用",),
    "operating_profit": ("營業利益", "營業利益（損失）", "營業損益", "營業淨利"),
    "non_operating_income_expense": ("營業外收入及支出", "營業外收益及費損"),
    "profit_before_tax": ("稅前淨利", "繼續營業單位稅前淨利", "稅前利益"),
    "tax_expense": ("所得稅費用", "所得稅利益", "所得稅"),
    "net_income": ("本期淨利", "本期淨利（淨損）", "本期稅後淨利", "淨利", "稅後淨利"),
    "eps": ("基本每股盈餘", "基本每股盈餘（元）", "每股盈餘"),
    "total_comprehensive_income": ("本期綜合損益總額", "綜合損益總額"),
    "depreciation_amortization": ("折舊", "折舊費用", "折舊及攤銷", "折舊與攤銷"),
    "interest_expense": ("利息費用",),
    "ebitda": ("稅前息前折舊攤銷前淨利", "EBITDA"),
}

BALANCE_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "cash": ("現金及約當現金", "現金及約當現金總額", "現金及約當現金合計"),
    "current_financial_assets": ("流動金融資產", "透過損益按公允價值衡量之金融資產－流動"),
    "trade_receivables": ("應收帳款", "應收票據及帳款", "應收帳款淨額"),
    "inventories": ("存貨",),
    "current_assets": ("流動資產合計", "流動資產總計", "流動資產"),
    "ppe": ("不動產、廠房及設備", "不動產廠房及設備", "固定資產"),
    "right_of_use_assets": ("使用權資產",),
    "intangible_assets": ("無形資產",),
    "non_current_assets": ("非流動資產合計", "非流動資產總計", "非流動資產"),
    "total_assets": ("資產總計", "資產合計", "資產總額"),
    "short_term_borrowings": ("短期借款",),
    "current_borrowings": ("一年內到期長期負債", "一年或一營業週期內到期長期借款", "流動借款"),
    "current_lease_liabilities": ("租賃負債－流動", "流動租賃負債"),
    "current_liabilities": ("流動負債合計", "流動負債總計", "流動負債"),
    "long_term_borrowings": ("長期借款", "應付公司債", "非流動借款"),
    "non_current_lease_liabilities": ("租賃負債－非流動", "非流動租賃負債"),
    "non_current_liabilities": ("非流動負債合計", "非流動負債總計", "非流動負債"),
    "total_liabilities": ("負債總計", "負債合計", "負債總額"),
    "share_capital": ("股本", "普通股股本"),
    "retained_earnings": ("保留盈餘", "保留盈餘合計"),
    "total_equity": ("權益總計", "權益合計", "歸屬於母公司業主之權益合計", "股東權益總計"),
    "treasury_shares": ("庫藏股票",),
    "other_equity": ("其他權益",),
    "total_liabilities_and_equity": ("負債及權益總計", "負債及股東權益總計"),
}

CASH_FLOW_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "operating_cash_flow": ("營業活動之淨現金流入", "營業活動之淨現金流出", "營業活動現金流量", "營業活動之淨現金流量"),
    "investing_cash_flow": ("投資活動之淨現金流入", "投資活動之淨現金流出", "投資活動現金流量", "投資活動之淨現金流量"),
    "financing_cash_flow": ("籌資活動之淨現金流入", "籌資活動之淨現金流出", "籌資活動現金流量", "籌資活動之淨現金流量"),
    "capex": ("取得不動產、廠房及設備", "購置不動產、廠房及設備", "資本支出"),
    "depreciation_amortization": ("折舊費用", "折舊及攤銷", "折舊與攤銷"),
    "interest_paid": ("支付之利息", "利息支付"),
    "dividends_paid": ("發放現金股利", "支付之股利", "股利支付"),
    "cash_beginning": ("期初現金及約當現金餘額",),
    "cash_ending": ("期末現金及約當現金餘額",),
    "free_cash_flow": ("自由現金流量",),
}

PROFILE_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "company_code": ("公司代號", "證券代號"),
    "company_name_zh": ("公司名稱",),
    "company_short_name": ("公司簡稱",),
    "industry": ("產業別",),
    "chairman": ("董事長",),
    "general_manager": ("總經理",),
    "spokesperson": ("發言人",),
    "phone": ("總機電話",),
    "incorporation_date": ("成立日期",),
    "listing_date": ("上市日期",),
    "paid_in_capital": ("實收資本額",),
    "financial_statement_type": ("編制財務報表類型",),
    "auditor": ("簽證會計師事務所",),
    "auditor_1": ("簽證會計師1",),
    "auditor_2": ("簽證會計師2",),
    "company_name_en": ("英文簡稱",),
    "website": ("網址",),
    "shares_outstanding": ("已發行普通股數或TDR原股發行股數",),
}


def _norm(s: Any) -> str:
    return re.sub(r"[\s　（）()\-—_:：,，]+", "", str(s or "").lower())


def _get(row: dict[str, Any], aliases: Iterable[str]) -> Any:
    norm_map = {_norm(k): v for k, v in row.items()}
    for alias in aliases:
        val = norm_map.get(_norm(alias))
        if val not in (None, ""):
            return val
    return None


def _to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"--", "-", "N/A", "NaN"}:
        return None
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", "-", "."}:
        return None
    try:
        n = float(text)
    except ValueError:
        return None
    return -n if neg else n


def _first_number(row: dict[str, Any], excluded_keys: set[str]) -> Optional[float]:
    for key, val in row.items():
        if key in excluded_keys:
            continue
        num = _to_number(val)
        if num is not None:
            return num
    return None


def _period_key(row: dict[str, Any]) -> str:
    year = _get(row, PERIOD_ALIASES)
    q = _get(row, QUARTER_ALIASES)
    year_text = str(year).strip() if year not in (None, "") else "latest"
    if year_text.isdigit() and int(year_text) < 1911:
        year_text = str(int(year_text) + 1911)
    if q not in (None, ""):
        return f"FY{year_text}Q{str(q).strip()}"
    return f"FY{year_text}" if year_text.isdigit() else year_text


def _metric_from_label(label: str, alias_map: dict[str, tuple[str, ...]]) -> Optional[str]:
    nlabel = _norm(label)
    if not nlabel:
        return None
    best: tuple[int, str] | None = None
    for metric, aliases in alias_map.items():
        for alias in aliases:
            nalias = _norm(alias)
            if nalias and nalias in nlabel:
                score = len(nalias)
                if best is None or score > best[0]:
                    best = (score, metric)
    return best[1] if best else None


def _find_statement_label(row: dict[str, Any]) -> str:
    explicit = _get(row, STATEMENT_LABEL_ALIASES)
    if explicit not in (None, ""):
        return str(explicit)
    for key, val in row.items():
        if key in STATEMENT_META_ALIASES:
            continue
        if val and _to_number(val) is None:
            return str(val)
    # Sometimes the account name is the column key and the amount is its value.
    for key in row:
        metric_key = str(key)
        if _metric_from_label(metric_key, {**INCOME_METRIC_ALIASES, **BALANCE_METRIC_ALIASES, **CASH_FLOW_METRIC_ALIASES}):
            return metric_key
    return ""


def _clean_line_item_key(label: str) -> str:
    cleaned = re.sub(r"\s+", "_", str(label or "").strip())
    cleaned = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", cleaned).strip("_")
    return cleaned[:80] or "unnamed_line_item"


def _statement_value(row: dict[str, Any], label: str) -> Optional[float]:
    explicit = _get(row, STATEMENT_VALUE_ALIASES)
    value = _to_number(explicit)
    if value is not None:
        return value
    excluded = set(STATEMENT_META_ALIASES) | set(STATEMENT_LABEL_ALIASES) | {label}
    value = _first_number(row, excluded)
    if value is not None:
        return value
    return _to_number(row.get(label))


def _rows_for_code(rows: list[dict[str, Any]], stock_code: str) -> list[dict[str, Any]]:
    return [row for row in rows if str(_get(row, REPORTING_ENTITY_ALIASES) or "").strip() == stock_code]


def _parse_statement(rows: list[dict[str, Any]], stock_code: str, aliases: dict[str, tuple[str, ...]]) -> dict[str, dict[str, float]]:
    periods: dict[str, dict[str, float]] = defaultdict(dict)
    for row in _rows_for_code(rows, stock_code):
        label = _find_statement_label(row)
        if label:
            metric = _metric_from_label(label, aliases)
            value = _statement_value(row, label)
            if metric and value is not None:
                periods[_period_key(row)][metric] = value
            continue

        # Wide-format fallback: treat matching account columns as individual metrics.
        for key, val in row.items():
            if key in STATEMENT_META_ALIASES:
                continue
            metric = _metric_from_label(str(key), aliases)
            value = _to_number(val)
            if metric and value is not None:
                periods[_period_key(row)][metric] = value
    return dict(periods)


def _parse_statement_line_items(rows: list[dict[str, Any]], stock_code: str) -> dict[str, dict[str, float]]:
    """Preserve every numeric statement line item, not only curated ratio fields."""
    periods: dict[str, dict[str, float]] = defaultdict(dict)
    for row in _rows_for_code(rows, stock_code):
        period = _period_key(row)
        label = _find_statement_label(row)
        if label:
            value = _statement_value(row, label)
            if value is not None:
                periods[period][_clean_line_item_key(label)] = value
            continue
        for key, val in row.items():
            if key in STATEMENT_META_ALIASES:
                continue
            value = _to_number(val)
            if value is not None:
                periods[period][_clean_line_item_key(str(key))] = value
    return dict(periods)


def _latest_period(periods: dict[str, dict[str, float]]) -> Optional[str]:
    if not periods:
        return None
    return sorted(periods.keys())[-1]


def _safe_div(n: Optional[float], d: Optional[float]) -> Optional[float]:
    if n is None or d in (None, 0):
        return None
    return round(n / d, 4)


def _derive_ratios(income: dict[str, dict[str, float]], balance: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    ratios: dict[str, dict[str, float]] = {}
    for period in sorted(set(income) | set(balance)):
        inc = income.get(period, {})
        bal = balance.get(period, {})
        total_debt = sum(v for v in (bal.get("short_term_borrowings"), bal.get("current_borrowings"), bal.get("current_lease_liabilities"), bal.get("long_term_borrowings"), bal.get("non_current_lease_liabilities")) if v is not None) or None
        net_debt = (total_debt - bal.get("cash")) if total_debt is not None and bal.get("cash") is not None else None
        ebitda = inc.get("ebitda")
        if ebitda is None and inc.get("operating_profit") is not None:
            ebitda = inc.get("operating_profit", 0) + (inc.get("depreciation_amortization") or 0)
        row = {
            "gross_margin_pct": _safe_div(inc.get("gross_profit"), inc.get("revenue")),
            "operating_margin_pct": _safe_div(inc.get("operating_profit"), inc.get("revenue")),
            "net_margin_pct": _safe_div(inc.get("net_income"), inc.get("revenue")),
            "current_ratio": _safe_div(bal.get("current_assets"), bal.get("current_liabilities")),
            "liabilities_to_assets": _safe_div(bal.get("total_liabilities"), bal.get("total_assets")),
            "debt_to_equity": _safe_div(total_debt, bal.get("total_equity")),
            "debt_to_ebitda": _safe_div(total_debt, ebitda),
            "net_debt_to_ebitda": _safe_div(net_debt, ebitda),
            "interest_coverage": _safe_div(inc.get("operating_profit"), inc.get("interest_expense")),
            "roe_pct": _safe_div(inc.get("net_income"), bal.get("total_equity")),
            "roa_pct": _safe_div(inc.get("net_income"), bal.get("total_assets")),
            "equity_to_assets": _safe_div(bal.get("total_equity"), bal.get("total_assets")),
            "quick_ratio": _safe_div((bal.get("current_assets") - bal.get("inventories")) if bal.get("current_assets") is not None and bal.get("inventories") is not None else None, bal.get("current_liabilities")),
        }
        ratios[period] = {k: v for k, v in row.items() if v is not None}
    return ratios


@dataclass
class TWSEOpenAPIClient:
    base_url: str = TWSE_BASE_URL
    timeout_seconds: float = 30.0
    exchange_name: str = field(default="TWSE", init=False)

    async def fetch(self, endpoint: str) -> list[dict[str, Any]]:
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {"Accept": "application/json", "User-Agent": "financial-report-analyzer/1.0"}
        try:
            # TWSE_VERIFY_SSL defaults True; set False only if TWSE's cert chain
            # fails on the deployment host (known issue on some cloud egress IPs).
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                verify=TWSE_VERIFY_SSL,
                headers=headers,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "json" not in content_type and not resp.text.strip().startswith("["):
                    logger.warning(
                        "TWSE OpenAPI returned non-JSON content-type=%r endpoint=%s — skipping JSON parse",
                        content_type, endpoint,
                    )
                    raise ValueError(f"Non-JSON response from TWSE: content-type={content_type!r}")
                data = resp.json()
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("TWSE OpenAPI JSON fetch failed endpoint=%s: %s", endpoint, exc)

        # Official TWSE/MOPS opendata CSV mirror is used as a resilience fallback.
        # Some hosting environments receive HTTP 403 from openapi.twse.com.tw even
        # though the same dataset is available from the official CSV endpoint.
        if endpoint.startswith("/opendata/"):
            dataset = endpoint.rsplit("/", 1)[-1]
            csv_url = f"{MOPS_OPEN_DATA_CSV_BASE_URL}/{dataset}.csv"
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    verify=TWSE_VERIFY_SSL,
                    headers={"User-Agent": "financial-report-analyzer/1.0"},
                ) as client:
                    resp = await client.get(csv_url)
                    resp.raise_for_status()
                    text = resp.content.decode("utf-8-sig", errors="replace")
                    return list(csv.DictReader(io.StringIO(text)))
            except Exception as exc:
                logger.warning("TWSE CSV fallback failed endpoint=%s url=%s: %s", endpoint, csv_url, exc)
        return []

    async def fetch_company_bundle(self, stock_code: str) -> dict[str, list[dict[str, Any]]]:
        now = time.monotonic()
        cached = _bundle_cache.get(stock_code)
        if cached is not None:
            cached_at, bundle = cached
            if now - cached_at < TWSE_BUNDLE_CACHE_TTL:
                logger.debug("fetch_company_bundle: cache hit stock=%s age=%.0fs", stock_code, now - cached_at)
                return bundle

        bundle: dict[str, list[dict[str, Any]]] = {}
        for name, endpoint in ENDPOINTS.items():
            bundle[name] = await self.fetch(endpoint)
        _bundle_cache[stock_code] = (now, bundle)
        return bundle


def build_section7_input(stock_code: str, bundle: dict[str, list[dict[str, Any]]], role: str = "guarantor", exchange_name: str = "TWSE") -> dict[str, Any]:
    profile_rows = _rows_for_code(bundle.get("company_profile", []), stock_code)
    profile_row = profile_rows[0] if profile_rows else {}
    profile = {name: _get(profile_row, aliases) for name, aliases in PROFILE_FIELD_ALIASES.items() if _get(profile_row, aliases) not in (None, "")}
    # paid_in_capital from TWSE is raw TWD (e.g. 259_303_805_450 for TSMC).
    # Financial statement metrics use thousands NTD — normalise here for consistency.
    if "paid_in_capital" in profile:
        try:
            raw = float(str(profile["paid_in_capital"]).replace(",", ""))
            profile["paid_in_capital"] = raw / 1_000
            profile["paid_in_capital_unit"] = "thousands_NTD"
        except (ValueError, TypeError):
            pass

    income = _parse_statement(bundle.get("income_statement_general", []), stock_code, INCOME_METRIC_ALIASES)
    balance = _parse_statement(bundle.get("balance_sheet_general", []), stock_code, BALANCE_METRIC_ALIASES)
    cash_flow = _parse_statement(bundle.get("cash_flow_general", []), stock_code, CASH_FLOW_METRIC_ALIASES)
    income_line_items = _parse_statement_line_items(bundle.get("income_statement_general", []), stock_code)
    balance_line_items = _parse_statement_line_items(bundle.get("balance_sheet_general", []), stock_code)
    cash_flow_line_items = _parse_statement_line_items(bundle.get("cash_flow_general", []), stock_code)
    ratios = _derive_ratios(income, balance)
    latest = _latest_period(balance) or _latest_period(income)
    latest_income = income.get(latest or "", {})
    latest_balance = balance.get(latest or "", {})
    latest_ratios = ratios.get(latest or "", {})

    monthly_revenue = _rows_for_code(bundle.get("monthly_revenue", []), stock_code)[:24]
    valuation = _rows_for_code(bundle.get("valuation_ratios", []), stock_code)[:5]
    trading = _rows_for_code(bundle.get("daily_trading", []), stock_code)[:5]
    dividends = _rows_for_code(bundle.get("dividend", []), stock_code)[:10]

    source_summary = {
        "stock_code": stock_code,
        "source": f"{exchange_name} OpenAPI",
        "exchange": exchange_name,
        "endpoints_used": ENDPOINTS if exchange_name == "TWSE" else None,
        "row_counts": {k: len(_rows_for_code(v, stock_code)) for k, v in bundle.items()},
        "coverage_fields": {
            "profile": len(profile),
            "income_statement_metrics": sum(len(v) for v in income.values()),
            "balance_sheet_metrics": sum(len(v) for v in balance.values()),
            "cash_flow_metrics": sum(len(v) for v in cash_flow.values()),
            "raw_income_statement_line_items": sum(len(v) for v in income_line_items.values()),
            "raw_balance_sheet_line_items": sum(len(v) for v in balance_line_items.values()),
            "raw_cash_flow_line_items": sum(len(v) for v in cash_flow_line_items.values()),
            "derived_ratio_metrics": sum(len(v) for v in ratios.values()),
            "monthly_revenue_rows": len(monthly_revenue),
            "valuation_rows": len(valuation),
            "trading_rows": len(trading),
            "dividend_rows": len(dividends),
        },
    }

    financials = {
        "reporting_entity": profile.get("company_name_zh") or profile.get("company_short_name") or stock_code,
        "auditor": profile.get("auditor"),
        "accounting_standard": "Taiwan IFRS",
        "reporting_currency": "TWD",
        "unit": "thousands",
        "income_statement": income,
        "balance_sheet": balance,
        "cash_flow_statement": cash_flow,
        "key_ratios_by_period": ratios,
        "raw_income_statement_line_items": income_line_items,
        "raw_balance_sheet_line_items": balance_line_items,
        "raw_cash_flow_line_items": cash_flow_line_items,
        "latest_period": latest,
        "twse_company_profile": profile,
        "twse_monthly_revenue": monthly_revenue,
        "twse_valuation_ratios": valuation,
        "twse_recent_trading": trading,
        "twse_dividends": dividends,
        "twse_source_summary": source_summary,
    }

    if latest:
        # Populate existing flat FY2024 fields from the latest available period to preserve old UI compatibility.
        financials.update({
            "revenue_fy2024": latest_income.get("revenue"),
            "ebitda_fy2024": latest_income.get("ebitda") or ((latest_income.get("operating_profit") or 0) + (latest_income.get("depreciation_amortization") or 0) if latest_income.get("operating_profit") is not None else None),
            "depreciation_fy2024": latest_income.get("depreciation_amortization"),
            "op_profit_fy2024": latest_income.get("operating_profit"),
            "interest_expense_fy2024": latest_income.get("interest_expense"),
            "net_income_fy2024": latest_income.get("net_income"),
            "bs_cash": latest_balance.get("cash"),
            "bs_total_ca": latest_balance.get("current_assets"),
            "bs_total_nca": latest_balance.get("non_current_assets"),
            "bs_total_assets": latest_balance.get("total_assets"),
            "bs_total_cl": latest_balance.get("current_liabilities"),
            "bs_total_ncl": latest_balance.get("non_current_liabilities"),
            "bs_total_liabilities": latest_balance.get("total_liabilities"),
            "bs_total_equity": latest_balance.get("total_equity"),
        })

    section: dict[str, Any] = {
        "twse_import": source_summary,
        "7B_key_ratios": {
            "fy2024_debt_ebitda": latest_ratios.get("debt_to_ebitda"),
            "fy2024_interest_coverage": latest_ratios.get("interest_coverage"),
            "fy2024_current_ratio": latest_ratios.get("current_ratio"),
            "fy2024_net_margin_pct": (latest_ratios.get("net_margin_pct") * 100) if latest_ratios.get("net_margin_pct") is not None else None,
            "twse_ratios_by_period": ratios,
        },
    }

    if role == "borrower":
        section["entities_to_analyze"] = {
            "borrower_name": financials["reporting_entity"],
            "borrower_currency": "TWD",
            "borrower_unit": "thousands",
        }
        section["7A_borrower_financials"] = financials
    else:
        section["entities_to_analyze"] = {
            "guarantor_name": financials["reporting_entity"],
            "guarantor_currency": "TWD",
            "guarantor_exists": True,
        }
        section["7C_guarantor_financials"] = {
            "applicable": True,
            "reporting_currency": "TWD",
            "revenue_fy2024": latest_income.get("revenue"),
            "ebitda_fy2024": financials.get("ebitda_fy2024"),
            "net_income_fy2024": latest_income.get("net_income"),
            "cash_fy2024": latest_balance.get("cash"),
            "total_assets_fy2024": latest_balance.get("total_assets"),
            "total_equity_fy2024": latest_balance.get("total_equity"),
            "income_statement": income,
            "balance_sheet": balance,
            "cash_flow_statement": cash_flow,
            "key_ratios_by_period": ratios,
            "raw_income_statement_line_items": income_line_items,
            "raw_balance_sheet_line_items": balance_line_items,
            "raw_cash_flow_line_items": cash_flow_line_items,
            "twse_company_profile": profile,
            "twse_monthly_revenue": monthly_revenue,
            "twse_valuation_ratios": valuation,
            "twse_recent_trading": trading,
            "twse_dividends": dividends,
            "twse_source_summary": source_summary,
        }

    return _drop_none(section)


def _drop_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for key, value in obj.items():
            if value is None:
                continue
            cleaned_value = _drop_none(value)
            if cleaned_value not in ({}, []):
                cleaned[key] = cleaned_value
        return cleaned
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj if v is not None]
    return obj

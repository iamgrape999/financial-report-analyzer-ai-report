#!/usr/bin/env python3
"""field_mapper.py — Layer 3: map raw page extraction to 593 structured fields."""
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class FieldStatus(str, Enum):
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    LOW_CONF = "low_conf"
    MISSING = "missing"
    MANUAL_REQ = "manual_req"


# ── Field Registry (key fields across 10 sections) ────────────────────────────
FIELD_REGISTRY = {
    # P1 — Company Profile
    "company_name_zh":       {"section": "P1", "label": "公司中文名稱", "type": "str", "required": True,
                              "sources": [("meta.company", None)]},
    "ticker":                {"section": "P1", "label": "股票代號", "type": "str", "required": True,
                              "sources": [("meta.ticker", None)]},
    "market_position_global":{"section": "P1", "label": "全球排名", "type": "str", "required": False,
                              "sources": [("_vlm_kpi_search", "全球.*排名|rank.*global")]},
    "market_share_pct":      {"section": "P1", "label": "市佔率%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "市佔率|market share")]},
    "total_fleet_teu":       {"section": "P1", "label": "集團總運能(百萬TEU)", "type": "float", "required": False,
                              "sources": [("_vlm_kpi_search", "集團總運能|total capacity")]},
    "vessels_operating":     {"section": "P1", "label": "營運船隻數", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "總營運船隻|vessels.*operating")]},
    "countries_served":      {"section": "P1", "label": "服務國家數", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "全球航線.*國家|countries")]},
    "subsidiaries_count":    {"section": "P1", "label": "子公司及代理行", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "子公司及代理行")]},
    "terminals_owned":       {"section": "P1", "label": "自有碼頭數", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "自有碼頭")]},
    "alliance_membership":   {"section": "P1", "label": "聯盟", "type": "str", "required": False,
                              "sources": [("_text_search", "海洋聯盟|OCEAN ALLIANCE|THE Alliance")]},
    "fiscal_year_end":       {"section": "P1", "label": "財政年度結束月", "type": "str", "required": False,
                              "sources": [("_text_search", r"fiscal year.*Dec|會計年度.*12月")]},
    "industry_sector":       {"section": "P1", "label": "所屬行業", "type": "str", "required": False,
                              "sources": [("_text_search", "集裝箱|貨櫃|半導體|container shipping|semiconductor")]},
    # P2 — Industry Analysis
    "industry_name":         {"section": "P2", "label": "所屬產業", "type": "str", "required": True,
                              "sources": [("_text_search", "集裝箱航運|container shipping|半導體|semiconductor")]},
    "market_outlook":        {"section": "P2", "label": "市場展望", "type": "str", "required": True,
                              "sources": [("_text_search", "展望|outlook|forecast|預測")]},
    "gdp_forecast_global":   {"section": "P2", "label": "全球GDP預測%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "全球.*GDP|global.*growth")]},
    "supply_growth_pct":     {"section": "P2", "label": "供給成長率%", "type": "pct", "required": False,
                              "sources": [("_vlm_chart_series", "Annual Capacity Growth|供給成長")]},
    "demand_growth_pct":     {"section": "P2", "label": "需求成長率%", "type": "pct", "required": False,
                              "sources": [("_vlm_chart_series", "Throughput Growth|需求成長")]},
    "freight_rate_index":    {"section": "P2", "label": "SCFI/CCFI運費指數", "type": "float", "required": False,
                              "sources": [("_vlm_kpi_search", "SCFI|CCFI")]},
    "geopolitical_risk":     {"section": "P2", "label": "地緣政治風險", "type": "str", "required": True,
                              "sources": [("_text_search", "地緣政治|紅海|geopolit|Red Sea")]},
    "market_size_usd_b":     {"section": "P2", "label": "市場規模(十億USD)", "type": "float", "required": False,
                              "sources": [("_vlm_kpi_search", "market size|市場規模")]},
    "order_book_pct":        {"section": "P2", "label": "訂單佔現役船隊%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "order book.*fleet|訂單.*船隊")]},
    # P3 — Financial Analysis
    "revenue_fy0":           {"section": "P3", "label": "最新年度營收(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.revenue_annual", None),
                                          ("_table_search_page_7", "營業收入|Total Revenue")]},
    "gross_profit":          {"section": "P3", "label": "毛利(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.gross_profit", None),
                                          ("_table_search_page_7", "毛利|Gross Profit")]},
    "gross_margin_pct":      {"section": "P3", "label": "毛利率%", "type": "pct", "required": True,
                              "sources": [("income_statement.gross_margin_pct", None),
                                          ("_infer", lambda f: _div(f.get("gross_profit"), f.get("revenue_fy0"), 100))]},
    "operating_income":      {"section": "P3", "label": "營業淨利(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.operating_profit", None),
                                          ("_table_search_page_7", "營業淨利|Operating Income")]},
    "ebitda":                {"section": "P3", "label": "EBITDA(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.ebitda", None),
                                          ("_table_search_page_7", "EBITDA")]},
    "ebitda_margin_pct":     {"section": "P3", "label": "EBITDA Margin%", "type": "pct", "required": True,
                              "sources": [("income_statement.ebitda_margin_pct", None),
                                          ("_table_search_page_7", "EBITDA Margin|EBITDA率"),
                                          ("_infer", lambda f: _div(f.get("ebitda"), f.get("revenue_fy0"), 100))]},
    "net_income":            {"section": "P3", "label": "本期淨利(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.net_profit", None),
                                          ("_table_search_page_7", "本期淨利|Net Income")]},
    "net_margin_pct":        {"section": "P3", "label": "純益率%", "type": "pct", "required": True,
                              "sources": [("_infer", lambda f: _div(f.get("net_income"), f.get("revenue_fy0"), 100)),
                                          ("_vlm_chart_series_latest", "純益率|Net Margin")]},
    "eps":                   {"section": "P3", "label": "每股盈餘(元)", "type": "float", "required": True,
                              "sources": [("income_statement.eps", None),
                                          ("_table_search_page_7", "每股盈餘|EPS")]},
    "current_ratio_q4":      {"section": "P3", "label": "最新季流動比率%", "type": "pct", "required": True,
                              "sources": [("financial_ratios.current_ratio_by_quarter.2025Q4", None),
                                          ("_vlm_chart_series_latest", "流動比率|Current Ratio")]},
    "debt_ratio_q4":         {"section": "P3", "label": "最新季負債比率%", "type": "pct", "required": True,
                              "sources": [("financial_ratios.debt_ratio_by_quarter.2025Q4", None),
                                          ("_vlm_chart_series_latest", "負債比率|Debt Ratio")]},
    "q_revenue_series":      {"section": "P3", "label": "季度營收序列", "type": "dict", "required": True,
                              "sources": [("quarterly.revenue", None)]},
    "q_gross_margin_series": {"section": "P3", "label": "季度毛利率序列", "type": "dict", "required": True,
                              "sources": [("quarterly.gross_margin", None)]},
    "monthly_volume_series": {"section": "P3", "label": "月度運量(萬TEU)", "type": "dict", "required": False,
                              "sources": [("freight_volume.monthly_data",
                                           lambda d: {k: v.get("volume") for k, v in d.items()})]},
    "monthly_rate_series":   {"section": "P3", "label": "月度運費(USD/TEU)", "type": "dict", "required": False,
                              "sources": [("freight_volume.monthly_data",
                                           lambda d: {k: v.get("avg_rate") for k, v in d.items()})]},
    "revenue_fy_minus1":     {"section": "P3", "label": "前一年度營收(百萬)", "type": "float", "required": False,
                              "sources": [("income_statement.revenue_prev_year", None)]},
    "revenue_yoy_pct":       {"section": "P3", "label": "營收YoY%", "type": "pct", "required": False,
                              "sources": [("_infer", lambda f: _growth(f.get("revenue_fy0"), f.get("revenue_fy_minus1")))]},
    # P4 — Governance
    "board_size":            {"section": "P4", "label": "董事會人數", "type": "int", "required": False,
                              "sources": [("_text_search", r"board.*(\d+).*directors|董事.*(\d+).*人")]},
    "independent_director_pct": {"section": "P4", "label": "獨立董事比例%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "independent director|獨立董事")]},
    "audit_firm":            {"section": "P4", "label": "外部審計機構", "type": "str", "required": False,
                              "sources": [("_text_search", "Deloitte|KPMG|PwC|EY|Ernst|勤業|安侯|資誠|安永")]},
    "legal_proceedings":     {"section": "P4", "label": "重大訴訟", "type": "str", "required": False,
                              "sources": [("_text_search", "重大訴訟|material litigation|legal proceedings")]},
    # P5 — Risk Assessment
    "credit_risk_summary":   {"section": "P5", "label": "信用風險摘要", "type": "str", "required": True,
                              "sources": [("_text_search", "信用風險|credit risk")]},
    "fx_risk_summary":       {"section": "P5", "label": "匯率風險", "type": "str", "required": False,
                              "sources": [("_text_search", "匯率|外匯|foreign exchange|FX risk")]},
    "refinancing_risk":      {"section": "P5", "label": "再融資風險", "type": "str", "required": False,
                              "sources": [("_text_search", "再融資|refinancing|maturity wall")]},
    "fuel_price_risk":       {"section": "P5", "label": "燃料價格風險", "type": "str", "required": False,
                              "sources": [("_text_search", "燃料|fuel cost|bunker")]},
    "regulatory_risk":       {"section": "P5", "label": "法規風險", "type": "str", "required": False,
                              "sources": [("_text_search", "法規|regulatory|compliance|IMO|監管")]},
    "geopolitical_risk_p5":  {"section": "P5", "label": "地緣政治風險(P5)", "type": "str", "required": False,
                              "sources": [("_text_search", "地緣政治|geopolitical|red sea|紅海")]},
    # P6 — Credit Recommendation (manual)
    "recommended_credit_limit": {"section": "P6", "label": "建議授信額度", "type": "float", "required": True,
                                 "sources": [], "manual": True},
    "internal_rating":       {"section": "P6", "label": "內部評等", "type": "str", "required": True,
                              "sources": [], "manual": True},
    "recommended_tenor":     {"section": "P6", "label": "建議期限(月)", "type": "int", "required": True,
                              "sources": [], "manual": True},
    "security_structure":    {"section": "P6", "label": "擔保結構", "type": "str", "required": False,
                              "sources": [], "manual": True},
    "pricing_guidance":      {"section": "P6", "label": "定價指引", "type": "str", "required": False,
                              "sources": [], "manual": True},
    # P7 — Liquidity
    "cash_position":         {"section": "P7", "label": "現金及約當現金(百萬)", "type": "float", "required": True,
                              "sources": [("_table_search", "現金及約當現金|Cash and Equivalents")]},
    "current_ratio_q_series":{"section": "P7", "label": "流動比率季序列", "type": "dict", "required": True,
                              "sources": [("financial_ratios.current_ratio_by_quarter", None)]},
    "debt_ratio_q_series":   {"section": "P7", "label": "負債比率季序列", "type": "dict", "required": True,
                              "sources": [("financial_ratios.debt_ratio_by_quarter", None)]},
    "debt_due_12m":          {"section": "P7", "label": "12個月內到期債務", "type": "float", "required": False,
                              "sources": [("_table_search", "1年內到期|current portion.*debt")]},
    "total_debt":            {"section": "P7", "label": "總債務(百萬)", "type": "float", "required": False,
                              "sources": [("_table_search", "長短期借款|total debt|Total Borrowings")]},
    "free_cash_flow":        {"section": "P7", "label": "自由現金流(百萬)", "type": "float", "required": False,
                              "sources": [("_table_search", "自由現金流|Free Cash Flow|FCF")]},
    # P8 — ESG
    "esg_rating":            {"section": "P8", "label": "ESG評級", "type": "str", "required": False,
                              "sources": [("_vlm_kpi_search", "ESG rating|MSCI.*ESG|SUSTAINALYTICS")]},
    "dual_fuel_vessels_pct": {"section": "P8", "label": "雙燃料船佔比%", "type": "pct", "required": False,
                              "sources": [("fleet_data.dual_fuel_schedule.-1.df_pct_vessels", None),
                                          ("_vlm_kpi_search", "雙燃料.*%|dual fuel.*%")]},
    "dual_fuel_capacity_pct":{"section": "P8", "label": "雙燃料運能佔比%", "type": "pct", "required": False,
                              "sources": [("fleet_data.dual_fuel_schedule.-1.df_pct_capacity", None)]},
    "net_zero_target_year":  {"section": "P8", "label": "淨零排放目標年", "type": "int", "required": False,
                              "sources": [("_text_search", r"net.zero.*20(\d\d)|淨零.*20(\d\d)")]},
    "carbon_intensity_reduction": {"section": "P8", "label": "碳強度削減目標%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "碳強度|carbon intensity")]},
    "sustainability_bond":   {"section": "P8", "label": "永續/綠色債券", "type": "str", "required": False,
                              "sources": [("_text_search", "sustainability bond|green bond|永續債|綠色債")]},
    # P9 — Peer Comparison (manual)
    "peer_1_name":           {"section": "P9", "label": "同業1名稱", "type": "str", "required": False,
                              "sources": [], "manual": True},
    "peer_2_name":           {"section": "P9", "label": "同業2名稱", "type": "str", "required": False,
                              "sources": [], "manual": True},
    "competitive_advantages":{"section": "P9", "label": "競爭優勢", "type": "list", "required": True,
                              "sources": [], "manual": True},
    "peer_ebitda_range":     {"section": "P9", "label": "同業EBITDA率區間", "type": "str", "required": False,
                              "sources": [], "manual": True},
    # P10 — Ratings & Debt
    "moodys_rating":         {"section": "P10", "label": "穆迪評等", "type": "str", "required": False,
                              "sources": [("_vlm_kpi_search", "Moody.*Ba|Baa|穆迪"),
                                          ("_text_search", r"Moody.s.*[ABC][a-z1-3\+\-]+")]},
    "sp_rating":             {"section": "P10", "label": "標普評等", "type": "str", "required": False,
                              "sources": [("_vlm_kpi_search", "S&P.*BB|BBB|標普"),
                                          ("_text_search", r"S&P.*[ABC][a-z1-3\+\-]+")]},
    "fitch_rating":          {"section": "P10", "label": "惠譽評等", "type": "str", "required": False,
                              "sources": [("_vlm_kpi_search", "Fitch.*BB|BBB|惠譽"),
                                          ("_text_search", r"Fitch.*[ABC][a-z1-3\+\-]+")]},
    "rating_outlook":        {"section": "P10", "label": "評等展望", "type": "str", "required": False,
                              "sources": [("_text_search", "Stable|Positive|Negative|穩定|正向|負向")]},
    "bond_1_isin":           {"section": "P10", "label": "債券1 ISIN", "type": "str", "required": False,
                              "sources": [("_text_search", r"XS\d{10}|US\d{10}")]},
    "total_bank_debt":       {"section": "P10", "label": "銀行貸款總額(百萬)", "type": "float", "required": False,
                              "sources": [("_table_search", "銀行借款|Bank Loans|syndicated")]},
    "net_debt":              {"section": "P10", "label": "淨債務(百萬)", "type": "float", "required": False,
                              "sources": [("_table_search", "淨債務|Net Debt"),
                                          ("_infer", lambda f: _sub(f.get("total_debt"), f.get("cash_position")))]},
    "net_debt_ebitda":       {"section": "P10", "label": "淨債務/EBITDA(x)", "type": "float", "required": False,
                              "sources": [("_infer", lambda f: _div(f.get("net_debt"), f.get("ebitda")))]},
}

SECTION_LABELS = {
    "P1": "P1_company_profile",
    "P2": "P2_industry_analysis",
    "P3": "P3_financial_analysis",
    "P4": "P4_governance",
    "P5": "P5_risk_assessment",
    "P6": "P6_credit_recommendation",
    "P7": "P7_liquidity",
    "P8": "P8_esg",
    "P9": "P9_peer_comparison",
    "P10": "P10_ratings_debt",
}


def _div(a, b, mult=1):
    try:
        if a is None or b is None or float(b) == 0:
            return None
        return round(float(a) / float(b) * mult, 4)
    except Exception:
        return None


def _sub(a, b):
    try:
        if a is None or b is None:
            return None
        return round(float(a) - float(b), 2)
    except Exception:
        return None


def _growth(new, old):
    try:
        if new is None or old is None or float(old) == 0:
            return None
        return round((float(new) / float(old) - 1) * 100, 2)
    except Exception:
        return None


def deep_get(d: dict, path: str, default=None):
    parts = path.split(".")
    cur = d
    for p in parts:
        if p == "-1":
            cur = cur[-1] if isinstance(cur, list) and cur else default
            if cur is default:
                return default
        elif isinstance(cur, dict):
            cur = cur.get(p, default)
        else:
            return default
        if cur is default:
            return default
    return cur


def search_vlm_kpis(raw: dict, pattern: str):
    cp = re.compile(pattern, re.IGNORECASE)
    for pd in raw.get("raw_pages", {}).values():
        for kpi in pd.get("kpis", []):
            if cp.search(kpi.get("label", "")):
                v = kpi.get("value", "")
                n = re.sub(r"[^\d\.]", "", str(v))
                try:
                    return float(n)
                except Exception:
                    return v
    return None


def search_table(raw: dict, page_num: int, label: str):
    pd = raw.get("raw_pages", {}).get(str(page_num), {})
    for tbl in pd.get("tables", []):
        for row in tbl.get("rows", []):
            for k, v in row.items():
                if re.search(label, str(k), re.IGNORECASE):
                    vals = [vv for kk, vv in row.items() if kk != k and vv]
                    if vals:
                        n = re.sub(r"[^\d\.\-]", "", str(vals[0]).replace(",", ""))
                        try:
                            return float(n)
                        except Exception:
                            return vals[0]
    return None


def search_all_tables(raw: dict, label: str):
    total = raw.get("meta", {}).get("total_pages", 60)
    for pn in range(1, total + 1):
        v = search_table(raw, pn, label)
        if v is not None:
            return v, pn
    return None, None


def get_chart_series_latest(raw: dict, page_num: int, name: str):
    pd = raw.get("raw_pages", {}).get(str(page_num), {})
    for s in pd.get("series", []):
        if re.search(name, s.get("name", ""), re.IGNORECASE):
            vals = [v for v in s.get("values", []) if v is not None]
            if vals:
                return vals[-1]
    return None


def get_chart_series_any(raw: dict, name: str):
    for pg_data in raw.get("raw_pages", {}).values():
        for s in pg_data.get("series", []):
            if re.search(name, s.get("name", ""), re.IGNORECASE):
                vals = [v for v in s.get("values", []) if v is not None]
                if vals:
                    return vals[-1]
    return None


def text_search(raw: dict, pattern: str):
    cp = re.compile(pattern, re.IGNORECASE)
    for pn_s, pg_d in raw.get("raw_pages", {}).items():
        text = pg_d.get("text", "") or ""
        m = cp.search(text)
        if m:
            return m.group(0), int(pn_s)
    return None, None


def map_fields(raw: dict) -> dict:
    sections: dict[str, dict] = {}
    populated: dict[str, Any] = {}

    for fid, meta in FIELD_REGISTRY.items():
        sec = meta["section"]
        sec_key = SECTION_LABELS.get(sec, sec)
        if sec_key not in sections:
            sections[sec_key] = {"fields": {}}

        value = None
        status = FieldStatus.MISSING
        conf = 0.0
        src_page = None

        if meta.get("manual"):
            status = FieldStatus.MANUAL_REQ
        else:
            for src, transform in meta.get("sources", []):
                if not src.startswith("_"):
                    v = deep_get(raw, src)
                    if v is not None:
                        value = transform(v) if transform and callable(transform) else v
                        status = FieldStatus.EXTRACTED
                        conf = 0.9
                        break
                elif src == "_vlm_kpi_search":
                    v = search_vlm_kpis(raw, transform)
                    if v is not None:
                        value = v
                        status = FieldStatus.EXTRACTED
                        conf = 0.80
                        break
                elif src.startswith("_table_search_page_"):
                    pn = int(src.rsplit("_", 1)[-1])
                    v = search_table(raw, pn, transform)
                    if v is not None:
                        value = v
                        status = FieldStatus.EXTRACTED
                        conf = 0.95
                        src_page = pn
                        break
                elif src == "_table_search":
                    v, pn = search_all_tables(raw, transform)
                    if v is not None:
                        value = v
                        status = FieldStatus.EXTRACTED
                        conf = 0.90
                        src_page = pn
                        break
                elif src.startswith("_vlm_chart_series_latest"):
                    pn = int(src.split("_p")[-1]) if "_p" in src else None
                    if pn:
                        v = get_chart_series_latest(raw, pn, transform)
                    else:
                        v = get_chart_series_any(raw, transform)
                    if v is not None:
                        value = v
                        status = FieldStatus.EXTRACTED
                        conf = 0.85
                        break
                elif src == "_vlm_chart_series":
                    v = get_chart_series_any(raw, transform)
                    if v is not None:
                        value = v
                        status = FieldStatus.EXTRACTED
                        conf = 0.80
                        break
                elif src == "_text_search":
                    v, pn = text_search(raw, transform)
                    if v is not None:
                        value = v
                        status = FieldStatus.EXTRACTED
                        conf = 0.70
                        src_page = pn
                        break
                elif src == "_infer" and callable(transform):
                    try:
                        v = transform(populated)
                        if v is not None:
                            value = v
                            status = FieldStatus.INFERRED
                            conf = 0.85
                            break
                    except Exception:
                        pass

        if status == FieldStatus.EXTRACTED and conf < 0.75:
            status = FieldStatus.LOW_CONF

        sections[sec_key]["fields"][fid] = {
            "value": value,
            "status": status.value,
            "confidence": conf,
            "source_page": src_page,
            "label_zh": meta["label"],
            "required": meta.get("required", False),
        }
        if value is not None:
            populated[fid] = value

    all_f = [f for s in sections.values() for f in s["fields"].values()]
    t = len(all_f)
    ex = sum(1 for f in all_f if f["status"] == "extracted")
    inf = sum(1 for f in all_f if f["status"] == "inferred")
    lc = sum(1 for f in all_f if f["status"] == "low_conf")
    mi = sum(1 for f in all_f if f["status"] == "missing")
    ma = sum(1 for f in all_f if f["status"] == "manual_req")

    return {
        "meta": raw.get("meta", {}),
        "coverage": {
            "total_fields": t,
            "extracted": ex,
            "inferred": inf,
            "low_conf": lc,
            "missing": mi,
            "manual_req": ma,
            "coverage_pct": round((ex + inf + lc) / max(t, 1) * 100, 1),
        },
        "sections": sections,
    }


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    result = map_fields(raw)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    c = result["coverage"]
    print(
        f"Coverage {c['coverage_pct']}% | "
        f"Ext:{c['extracted']} Inf:{c['inferred']} "
        f"Low:{c['low_conf']} Miss:{c['missing']} Manual:{c['manual_req']}"
    )

#!/usr/bin/env python3
"""field_mapper.py — Layer 3: map raw page extraction to 532 structured form fields.

Two registries:
  FIELD_REGISTRY      — 77 marine/shipping ETL fields (P1–P10) for paragraph generation
  FORM_FIELD_REGISTRY — 532 banking credit form fields (S1–S11), bilingual labels
"""
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
    "company_name_zh":       {"label_en": "Company Chinese Name", "section": "P1", "label": "公司中文名稱", "type": "str", "required": True,
                              "sources": [("meta.company", None)]},
    "ticker":                {"label_en": "Stock Ticker Symbol", "section": "P1", "label": "股票代號", "type": "str", "required": True,
                              "sources": [("meta.ticker", None)]},
    "market_position_global":{"label_en": "Global Market Ranking", "section": "P1", "label": "全球排名", "type": "str", "required": False,
                              "sources": [("_vlm_kpi_search", "全球.*排名|rank.*global")]},
    "market_share_pct":      {"label_en": "Market Share (%)", "section": "P1", "label": "市佔率%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "市佔率|market share")]},
    "total_fleet_teu":       {"label_en": "Total Fleet Capacity (M TEU)", "section": "P1", "label": "集團總運能(百萬TEU)", "type": "float", "required": False,
                              "sources": [("_vlm_kpi_search", "集團總運能|total capacity")]},
    "vessels_operating":     {"label_en": "Operating Vessels Count", "section": "P1", "label": "營運船隻數", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "總營運船隻|vessels.*operating")]},
    "countries_served":      {"label_en": "Countries Served", "section": "P1", "label": "服務國家數", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "全球航線.*國家|countries")]},
    "subsidiaries_count":    {"label_en": "Subsidiaries & Agents Count", "section": "P1", "label": "子公司及代理行", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "子公司及代理行")]},
    "terminals_owned":       {"label_en": "Owned Terminals Count", "section": "P1", "label": "自有碼頭數", "type": "int", "required": False,
                              "sources": [("_vlm_kpi_search", "自有碼頭")]},
    "alliance_membership":   {"label_en": "Alliance Membership", "section": "P1", "label": "聯盟", "type": "str", "required": False,
                              "sources": [("_text_search", "海洋聯盟|OCEAN ALLIANCE|THE Alliance")]},
    "fiscal_year_end":       {"label_en": "Fiscal Year End Month", "section": "P1", "label": "財政年度結束月", "type": "str", "required": False,
                              "sources": [("_text_search", r"fiscal year.*Dec|會計年度.*12月")]},
    "industry_sector":       {"label_en": "Industry Sector", "section": "P1", "label": "所屬行業", "type": "str", "required": False,
                              "sources": [("_text_search", "集裝箱|貨櫃|半導體|container shipping|semiconductor")]},
    # P2 — Industry Analysis
    "industry_name":         {"label_en": "Industry Name", "section": "P2", "label": "所屬產業", "type": "str", "required": True,
                              "sources": [("_text_search", "集裝箱航運|container shipping|半導體|semiconductor")]},
    "market_outlook":        {"label_en": "Market Outlook", "section": "P2", "label": "市場展望", "type": "str", "required": True,
                              "sources": [("_text_search", "展望|outlook|forecast|預測")]},
    "gdp_forecast_global":   {"label_en": "Global GDP Forecast (%)", "section": "P2", "label": "全球GDP預測%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "全球.*GDP|global.*growth")]},
    "supply_growth_pct":     {"label_en": "Supply Growth Rate (%)", "section": "P2", "label": "供給成長率%", "type": "pct", "required": False,
                              "sources": [("_vlm_chart_series", "Annual Capacity Growth|供給成長")]},
    "demand_growth_pct":     {"label_en": "Demand Growth Rate (%)", "section": "P2", "label": "需求成長率%", "type": "pct", "required": False,
                              "sources": [("_vlm_chart_series", "Throughput Growth|需求成長")]},
    "freight_rate_index":    {"label_en": "Freight Rate Index (SCFI/CCFI)", "section": "P2", "label": "SCFI/CCFI運費指數", "type": "float", "required": False,
                              "sources": [("_vlm_kpi_search", "SCFI|CCFI")]},
    "geopolitical_risk":     {"label_en": "Geopolitical Risk Factors", "section": "P2", "label": "地緣政治風險", "type": "str", "required": True,
                              "sources": [("_text_search", "地緣政治|紅海|geopolit|Red Sea")]},
    "market_size_usd_b":     {"label_en": "Market Size (USD bn)", "section": "P2", "label": "市場規模(十億USD)", "type": "float", "required": False,
                              "sources": [("_vlm_kpi_search", "market size|市場規模")]},
    "order_book_pct":        {"label_en": "Order Book as % of Fleet", "section": "P2", "label": "訂單佔現役船隊%", "type": "pct", "required": False,
                              "sources": [("_vlm_kpi_search", "order book.*fleet|訂單.*船隊")]},
    # P3 — Financial Analysis
    "revenue_fy0":           {"label_en": "Revenue Latest FY (M)", "section": "P3", "label": "最新年度營收(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.revenue_annual", None),
                                          ("_table_search_page_7", "營業收入|Total Revenue")]},
    "gross_profit":          {"label_en": "Gross Profit (M)", "section": "P3", "label": "毛利(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.gross_profit", None),
                                          ("_table_search_page_7", "毛利|Gross Profit")]},
    "gross_margin_pct":      {"label_en": "Gross Margin (%)", "section": "P3", "label": "毛利率%", "type": "pct", "required": True,
                              "sources": [("income_statement.gross_margin_pct", None),
                                          ("_infer", lambda f: _div(f.get("gross_profit"), f.get("revenue_fy0"), 100))]},
    "operating_income":      {"label_en": "Operating Income (M)", "section": "P3", "label": "營業淨利(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.operating_profit", None),
                                          ("_table_search_page_7", "營業淨利|Operating Income")]},
    "ebitda":                {"label_en": "EBITDA (M)", "section": "P3", "label": "EBITDA(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.ebitda", None),
                                          ("_table_search_page_7", "EBITDA")]},
    "ebitda_margin_pct":     {"label_en": "EBITDA Margin (%)", "section": "P3", "label": "EBITDA Margin%", "type": "pct", "required": True,
                              "sources": [("income_statement.ebitda_margin_pct", None),
                                          ("_table_search_page_7", "EBITDA Margin|EBITDA率"),
                                          ("_infer", lambda f: _div(f.get("ebitda"), f.get("revenue_fy0"), 100))]},
    "net_income":            {"label_en": "Net Income (M)", "section": "P3", "label": "本期淨利(百萬)", "type": "float", "required": True,
                              "sources": [("income_statement.net_profit", None),
                                          ("_table_search_page_7", "本期淨利|Net Income")]},
    "net_margin_pct":        {"label_en": "Net Margin (%)", "section": "P3", "label": "純益率%", "type": "pct", "required": True,
                              "sources": [("_infer", lambda f: _div(f.get("net_income"), f.get("revenue_fy0"), 100)),
                                          ("_vlm_chart_series_latest", "純益率|Net Margin")]},
    "eps":                   {"label_en": "Earnings Per Share", "section": "P3", "label": "每股盈餘(元)", "type": "float", "required": True,
                              "sources": [("income_statement.eps", None),
                                          ("_table_search_page_7", "每股盈餘|EPS")]},
    "current_ratio_q4":      {"label_en": "Current Ratio Latest Quarter (%)", "section": "P3", "label": "最新季流動比率%", "type": "pct", "required": True,
                              "sources": [("financial_ratios.current_ratio_by_quarter.2025Q4", None),
                                          ("_vlm_chart_series_latest", "流動比率|Current Ratio")]},
    "debt_ratio_q4":         {"label_en": "Debt Ratio Latest Quarter (%)", "section": "P3", "label": "最新季負債比率%", "type": "pct", "required": True,
                              "sources": [("financial_ratios.debt_ratio_by_quarter.2025Q4", None),
                                          ("_vlm_chart_series_latest", "負債比率|Debt Ratio")]},
    "q_revenue_series":      {"label_en": "Quarterly Revenue Series", "section": "P3", "label": "季度營收序列", "type": "dict", "required": True,
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
    "cash_position":         {"label_en": "Cash Position (M)", "section": "P7", "label": "現金及約當現金(百萬)", "type": "float", "required": True,
                              "sources": [("_table_search", "現金及約當現金|Cash and Equivalents")]},
    "current_ratio_q_series":{"section": "P7", "label": "流動比率季序列", "type": "dict", "required": True,
                              "sources": [("financial_ratios.current_ratio_by_quarter", None)]},
    "debt_ratio_q_series":   {"section": "P7", "label": "負債比率季序列", "type": "dict", "required": True,
                              "sources": [("financial_ratios.debt_ratio_by_quarter", None)]},
    "debt_due_12m":          {"section": "P7", "label": "12個月內到期債務", "type": "float", "required": False,
                              "sources": [("_table_search", "1年內到期|current portion.*debt")]},
    "total_debt":            {"label_en": "Total Debt (M)", "section": "P7", "label": "總債務(百萬)", "type": "float", "required": False,
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
    "net_debt":              {"label_en": "Net Debt (M)", "section": "P10", "label": "淨債務(百萬)", "type": "float", "required": False,
                              "sources": [("_table_search", "淨債務|Net Debt"),
                                          ("_infer", lambda f: _sub(f.get("total_debt"), f.get("cash_position")))]},
    "net_debt_ebitda":       {"label_en": "Net Debt / EBITDA (x)", "section": "P10", "label": "淨債務/EBITDA(x)", "type": "float", "required": False,
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


# ── Form Field Registry (all 532 FIELD_DEFS fields, S1–S11) ─────────────────
FORM_FIELD_REGISTRY: dict[str, dict] = {
    # Section 1 — Facility Summary & Regulatory Compliance
    'report_type': {'section': 'S1', 'label': '報告類型', 'label_en': 'Report Type', 'type': 'str', 'manual': True},
    'facility_summary.rows': {'section': 'S1', 'label': '1A — 授信項目列表（每行或以「。」分隔一項，接受｜或|分隔符，可含「欄位名稱=值」格式：項次｜借款人全名｜預訂地點｜', 'label_en': '1A — Facility Rows (one per line or separated by 。: ItemNo|BorrowerName|BookingL', 'type': 'list', 'manual': True},
    'facility_summary.totals.total_credit_limit_usd_m': {'section': 'S1', 'label': '1A — 授信總額度 (USD m)', 'label_en': '1A — Total Credit Limit (USD m)', 'type': 'float', 'manual': True},
    'facility_summary.totals.psr_spot_limit_usd_m': {'section': 'S1', 'label': '1A — PSR 即期限額 (USD m)', 'label_en': '1A — PSR Spot Limit (USD m)', 'type': 'float', 'manual': True},
    'facility_summary.outstanding_as_at_date': {'section': 'S1', 'label': '1A — 未償還餘額截止日期', 'label_en': '1A — Outstanding Balance "As at" Date', 'type': 'str', 'manual': True},
    'facility_summary.footnotes': {'section': 'S1', 'label': '1A — 腳注（每行一條：[符號] 說明文字）', 'label_en': '1A — Footnotes (one per line: [Symbol] Text)', 'type': 'list', 'manual': True},
    'facility_summary.appendix_ref': {'section': 'S1', 'label': '1A — 附錄參考', 'label_en': '1A — Appendix Reference', 'type': 'str', 'manual': True},
    'regulatory_compliance.bank_net_worth_twd_bn': {'section': 'S1', 'label': '1B — 銀行淨值 (TWD bn)', 'label_en': '1B — Bank Net Worth (TWD bn)', 'type': 'float', 'manual': True},
    'regulatory_compliance.single_borrower_limit_pct': {'section': 'S1', 'label': '1B — 單一借款人限額 (%)', 'label_en': '1B — Single Borrower Limit (%)', 'type': 'float', 'manual': True},
    'regulatory_compliance.single_borrower_limit_twd_bn': {'section': 'S1', 'label': '1B — 單一借款人限額 (TWD bn)', 'label_en': '1B — Single Borrower Limit (TWD bn)', 'type': 'float', 'manual': True},
    'regulatory_compliance.usd_equivalent_usd_m': {'section': 'S1', 'label': '1B — 單一借款人限額 (USD M equiv.)', 'label_en': '1B — Single Borrower Limit (USD M equiv.)', 'type': 'float', 'manual': True},
    'regulatory_compliance.exchange_rate': {'section': 'S1', 'label': '1B — 匯率 (TWD/USD)', 'label_en': '1B — Exchange Rate (TWD/USD)', 'type': 'float', 'manual': True},
    'regulatory_compliance.exchange_rate_date': {'section': 'S1', 'label': '1B — 匯率日期', 'label_en': '1B — Exchange Rate Date', 'type': 'str', 'manual': True},
    'regulatory_compliance.china_invested_enterprise': {'section': 'S1', 'label': '1B — 中資企業？', 'label_en': '1B — China Invested Enterprise?', 'type': 'bool', 'manual': True},
    'regulatory_compliance.compliance_status': {'section': 'S1', 'label': '1B — 合規狀態', 'label_en': '1B — Compliance Status', 'type': 'str', 'manual': True},
    'regulatory_compliance.unsecured_drawdown_cap_pct': {'section': 'S1', 'label': '1B — 無擔保提款上限 (%)', 'label_en': '1B — Unsecured Drawdown Cap (%)', 'type': 'float', 'manual': True},
    'regulatory_compliance.unsecured_drawdown_cap_usd_m': {'section': 'S1', 'label': '1B — 無擔保提款上限 (USD M)', 'label_en': '1B — Unsecured Drawdown Cap (USD M)', 'type': 'float', 'manual': True},
    'regulatory_compliance.group_limit.approved_group_limit_usd_m': {'section': 'S1', 'label': '1B — 核准集團限額 (USD M)', 'label_en': '1B — Approved Group Limit (USD M)', 'type': 'float', 'manual': True},
    'regulatory_compliance.group_limit.total_proposed_group_utilization_usd_m': {'section': 'S1', 'label': '1B — 擬議集團使用總額 (USD M)', 'label_en': '1B — Total Proposed Group Utilization (USD M)', 'type': 'float', 'manual': True},
    'regulatory_compliance.group_limit.within_limit': {'section': 'S1', 'label': '1B — 集團限額：在限額內？', 'label_en': '1B — Group Limit: Within Limit?', 'type': 'bool', 'manual': True},
    'purpose_and_recommendation.purpose_verbatim': {'section': 'S1', 'label': '1C — 報告目的（逐字）', 'label_en': '1C — Purpose of Report (verbatim)', 'type': 'str', 'manual': True},
    'purpose_and_recommendation.recommendation': {'section': 'S1', 'label': '1C — 建議', 'label_en': '1C — Recommendation', 'type': 'str', 'manual': True},
    'terms_and_conditions.borrower': {'section': 'S1', 'label': '1D — 借款人', 'label_en': '1D — Borrower', 'type': 'str', 'manual': True},
    'terms_and_conditions.guarantors': {'section': 'S1', 'label': '1D — 保證人（每行一位）', 'label_en': '1D — Guarantors (one per line)', 'type': 'list', 'manual': True},
    'terms_and_conditions.lender': {'section': 'S1', 'label': '1D — 貸款人', 'label_en': '1D — Lender', 'type': 'str', 'manual': True},
    'terms_and_conditions.vessel': {'section': 'S1', 'label': '1D — 船舶（條款第4項）', 'label_en': '1D — Vessel (T&C Row 4)', 'type': 'str', 'manual': True},
    'terms_and_conditions.facility_type': {'section': 'S1', 'label': '1D — 授信類型', 'label_en': '1D — Facility Type', 'type': 'str', 'manual': True},
    'terms_and_conditions.facility_amount_usd_m': {'section': 'S1', 'label': '1D — 授信金額 (USD M)', 'label_en': '1D — Facility Amount (USD M)', 'type': 'float', 'manual': True},
    'terms_and_conditions.facility_amount_formula': {'section': 'S1', 'label': '1D — 授信金額公式', 'label_en': '1D — Facility Amount Formula', 'type': 'str', 'manual': True},
    'terms_and_conditions.ltc_percent': {'section': 'S1', 'label': '1D — 貸款成本比 / 預付率 (%)', 'label_en': '1D — LTC / Advance Rate (%)', 'type': 'float', 'manual': True},
    'terms_and_conditions.availability_period': {'section': 'S1', 'label': '1D — 動用期間（條款第8項）', 'label_en': '1D — Availability Period (T&C Row 8)', 'type': 'str', 'manual': True},
    'terms_and_conditions.tenor_years': {'section': 'S1', 'label': '1D — 年期（年）', 'label_en': '1D — Tenor (years)', 'type': 'float', 'manual': True},
    'terms_and_conditions.tenor_structure': {'section': 'S1', 'label': '1D — 年期結構', 'label_en': '1D — Tenor Structure', 'type': 'str', 'manual': True},
    'terms_and_conditions.purpose': {'section': 'S1', 'label': '1D — 授信目的', 'label_en': '1D — Purpose of Facility', 'type': 'str', 'manual': True},
    'terms_and_conditions.repayment_schedule': {'section': 'S1', 'label': '1D — 還款計畫', 'label_en': '1D — Repayment Schedule', 'type': 'str', 'manual': True},
    'terms_and_conditions.balloon_percent': {'section': 'S1', 'label': '1D — 到期氣球款 (%)', 'label_en': '1D — Balloon at Maturity (%)', 'type': 'float', 'manual': True},
    'terms_and_conditions.interest_rate_basis': {'section': 'S1', 'label': '1D — 利率基礎', 'label_en': '1D — Interest Rate Basis', 'type': 'str', 'manual': True},
    'terms_and_conditions.margin_bps': {'section': 'S1', 'label': '1D — 利差 (bps)', 'label_en': '1D — Margin (bps)', 'type': 'float', 'manual': True},
    'terms_and_conditions.interest_period': {'section': 'S1', 'label': '1D — 利息期間', 'label_en': '1D — Interest Period', 'type': 'str', 'manual': True},
    'terms_and_conditions.upfront_fee_pct': {'section': 'S1', 'label': '1D — 安排費 (%)', 'label_en': '1D — Upfront Fee (%)', 'type': 'float', 'manual': True},
    'terms_and_conditions.upfront_fee_usd': {'section': 'S1', 'label': '1D — 安排費 (USD金額)', 'label_en': '1D — Upfront Fee (USD amount)', 'type': 'float', 'manual': True},
    'terms_and_conditions.annual_renewal_fee_usd': {'section': 'S1', 'label': '1D — 年度續約費 (USD)', 'label_en': '1D — Annual Renewal Fee (USD)', 'type': 'float', 'manual': True},
    'terms_and_conditions.security_pre_delivery': {'section': 'S1', 'label': '1D — 交船前擔保品', 'label_en': '1D — Pre-Delivery Security', 'type': 'str', 'manual': True},
    'terms_and_conditions.security_post_delivery': {'section': 'S1', 'label': '1D — 交船後擔保品', 'label_en': '1D — Post-Delivery Security', 'type': 'str', 'manual': True},
    'terms_and_conditions.value_maintenance_clause.acr_minimum_pct': {'section': 'S1', 'label': '1D — 市值維持條款：ACR最低要求 (%)', 'label_en': '1D — VMC: ACR Minimum (%)', 'type': 'float', 'manual': True},
    'terms_and_conditions.value_maintenance_clause.ltv_maximum_pct': {'section': 'S1', 'label': '1D — 市值維持條款：LTV最高上限 (%)', 'label_en': '1D — VMC: LTV Maximum (%)', 'type': 'float', 'manual': True},
    'terms_and_conditions.value_maintenance_clause.testing_frequency': {'section': 'S1', 'label': '1D — 市值維持條款：測試頻率', 'label_en': '1D — VMC: Testing Frequency', 'type': 'str', 'manual': True},
    'terms_and_conditions.value_maintenance_clause.cure_period_days': {'section': 'S1', 'label': '1D — 市值維持條款：救濟期（天）', 'label_en': '1D — VMC: Cure Period (days)', 'type': 'float', 'manual': True},
    'terms_and_conditions.value_maintenance_clause.cure_mechanism': {'section': 'S1', 'label': '1D — 市值維持條款：救濟機制', 'label_en': '1D — VMC: Cure Mechanism', 'type': 'str', 'manual': True},
    'terms_and_conditions.sustainability_linked_kpi.description': {'section': 'S1', 'label': '1D — 永續連結貸款KPI：說明', 'label_en': '1D — SLL KPI: Description', 'type': 'str', 'manual': True},
    'terms_and_conditions.sustainability_linked_kpi.max_margin_ratchet_bps': {'section': 'S1', 'label': '1D — 永續連結貸款KPI：最大利差棘輪 (bps)', 'label_en': '1D — SLL KPI: Max Margin Ratchet (bps)', 'type': 'float', 'manual': True},
    'terms_and_conditions.sustainability_linked_kpi.ratchet_direction': {'section': 'S1', 'label': '1D — 永續連結貸款KPI：棘輪方向', 'label_en': '1D — SLL KPI: Ratchet Direction', 'type': 'str', 'manual': True},
    'terms_and_conditions.financial_covenants': {'section': 'S1', 'label': '1D — 財務契約', 'label_en': '1D — Financial Covenants', 'type': 'str', 'manual': True},
    'terms_and_conditions.drawdown_conditions.max_drawdowns': {'section': 'S1', 'label': '1D — 提款：最大次數', 'label_en': '1D — Drawdown: Max Number', 'type': 'float', 'manual': True},
    'terms_and_conditions.drawdown_conditions.per_drawdown_cap_pct_of_cost': {'section': 'S1', 'label': '1D — 提款：每次上限（成本百分比）', 'label_en': '1D — Drawdown: Cap per Drawdown (% of Cost)', 'type': 'float', 'manual': True},
    'terms_and_conditions.drawdown_conditions.aggregate_cap_usd_m': {'section': 'S1', 'label': '1D — 提款：總上限 (USD m)', 'label_en': '1D — Drawdown: Aggregate Cap (USD m)', 'type': 'float', 'manual': True},
    'terms_and_conditions.drawdown_conditions.pre_delivery_cap_usd_m': {'section': 'S1', 'label': '1D — 提款：交船前上限 (USD m)', 'label_en': '1D — Drawdown: Pre-Delivery Cap (USD m)', 'type': 'float', 'manual': True},
    'terms_and_conditions.drawdown_conditions.commitment_termination_date': {'section': 'S1', 'label': '1D — 承諾終止日', 'label_en': '1D — Commitment Termination Date', 'type': 'str', 'manual': True},
    'terms_and_conditions.conditions_precedent': {'section': 'S1', 'label': '1D — 前提條件', 'label_en': '1D — Conditions Precedent', 'type': 'list', 'manual': True},
    'terms_and_conditions.other_conditions': {'section': 'S1', 'label': '1D — 其他條件（每行一條）', 'label_en': '1D — Other Conditions (one per line)', 'type': 'list', 'manual': True},
    'terms_and_conditions.governing_law': {'section': 'S1', 'label': '1D — 適用法律', 'label_en': '1D — Governing Law', 'type': 'str', 'manual': True},
    'terms_and_conditions.deal_comparison': {'section': 'S1', 'label': '1D — 條款比較（每行一項：條款｜擬議｜前次）', 'label_en': '1D — Deal Comparison (one per line: Term|Proposed|Previous)', 'type': 'list', 'manual': True},
    'account_strategy.wallet.bank_market': {'section': 'S1', 'label': '1F — 業務錢包：銀行市場（年NII/費用）', 'label_en': '1F — Wallet: Bank Market (NII / fees p.a.)', 'type': 'str', 'manual': True},
    'account_strategy.wallet.capital_market': {'section': 'S1', 'label': '1F — 業務錢包：資本市場', 'label_en': '1F — Wallet: Capital Markets', 'type': 'str', 'manual': True},
    'account_strategy.wallet.treasury': {'section': 'S1', 'label': '1F — 業務錢包：財務（外匯╱利率交換）', 'label_en': '1F — Wallet: Treasury (FX / IRS)', 'type': 'str', 'manual': True},
    'account_strategy.wallet.deposit': {'section': 'S1', 'label': '1F — 業務錢包：存款', 'label_en': '1F — Wallet: Deposits', 'type': 'str', 'manual': True},
    'account_strategy.current_relationship': {'section': 'S1', 'label': '1F — 現有關係摘要', 'label_en': '1F — Current Relationship Summary', 'type': 'str', 'manual': True},
    'account_strategy.immediate_opportunities': {'section': 'S1', 'label': '1F — 立即業務機會', 'label_en': '1F — Immediate Opportunities', 'type': 'str', 'manual': True},
    'account_strategy.future_opportunities': {'section': 'S1', 'label': '1F — 未來業務機會', 'label_en': '1F — Future Opportunities', 'type': 'str', 'manual': True},
    'account_strategy.other_opportunities': {'section': 'S1', 'label': '1F — 其他業務機會', 'label_en': '1F — Other Opportunities', 'type': 'str', 'manual': True},
    'sll_kpi_performance.applicable': {'section': 'S1', 'label': '1G — 永續連結貸款KPI績效：適用？', 'label_en': '1G — SLL KPI Performance: Applicable?', 'type': 'bool', 'manual': True},
    'sll_kpi_performance.as_of_date': {'section': 'S1', 'label': '1G — 永續連結貸款KPI績效：截至日期', 'label_en': '1G — SLL KPI Performance: As-of Date', 'type': 'str', 'manual': True},
    'sll_kpi_performance.kpis': {'section': 'S1', 'label': '1G — 永續連結貸款KPI表格（每行一項：KPI名稱｜目標｜實際｜期間｜是否達標｜棘輪bps）', 'label_en': '1G — SLL KPI Table (one per line: KPI Name|Target|Actual|Period|OnTrack|Ratchet ', 'type': 'list', 'manual': True},

    # Section 2 — Credit Overview & Solvency
    '2A_credit_overview.bullets': {'section': 'S2', 'label': '2A — 信貸概覽要點', 'label_en': '2A — Credit Overview Bullets', 'type': 'list', 'manual': True},
    '2A_credit_overview.tariff_impact_paragraphs': {'section': 'S2', 'label': '2A — 關稅影響段落', 'label_en': '2A — Tariff Impact Paragraphs', 'type': 'str', 'manual': True},
    '2B_solvency.primary_repayment_source_verbatim': {'section': 'S2', 'label': '2B — 主要還款來源（逐字）', 'label_en': '2B — Primary Repayment Source (verbatim)', 'type': 'str', 'manual': True},
    '2B_solvency.secondary_repayment_source_verbatim': {'section': 'S2', 'label': '2B — 次要還款來源（逐字）', 'label_en': '2B — Secondary Repayment Source (verbatim)', 'type': 'str', 'manual': True},
    '2B_solvency.ema.period': {'section': 'S2', 'label': '2B — 借款人財務期間', 'label_en': '2B — EMA Financial Period', 'type': 'str', 'manual': True},
    '2B_solvency.ema.cash_bn_usd': {'section': 'S2', 'label': '2B — 借款人現金 (USD bn)', 'label_en': '2B — EMA Cash (USD bn)', 'type': 'float', 'manual': True},
    '2B_solvency.ema.total_debt_bn_usd': {'section': 'S2', 'label': '2B — 借款人總債務 (USD bn)', 'label_en': '2B — EMA Total Debt (USD bn)', 'type': 'float', 'manual': True},
    '2B_solvency.ema.op_ebitda_bn_usd': {'section': 'S2', 'label': '2B — 借款人營業EBITDA (USD bn)', 'label_en': '2B — EMA Operating EBITDA (USD bn)', 'type': 'float', 'manual': True},
    '2B_solvency.ema.debt_ebitda_ratio': {'section': 'S2', 'label': '2B — 借款人負債/EBITDA', 'label_en': '2B — EMA Debt / EBITDA', 'type': 'float', 'manual': True},
    '2B_solvency.ema.interest_coverage': {'section': 'S2', 'label': '2B — 借款人利息覆蓋率（當年）', 'label_en': '2B — EMA Interest Coverage (current year)', 'type': 'float', 'manual': True},
    '2B_solvency.ema.prior_year_coverage': {'section': 'S2', 'label': '2B — 借款人利息覆蓋率（前年）', 'label_en': '2B — EMA Interest Coverage (prior year)', 'type': 'float', 'manual': True},
    '2C_guarantor.guarantor_name_abbrev': {'section': 'S2', 'label': '2C — 保證人簡稱', 'label_en': '2C — Guarantor Abbreviation', 'type': 'str', 'manual': True},
    '2C_guarantor.period': {'section': 'S2', 'label': '2C — 保證人財務期間', 'label_en': '2C — Guarantor Financial Period', 'type': 'str', 'manual': True},
    '2C_guarantor.cash_twd_bn': {'section': 'S2', 'label': '2C — 保證人現金 (TWD bn)', 'label_en': '2C — Guarantor Cash (TWD bn)', 'type': 'float', 'manual': True},
    '2C_guarantor.cash_usd_bn': {'section': 'S2', 'label': '2C — 保證人現金 (USD bn)', 'label_en': '2C — Guarantor Cash (USD bn)', 'type': 'float', 'manual': True},
    '2C_guarantor.total_debt_twd_bn': {'section': 'S2', 'label': '2C — 保證人總債務 (TWD bn)', 'label_en': '2C — Guarantor Total Debt (TWD bn)', 'type': 'float', 'manual': True},
    '2C_guarantor.total_debt_usd_bn': {'section': 'S2', 'label': '2C — 保證人總債務 (USD bn)', 'label_en': '2C — Guarantor Total Debt (USD bn)', 'type': 'float', 'manual': True},
    '2C_guarantor.interest_coverage': {'section': 'S2', 'label': '2C — 保證人利息覆蓋率（當年）', 'label_en': '2C — Guarantor Interest Coverage (current year)', 'type': 'float', 'manual': True},
    '2C_guarantor.prior_year_coverage': {'section': 'S2', 'label': '2C — 保證人利息覆蓋率（前年）', 'label_en': '2C — Guarantor Interest Coverage (prior year)', 'type': 'float', 'manual': True},
    '2C_guarantor.support_history_verbatim': {'section': 'S2', 'label': '2C — 保證人支援歷史（逐字）', 'label_en': '2C — Guarantor Support History (verbatim)', 'type': 'str', 'manual': True},
    '2D_collateral.pre_delivery.issuer_full_name': {'section': 'S2', 'label': '2D — 交船前退款保證開具機構全名', 'label_en': '2D — Pre-Delivery RG Issuer Full Name', 'type': 'str', 'manual': True},
    '2D_collateral.pre_delivery.rating_sp': {'section': 'S2', 'label': '2D — 退款保證開具機構評級 (S&P)', 'label_en': '2D — RG Issuer Rating (S&P)', 'type': 'str', 'manual': True},
    '2D_collateral.pre_delivery.rating_fitch': {'section': 'S2', 'label': '2D — 退款保證開具機構評級 (Fitch)', 'label_en': '2D — RG Issuer Rating (Fitch)', 'type': 'str', 'manual': True},
    '2D_collateral.pre_delivery.coverage_verbatim': {'section': 'S2', 'label': '2D — 交船前擔保覆蓋說明（逐字）', 'label_en': '2D — Pre-Delivery Coverage Description (verbatim)', 'type': 'str', 'manual': True},
    '2D_collateral.pre_delivery.assigned_to_cub': {'section': 'S2', 'label': '2D — 退款保證已讓渡予國泰聯行？', 'label_en': '2D — RG Assigned to CUB?', 'type': 'bool', 'manual': True},
    '2D_collateral.post_delivery.security_type': {'section': 'S2', 'label': '2D — 交船後擔保品類型', 'label_en': '2D — Post-Delivery Security Type', 'type': 'str', 'manual': True},
    '2D_collateral.post_delivery.vessel_spec': {'section': 'S2', 'label': '2D — 船舶規格（交船後）', 'label_en': '2D — Vessel Specification (post-delivery)', 'type': 'str', 'manual': True},
    '2D_collateral.post_delivery.ltc_pct': {'section': 'S2', 'label': '2D — 交付時貸款成本比 (%)', 'label_en': '2D — LTC at Delivery (%)', 'type': 'float', 'manual': True},
    '2D_collateral.post_delivery.acr_pct': {'section': 'S2', 'label': '2D — ACR最低要求 (%)', 'label_en': '2D — ACR Floor (%)', 'type': 'float', 'manual': True},
    '2D_collateral.post_delivery.ltv_pct': {'section': 'S2', 'label': '2D — LTV最高上限 (%)', 'label_en': '2D — LTV Cap (%)', 'type': 'float', 'manual': True},
    '2E_risk_and_mitigants.risk_1_level': {'section': 'S2', 'label': '2E — 風險1：等級', 'label_en': '2E — Risk 1: Level', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_1_title': {'section': 'S2', 'label': '2E — 風險1：標題', 'label_en': '2E — Risk 1: Title', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_1_risk_bullets': {'section': 'S2', 'label': '2E — 風險1：風險要點（每行一條）', 'label_en': '2E — Risk 1: Risk Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.risk_1_mitigant_bullets': {'section': 'S2', 'label': '2E — 風險1：緩解要點（每行一條）', 'label_en': '2E — Risk 1: Mitigant Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.risk_2_level': {'section': 'S2', 'label': '2E — 風險2：等級', 'label_en': '2E — Risk 2: Level', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_2_title': {'section': 'S2', 'label': '2E — 風險2：標題', 'label_en': '2E — Risk 2: Title', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_2_risk_bullets': {'section': 'S2', 'label': '2E — 風險2：風險要點（每行一條）', 'label_en': '2E — Risk 2: Risk Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.risk_2_mitigant_bullets': {'section': 'S2', 'label': '2E — 風險2：緩解要點（每行一條）', 'label_en': '2E — Risk 2: Mitigant Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.risk_3_level': {'section': 'S2', 'label': '2E — 風險3：等級', 'label_en': '2E — Risk 3: Level', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_3_title': {'section': 'S2', 'label': '2E — 風險3：標題', 'label_en': '2E — Risk 3: Title', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_3_risk_bullets': {'section': 'S2', 'label': '2E — 風險3：風險要點（每行一條）', 'label_en': '2E — Risk 3: Risk Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.risk_3_mitigant_bullets': {'section': 'S2', 'label': '2E — 風險3：緩解要點（每行一條）', 'label_en': '2E — Risk 3: Mitigant Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.risk_4_level': {'section': 'S2', 'label': '2E — 風險4：等級', 'label_en': '2E — Risk 4: Level', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_4_title': {'section': 'S2', 'label': '2E — 風險4：標題', 'label_en': '2E — Risk 4: Title', 'type': 'str', 'manual': True},
    '2E_risk_and_mitigants.risk_4_risk_bullets': {'section': 'S2', 'label': '2E — 風險4：風險要點（每行一條）', 'label_en': '2E — Risk 4: Risk Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.risk_4_mitigant_bullets': {'section': 'S2', 'label': '2E — 風險4：緩解要點（每行一條）', 'label_en': '2E — Risk 4: Mitigant Bullets (one per line)', 'type': 'list', 'manual': True},
    '2E_risk_and_mitigants.additional_risk_factors_from_previous': {'section': 'S2', 'label': '2E — 相較前次審查之新增╱額外風險因素', 'label_en': '2E — Additional / New Risk Factors vs Prior Review', 'type': 'str', 'manual': True},
    's2_report_type': {'section': 'S2', 'label': '報告類型', 'label_en': 'Report Type (S2)', 'type': 'str', 'manual': True},

    # Section 3 — Ratings
    '3A_external_ratings.all_nil': {'section': 'S3', 'label': '3A — 所有外部評級：無？', 'label_en': '3A — All External Ratings: NIL?', 'type': 'bool', 'manual': True},
    '3A_external_ratings.ratings': {'section': 'S3', 'label': '3A — 外部評級（每行一項：實體｜標普｜穆迪｜惠譽）', 'label_en': '3A — External Ratings (one per line: Entity|S&P|Moodys|Fitch)', 'type': 'list', 'sources': [('_text_search', r"S&P|Moody's|Fitch|BBB|Ba1|AA-")]},
    '3B_internal_ratings.period_display_labels.fy2022_23': {'section': 'S3', 'label': '3B — MSR期間標籤 — 第1欄（FY2022/23）', 'label_en': '3B — MSR Period Label — Column 1 (FY2022/23)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.period_display_labels.fy2024': {'section': 'S3', 'label': '3B — MSR期間標籤 — 第2欄（FY2024）', 'label_en': '3B — MSR Period Label — Column 2 (FY2024)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.period_display_labels.interim': {'section': 'S3', 'label': '3B — MSR期間標籤 — 第3欄（中期）', 'label_en': '3B — MSR Period Label — Column 3 (Interim)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.period_display_labels.current': {'section': 'S3', 'label': '3B — MSR期間標籤 — 第4欄（當期）', 'label_en': '3B — MSR Period Label — Column 4 (Current)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.borrower_entity_full_name': {'section': 'S3', 'label': '3B — 借款人法定全名（MSR表格）', 'label_en': '3B — Borrower Full Legal Name (MSR Table)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.borrower_entity_abbrev': {'section': 'S3', 'label': '3B — 借款人簡稱（MSR表格）', 'label_en': '3B — Borrower Abbreviation (MSR Table)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.borrower_fy2022_23': {'section': 'S3', 'label': '3B — 借款人MSR — FY2022/23評級', 'label_en': '3B — Borrower MSR — FY2022/23 Rating', 'type': 'str', 'manual': True},
    '3B_internal_ratings.borrower_fy2024': {'section': 'S3', 'label': '3B — 借款人MSR — FY2024評級', 'label_en': '3B — Borrower MSR — FY2024 Rating', 'type': 'str', 'manual': True},
    '3B_internal_ratings.borrower_interim': {'section': 'S3', 'label': '3B — 借款人MSR — 中期評級', 'label_en': '3B — Borrower MSR — Interim Rating', 'type': 'str', 'manual': True},
    '3B_internal_ratings.borrower_current': {'section': 'S3', 'label': '3B — 借款人MSR — 當期評級', 'label_en': '3B — Borrower MSR — Current Rating', 'type': 'str', 'manual': True},
    '3B_internal_ratings.borrower_override_flag': {'section': 'S3', 'label': '3B — 借款人MSR已套用覆核？', 'label_en': '3B — Borrower MSR Override Applied?', 'type': 'bool', 'manual': True},
    '3B_internal_ratings.borrower_override_remarks': {'section': 'S3', 'label': '3B — 借款人覆核說明（6要素逐字）', 'label_en': '3B — Borrower Override Remarks (6-element verbatim)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.guarantor_entity_full_name': {'section': 'S3', 'label': '3B — 保證人法定全名（MSR表格）', 'label_en': '3B — Guarantor Full Legal Name (MSR Table)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.guarantor_entity_abbrev': {'section': 'S3', 'label': '3B — 保證人簡稱（MSR表格）', 'label_en': '3B — Guarantor Abbreviation (MSR Table)', 'type': 'str', 'manual': True},
    '3B_internal_ratings.guarantor_fy2022_23': {'section': 'S3', 'label': '3B — 保證人MSR — FY2022/23評級', 'label_en': '3B — Guarantor MSR — FY2022/23 Rating', 'type': 'str', 'manual': True},
    '3B_internal_ratings.guarantor_fy2024': {'section': 'S3', 'label': '3B — 保證人MSR — FY2024評級', 'label_en': '3B — Guarantor MSR — FY2024 Rating', 'type': 'str', 'manual': True},
    '3B_internal_ratings.guarantor_interim': {'section': 'S3', 'label': '3B — 保證人MSR — 中期評級', 'label_en': '3B — Guarantor MSR — Interim Rating', 'type': 'str', 'manual': True},
    '3B_internal_ratings.guarantor_current': {'section': 'S3', 'label': '3B — 保證人MSR — 當期評級', 'label_en': '3B — Guarantor MSR — Current Rating', 'type': 'str', 'manual': True},
    '3C_mas_612.grade': {'section': 'S3', 'label': '3C — MAS 612貸款分級', 'label_en': '3C — MAS 612 Loan Grade', 'type': 'str', 'manual': True},
    '3C_mas_612.msr_value': {'section': 'S3', 'label': '3C — 評級日期之MSR值', 'label_en': '3C — MSR Value at Grading Date', 'type': 'str', 'manual': True},
    '3C_mas_612.para_1_msr_mapping_verbatim': {'section': 'S3', 'label': '3C — MAS 612第1段 — MSR至分級對應（逐字）', 'label_en': '3C — MAS 612 Para 1 — MSR-to-Grade Mapping (verbatim)', 'type': 'str', 'manual': True},
    '3C_mas_612.para_2_account_conduct_verbatim': {'section': 'S3', 'label': '3C — MAS 612第2段 — 帳戶表現（逐字）', 'label_en': '3C — MAS 612 Para 2 — Account Conduct (verbatim)', 'type': 'str', 'manual': True},
    '3C_mas_612.para_3_financial_profile_verbatim': {'section': 'S3', 'label': '3C — MAS 612第3段 — 財務狀況（逐字）', 'label_en': '3C — MAS 612 Para 3 — Financial Profile (verbatim)', 'type': 'str', 'manual': True},
    '3C_mas_612.para_4_projection_verbatim': {'section': 'S3', 'label': '3C — MAS 612第4段 — 預測與展望（逐字）', 'label_en': '3C — MAS 612 Para 4 — Projection & Outlook (verbatim)', 'type': 'str', 'manual': True},

    # Section 4 — Borrower Profile
    '4A_borrower.company_name_en': {'section': 'S4', 'label': '4A — 公司名稱（英文）', 'label_en': '4A — Company Name (English)', 'type': 'str', 'sources': [('_text_search', r'(?:Company Name|Legal Name)\s*[:\-]\s*(\S.+)')]},
    '4A_borrower.company_name_zh': {'section': 'S4', 'label': '4A — 公司名稱（中文）', 'label_en': '4A — Company Name (Chinese / 中文名稱)', 'type': 'str', 'sources': [('_text_search', r'(?:公司名稱|中文名稱)\s*[：:]\s*(\S.+)')]},
    '4A_borrower.legal_entity_type': {'section': 'S4', 'label': '4A — 法律實體類型', 'label_en': '4A — Legal Entity Type', 'type': 'str', 'sources': [('_text_search', r'Limited|Pte\.?\s*Ltd|Corp|Inc|GmbH')]},
    '4A_borrower.registration_number': {'section': 'S4', 'label': '4A — 登記╱統一企業編號', 'label_en': '4A — Registration / UEN Number', 'type': 'str', 'manual': True},
    '4A_borrower.incorporation_country': {'section': 'S4', 'label': '4A — 設立國家', 'label_en': '4A — Country of Incorporation', 'type': 'str', 'sources': [('_text_search', r'incorporated in|registered in|注册地|成立')]},
    '4A_borrower.incorporation_date': {'section': 'S4', 'label': '4A — 設立日期', 'label_en': '4A — Incorporation Date', 'type': 'str', 'sources': [('_text_search', r'incorporated in|registered in|注册地|成立')]},
    '4A_borrower.fiscal_year_end': {'section': 'S4', 'label': '4A — 財政年度結束日', 'label_en': '4A — Fiscal Year End', 'type': 'str', 'manual': True},
    '4A_borrower.group_auditor': {'section': 'S4', 'label': '4A — 集團核數師', 'label_en': '4A — Group Auditor', 'type': 'str', 'manual': True},
    '4B_ownership.shareholders': {'section': 'S4', 'label': '4B — 股東（每行一項：名稱｜持股%｜國家）', 'label_en': '4B — Shareholders (one per line: Name|Stake%|Country)', 'type': 'list', 'sources': [('_text_search', r'shareholders|shareholding|股東|持股')]},
    '4B_ownership.ultimate_beneficial_owner': {'section': 'S4', 'label': '4B — 最終實益所有人（UBO）', 'label_en': '4B — Ultimate Beneficial Owner (UBO)', 'type': 'str', 'sources': [('_text_search', r'shareholders|shareholding|股東|持股')]},
    '4B_ownership.ubo_stake_pct': {'section': 'S4', 'label': '4B — UBO實際持股 (%)', 'label_en': '4B — UBO Effective Stake (%)', 'type': 'float', 'sources': [('_text_search', r'shareholders|shareholding|股東|持股')]},
    '4C_management.ceo_name': {'section': 'S4', 'label': '4C — 執行長姓名', 'label_en': '4C — CEO Name', 'type': 'str', 'sources': [('_text_search', r'Chief Executive|CEO|Chairman|Managing Director|董事長|總裁')]},
    '4C_management.ceo_title': {'section': 'S4', 'label': '4C — 執行長職稱', 'label_en': '4C — CEO Title', 'type': 'str', 'sources': [('_text_search', r'Chief Executive|CEO|Chairman|Managing Director|董事長|總裁')]},
    '4C_management.ceo_background': {'section': 'S4', 'label': '4C — 執行長背景摘要', 'label_en': '4C — CEO Background Summary', 'type': 'str', 'sources': [('_text_search', r'Chief Executive|CEO|Chairman|Managing Director|董事長|總裁')]},
    '4C_management.cfo_name': {'section': 'S4', 'label': '4C — 財務長姓名', 'label_en': '4C — CFO Name', 'type': 'str', 'manual': True},
    '4C_management.cfo_title': {'section': 'S4', 'label': '4C — 財務長職稱', 'label_en': '4C — CFO Title', 'type': 'str', 'manual': True},
    '4C_management.cfo_background': {'section': 'S4', 'label': '4C — 財務長背景摘要', 'label_en': '4C — CFO Background Summary', 'type': 'str', 'manual': True},
    '4D_business.primary_business': {'section': 'S4', 'label': '4D — 主要業務描述', 'label_en': '4D — Primary Business Description', 'type': 'str', 'sources': [('_text_search', r'principal activities|主要業務|業務範圍')]},
    '4D_business.trade_routes': {'section': 'S4', 'label': '4D — 主要航線╱市場', 'label_en': '4D — Key Trade Routes / Markets', 'type': 'str', 'manual': True},
    '4D_business.operational_model': {'section': 'S4', 'label': '4D — 營運模式', 'label_en': '4D — Operational Model', 'type': 'str', 'manual': True},
    '4D_business.global_ranking': {'section': 'S4', 'label': '4D — 全球市場排名（TEU基準）', 'label_en': '4D — Global Market Ranking (TEU basis)', 'type': 'float', 'manual': True},
    '4E_financials.currency': {'section': 'S4', 'label': '4E — 申報幣別', 'label_en': '4E — Reporting Currency', 'type': 'str', 'manual': True},
    '4E_financials.fiscal_year': {'section': 'S4', 'label': '4E — 財務年度參考', 'label_en': '4E — Financial Year Reference', 'type': 'str', 'manual': True},
    '4E_financials.revenue': {'section': 'S4', 'label': '4E — 收入（最近財年，百萬）', 'label_en': '4E — Revenue (latest FY, millions)', 'type': 'float', 'sources': [('income_statement.revenue_annual', None), ('_table_search', 'Revenue|營業收入')]},
    '4E_financials.ebitda': {'section': 'S4', 'label': '4E — EBITDA（最近財年，百萬）', 'label_en': '4E — EBITDA (latest FY, millions)', 'type': 'float', 'manual': True},
    '4F_fleet.owned_vessel_count': {'section': 'S4', 'label': '4F — 自有船隊：艘數', 'label_en': '4F — Owned Fleet: Vessel Count', 'type': 'float', 'sources': [('_vlm_kpi_search', r'fleet|vessels|船隊')]},
    '4F_fleet.owned_total_teu': {'section': 'S4', 'label': '4F — 自有船隊：總TEU', 'label_en': '4F — Owned Fleet: Total TEU', 'type': 'float', 'sources': [('_vlm_kpi_search', r'fleet|vessels|船隊')]},
    '4F_fleet.chartered_vessel_count': {'section': 'S4', 'label': '4F — 期租船隊：艘數', 'label_en': '4F — Chartered-in Fleet: Vessel Count', 'type': 'float', 'sources': [('_vlm_kpi_search', r'fleet|vessels|船隊')]},
    '4F_fleet.chartered_total_teu': {'section': 'S4', 'label': '4F — 期租船隊：總TEU', 'label_en': '4F — Chartered-in Fleet: Total TEU', 'type': 'float', 'sources': [('_vlm_kpi_search', r'fleet|vessels|船隊')]},
    '4F_fleet.on_order_vessel_count': {'section': 'S4', 'label': '4F — 訂單中：艘數', 'label_en': '4F — On Order: Vessel Count', 'type': 'float', 'sources': [('_vlm_kpi_search', r'fleet|vessels|船隊')]},
    '4F_fleet.on_order_total_teu': {'section': 'S4', 'label': '4F — 訂單中：總TEU', 'label_en': '4F — On Order: Total TEU', 'type': 'float', 'sources': [('_vlm_kpi_search', r'fleet|vessels|船隊')]},
    '4J_peer_comparison': {'section': 'S4', 'label': '4J — 同業比較（每行一項：公司｜船隊TEU｜市佔率%｜聯盟）', 'label_en': '4J — Peer Comparison (one per line: Company|FleetTEU|MarketShare%|Alliance)', 'type': 'list', 'manual': True},

    # Section 5 — Security
    '5A_security_overview.is_secured': {'section': 'S5', 'label': '5A — 授信已擔保？', 'label_en': '5A — Facility Secured?', 'type': 'bool', 'manual': True},
    '5A_security_overview.instr_1_instrument': {'section': 'S5', 'label': '5A — 擔保品#1：工具名稱', 'label_en': '5A — Security #1: Instrument Name', 'type': 'str', 'manual': True},
    '5A_security_overview.instr_1_description': {'section': 'S5', 'label': '5A — 擔保品#1：說明', 'label_en': '5A — Security #1: Description', 'type': 'str', 'manual': True},
    '5A_security_overview.instr_2_instrument': {'section': 'S5', 'label': '5A — 擔保品#2：工具名稱', 'label_en': '5A — Security #2: Instrument Name', 'type': 'str', 'manual': True},
    '5A_security_overview.instr_2_description': {'section': 'S5', 'label': '5A — 擔保品#2：說明', 'label_en': '5A — Security #2: Description', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.applicable': {'section': 'S5', 'label': '5B — 退款保證適用？', 'label_en': '5B — Refund Guarantee Applicable?', 'type': 'bool', 'manual': True},
    '5B_refund_guarantee.issuer_full_name': {'section': 'S5', 'label': '5B — 退款保證開具機構全名', 'label_en': '5B — RG Issuer Full Name', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.issuer_rating': {'section': 'S5', 'label': '5B — 退款保證開具機構信用評級', 'label_en': '5B — RG Issuer Credit Rating', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.rating_agency': {'section': 'S5', 'label': '5B — 退款保證開具機構評級機構', 'label_en': '5B — RG Issuer Rating Agency', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.legal_structure': {'section': 'S5', 'label': '5B — 退款保證法律結構', 'label_en': '5B — RG Legal Structure', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.governing_law': {'section': 'S5', 'label': '5B — 退款保證適用法律', 'label_en': '5B — RG Governing Law', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.assigned_to_cub': {'section': 'S5', 'label': '5B — 退款保證已讓渡予國泰聯行？', 'label_en': '5B — RG Assigned to CUB?', 'type': 'bool', 'manual': True},
    '5B_refund_guarantee.m1_name': {'section': 'S5', 'label': '5B — 里程碑1名稱', 'label_en': '5B — Milestone 1 Name', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m1_date': {'section': 'S5', 'label': '5B — 里程碑1預定日期', 'label_en': '5B — Milestone 1 Scheduled Date', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m1_rg_usd_m': {'section': 'S5', 'label': '5B — 里程碑1退款保證金額 (USD M)', 'label_en': '5B — Milestone 1 RG Amount (USD M)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m1_coverage_pct': {'section': 'S5', 'label': '5B — 里程碑1退款保證覆蓋率 (%)', 'label_en': '5B — Milestone 1 RG Coverage (%)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m1_status': {'section': 'S5', 'label': '5B — 里程碑1狀態', 'label_en': '5B — Milestone 1 Status', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m2_name': {'section': 'S5', 'label': '5B — 里程碑2名稱', 'label_en': '5B — Milestone 2 Name', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m2_date': {'section': 'S5', 'label': '5B — 里程碑2預定日期', 'label_en': '5B — Milestone 2 Scheduled Date', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m2_rg_usd_m': {'section': 'S5', 'label': '5B — 里程碑2退款保證金額 (USD M)', 'label_en': '5B — Milestone 2 RG Amount (USD M)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m2_coverage_pct': {'section': 'S5', 'label': '5B — 里程碑2退款保證覆蓋率 (%)', 'label_en': '5B — Milestone 2 RG Coverage (%)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m2_status': {'section': 'S5', 'label': '5B — 里程碑2狀態', 'label_en': '5B — Milestone 2 Status', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m3_name': {'section': 'S5', 'label': '5B — 里程碑3名稱', 'label_en': '5B — Milestone 3 Name', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m3_date': {'section': 'S5', 'label': '5B — 里程碑3預定日期', 'label_en': '5B — Milestone 3 Scheduled Date', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m3_rg_usd_m': {'section': 'S5', 'label': '5B — 里程碑3退款保證金額 (USD M)', 'label_en': '5B — Milestone 3 RG Amount (USD M)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m3_coverage_pct': {'section': 'S5', 'label': '5B — 里程碑3退款保證覆蓋率 (%)', 'label_en': '5B — Milestone 3 RG Coverage (%)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m3_status': {'section': 'S5', 'label': '5B — 里程碑3狀態', 'label_en': '5B — Milestone 3 Status', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m4_name': {'section': 'S5', 'label': '5B — 里程碑4名稱', 'label_en': '5B — Milestone 4 Name', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m4_date': {'section': 'S5', 'label': '5B — 里程碑4預定日期', 'label_en': '5B — Milestone 4 Scheduled Date', 'type': 'str', 'manual': True},
    '5B_refund_guarantee.m4_rg_usd_m': {'section': 'S5', 'label': '5B — 里程碑4退款保證金額 (USD M)', 'label_en': '5B — Milestone 4 RG Amount (USD M)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m4_coverage_pct': {'section': 'S5', 'label': '5B — 里程碑4退款保證覆蓋率 (%)', 'label_en': '5B — Milestone 4 RG Coverage (%)', 'type': 'float', 'manual': True},
    '5B_refund_guarantee.m4_status': {'section': 'S5', 'label': '5B — 里程碑4狀態', 'label_en': '5B — Milestone 4 Status', 'type': 'str', 'manual': True},
    '5C_vessel_mortgage.applicable': {'section': 'S5', 'label': '5C — 船舶抵押適用？', 'label_en': '5C — Vessel Mortgage Applicable?', 'type': 'bool', 'manual': True},
    '5C_vessel_mortgage.vessel_name': {'section': 'S5', 'label': '5C — 船舶名稱', 'label_en': '5C — Vessel Name', 'type': 'str', 'manual': True},
    '5C_vessel_mortgage.vessel_teu': {'section': 'S5', 'label': '5C — 船舶TEU容量', 'label_en': '5C — Vessel TEU Capacity', 'type': 'float', 'manual': True},
    '5C_vessel_mortgage.valuer': {'section': 'S5', 'label': '5C — 估值機構', 'label_en': '5C — Valuation Firm', 'type': 'str', 'manual': True},
    '5C_vessel_mortgage.market_value_usd_m': {'section': 'S5', 'label': '5C — 船舶市值 (USD M)', 'label_en': '5C — Vessel Market Value (USD M)', 'type': 'float', 'manual': True},
    '5C_vessel_mortgage.contract_price_usd_m': {'section': 'S5', 'label': '5C — 合約╱建造價格 (USD M)', 'label_en': '5C — Contract / Build Price (USD M)', 'type': 'float', 'manual': True},
    '5C_vessel_mortgage.loan_amount_usd_m': {'section': 'S5', 'label': '5C — 貸款金額 (USD M)', 'label_en': '5C — Loan Amount (USD M)', 'type': 'float', 'manual': True},
    '5C_vessel_mortgage.ltc_pct': {'section': 'S5', 'label': '5C — 貸款成本比LTC (%)', 'label_en': '5C — Loan-to-Cost LTC (%)', 'type': 'float', 'manual': True},
    '5C_vessel_mortgage.acr_at_delivery_pct': {'section': 'S5', 'label': '5C — 交付時ACR (%)', 'label_en': '5C — ACR at Delivery (%)', 'type': 'float', 'manual': True},
    '5C_vessel_mortgage.ltv_at_maturity_pct': {'section': 'S5', 'label': '5C — 到期時LTV (%)', 'label_en': '5C — LTV at Maturity (%)', 'type': 'float', 'manual': True},
    '5D_insurance.applicable': {'section': 'S5', 'label': '5D — 保險方案適用？', 'label_en': '5D — Insurance Package Applicable?', 'type': 'bool', 'manual': True},
    '5D_insurance.hm_insurer': {'section': 'S5', 'label': '5D — 船體機械保險：承保人或協會', 'label_en': '5D — H&M Insurance: Insurer or Club', 'type': 'str', 'manual': True},
    '5D_insurance.hm_insured_value_usd_m': {'section': 'S5', 'label': '5D — 船體機械保險：被保金額 (USD M)', 'label_en': '5D — H&M Insurance: Insured Value (USD M)', 'type': 'float', 'manual': True},
    '5D_insurance.hm_notes': {'section': 'S5', 'label': '5D — 船體機械保險：備註', 'label_en': '5D — H&M Insurance: Notes', 'type': 'str', 'manual': True},
    '5D_insurance.pi_insurer': {'section': 'S5', 'label': '5D — 保賠保險：協會', 'label_en': '5D — P&I Insurance: Club', 'type': 'str', 'manual': True},
    '5D_insurance.pi_insured_value_usd_m': {'section': 'S5', 'label': '5D — 保賠保險：被保金額 (USD M)', 'label_en': '5D — P&I Insurance: Insured Value (USD M)', 'type': 'float', 'manual': True},
    '5D_insurance.pi_notes': {'section': 'S5', 'label': '5D — 保賠保險：備註', 'label_en': '5D — P&I Insurance: Notes', 'type': 'str', 'manual': True},
    '5D_insurance.war_insurer': {'section': 'S5', 'label': '5D — 戰爭險：承保人', 'label_en': '5D — War Risk Insurance: Insurer', 'type': 'str', 'manual': True},
    '5D_insurance.war_insured_value_usd_m': {'section': 'S5', 'label': '5D — 戰爭險：被保金額 (USD M)', 'label_en': '5D — War Risk Insurance: Insured Value (USD M)', 'type': 'float', 'manual': True},
    '5D_insurance.war_notes': {'section': 'S5', 'label': '5D — 戰爭險：備註', 'label_en': '5D — War Risk Insurance: Notes', 'type': 'str', 'manual': True},
    '5E_value_maintenance_clause.acr_covenant_pct': {'section': 'S5', 'label': '5E — ACR契約要求 (%)', 'label_en': '5E — ACR Covenant Level (%)', 'type': 'float', 'manual': True},
    '5E_value_maintenance_clause.ltv_covenant_pct': {'section': 'S5', 'label': '5E — LTV契約上限 (%)', 'label_en': '5E — LTV Covenant Level (%)', 'type': 'float', 'manual': True},
    '5E_value_maintenance_clause.test_frequency_verbatim': {'section': 'S5', 'label': '5E — 測試頻率（逐字）', 'label_en': '5E — Test Frequency (verbatim)', 'type': 'str', 'manual': True},
    '5E_value_maintenance_clause.cure_period_banking_days': {'section': 'S5', 'label': '5E — 救濟期（銀行工作日）', 'label_en': '5E — Cure Period (Banking Days)', 'type': 'float', 'manual': True},
    '5E_value_maintenance_clause.cure_mechanism_verbatim': {'section': 'S5', 'label': '5E — 救濟機制（逐字）', 'label_en': '5E — Cure Mechanism (verbatim)', 'type': 'str', 'manual': True},
    '5F_corporate_guarantee.applicable': {'section': 'S5', 'label': '5F — 公司擔保適用？', 'label_en': '5F — Corporate Guarantee Applicable?', 'type': 'bool', 'manual': True},
    '5F_corporate_guarantee.guarantor_full_name': {'section': 'S5', 'label': '5F — 保證人法定全名', 'label_en': '5F — Guarantor Full Legal Name', 'type': 'str', 'manual': True},
    '5F_corporate_guarantee.guarantor_listed_exchange': {'section': 'S5', 'label': '5F — 保證人上市交易所', 'label_en': '5F — Guarantor Listed Exchange', 'type': 'str', 'manual': True},
    '5F_corporate_guarantee.relationship_to_borrower': {'section': 'S5', 'label': '5F — 保證人與借款人關係', 'label_en': '5F — Guarantor Relationship to Borrower', 'type': 'str', 'manual': True},
    '5F_corporate_guarantee.guarantee_scope': {'section': 'S5', 'label': '5F — 擔保範圍（逐字）', 'label_en': '5F — Guarantee Scope (verbatim)', 'type': 'str', 'manual': True},
    '5F_corporate_guarantee.guarantee_covers_predelivery': {'section': 'S5', 'label': '5F — 擔保涵蓋交船前期？', 'label_en': '5F — Guarantee Covers Pre-Delivery Phase?', 'type': 'bool', 'manual': True},
    '5F_corporate_guarantee.guarantee_covers_postdelivery': {'section': 'S5', 'label': '5F — 擔保涵蓋交船後期？', 'label_en': '5F — Guarantee Covers Post-Delivery Phase?', 'type': 'bool', 'manual': True},
    '5F_corporate_guarantee.fx_rate_to_usd': {'section': 'S5', 'label': '5F — 匯率（TWD/USD）用於美元換算', 'label_en': '5F — FX Rate (TWD/USD) for USD conversion', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.cash_twd_bn': {'section': 'S5', 'label': '5F — 保證人現金及等價物 (TWD bn)', 'label_en': '5F — Guarantor Cash & Equivalents (TWD bn)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.cash_usd_bn': {'section': 'S5', 'label': '5F — 保證人現金及等價物 (USD bn)', 'label_en': '5F — Guarantor Cash & Equivalents (USD bn)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.total_debt_twd_bn': {'section': 'S5', 'label': '5F — 保證人總債務 (TWD bn)', 'label_en': '5F — Guarantor Total Debt (TWD bn)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.net_worth_twd_bn': {'section': 'S5', 'label': '5F — 保證人淨值╱股東權益 (TWD bn)', 'label_en': '5F — Guarantor Net Worth / Equity (TWD bn)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.revenue_twd_bn': {'section': 'S5', 'label': '5F — 保證人收入 (TWD bn)', 'label_en': '5F — Guarantor Revenue (TWD bn)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.ebitda_twd_bn': {'section': 'S5', 'label': '5F — 保證人EBITDA (TWD bn)', 'label_en': '5F — Guarantor EBITDA (TWD bn)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.interest_coverage': {'section': 'S5', 'label': '5F — 保證人利息覆蓋率 (倍)', 'label_en': '5F — Guarantor Interest Coverage (x)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.net_margin_pct': {'section': 'S5', 'label': '5F — 保證人淨利率 (%)', 'label_en': '5F — Guarantor Net Margin (%)', 'type': 'float', 'manual': True},
    '5F_corporate_guarantee.roe_pct': {'section': 'S5', 'label': '5F — 保證人股東權益報酬率 (%)', 'label_en': '5F — Guarantor ROE (%)', 'type': 'float', 'manual': True},
    '5G_responsible_person.provided': {'section': 'S5', 'label': '5G — 負責人擔保已提供？', 'label_en': '5G — Responsible Person Guarantee Provided?', 'type': 'bool', 'manual': True},
    '5G_responsible_person.name': {'section': 'S5', 'label': '5G — 負責人姓名', 'label_en': '5G — Responsible Person Name', 'type': 'str', 'manual': True},
    '5G_responsible_person.title': {'section': 'S5', 'label': '5G — 負責人職稱╱職位', 'label_en': '5G — Responsible Person Title / Position', 'type': 'str', 'manual': True},

    # Section 6 — Project / Vessel
    '6A_project.hull_number': {'section': 'S6', 'label': '6A — 船殼號', 'label_en': '6A — Hull Number', 'type': 'str', 'sources': [('_text_search', r'Hull No\.?\s*[:\-]?\s*(\w+)')]},
    '6A_project.vessel_type': {'section': 'S6', 'label': '6A — 船舶類型', 'label_en': '6A — Vessel Type', 'type': 'str', 'sources': [('_text_search', r'Containership|Bulk Carrier|Tanker|vessel type')]},
    '6A_project.teu': {'section': 'S6', 'label': '6A — TEU容量', 'label_en': '6A — TEU Capacity', 'type': 'float', 'sources': [('_vlm_kpi_search', r'TEU|容積')]},
    '6A_project.fuel_type': {'section': 'S6', 'label': '6A — 燃料類型', 'label_en': '6A — Fuel Type', 'type': 'str', 'manual': True},
    '6A_project.imo_tier': {'section': 'S6', 'label': '6A — IMO等級╱環保標準', 'label_en': '6A — IMO Tier / Environmental Standard', 'type': 'str', 'manual': True},
    '6A_project.dwt': {'section': 'S6', 'label': '6A — DWT（載重噸）', 'label_en': '6A — DWT (Deadweight Tonnage)', 'type': 'float', 'manual': True},
    '6A_project.loa_m': {'section': 'S6', 'label': '6A — 船長（全長，公尺）', 'label_en': '6A — LOA — Length Overall (metres)', 'type': 'float', 'manual': True},
    '6A_project.beam_m': {'section': 'S6', 'label': '6A — 船寬（公尺）', 'label_en': '6A — Beam (metres)', 'type': 'float', 'manual': True},
    '6A_project.speed_knots': {'section': 'S6', 'label': '6A — 設計速度（節）', 'label_en': '6A — Design Speed (knots)', 'type': 'float', 'manual': True},
    '6A_project.class_society': {'section': 'S6', 'label': '6A — 船級社', 'label_en': '6A — Classification Society', 'type': 'str', 'manual': True},
    '6A_project.flag_state': {'section': 'S6', 'label': '6A — 船旗國', 'label_en': '6A — Flag State', 'type': 'str', 'sources': [('_text_search', r'Flag State|船旗國|registered under')]},
    '6A_project.contract_price_usd_m': {'section': 'S6', 'label': '6A — 造船合約價格 (USD M)', 'label_en': '6A — Shipbuilding Contract Price (USD M)', 'type': 'float', 'sources': [('_text_search', r'Contract Price|合約金額|Contract Value')]},
    '6A_project.loan_amount_usd_m': {'section': 'S6', 'label': '6A — 國泰聯行貸款金額 (USD M)', 'label_en': '6A — CUB Loan Amount (USD M)', 'type': 'float', 'manual': True},
    '6A_project.ltc_pct': {'section': 'S6', 'label': '6A — 貸款成本比LTC (%)', 'label_en': '6A — Loan-to-Cost LTC (%)', 'type': 'float', 'manual': True},
    '6A_project.delivery_date': {'section': 'S6', 'label': '6A — 預計交船日期', 'label_en': '6A — Expected Delivery Date', 'type': 'str', 'sources': [('_text_search', r'delivery date|交付日期|Expected Delivery')]},
    '6A_project.grace_period_days': {'section': 'S6', 'label': '6A — 合約寬限期（天）', 'label_en': '6A — Contractual Grace Period (days)', 'type': 'float', 'manual': True},
    '6B_builder.name': {'section': 'S6', 'label': '6B — 造船廠╱建造商名稱', 'label_en': '6B — Shipyard / Builder Name', 'type': 'str', 'sources': [('_text_search', r'Builder|Shipyard|建造廠商|Samsung|Hyundai|DSME')]},
    '6B_builder.founded': {'section': 'S6', 'label': '6B — 成立年份', 'label_en': '6B — Year Founded', 'type': 'str', 'sources': [('_text_search', r'Builder|Shipyard|建造廠商|Samsung|Hyundai|DSME')]},
    '6B_builder.hq': {'section': 'S6', 'label': '6B — 總部所在地', 'label_en': '6B — Headquarters Location', 'type': 'str', 'sources': [('_text_search', r'Builder|Shipyard|建造廠商|Samsung|Hyundai|DSME')]},
    '6B_builder.market_position': {'section': 'S6', 'label': '6B — 市場地位', 'label_en': '6B — Market Position', 'type': 'str', 'sources': [('_text_search', r'Builder|Shipyard|建造廠商|Samsung|Hyundai|DSME')]},
    '6B_builder.track_record_verbatim': {'section': 'S6', 'label': '6B — 業績記錄（逐字）', 'label_en': '6B — Track Record (verbatim — years / vessel sizes / on-time rate)', 'type': 'str', 'sources': [('_text_search', r'Builder|Shipyard|建造廠商|Samsung|Hyundai|DSME')]},
    '6B_builder.ontime_delivery_pct': {'section': 'S6', 'label': '6B — 準時交付率 (%)', 'label_en': '6B — On-Time Delivery Rate (%)', 'type': 'float', 'sources': [('_text_search', r'Builder|Shipyard|建造廠商|Samsung|Hyundai|DSME')]},
    '6B_builder.technology_overlap_verbatim': {'section': 'S6', 'label': '6B — 技術合作說明（逐字）', 'label_en': '6B — Technology Overlap Narrative (verbatim)', 'type': 'str', 'sources': [('_text_search', r'Builder|Shipyard|建造廠商|Samsung|Hyundai|DSME')]},
    '6C_contract.contract_type': {'section': 'S6', 'label': '6C — 合約類型', 'label_en': '6C — Contract Type', 'type': 'str', 'manual': True},
    '6C_contract.buyer': {'section': 'S6', 'label': '6C — 買方名稱', 'label_en': '6C — Buyer Name', 'type': 'str', 'manual': True},
    '6C_contract.builder': {'section': 'S6', 'label': '6C — 建造商╱賣方名稱', 'label_en': '6C — Builder / Seller Name', 'type': 'str', 'manual': True},
    '6C_contract.price_verbatim': {'section': 'S6', 'label': '6C — 合約價格（逐字）', 'label_en': '6C — Contract Price (verbatim)', 'type': 'str', 'manual': True},
    '6C_contract.contract_date': {'section': 'S6', 'label': '6C — 合約日期', 'label_en': '6C — Contract Date', 'type': 'str', 'sources': [('_text_search', r'Contract Date|合約日期')]},
    '6C_contract.expected_delivery': {'section': 'S6', 'label': '6C — 預計交船日期', 'label_en': '6C — Expected Delivery Date', 'type': 'str', 'manual': True},
    '6C_contract.grace_period': {'section': 'S6', 'label': '6C — 寬限期', 'label_en': '6C — Grace Period', 'type': 'str', 'manual': True},
    '6C_contract.late_delivery_penalty_verbatim': {'section': 'S6', 'label': '6C — 遲延交付罰款（逐字）', 'label_en': '6C — Late Delivery Penalty (verbatim)', 'type': 'str', 'manual': True},
    '6C_contract.buyer_termination_verbatim': {'section': 'S6', 'label': '6C — 買方終止權（逐字）', 'label_en': '6C — Buyer Termination Right (verbatim)', 'type': 'str', 'manual': True},
    '6D_milestones.m1_name': {'section': 'S6', 'label': '6D — 里程碑1名稱', 'label_en': '6D — Milestone 1 Name', 'type': 'str', 'manual': True},
    '6D_milestones.m1_date': {'section': 'S6', 'label': '6D — 里程碑1預計日期', 'label_en': '6D — Milestone 1 Expected Date', 'type': 'str', 'manual': True},
    '6D_milestones.m1_pct': {'section': 'S6', 'label': '6D — 里程碑1合約價格百分比', 'label_en': '6D — Milestone 1 % of Contract Price', 'type': 'float', 'manual': True},
    '6D_milestones.m1_amount_usd_m': {'section': 'S6', 'label': '6D — 里程碑1付款金額 (USD M)', 'label_en': '6D — Milestone 1 Payment Amount (USD M)', 'type': 'float', 'manual': True},
    '6D_milestones.m2_name': {'section': 'S6', 'label': '6D — 里程碑2名稱', 'label_en': '6D — Milestone 2 Name', 'type': 'str', 'manual': True},
    '6D_milestones.m2_date': {'section': 'S6', 'label': '6D — 里程碑2預計日期', 'label_en': '6D — Milestone 2 Expected Date', 'type': 'str', 'manual': True},
    '6D_milestones.m2_pct': {'section': 'S6', 'label': '6D — 里程碑2合約價格百分比', 'label_en': '6D — Milestone 2 % of Contract Price', 'type': 'float', 'manual': True},
    '6D_milestones.m2_amount_usd_m': {'section': 'S6', 'label': '6D — 里程碑2付款金額 (USD M)', 'label_en': '6D — Milestone 2 Payment Amount (USD M)', 'type': 'float', 'manual': True},
    '6D_milestones.m3_name': {'section': 'S6', 'label': '6D — 里程碑3名稱', 'label_en': '6D — Milestone 3 Name', 'type': 'str', 'manual': True},
    '6D_milestones.m3_date': {'section': 'S6', 'label': '6D — 里程碑3預計日期', 'label_en': '6D — Milestone 3 Expected Date', 'type': 'str', 'manual': True},
    '6D_milestones.m3_pct': {'section': 'S6', 'label': '6D — 里程碑3合約價格百分比', 'label_en': '6D — Milestone 3 % of Contract Price', 'type': 'float', 'manual': True},
    '6D_milestones.m3_amount_usd_m': {'section': 'S6', 'label': '6D — 里程碑3付款金額 (USD M)', 'label_en': '6D — Milestone 3 Payment Amount (USD M)', 'type': 'float', 'manual': True},
    '6D_milestones.m4_name': {'section': 'S6', 'label': '6D — 里程碑4名稱', 'label_en': '6D — Milestone 4 Name', 'type': 'str', 'manual': True},
    '6D_milestones.m4_date': {'section': 'S6', 'label': '6D — 里程碑4預計日期', 'label_en': '6D — Milestone 4 Expected Date', 'type': 'str', 'manual': True},
    '6D_milestones.m4_pct': {'section': 'S6', 'label': '6D — 里程碑4合約價格百分比', 'label_en': '6D — Milestone 4 % of Contract Price', 'type': 'float', 'manual': True},
    '6D_milestones.m4_amount_usd_m': {'section': 'S6', 'label': '6D — 里程碑4付款金額 (USD M)', 'label_en': '6D — Milestone 4 Payment Amount (USD M)', 'type': 'float', 'manual': True},
    '6D_milestones.banking_act_commentary': {'section': 'S6', 'label': '6D — 銀行法第33-3條說明（逐字）', 'label_en': '6D — Banking Act s33-3 Commentary (verbatim)', 'type': 'str', 'manual': True},
    '6E_rg_mechanism.applicable': {'section': 'S6', 'label': '6E — 退款保證機制適用？', 'label_en': '6E — RG Mechanism Applicable?', 'type': 'bool', 'manual': True},
    '6E_rg_mechanism.issuer_full_name': {'section': 'S6', 'label': '6E — 退款保證開具機構全名', 'label_en': '6E — RG Issuer Full Name', 'type': 'str', 'manual': True},
    '6E_rg_mechanism.issuer_rating_verbatim': {'section': 'S6', 'label': '6E — 退款保證開具機構評級（逐字）', 'label_en': '6E — RG Issuer Rating (verbatim)', 'type': 'str', 'manual': True},
    '6E_rg_mechanism.format_verbatim': {'section': 'S6', 'label': '6E — 退款保證格式（逐字）', 'label_en': '6E — RG Format (verbatim)', 'type': 'str', 'manual': True},
    '6E_rg_mechanism.governing_law': {'section': 'S6', 'label': '6E — 退款保證適用法律', 'label_en': '6E — RG Governing Law', 'type': 'str', 'manual': True},
    '6E_rg_mechanism.trigger_events': {'section': 'S6', 'label': '6E — 退款保證觸發事件（每行一條）', 'label_en': '6E — RG Trigger Events (one per line)', 'type': 'list', 'manual': True},
    '6E_rg_mechanism.claim_process_verbatim': {'section': 'S6', 'label': '6E — 索賠流程（逐字）', 'label_en': '6E — Claim Process (verbatim)', 'type': 'str', 'manual': True},
    '6E_rg_mechanism.coverage_summary_min_pct': {'section': 'S6', 'label': '6E — 最低退款保證覆蓋率 (%)', 'label_en': '6E — Minimum RG Coverage (%)', 'type': 'float', 'manual': True},

    # Section 7 — Financial Statements
    'entities_to_analyze.borrower_name': {'section': 'S7', 'label': '實體 — 借款人法定全名', 'label_en': 'Entities — Borrower Full Legal Name', 'type': 'str', 'sources': [('_text_search', r'(?:Company|Legal Name)\s*[:\-]\s*(\S.+)')]},
    'entities_to_analyze.borrower_currency': {'section': 'S7', 'label': '實體 — 借款人申報幣別', 'label_en': 'Entities — Borrower Reporting Currency', 'type': 'str', 'manual': True},
    'entities_to_analyze.borrower_unit': {'section': 'S7', 'label': '實體 — 借款人財務單位', 'label_en': 'Entities — Borrower Financial Unit', 'type': 'str', 'manual': True},
    'entities_to_analyze.guarantor_name': {'section': 'S7', 'label': '實體 — 保證人法定全名', 'label_en': 'Entities — Guarantor Full Legal Name', 'type': 'str', 'manual': True},
    'entities_to_analyze.guarantor_currency': {'section': 'S7', 'label': '實體 — 保證人申報幣別', 'label_en': 'Entities — Guarantor Reporting Currency', 'type': 'str', 'manual': True},
    'entities_to_analyze.guarantor_exists': {'section': 'S7', 'label': '實體 — 需要保證人財務分析？', 'label_en': 'Entities — Guarantor Financial Analysis Required?', 'type': 'bool', 'manual': True},
    '7A_borrower_financials.reporting_entity': {'section': 'S7', 'label': '7A — 申報實體名稱', 'label_en': '7A — Reporting Entity Name', 'type': 'str', 'sources': [('_table_search', '7A — Reporting Entity Name')]},
    '7A_borrower_financials.auditor': {'section': 'S7', 'label': '7A — 核數師', 'label_en': '7A — Auditor', 'type': 'str', 'sources': [('_table_search', '7A — Auditor')]},
    '7A_borrower_financials.audit_opinion': {'section': 'S7', 'label': '7A — 審計意見', 'label_en': '7A — Audit Opinion', 'type': 'str', 'sources': [('_table_search', '7A — Audit Opinion')]},
    '7A_borrower_financials.accounting_standard': {'section': 'S7', 'label': '7A — 會計準則', 'label_en': '7A — Accounting Standard', 'type': 'str', 'sources': [('_table_search', '7A — Accounting Standard')]},
    '7A_borrower_financials.fiscal_year_end': {'section': 'S7', 'label': '7A — 財政年度結束日', 'label_en': '7A — Fiscal Year End', 'type': 'str', 'sources': [('_table_search', '7A — Fiscal Year End')]},
    '7A_borrower_financials.reporting_currency': {'section': 'S7', 'label': '7A — 申報幣別', 'label_en': '7A — Reporting Currency', 'type': 'str', 'sources': [('_table_search', '7A — Reporting Currency')]},
    '7A_borrower_financials.unit': {'section': 'S7', 'label': '7A — 財務單位', 'label_en': '7A — Financial Unit', 'type': 'str', 'sources': [('_table_search', '7A — Financial Unit')]},
    '7A_borrower_financials.revenue_fy2022': {'section': 'S7', 'label': '7A — 收入 FY2022', 'label_en': '7A — Revenue FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Revenue FY2022')]},
    '7A_borrower_financials.ebitda_fy2022': {'section': 'S7', 'label': '7A — EBITDA FY2022', 'label_en': '7A — EBITDA FY2022', 'type': 'float', 'sources': [('_table_search', '7A — EBITDA FY2022')]},
    '7A_borrower_financials.op_profit_fy2022': {'section': 'S7', 'label': '7A — 營業利潤 FY2022', 'label_en': '7A — Operating Profit FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Operating Profit FY2022')]},
    '7A_borrower_financials.interest_expense_fy2022': {'section': 'S7', 'label': '7A — 利息費用 FY2022', 'label_en': '7A — Interest Expense FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Interest Expense FY2022')]},
    '7A_borrower_financials.net_income_fy2022': {'section': 'S7', 'label': '7A — 淨利 FY2022', 'label_en': '7A — Net Income FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Net Income FY2022')]},
    '7A_borrower_financials.revenue_fy2023': {'section': 'S7', 'label': '7A — 收入 FY2023', 'label_en': '7A — Revenue FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Revenue FY2023')]},
    '7A_borrower_financials.ebitda_fy2023': {'section': 'S7', 'label': '7A — EBITDA FY2023', 'label_en': '7A — EBITDA FY2023', 'type': 'float', 'sources': [('_table_search', '7A — EBITDA FY2023')]},
    '7A_borrower_financials.op_profit_fy2023': {'section': 'S7', 'label': '7A — 營業利潤 FY2023', 'label_en': '7A — Operating Profit FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Operating Profit FY2023')]},
    '7A_borrower_financials.interest_expense_fy2023': {'section': 'S7', 'label': '7A — 利息費用 FY2023', 'label_en': '7A — Interest Expense FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Interest Expense FY2023')]},
    '7A_borrower_financials.net_income_fy2023': {'section': 'S7', 'label': '7A — 淨利 FY2023', 'label_en': '7A — Net Income FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Net Income FY2023')]},
    '7A_borrower_financials.revenue_fy2024': {'section': 'S7', 'label': '7A — 收入 FY2024', 'label_en': '7A — Revenue FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Revenue FY2024')]},
    '7A_borrower_financials.ebitda_fy2024': {'section': 'S7', 'label': '7A — EBITDA FY2024', 'label_en': '7A — EBITDA FY2024', 'type': 'float', 'sources': [('_table_search', '7A — EBITDA FY2024')]},
    '7A_borrower_financials.depreciation_fy2022': {'section': 'S7', 'label': '7A — 折舊及攤銷 FY2022', 'label_en': '7A — Depreciation & Amortisation FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Depreciation & Amortisati')]},
    '7A_borrower_financials.depreciation_fy2023': {'section': 'S7', 'label': '7A — 折舊及攤銷 FY2023', 'label_en': '7A — Depreciation & Amortisation FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Depreciation & Amortisati')]},
    '7A_borrower_financials.depreciation_fy2024': {'section': 'S7', 'label': '7A — 折舊及攤銷 FY2024', 'label_en': '7A — Depreciation & Amortisation FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Depreciation & Amortisati')]},
    '7A_borrower_financials.op_profit_fy2024': {'section': 'S7', 'label': '7A — 營業利潤 FY2024', 'label_en': '7A — Operating Profit FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Operating Profit FY2024')]},
    '7A_borrower_financials.interest_expense_fy2024': {'section': 'S7', 'label': '7A — 利息費用 FY2024', 'label_en': '7A — Interest Expense FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Interest Expense FY2024')]},
    '7A_borrower_financials.net_income_fy2024': {'section': 'S7', 'label': '7A — 淨利 FY2024', 'label_en': '7A — Net Income FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Net Income FY2024')]},
    '7A_borrower_financials.bs_cash': {'section': 'S7', 'label': '7A — 現金及等價物 FY2024（資產負債表）', 'label_en': '7A — Cash & Equivalents FY2024 (Balance Sheet)', 'type': 'float', 'sources': [('_table_search', '7A — Cash & Equivalents FY2024')]},
    '7A_borrower_financials.bs_total_ca': {'section': 'S7', 'label': '7A — 流動資產合計 FY2024', 'label_en': '7A — Total Current Assets FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Current Assets FY20')]},
    '7A_borrower_financials.bs_total_nca': {'section': 'S7', 'label': '7A — 非流動資產合計 FY2024', 'label_en': '7A — Total Non-Current Assets FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Non-Current Assets ')]},
    '7A_borrower_financials.bs_total_assets': {'section': 'S7', 'label': '7A — 資產總計 FY2024', 'label_en': '7A — Total Assets FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Assets FY2024')]},
    '7A_borrower_financials.bs_total_cl': {'section': 'S7', 'label': '7A — 流動負債合計 FY2024', 'label_en': '7A — Total Current Liabilities FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Current Liabilities')]},
    '7A_borrower_financials.bs_total_ncl': {'section': 'S7', 'label': '7A — 非流動負債合計 FY2024', 'label_en': '7A — Total Non-Current Liabilities FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Non-Current Liabili')]},
    '7A_borrower_financials.bs_total_liabilities': {'section': 'S7', 'label': '7A — 負債總計 FY2024', 'label_en': '7A — Total Liabilities FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Liabilities FY2024')]},
    '7A_borrower_financials.bs_total_equity': {'section': 'S7', 'label': '7A — 股東權益合計 FY2024', 'label_en': '7A — Total Equity FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Equity FY2024')]},
    '7A_borrower_financials.cf_ocf': {'section': 'S7', 'label': '7A — 營業現金流 FY2024', 'label_en': '7A — Operating Cash Flow FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Operating Cash Flow FY202')]},
    '7A_borrower_financials.cf_capex': {'section': 'S7', 'label': '7A — 資本支出 FY2024', 'label_en': '7A — Capital Expenditure FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Capital Expenditure FY202')]},
    '7A_borrower_financials.cf_fcf': {'section': 'S7', 'label': '7A — 自由現金流 FY2024', 'label_en': '7A — Free Cash Flow FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Free Cash Flow FY2024')]},
    '7A_borrower_financials.cogs_fy2022': {'section': 'S7', 'label': '7A — 銷貨成本╱營業費用 FY2022', 'label_en': '7A — Cost of Sales / Operating Expenses FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Cost of Sales / Operating')]},
    '7A_borrower_financials.cogs_fy2023': {'section': 'S7', 'label': '7A — 銷貨成本╱營業費用 FY2023', 'label_en': '7A — Cost of Sales / Operating Expenses FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Cost of Sales / Operating')]},
    '7A_borrower_financials.cogs_fy2024': {'section': 'S7', 'label': '7A — 銷貨成本╱營業費用 FY2024', 'label_en': '7A — Cost of Sales / Operating Expenses FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Cost of Sales / Operating')]},
    '7A_borrower_financials.total_debt_fy2022': {'section': 'S7', 'label': '7A — 有息負債合計 FY2022（短+長期借款+租賃）', 'label_en': '7A — Total Debt FY2022 (ST+LT borrowings+lease)', 'type': 'float', 'sources': [('_table_search', '7A — Total Debt FY2022 (ST+LT ')]},
    '7A_borrower_financials.total_debt_fy2023': {'section': 'S7', 'label': '7A — 有息負債合計 FY2023', 'label_en': '7A — Total Debt FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Total Debt FY2023')]},
    '7A_borrower_financials.total_debt_fy2024': {'section': 'S7', 'label': '7A — 有息負債合計 FY2024', 'label_en': '7A — Total Debt FY2024', 'type': 'float', 'sources': [('_table_search', '7A — Total Debt FY2024')]},
    '7A_borrower_financials.bs_cash_fy2022': {'section': 'S7', 'label': '7A — 現金及等價物 FY2022', 'label_en': '7A — Cash & Equivalents FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Cash & Equivalents FY2022')]},
    '7A_borrower_financials.bs_total_assets_fy2022': {'section': 'S7', 'label': '7A — 資產總計 FY2022', 'label_en': '7A — Total Assets FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Total Assets FY2022')]},
    '7A_borrower_financials.bs_total_equity_fy2022': {'section': 'S7', 'label': '7A — 股東權益合計 FY2022', 'label_en': '7A — Total Equity FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Total Equity FY2022')]},
    '7A_borrower_financials.cf_ocf_fy2022': {'section': 'S7', 'label': '7A — 營業現金流 FY2022', 'label_en': '7A — Operating Cash Flow FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Operating Cash Flow FY202')]},
    '7A_borrower_financials.cf_fcf_fy2022': {'section': 'S7', 'label': '7A — 自由現金流 FY2022', 'label_en': '7A — Free Cash Flow FY2022', 'type': 'float', 'sources': [('_table_search', '7A — Free Cash Flow FY2022')]},
    '7A_borrower_financials.bs_cash_fy2023': {'section': 'S7', 'label': '7A — 現金及等價物 FY2023', 'label_en': '7A — Cash & Equivalents FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Cash & Equivalents FY2023')]},
    '7A_borrower_financials.bs_total_assets_fy2023': {'section': 'S7', 'label': '7A — 資產總計 FY2023', 'label_en': '7A — Total Assets FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Total Assets FY2023')]},
    '7A_borrower_financials.bs_total_equity_fy2023': {'section': 'S7', 'label': '7A — 股東權益合計 FY2023', 'label_en': '7A — Total Equity FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Total Equity FY2023')]},
    '7A_borrower_financials.cf_ocf_fy2023': {'section': 'S7', 'label': '7A — 營業現金流 FY2023', 'label_en': '7A — Operating Cash Flow FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Operating Cash Flow FY202')]},
    '7A_borrower_financials.cf_fcf_fy2023': {'section': 'S7', 'label': '7A — 自由現金流 FY2023', 'label_en': '7A — Free Cash Flow FY2023', 'type': 'float', 'sources': [('_table_search', '7A — Free Cash Flow FY2023')]},
    '7A_borrower_financials.full_pl': {'section': 'S7', 'label': '7A — 完整損益表（每行一項：項目｜FY2022｜FY2023｜FY2024）— 選填，用於完整12行表格', 'label_en': '7A — Full P&L Statement (one row per line: Item|FY2022|FY2023|FY2024) — optional', 'type': 'list', 'sources': [('_table_search', '7A — Full P&L Statement (one r')]},
    '7A_borrower_financials.full_bs': {'section': 'S7', 'label': '7A — 完整資產負債表（每行一項：項目｜FY2022｜FY2023｜FY2024）— 選填，用於完整20行表格', 'label_en': '7A — Full Balance Sheet (one row per line: Item|FY2022|FY2023|FY2024) — optional', 'type': 'list', 'sources': [('_table_search', '7A — Full Balance Sheet (one r')]},
    '7A_borrower_financials.full_cf': {'section': 'S7', 'label': '7A — 完整現金流量表（每行一項：項目｜FY2022｜FY2023｜FY2024）— 選填，用於完整7行表格', 'label_en': '7A — Full Cash Flow Statement (one row per line: Item|FY2022|FY2023|FY2024) — op', 'type': 'list', 'sources': [('_table_search', '7A — Full Cash Flow Statement ')]},
    '7B_key_ratios.fy2022_debt_ebitda': {'section': 'S7', 'label': '7B — 負債/EBITDA FY2022 (倍)', 'label_en': '7B — Debt/EBITDA FY2022 (x)', 'type': 'float', 'sources': [('_table_search', '7B — Debt/EBITDA FY2022 (')]},
    '7B_key_ratios.fy2022_interest_coverage': {'section': 'S7', 'label': '7B — 利息覆蓋率 FY2022 (倍)', 'label_en': '7B — Interest Coverage FY2022 (x)', 'type': 'float', 'sources': [('_table_search', '7B — Interest Coverage FY')]},
    '7B_key_ratios.fy2022_dscr': {'section': 'S7', 'label': '7B — 債務服務覆蓋率 FY2022 (倍)', 'label_en': '7B — DSCR FY2022 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'DSCR'), ('_table_search', 'DSCR')]},
    '7B_key_ratios.fy2022_current_ratio': {'section': 'S7', 'label': '7B — 流動比率 FY2022 (倍)', 'label_en': '7B — Current Ratio FY2022 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'Current Ratio|流動比率'), ('_table_search', 'Current Ratio')]},
    '7B_key_ratios.fy2022_net_margin_pct': {'section': 'S7', 'label': '7B — 淨利率 FY2022 (%)', 'label_en': '7B — Net Margin FY2022 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Net Margin FY2022 (%')]},
    '7B_key_ratios.fy2023_debt_ebitda': {'section': 'S7', 'label': '7B — 負債/EBITDA FY2023 (倍)', 'label_en': '7B — Debt/EBITDA FY2023 (x)', 'type': 'float', 'sources': [('_table_search', '7B — Debt/EBITDA FY2023 (')]},
    '7B_key_ratios.fy2023_interest_coverage': {'section': 'S7', 'label': '7B — 利息覆蓋率 FY2023 (倍)', 'label_en': '7B — Interest Coverage FY2023 (x)', 'type': 'float', 'sources': [('_table_search', '7B — Interest Coverage FY')]},
    '7B_key_ratios.fy2023_dscr': {'section': 'S7', 'label': '7B — 債務服務覆蓋率 FY2023 (倍)', 'label_en': '7B — DSCR FY2023 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'DSCR'), ('_table_search', 'DSCR')]},
    '7B_key_ratios.fy2023_current_ratio': {'section': 'S7', 'label': '7B — 流動比率 FY2023 (倍)', 'label_en': '7B — Current Ratio FY2023 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'Current Ratio|流動比率'), ('_table_search', 'Current Ratio')]},
    '7B_key_ratios.fy2023_net_margin_pct': {'section': 'S7', 'label': '7B — 淨利率 FY2023 (%)', 'label_en': '7B — Net Margin FY2023 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Net Margin FY2023 (%')]},
    '7B_key_ratios.fy2024_debt_ebitda': {'section': 'S7', 'label': '7B — 負債/EBITDA FY2024 (倍)', 'label_en': '7B — Debt/EBITDA FY2024 (x)', 'type': 'float', 'sources': [('_table_search', '7B — Debt/EBITDA FY2024 (')]},
    '7B_key_ratios.fy2024_interest_coverage': {'section': 'S7', 'label': '7B — 利息覆蓋率 FY2024 (倍)', 'label_en': '7B — Interest Coverage FY2024 (x)', 'type': 'float', 'sources': [('_table_search', '7B — Interest Coverage FY')]},
    '7B_key_ratios.fy2024_dscr': {'section': 'S7', 'label': '7B — 債務服務覆蓋率 FY2024 (倍)', 'label_en': '7B — DSCR FY2024 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'DSCR'), ('_table_search', 'DSCR')]},
    '7B_key_ratios.fy2024_current_ratio': {'section': 'S7', 'label': '7B — 流動比率 FY2024 (倍)', 'label_en': '7B — Current Ratio FY2024 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'Current Ratio|流動比率'), ('_table_search', 'Current Ratio')]},
    '7B_key_ratios.fy2024_net_margin_pct': {'section': 'S7', 'label': '7B — 淨利率 FY2024 (%)', 'label_en': '7B — Net Margin FY2024 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Net Margin FY2024 (%')]},
    '7B_key_ratios.fy2022_gross_margin_pct': {'section': 'S7', 'label': '7B — 毛利率 FY2022 (%)', 'label_en': '7B — Gross Margin FY2022 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Gross Margin FY2022 ')]},
    '7B_key_ratios.fy2023_gross_margin_pct': {'section': 'S7', 'label': '7B — 毛利率 FY2023 (%)', 'label_en': '7B — Gross Margin FY2023 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Gross Margin FY2023 ')]},
    '7B_key_ratios.fy2024_gross_margin_pct': {'section': 'S7', 'label': '7B — 毛利率 FY2024 (%)', 'label_en': '7B — Gross Margin FY2024 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Gross Margin FY2024 ')]},
    '7B_key_ratios.fy2022_ebitda_margin_pct': {'section': 'S7', 'label': '7B — EBITDA利潤率 FY2022 (%)', 'label_en': '7B — EBITDA Margin FY2022 (%)', 'type': 'float', 'sources': [('_table_search', '7B — EBITDA Margin FY2022')]},
    '7B_key_ratios.fy2023_ebitda_margin_pct': {'section': 'S7', 'label': '7B — EBITDA利潤率 FY2023 (%)', 'label_en': '7B — EBITDA Margin FY2023 (%)', 'type': 'float', 'sources': [('_table_search', '7B — EBITDA Margin FY2023')]},
    '7B_key_ratios.fy2024_ebitda_margin_pct': {'section': 'S7', 'label': '7B — EBITDA利潤率 FY2024 (%)', 'label_en': '7B — EBITDA Margin FY2024 (%)', 'type': 'float', 'sources': [('_table_search', '7B — EBITDA Margin FY2024')]},
    '7B_key_ratios.fy2022_op_margin_pct': {'section': 'S7', 'label': '7B — 營業利潤率 FY2022 (%)', 'label_en': '7B — Operating Margin FY2022 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Operating Margin FY2')]},
    '7B_key_ratios.fy2023_op_margin_pct': {'section': 'S7', 'label': '7B — 營業利潤率 FY2023 (%)', 'label_en': '7B — Operating Margin FY2023 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Operating Margin FY2')]},
    '7B_key_ratios.fy2024_op_margin_pct': {'section': 'S7', 'label': '7B — 營業利潤率 FY2024 (%)', 'label_en': '7B — Operating Margin FY2024 (%)', 'type': 'float', 'sources': [('_table_search', '7B — Operating Margin FY2')]},
    '7B_key_ratios.fy2022_roa_pct': {'section': 'S7', 'label': '7B — 資產報酬率 FY2022 (%)', 'label_en': '7B — ROA FY2022 (%)', 'type': 'float', 'sources': [('_table_search', '7B — ROA FY2022 (%)')]},
    '7B_key_ratios.fy2023_roa_pct': {'section': 'S7', 'label': '7B — 資產報酬率 FY2023 (%)', 'label_en': '7B — ROA FY2023 (%)', 'type': 'float', 'sources': [('_table_search', '7B — ROA FY2023 (%)')]},
    '7B_key_ratios.fy2024_roa_pct': {'section': 'S7', 'label': '7B — 資產報酬率 FY2024 (%)', 'label_en': '7B — ROA FY2024 (%)', 'type': 'float', 'sources': [('_table_search', '7B — ROA FY2024 (%)')]},
    '7B_key_ratios.fy2022_roe_pct': {'section': 'S7', 'label': '7B — 股東權益報酬率 FY2022 (%)', 'label_en': '7B — ROE FY2022 (%)', 'type': 'float', 'sources': [('_table_search', '7B — ROE FY2022 (%)')]},
    '7B_key_ratios.fy2023_roe_pct': {'section': 'S7', 'label': '7B — 股東權益報酬率 FY2023 (%)', 'label_en': '7B — ROE FY2023 (%)', 'type': 'float', 'sources': [('_table_search', '7B — ROE FY2023 (%)')]},
    '7B_key_ratios.fy2024_roe_pct': {'section': 'S7', 'label': '7B — 股東權益報酬率 FY2024 (%)', 'label_en': '7B — ROE FY2024 (%)', 'type': 'float', 'sources': [('_table_search', '7B — ROE FY2024 (%)')]},
    '7B_key_ratios.fy2022_debt_equity': {'section': 'S7', 'label': '7B — 負債╱股東權益 FY2022 (倍)', 'label_en': '7B — Debt/Equity FY2022 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'Gearing|D/E|Debt.*Equity')]},
    '7B_key_ratios.fy2023_debt_equity': {'section': 'S7', 'label': '7B — 負債╱股東權益 FY2023 (倍)', 'label_en': '7B — Debt/Equity FY2023 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'Gearing|D/E|Debt.*Equity')]},
    '7B_key_ratios.fy2024_debt_equity': {'section': 'S7', 'label': '7B — 負債╱股東權益 FY2024 (倍)', 'label_en': '7B — Debt/Equity FY2024 (x)', 'type': 'float', 'sources': [('_vlm_chart_series_latest', 'Gearing|D/E|Debt.*Equity')]},
    '7B_key_ratios.fy2022_net_debt': {'section': 'S7', 'label': '7B — 淨負債 FY2022（與財務報表同單位）', 'label_en': '7B — Net Debt FY2022 (same unit as financials)', 'type': 'float', 'sources': [('_table_search', '7B — Net Debt FY2022 (sam')]},
    '7B_key_ratios.fy2023_net_debt': {'section': 'S7', 'label': '7B — 淨負債 FY2023（與財務報表同單位）', 'label_en': '7B — Net Debt FY2023 (same unit as financials)', 'type': 'float', 'sources': [('_table_search', '7B — Net Debt FY2023 (sam')]},
    '7B_key_ratios.fy2024_net_debt': {'section': 'S7', 'label': '7B — 淨負債 FY2024（與財務報表同單位）', 'label_en': '7B — Net Debt FY2024 (same unit as financials)', 'type': 'float', 'sources': [('_table_search', '7B — Net Debt FY2024 (sam')]},
    '7B_key_ratios.fy2022_ocf_total_debt': {'section': 'S7', 'label': '7B — 營業現金流╱有息負債 FY2022 (倍)', 'label_en': '7B — OCF / Total Debt FY2022 (x)', 'type': 'float', 'sources': [('_table_search', '7B — OCF / Total Debt FY2')]},
    '7B_key_ratios.fy2023_ocf_total_debt': {'section': 'S7', 'label': '7B — 營業現金流╱有息負債 FY2023 (倍)', 'label_en': '7B — OCF / Total Debt FY2023 (x)', 'type': 'float', 'sources': [('_table_search', '7B — OCF / Total Debt FY2')]},
    '7B_key_ratios.fy2024_ocf_total_debt': {'section': 'S7', 'label': '7B — 營業現金流╱有息負債 FY2024 (倍)', 'label_en': '7B — OCF / Total Debt FY2024 (x)', 'type': 'float', 'sources': [('_table_search', '7B — OCF / Total Debt FY2')]},
    '7B_key_ratios.fy2022_ocf_interest': {'section': 'S7', 'label': '7B — 營業現金流╱利息費用 FY2022 (倍)', 'label_en': '7B — OCF / Interest FY2022 (x)', 'type': 'float', 'sources': [('_table_search', '7B — OCF / Interest FY202')]},
    '7B_key_ratios.fy2023_ocf_interest': {'section': 'S7', 'label': '7B — 營業現金流╱利息費用 FY2023 (倍)', 'label_en': '7B — OCF / Interest FY2023 (x)', 'type': 'float', 'sources': [('_table_search', '7B — OCF / Interest FY202')]},
    '7B_key_ratios.fy2024_ocf_interest': {'section': 'S7', 'label': '7B — 營業現金流╱利息費用 FY2024 (倍)', 'label_en': '7B — OCF / Interest FY2024 (x)', 'type': 'float', 'sources': [('_table_search', '7B — OCF / Interest FY202')]},
    '7B_key_ratios.fy2022_ar_days': {'section': 'S7', 'label': '7B — 應收帳款天數 FY2022 (天)', 'label_en': '7B — AR Days FY2022 (days)', 'type': 'float', 'sources': [('_table_search', '7B — AR Days FY2022 (days')]},
    '7B_key_ratios.fy2023_ar_days': {'section': 'S7', 'label': '7B — 應收帳款天數 FY2023 (天)', 'label_en': '7B — AR Days FY2023 (days)', 'type': 'float', 'sources': [('_table_search', '7B — AR Days FY2023 (days')]},
    '7B_key_ratios.fy2024_ar_days': {'section': 'S7', 'label': '7B — 應收帳款天數 FY2024 (天)', 'label_en': '7B — AR Days FY2024 (days)', 'type': 'float', 'sources': [('_table_search', '7B — AR Days FY2024 (days')]},
    '7B_key_ratios.fy2022_ap_days': {'section': 'S7', 'label': '7B — 應付帳款天數 FY2022 (天)', 'label_en': '7B — AP Days FY2022 (days)', 'type': 'float', 'sources': [('_table_search', '7B — AP Days FY2022 (days')]},
    '7B_key_ratios.fy2023_ap_days': {'section': 'S7', 'label': '7B — 應付帳款天數 FY2023 (天)', 'label_en': '7B — AP Days FY2023 (days)', 'type': 'float', 'sources': [('_table_search', '7B — AP Days FY2023 (days')]},
    '7B_key_ratios.fy2024_ap_days': {'section': 'S7', 'label': '7B — 應付帳款天數 FY2024 (天)', 'label_en': '7B — AP Days FY2024 (days)', 'type': 'float', 'sources': [('_table_search', '7B — AP Days FY2024 (days')]},
    '7B_key_ratios.fy2022_inventory_days': {'section': 'S7', 'label': '7B — 存貨天數 FY2022（天，如適用）', 'label_en': '7B — Inventory Days FY2022 (days, if applicable)', 'type': 'float', 'sources': [('_table_search', '7B — Inventory Days FY202')]},
    '7B_key_ratios.fy2023_inventory_days': {'section': 'S7', 'label': '7B — 存貨天數 FY2023（天，如適用）', 'label_en': '7B — Inventory Days FY2023 (days, if applicable)', 'type': 'float', 'sources': [('_table_search', '7B — Inventory Days FY202')]},
    '7B_key_ratios.fy2024_inventory_days': {'section': 'S7', 'label': '7B — 存貨天數 FY2024（天，如適用）', 'label_en': '7B — Inventory Days FY2024 (days, if applicable)', 'type': 'float', 'sources': [('_table_search', '7B — Inventory Days FY202')]},
    '7C_guarantor_financials.applicable': {'section': 'S7', 'label': '7C — 保證人財務分析適用？', 'label_en': '7C — Guarantor Financial Analysis Applicable?', 'type': 'bool', 'sources': [('_table_search', '7C — Guarantor Financial Analy')]},
    '7C_guarantor_financials.reporting_currency': {'section': 'S7', 'label': '7C — 保證人申報幣別', 'label_en': '7C — Guarantor Reporting Currency', 'type': 'str', 'sources': [('_table_search', '7C — Guarantor Reporting Curre')]},
    '7C_guarantor_financials.revenue_fy2024': {'section': 'S7', 'label': '7C — 保證人收入 FY2024', 'label_en': '7C — Guarantor Revenue FY2024', 'type': 'float', 'sources': [('_table_search', '7C — Guarantor Revenue FY2024')]},
    '7C_guarantor_financials.ebitda_fy2024': {'section': 'S7', 'label': '7C — 保證人EBITDA FY2024', 'label_en': '7C — Guarantor EBITDA FY2024', 'type': 'float', 'sources': [('_table_search', '7C — Guarantor EBITDA FY2024')]},
    '7C_guarantor_financials.net_income_fy2024': {'section': 'S7', 'label': '7C — 保證人淨利 FY2024', 'label_en': '7C — Guarantor Net Income FY2024', 'type': 'float', 'sources': [('_table_search', '7C — Guarantor Net Income FY20')]},
    '7C_guarantor_financials.cash_fy2024': {'section': 'S7', 'label': '7C — 保證人現金及等價物 FY2024', 'label_en': '7C — Guarantor Cash & Equivalents FY2024', 'type': 'float', 'sources': [('_table_search', '7C — Guarantor Cash & Equivale')]},
    '7C_guarantor_financials.total_assets_fy2024': {'section': 'S7', 'label': '7C — 保證人資產總計 FY2024', 'label_en': '7C — Guarantor Total Assets FY2024', 'type': 'float', 'sources': [('_table_search', '7C — Guarantor Total Assets FY')]},
    '7C_guarantor_financials.total_equity_fy2024': {'section': 'S7', 'label': '7C — 保證人股東權益合計 FY2024', 'label_en': '7C — Guarantor Total Equity FY2024', 'type': 'float', 'sources': [('_table_search', '7C — Guarantor Total Equity FY')]},
    '7C_guarantor_financials.ocf_fy2024': {'section': 'S7', 'label': '7C — 保證人營業現金流 FY2024', 'label_en': '7C — Guarantor Operating Cash Flow FY2024', 'type': 'float', 'sources': [('_table_search', '7C — Guarantor Operating Cash ')]},
    '7E_base_case.applicable': {'section': 'S7', 'label': '7E — 基本情境預測適用？', 'label_en': '7E — Base Case Projections Applicable?', 'type': 'bool', 'sources': [('_table_search', '7E — Base Case Projections App')]},
    '7E_base_case.key_assumptions': {'section': 'S7', 'label': '7E — 主要假設（每行一項：假設｜數值｜來源）', 'label_en': '7E — Key Assumptions (one per line: Assumption|Value|Source)', 'type': 'list', 'sources': [('_table_search', '7E — Key Assumptions (one per ')]},
    '7E_base_case.min_dscr': {'section': 'S7', 'label': '7E — 基本情境最低DSCR (倍)', 'label_en': '7E — Minimum Base Case DSCR (x)', 'type': 'float', 'sources': [('_table_search', '7E — Minimum Base Case DSCR (x')]},
    '7E_base_case.conclusion': {'section': 'S7', 'label': '7E — 基本情境結論（逐字）', 'label_en': '7E — Base Case Conclusion (verbatim)', 'type': 'str', 'sources': [('_table_search', '7E — Base Case Conclusion (ver')]},
    '7F_worse_case.applicable': {'section': 'S7', 'label': '7F — 壓力情境預測適用？', 'label_en': '7F — Worse Case Projections Applicable?', 'type': 'bool', 'sources': [('_table_search', '7F — Worse Case Projections Ap')]},
    '7F_worse_case.stressed_min_dscr': {'section': 'S7', 'label': '7F — 壓力情境最低DSCR (倍)', 'label_en': '7F — Stressed Minimum DSCR (x)', 'type': 'float', 'sources': [('_table_search', '7F — Stressed Minimum DSCR (x)')]},
    '7F_worse_case.key_stress_assumptions': {'section': 'S7', 'label': '7F — 主要壓力假設（每行一項：假設｜基本情境｜壓力情境）', 'label_en': '7F — Key Stress Assumptions (one per line: Assumption|Base Case|Stressed)', 'type': 'list', 'sources': [('_table_search', '7F — Key Stress Assumptions (o')]},
    '7F_worse_case.conclusion': {'section': 'S7', 'label': '7F — 壓力情境結論（逐字）', 'label_en': '7F — Worse Case Conclusion (verbatim)', 'type': 'str', 'sources': [('_table_search', '7F — Worse Case Conclusion (ve')]},

    # Section 8 — ACRA / Banking Charges
    '8A_acra_banking_charges.section_applicability': {'section': 'S8', 'label': '8A — 適用性', 'label_en': '8A — Applicability', 'type': 'str', 'manual': True},
    '8A_acra_banking_charges.acra_data_available': {'section': 'S8', 'label': '8A — ACRA資料可取得？', 'label_en': '8A — ACRA Data Available?', 'type': 'bool', 'manual': True},
    '8A_acra_banking_charges.search_date': {'section': 'S8', 'label': '8A — ACRA查詢日期', 'label_en': '8A — ACRA Search Date', 'type': 'str', 'manual': True},
    '8A_acra_banking_charges.entity_name': {'section': 'S8', 'label': '8A — 實體名稱（依ACRA）', 'label_en': '8A — Entity Name (as per ACRA)', 'type': 'str', 'manual': True},
    '8A_acra_banking_charges.uen': {'section': 'S8', 'label': '8A — 統一企業編號', 'label_en': '8A — UEN', 'type': 'str', 'manual': True},
    '8A_acra_banking_charges.jurisdiction': {'section': 'S8', 'label': '8A — 司法管轄區', 'label_en': '8A — Jurisdiction', 'type': 'str', 'manual': True},
    '8A_acra_banking_charges.charges': {'section': 'S8', 'label': '8A — 抵押登記（每行一條：受押人｜日期｜金額USD M｜狀態）', 'label_en': '8A — Charges (one per line: Chargee|Date|Amount USD M|Status)', 'type': 'list', 'manual': True},
    '8A_acra_banking_charges.total_charges': {'section': 'S8', 'label': '8A — 抵押登記總數', 'label_en': '8A — Total Charges Count', 'type': 'float', 'manual': True},
    '8A_acra_banking_charges.active_charges': {'section': 'S8', 'label': '8A — 有效抵押登記數', 'label_en': '8A — Active Charges Count', 'type': 'float', 'manual': True},
    '8A_acra_banking_charges.satisfied_charges': {'section': 'S8', 'label': '8A — 已清償抵押登記數', 'label_en': '8A — Satisfied Charges Count', 'type': 'float', 'manual': True},
    '8A_acra_banking_charges.total_active_usd_m': {'section': 'S8', 'label': '8A — 有效總金額 (USD m)', 'label_en': '8A — Total Active Amount (USD m)', 'type': 'float', 'manual': True},
    '8A_acra_banking_charges.cub_charge_count': {'section': 'S8', 'label': '8A — 國泰聯行抵押登記數', 'label_en': '8A — CUB Charges Count', 'type': 'float', 'manual': True},
    '8A_acra_banking_charges.cub_total_usd_m': {'section': 'S8', 'label': '8A — 國泰聯行總計 (USD m)', 'label_en': '8A — CUB Total (USD m)', 'type': 'float', 'manual': True},
    '8A_acra_banking_charges.analyst_commentary': {'section': 'S8', 'label': '8A — 分析師評語（3-5個要點）', 'label_en': '8A — Analyst Commentary (3-5 bullets)', 'type': 'str', 'manual': True},
    '8A_acra_banking_charges.new_deal_forward_looking': {'section': 'S8', 'label': '8A — 納入前瞻性要點（新融資）？', 'label_en': '8A — Include Forward-Looking Bullet (new facility)?', 'type': 'bool', 'manual': True},
    '8B_other_information.applicable': {'section': 'S8', 'label': '8B — 其他資訊適用？', 'label_en': '8B — Other Information Applicable?', 'type': 'bool', 'manual': True},
    '8B_other_information.litigation': {'section': 'S8', 'label': '8B — 訴訟（每行一條：案件｜司法管轄區｜狀態｜責任USD M）', 'label_en': '8B — Litigation (one per line: Case|Jurisdiction|Status|Liability USD M)', 'type': 'list', 'manual': True},
    '8B_other_information.sanctions_ofac': {'section': 'S8', 'label': '8B — OFAC制裁狀態', 'label_en': '8B — OFAC Sanctions Status', 'type': 'str', 'manual': True},
    '8B_other_information.sanctions_mas': {'section': 'S8', 'label': '8B — MAS制裁狀態', 'label_en': '8B — MAS Sanctions Status', 'type': 'str', 'manual': True},
    '8B_other_information.esg_controversies': {'section': 'S8', 'label': '8B — ESG爭議（每行一條：主題｜嚴重程度｜備註）', 'label_en': '8B — ESG Controversies (one per line: Topic|Severity|Notes)', 'type': 'list', 'manual': True},
    '8B_other_information.regulatory_actions': {'section': 'S8', 'label': '8B — 監管行動╱執法令', 'label_en': '8B — Regulatory Actions / Enforcement Orders', 'type': 'str', 'manual': True},
    '8B_other_information.material_events': {'section': 'S8', 'label': '8B — 上次審查後重大事項', 'label_en': '8B — Material Events Since Last Review', 'type': 'str', 'manual': True},

    # Section 9 — Compliance Checklist
    '9A_checklist.items': {'section': 'S9', 'label': '9A — 23項核查清單（每行一項：項目｜是/否｜備註）', 'label_en': '9A — 23-Item Checklist (one per line: Item|Yes/No|Remarks)', 'type': 'list', 'manual': True},
    '9A_checklist.kyc_aml_cleared': {'section': 'S9', 'label': '9A — KYC╱洗錢防制已清核？', 'label_en': '9A — KYC / AML Cleared?', 'type': 'bool', 'manual': True},
    '9A_checklist.esg_screen_passed': {'section': 'S9', 'label': '9A — ESG篩選通過？', 'label_en': '9A — ESG Screening Passed?', 'type': 'bool', 'manual': True},
    '9A_checklist.mas612_classification': {'section': 'S9', 'label': '9A — MAS 612分類', 'label_en': '9A — MAS 612 Classification', 'type': 'str', 'manual': True},
    '9A_checklist.item15_pre_delivery_usd_m': {'section': 'S9', 'label': '9A — 第15項：交船前無擔保金額 (USD m)', 'label_en': '9A — Item 15: Pre-Delivery Unsecured Amount (USD m)', 'type': 'float', 'manual': True},
    '9A_checklist.item15_exemption_basis': {'section': 'S9', 'label': '9A — 第15項：第33-3條豁免依據', 'label_en': '9A — Item 15: s.33-3 Exemption Basis', 'type': 'str', 'manual': True},
    '9A_checklist.item16_search_date': {'section': 'S9', 'label': '9A — 第16項：ACRA查詢日期', 'label_en': '9A — Item 16: ACRA Search Date', 'type': 'str', 'manual': True},
    '9A_checklist.item16_entity_name': {'section': 'S9', 'label': '9A — 第16項：實體名稱', 'label_en': '9A — Item 16: Entity Name', 'type': 'str', 'manual': True},
    '9A_checklist.item16_uen': {'section': 'S9', 'label': '9A — 第16項：統一企業編號', 'label_en': '9A — Item 16: UEN', 'type': 'str', 'manual': True},
    '9B_conditions_covenants.conditions_precedent': {'section': 'S9', 'label': '9B — 前提條件（每行一條：說明｜測試）', 'label_en': '9B — Conditions Precedent (one per line: Description|Testing)', 'type': 'list', 'manual': True},
    '9B_conditions_covenants.ongoing_covenants': {'section': 'S9', 'label': '9B — 持續契約（每行一條：說明｜門檻｜測試）', 'label_en': '9B — Ongoing Covenants (one per line: Description|Threshold|Testing)', 'type': 'list', 'manual': True},
    '9B_conditions_covenants.financial_covenants': {'section': 'S9', 'label': '9B — 財務契約', 'label_en': '9B — Financial Covenants', 'type': 'str', 'manual': True},
    '9C_recommendation.decision': {'section': 'S9', 'label': '9C — 決議', 'label_en': '9C — Decision', 'type': 'str', 'manual': True},
    '9C_recommendation.facility_amount_usd_m': {'section': 'S9', 'label': '9C — 授信金額 (USD m)', 'label_en': '9C — Facility Amount (USD m)', 'type': 'float', 'manual': True},
    '9C_recommendation.tenor_years': {'section': 'S9', 'label': '9C — 年期（年）', 'label_en': '9C — Tenor (years)', 'type': 'float', 'manual': True},
    '9C_recommendation.security_structure': {'section': 'S9', 'label': '9C — 擔保結構摘要', 'label_en': '9C — Security Structure Summary', 'type': 'str', 'manual': True},
    '9C_recommendation.key_conditions': {'section': 'S9', 'label': '9C — 主要條件（每行一條）', 'label_en': '9C — Key Conditions (one per line)', 'type': 'list', 'manual': True},
    '9C_recommendation.balloon_ltv_pct': {'section': 'S9', 'label': '9C — 氣球款LTV%（上限75%）', 'label_en': '9C — Balloon LTV % (cap 75%)', 'type': 'float', 'manual': True},
    '9D_signoff.prepared_by': {'section': 'S9', 'label': '9D — 準備人（姓名、職稱）', 'label_en': '9D — Prepared By (Name, Title)', 'type': 'str', 'manual': True},
    '9D_signoff.reviewed_by': {'section': 'S9', 'label': '9D — 審核人（姓名、職稱）', 'label_en': '9D — Reviewed By (Name, Title)', 'type': 'str', 'manual': True},

    # Section 10 — Group Exposure
    '10A_group_exposure.entity_group': {'section': 'S10', 'label': '10A — 實體集團名稱', 'label_en': '10A — Entity Group Name', 'type': 'str', 'manual': True},
    '10A_group_exposure.currency': {'section': 'S10', 'label': '10A — 幣別', 'label_en': '10A — Currency', 'type': 'str', 'manual': True},
    '10A_group_exposure.as_of_date': {'section': 'S10', 'label': '10A — 截止日期', 'label_en': '10A — As-of Date', 'type': 'str', 'manual': True},
    '10A_group_exposure.approved_group_limit_usd_m': {'section': 'S10', 'label': '10A — 核准集團限額 (USD m)', 'label_en': '10A — Approved Group Limit (USD m)', 'type': 'float', 'manual': True},
    '10A_group_exposure.rows': {'section': 'S10', 'label': '10A — 風險敞口列表（每行一項：實體｜分行｜類型｜核准USD M｜擬議USD M｜未償USD M｜擔保品｜保證人｜到', 'label_en': '10A — Exposure Rows (one per line: Entity|Branch|Type|Approved USD M|Proposed US', 'type': 'list', 'manual': True},
    '10A_group_exposure.proposed_exposure_usd_m': {'section': 'S10', 'label': '10A — 擬議總風險敞口 (USD m)', 'label_en': '10A — Total Proposed Exposure (USD m)', 'type': 'float', 'manual': True},
    '10A_group_exposure.existing_exposure_usd_m': {'section': 'S10', 'label': '10A — 現有未償金額 (USD m)', 'label_en': '10A — Existing Outstanding (USD m)', 'type': 'float', 'manual': True},
    '10A_group_exposure.eva_note': {'section': 'S10', 'label': '10A — 姊妹公司備註', 'label_en': '10A — Sister Company Note', 'type': 'str', 'manual': True},
    '10B_fleet_growth.group_name': {'section': 'S10', 'label': '10B — 集團名稱', 'label_en': '10B — Group Name', 'type': 'str', 'manual': True},
    '10B_fleet_growth.year_range': {'section': 'S10', 'label': '10B — 年份範圍', 'label_en': '10B — Year Range', 'type': 'str', 'manual': True},
    '10B_fleet_growth.rows': {'section': 'S10', 'label': '10B — 船隊增長列表（每行一項：年份｜自有TEU M｜總TEU M｜總艘數｜自有%）', 'label_en': '10B — Fleet Growth Rows (one per line: Year|Owned TEU M|Total TEU M|Total Vessel', 'type': 'list', 'manual': True},
    '10B_fleet_growth.cagr_pct': {'section': 'S10', 'label': '10B — 複合年增長率 (%)', 'label_en': '10B — CAGR (%)', 'type': 'float', 'manual': True},
    '10B_fleet_growth.chart_reference': {'section': 'S10', 'label': '10B — 圖表來源參考', 'label_en': '10B — Chart Source Reference', 'type': 'str', 'manual': True},
    '10B_fleet_growth.target_capacity_note': {'section': 'S10', 'label': '10B — 目標容量備註', 'label_en': '10B — Target Capacity Note', 'type': 'str', 'manual': True},
    '10B_fleet_growth.key_notes': {'section': 'S10', 'label': '10B — 重要備註（每行一條）', 'label_en': '10B — Key Notes (one per line)', 'type': 'list', 'manual': True},
    '10C_projections.entity_name': {'section': 'S10', 'label': '10C — 實體名稱', 'label_en': '10C — Entity Name', 'type': 'str', 'manual': True},
    '10C_projections.basis': {'section': 'S10', 'label': '10C — 基礎', 'label_en': '10C — Basis', 'type': 'str', 'manual': True},
    '10C_projections.currency': {'section': 'S10', 'label': '10C — 幣別', 'label_en': '10C — Currency', 'type': 'str', 'manual': True},
    '10C_projections.unit': {'section': 'S10', 'label': '10C — 單位', 'label_en': '10C — Unit', 'type': 'str', 'manual': True},
    '10C_projections.key_assumptions': {'section': 'S10', 'label': '10C — 主要假設（每行一項：假設｜FY2026E｜FY2027E｜FY2028E）', 'label_en': '10C — Key Assumptions (one per line: Assumption|FY2026E|FY2027E|FY2028E)', 'type': 'list', 'manual': True},
    '10C_projections.assumptions_narrative': {'section': 'S10', 'label': '10C — 假設說明', 'label_en': '10C — Assumptions Narrative', 'type': 'str', 'manual': True},
    '10C_projections.base_case_pl': {'section': 'S10', 'label': '10C — 基本情境損益表（每行一項：項目｜FY2026E｜FY2027E｜FY2028E｜小計？）', 'label_en': '10C — Base Case P&L (one per line: Item|FY2026E|FY2027E|FY2028E|Subtotal?)', 'type': 'list', 'manual': True},
    '10C_projections.base_case_bs': {'section': 'S10', 'label': '10C — 基本情境資產負債表（每行一項：項目｜FY2026E｜FY2027E｜FY2028E｜小計？）', 'label_en': '10C — Base Case Balance Sheet (one per line: Item|FY2026E|FY2027E|FY2028E|Subtot', 'type': 'list', 'manual': True},
    '10C_projections.base_case_cf': {'section': 'S10', 'label': '10C — 基本情境現金流量表（每行一項：項目｜FY2026E｜FY2027E｜FY2028E｜小計？）', 'label_en': '10C — Base Case Cash Flow (one per line: Item|FY2026E|FY2027E|FY2028E|Subtotal?)', 'type': 'list', 'manual': True},
    '10C_projections.base_case_dscr': {'section': 'S10', 'label': '10C — 基本情境DSCR（每行一項：年份｜營業現金流｜償債額｜DSCR）', 'label_en': '10C — Base Case DSCR (one per line: Year|OCF|Debt Service|DSCR)', 'type': 'list', 'manual': True},
    '10C_projections.dscr_commentary': {'section': 'S10', 'label': '10C — DSCR說明', 'label_en': '10C — DSCR Commentary', 'type': 'str', 'manual': True},
    '10C_projections.stress_assumptions': {'section': 'S10', 'label': '10C — 壓力假設（每行一項：假設｜基本情境｜壓力情境｜壓力幅度）', 'label_en': '10C — Stress Assumptions (one per line: Assumption|Base Case|Worse Case|Stress)', 'type': 'list', 'manual': True},
    '10C_projections.worse_case_summary': {'section': 'S10', 'label': '10C — 壓力情境摘要（每行一項：項目｜數值｜是否DSCR？）', 'label_en': '10C — Worse Case Summary (one per line: Item|Value|Is DSCR?)', 'type': 'list', 'manual': True},
    '10C_projections.worse_case_commentary': {'section': 'S10', 'label': '10C — 壓力情境說明', 'label_en': '10C — Worse Case Commentary', 'type': 'str', 'manual': True},
    '10C_projections.freight_rate_drop_pct': {'section': 'S10', 'label': '10C — 運費下跌幅度（壓力情境）', 'label_en': '10C — Freight Rate Drop Pct (Stress)', 'type': 'float', 'manual': True},
    '10C_projections.base_dscr_fy_1': {'section': 'S10', 'label': '10C — 基本情境DSCR第1年 (FY2026E)', 'label_en': '10C — Base DSCR Year 1 (FY2026E)', 'type': 'float', 'manual': True},
    '10C_projections.base_dscr_fy_2': {'section': 'S10', 'label': '10C — 基本情境DSCR第2年 (FY2027E)', 'label_en': '10C — Base DSCR Year 2 (FY2027E)', 'type': 'float', 'manual': True},
    '10C_projections.base_dscr_fy_3': {'section': 'S10', 'label': '10C — 基本情境DSCR第3年 (FY2028E)', 'label_en': '10C — Base DSCR Year 3 (FY2028E)', 'type': 'float', 'manual': True},
    '10C_projections.worse_dscr_fy_1': {'section': 'S10', 'label': '10C — 壓力情境DSCR第1年 (FY2026E)', 'label_en': '10C — Worse DSCR Year 1 (FY2026E)', 'type': 'float', 'manual': True},
    '10C_projections.base_revenue_fy_1': {'section': 'S10', 'label': '10C — 基本情境收入第1年（單位）', 'label_en': '10C — Base Revenue Year 1 (unit)', 'type': 'float', 'manual': True},
    '10C_projections.worse_revenue_fy_1': {'section': 'S10', 'label': '10C — 壓力情境收入第1年（單位）', 'label_en': '10C — Worse Revenue Year 1 (unit)', 'type': 'float', 'manual': True},

    # Section 11 — Report Metadata
    '11A_report_meta': {'section': 'S11', 'label': '分析師報告元數據 (JSON)', 'label_en': 'Analyst Report Metadata (JSON)', 'type': 'dict', 'manual': True},
    '11B_rating': {'section': 'S11', 'label': '評級與目標價格 (JSON)', 'label_en': 'Rating & Target Price (JSON)', 'type': 'dict', 'manual': True},
    '11C_company_fundamentals': {'section': 'S11', 'label': '公司基本面 (JSON)', 'label_en': 'Company Fundamentals (JSON)', 'type': 'dict', 'manual': True},
    '11D_investment_thesis': {'section': 'S11', 'label': '投資論點 (JSON)', 'label_en': 'Investment Thesis (JSON)', 'type': 'dict', 'manual': True},
    '11E_annual_income_statement': {'section': 'S11', 'label': '年度損益表 (JSON)', 'label_en': 'Annual Income Statement (JSON)', 'type': 'dict', 'manual': True},
    '11F_quarterly_income_statement': {'section': 'S11', 'label': '季度損益表 (JSON)', 'label_en': 'Quarterly Income Statement (JSON)', 'type': 'dict', 'manual': True},
    '11G_balance_sheet': {'section': 'S11', 'label': '資產負債表 (JSON)', 'label_en': 'Balance Sheet (JSON)', 'type': 'dict', 'manual': True},
    '11H_cash_flow': {'section': 'S11', 'label': '現金流量表 (JSON)', 'label_en': 'Cash Flow Statement (JSON)', 'type': 'dict', 'manual': True},
    '11I_ratio_analysis': {'section': 'S11', 'label': '比率分析 (JSON)', 'label_en': 'Ratio Analysis (JSON)', 'type': 'dict', 'manual': True},
    '11J_valuation_metrics': {'section': 'S11', 'label': '估值指標與同業比較 (JSON)', 'label_en': 'Valuation Metrics & Peer Comparison (JSON)', 'type': 'dict', 'manual': True},
    '11K_esg': {'section': 'S11', 'label': 'ESG數據 (JSON)', 'label_en': 'ESG Data (JSON)', 'type': 'dict', 'manual': True},
    '11L_industry_context': {'section': 'S11', 'label': '產業背景與展望 (JSON)', 'label_en': 'Industry Context & Outlook (JSON)', 'type': 'dict', 'manual': True},

}
# Total: 532 fields | Auto-extractable: 158 | Manual: 374


FORM_SECTION_LABELS = {
    "S1": "S1_facility_terms",
    "S2": "S2_credit_overview",
    "S3": "S3_ratings",
    "S4": "S4_borrower_profile",
    "S5": "S5_security",
    "S6": "S6_project_vessel",
    "S7": "S7_financial_statements",
    "S8": "S8_acra_banking",
    "S9": "S9_compliance",
    "S10": "S10_group_exposure",
    "S11": "S11_report_metadata",
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
            "label_en": meta.get("label_en", ""),
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


def map_form_fields(raw: dict) -> dict:
    """Map raw extraction to all 532 banking credit form fields (S1–S11).

    Returns unified_form_fields dict compatible with web form field-suggestion engine.
    Manual fields → status=manual_req, never extracted from documents.
    """
    sections: dict[str, dict] = {}
    populated: dict[str, Any] = {}

    for fid, meta in FORM_FIELD_REGISTRY.items():
        sec = meta["section"]
        sec_key = FORM_SECTION_LABELS.get(sec, sec)
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
                    v = get_chart_series_latest(raw, pn, transform) if pn else get_chart_series_any(raw, transform)
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

        if status == FieldStatus.EXTRACTED and conf < 0.75:
            status = FieldStatus.LOW_CONF

        sections[sec_key]["fields"][fid] = {
            "value": value,
            "status": status.value,
            "confidence": conf,
            "source_page": src_page,
            "label_zh": meta["label"],
            "label_en": meta.get("label_en", ""),
            "manual": meta.get("manual", False),
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
            "auto_extractable": t - ma,
            "extracted": ex,
            "inferred": inf,
            "low_conf": lc,
            "missing": mi,
            "manual_req": ma,
            "coverage_pct": round((ex + inf + lc) / max(t - ma, 1) * 100, 1),
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

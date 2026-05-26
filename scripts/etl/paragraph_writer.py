#!/usr/bin/env python3
"""paragraph_writer.py — Layer 4: unified fields → 1–10 credit paragraphs (Gemini)."""
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types as genai_types

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

PARAGRAPH_CONFIG = {
    "P1":  {"title": "公司概況",      "section_key": "P1_company_profile",      "min": 200, "max": 400},
    "P2":  {"title": "產業分析",      "section_key": "P2_industry_analysis",    "min": 200, "max": 350},
    "P3":  {"title": "財務分析",      "section_key": "P3_financial_analysis",   "min": 350, "max": 550},
    "P4":  {"title": "公司治理",      "section_key": "P4_governance",           "min": 150, "max": 300},
    "P5":  {"title": "風險評估",      "section_key": "P5_risk_assessment",      "min": 250, "max": 400},
    "P6":  {"title": "授信建議",      "section_key": "P6_credit_recommendation","min": 150, "max": 250},
    "P7":  {"title": "流動性分析",    "section_key": "P7_liquidity",            "min": 200, "max": 350},
    "P8":  {"title": "ESG/永續分析",  "section_key": "P8_esg",                  "min": 150, "max": 280},
    "P9":  {"title": "同業比較",      "section_key": "P9_peer_comparison",      "min": 150, "max": 280},
    "P10": {"title": "評等與債務",    "section_key": "P10_ratings_debt",        "min": 150, "max": 280},
}

PROMPTS = {
"P1": """\
你是台灣商業銀行法人徵審報告撰寫專家。根據以下欄位撰寫「公司概況」段落。
規則：①僅用status=extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文正式文體 ④200-400字
結構：公司識別→主要業務→規模→市場地位→集團歸屬
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P2": """\
你是台灣商業銀行法人徵審報告撰寫專家。根據以下欄位撰寫「產業分析」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④200-350字
結構：產業特性→市場規模成長→供需結構→主要影響因素→競爭格局→展望
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P3": """\
你是台灣商業銀行法人徵審報告撰寫專家，擅長財務報表分析。根據以下欄位撰寫「財務分析」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④350-550字 ⑤數字附單位 ⑥YoY附%
結構：營收規模→獲利能力(毛利/EBITDA/純益率)→財務結構(負債比/流動比)→每股盈餘→季度趨勢
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P4": """\
你是台灣商業銀行法人徵審報告撰寫專家。根據以下欄位撰寫「公司治理」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④150-300字
結構：董事會組成→股權結構→資訊揭露→重大訴訟或違規
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P5": """\
你是台灣商業銀行法人徵審報告撰寫專家，擅長信用風險評估。根據以下欄位撰寫「風險評估」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④250-400字 ⑤客觀中立
結構：主要信用風險→財務風險→業務風險→市場風險→法規風險→緩解因素
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P6": """\
你是台灣商業銀行法人徵審報告撰寫專家。根據以下欄位撰寫「授信建議」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④150-250字
結構：建議額度與幣別→授信種類期限→擔保品要求→財務契約→先決條件→審批層級
注意：本段多為[待填]屬正常，需信用官員補填。
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P7": """\
你是台灣商業銀行法人徵審報告撰寫專家。根據以下欄位撰寫「流動性分析」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④200-350字
結構：現金及流動資源→流動比率趨勢(季序列高/低/最新)→債務到期結構→再融資能力→FCF→結論
流動比率解讀：>200%很強/150-200%穩健/100-150%尚可/<100%需關注
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P8": """\
你是台灣商業銀行法人徵審報告撰寫專家，熟悉永續金融。根據以下欄位撰寫「ESG/永續分析」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④150-280字
結構：ESG評級→環境(排放/減碳目標/綠色船隊)→社會面→永續金融工具→主要ESG風險
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P9": """\
你是台灣商業銀行法人徵審報告撰寫專家。根據以下欄位撰寫「同業比較」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④150-280字
結構：可比較同業→獲利比較(EBITDA率)→財務槓桿比較→規模比較→信評比較→相對地位結論
欄位：{fields_json}
直接輸出段落，不含標題。""",

"P10": """\
你是台灣商業銀行法人徵審報告撰寫專家。根據以下欄位撰寫「評等與債務結構」段落。
規則：①僅用extracted/inferred/low_conf欄位 ②missing→[待填] ③繁體中文 ④150-280字
結構：信用評等(三大行+本地/展望)→評等歷史→流通債券(ISIN/coupon/到期/規模)→聯貸記錄→債務成本結構
欄位：{fields_json}
直接輸出段落，不含標題。""",
}


def _serialize(fields: dict, max_f: int = 80) -> str:
    out = {}
    cnt = 0
    for fid, fd in fields.items():
        if cnt >= max_f:
            break
        st = fd.get("status", "missing")
        v = fd.get("value")
        if st in ("extracted", "inferred", "low_conf") and v is not None:
            out[fid] = {"label": fd.get("label_zh", fid), "value": v, "status": st}
        else:
            out[fid] = {"label": fd.get("label_zh", fid), "value": None, "status": "missing"}
        cnt += 1
    return json.dumps(out, ensure_ascii=False, indent=2)


def _post_process(text: str, fields: dict):
    issues = []
    n_placeholders = text.count("[待填]")
    if n_placeholders > 5:
        issues.append(f"⚠️ 高缺失率：{n_placeholders} 個 [待填]")
    source_vals = set()
    for fd in fields.values():
        v = fd.get("value")
        if v is not None:
            source_vals.add(str(v).replace(",", ""))
            try:
                source_vals.add(str(round(float(str(v).replace(",", "")), 0)))
            except Exception:
                pass
    suspicious = [
        n for n in re.findall(r"[\d,]+\.?\d*", text)
        if len(n.replace(",", "")) >= 4
        and not any(n.replace(",", "") in sv or sv in n.replace(",", "") for sv in source_vals)
    ]
    if suspicious:
        issues.append(f"⚠️ 潛在幻覺數字: {', '.join(suspicious[:3])}")
    return text.strip(), issues


def write_paragraphs(unified: dict, para_ids: list[str]) -> dict:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set — cannot generate paragraphs.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    results = {}
    for pid in para_ids:
        cfg = PARAGRAPH_CONFIG.get(pid)
        if not cfg:
            continue
        sk = cfg["section_key"]
        flds = unified.get("sections", {}).get(sk, {}).get("fields", {})
        print(f"  [{pid}] {cfg['title']} — {len(flds)} fields")
        fj = _serialize(flds)
        prompt = PROMPTS.get(pid, f"撰寫{cfg['title']}段落，欄位：{{fields_json}}").replace("{fields_json}", fj)
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=1400,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            text, issues = _post_process(response.text or "", flds)
        except Exception as e:
            text = f"[生成失敗: {e}]"
            issues = [str(e)]
        mi = sum(1 for f in flds.values() if f.get("status") == "missing")
        ex = sum(1 for f in flds.values() if f.get("status") in ("extracted", "inferred", "low_conf"))
        results[pid] = {
            "title": cfg["title"],
            "text": text,
            "issues": issues,
            "missing_field_count": mi,
            "extracted_field_count": ex,
            "total_field_count": len(flds),
        }
        print(f"         ✓ {len(text)}chars | {ex}extracted | {mi}missing")
    return results


def format_markdown_output(results: dict, unified: dict, pdf_name: str = "") -> str:
    meta = unified.get("meta", {})
    cov = unified.get("coverage", {})
    company = meta.get("company", "[公司名稱]")
    ticker = meta.get("ticker", "")
    date = meta.get("extraction_date", datetime.now().strftime("%Y-%m-%d"))
    lines = [
        f"# {company}（{ticker}）— 法人徵審報告", "",
        f"**資料來源：** {pdf_name or meta.get('source_file', '')}",
        f"**提取日期：** {date}",
        f"**欄位覆蓋率：** {cov.get('coverage_pct', 0):.1f}% "
        f"({cov.get('extracted', 0) + cov.get('inferred', 0)}/{cov.get('total_fields', 0)})",
        f"**缺少欄位：** {cov.get('missing', 0)} 個 | **需手動：** {cov.get('manual_req', 0)} 個",
        "", "---", "",
    ]
    for pid in sorted(results.keys()):
        r = results[pid]
        lines += [f"## {pid}  {r['title']}", "", r["text"], ""]
        for iss in r.get("issues", []):
            lines.append(f"> {iss}")
        if r["missing_field_count"] > 0:
            lines.append(
                f"> 📋 本段 **{r['missing_field_count']}** 個欄位未自動提取，以 [待填] 標記。"
            )
        lines += ["", "---", ""]
    lines += [
        f"*annual-report-etl v2 自動生成 — {date}*",
        "*請信用官員審閱所有 [待填] 欄位後方可使用*",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fields", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--paragraphs", default="all")
    args = ap.parse_args()
    uf = json.loads(Path(args.fields).read_text(encoding="utf-8"))
    pids = (
        list(PARAGRAPH_CONFIG.keys()) if args.paragraphs == "all"
        else [x.strip().upper() for x in args.paragraphs.split(",")]
    )
    res = write_paragraphs(uf, pids)
    md = format_markdown_output(res, uf)
    Path(args.out).write_text(md, encoding="utf-8")
    Path(args.out).with_suffix(".json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✅ {args.out}")

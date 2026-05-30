from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

ARTIFACT_DIR = Path(os.getenv("E2E_ARTIFACT_DIR", "test-results/e2e"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

SECTION7_METRIC_SYNONYMS: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "net revenue", "營業收入淨額", "營業收入", "收入"),
    "gross_profit": ("gross_profit", "gross profit", "營業毛利", "毛利"),
    "operating_income": ("operating_income", "operating income", "operating profit", "營業淨利", "營業利益"),
    "net_income": ("net_income", "net income", "profit for the year", "本年度淨利", "淨利"),
    "eps": ("eps", "earnings per share", "每股盈餘"),
    "total_assets": ("total_assets", "total assets", "資產總額"),
    "total_liabilities": ("total_liabilities", "total liabilities", "負債總額"),
    "total_equity": ("total_equity", "total equity", "equity attributable", "權益總額"),
    "operating_cash_flow": ("operating_cash_flow", "operating cash flow", "cash flows from operating", "營業活動現金流量"),
    "investing_cash_flow": ("investing_cash_flow", "investing cash flow", "cash flows from investing", "投資活動現金流量"),
    "financing_cash_flow": ("financing_cash_flow", "financing cash flow", "cash flows from financing", "籌資活動現金流量"),
    "cash": ("cash", "cash and cash equivalents", "現金及約當現金"),
}

IDENTITY_ONLY_TERMS = (
    "company_name", "company_name_zh", "company_name_en", "responsible_person", "spokesperson",
    "contact", "負責人", "發言人", "聯絡人", "公司名稱",
)


def assert_no_server_error(response):
    assert response.status_code < 500, (
        f"Unexpected server error {response.status_code} for {response.request.method} "
        f"{response.request.url}: {response.text[:2000]}"
    )


def flatten_json_keys_and_values(obj: Any) -> list[str]:
    out: list[str] = []
    def walk(value: Any, prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = f"{prefix}.{key}" if prefix else str(key)
                out.append(key_text)
                walk(nested, key_text)
        elif isinstance(value, list):
            for item in value:
                walk(item, prefix)
        elif value is not None:
            out.append(str(value))
    walk(obj)
    return out


def _payload_haystack(payload: Any) -> str:
    return "\n".join(flatten_json_keys_and_values(payload)).lower()


def contains_financial_metric_family(payload: Any) -> bool:
    return count_section7_core_metrics(payload)[0] > 0


def count_section7_core_metrics(payload: Any) -> tuple[int, list[str], list[str]]:
    haystack = _payload_haystack(payload)
    found: list[str] = []
    missing: list[str] = []
    for family, synonyms in SECTION7_METRIC_SYNONYMS.items():
        if any(term.lower() in haystack for term in synonyms):
            found.append(family)
        else:
            missing.append(family)
    return len(found), found, missing


def is_identity_only_payload(payload: Any) -> bool:
    haystack = _payload_haystack(payload)
    metric_count, _, _ = count_section7_core_metrics(payload)
    return metric_count == 0 and any(term.lower() in haystack for term in IDENTITY_ONLY_TERMS)


def save_json_artifact(name: str, data: Any) -> Path:
    safe = name if name.endswith(".json") else f"{name}.json"
    path = ARTIFACT_DIR / safe
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def save_text_artifact(name: str, text: str) -> Path:
    path = ARTIFACT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def require_real_file(path: str | os.PathLike[str] | None, label: str) -> Path:
    if not path:
        pytest.skip(f"{label} is required for real E2E. Set TSMC_ANNUAL_REPORT_PATH to an actual TSMC annual-report PDF; fake files are forbidden.")
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        pytest.skip(f"{label} does not exist: {p}. Provide a real TSMC annual-report PDF; fake E2E success is forbidden.")
    if p.stat().st_size < 1024 * 1024:
        pytest.fail(f"{label} exists but is too small for a real annual report ({p.stat().st_size} bytes): {p}")
    return p

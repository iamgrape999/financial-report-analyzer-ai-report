"""TPEx (OTC market) OpenAPI client — Stage 3 Item 2.

Taiwan's TPEx (Taipei Exchange / 證券櫃檯買賣中心) lists companies with codes
in the 3xxx, 5xxx–9xxx ranges.  Financial-statement data is available from
the same MOPS endpoints as TWSE; only exchange-specific data (daily price,
P/E ratios) comes from a separate TPEx base URL.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from credit_report.config import TWSE_VERIFY_SSL

logger = logging.getLogger(__name__)

TPEX_BASE_URL = "https://www.tpex.org.tw/openapi/v1"
MOPS_OPEN_DATA_CSV_BASE_URL = "https://mopsfin.twse.com.tw/opendata"

# MOPS financial-statement endpoints are exchange-agnostic (cover both TWSE and TPEx).
# TPEx-specific endpoints replace exchange-level data (valuation, trading).
TPEX_ENDPOINTS: dict[str, str] = {
    "company_profile":          "/mopsfin/t187ap03_L",
    "monthly_revenue":          "/mopsfin/t187ap05_L",
    "income_statement_general": "/mopsfin/t187ap06_L_ci",
    "balance_sheet_general":    "/mopsfin/t187ap07_L_ci",
    "cash_flow_general":        "/mopsfin/t187ap08_L_ci",
    "valuation_ratios":         "/tpexValueInfo/reportPE",
    "daily_trading":            "/tpexValueInfo/dailyPrice",
    "monthly_average":          "/tpexValueInfo/dailyPrice",   # same source, latest rows
    "dividend":                 "/mopsfin/t187ap45_L",
}

# MOPS datasets that have a CSV mirror on mopsfin.twse.com.tw (same as TWSE fallback).
_MOPS_DATASETS = {
    "company_profile", "monthly_revenue", "income_statement_general",
    "balance_sheet_general", "cash_flow_general", "dividend",
}


@dataclass
class TPExOpenAPIClient:
    base_url: str = TPEX_BASE_URL
    timeout_seconds: float = 30.0
    exchange_name: str = field(default="TPEx", init=False)

    async def fetch(self, name: str, endpoint: str) -> list[dict[str, Any]]:
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {"Accept": "application/json", "User-Agent": "financial-report-analyzer/1.0"}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                verify=TWSE_VERIFY_SSL,
                headers=headers,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "json" not in content_type and not resp.text.strip().startswith("["):
                    raise ValueError(f"Non-JSON response from TPEx: content-type={content_type!r}")
                data = resp.json()
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("TPEx OpenAPI fetch failed name=%s endpoint=%s: %s", name, endpoint, exc)

        # CSV mirror fallback for MOPS datasets.
        if name in _MOPS_DATASETS:
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
            except Exception as csv_exc:
                logger.warning(
                    "TPEx MOPS CSV fallback failed name=%s url=%s: %s", name, csv_url, csv_exc
                )
        return []

    async def fetch_company_bundle(
        self, stock_code: str
    ) -> dict[str, list[dict[str, Any]]]:
        bundle: dict[str, list[dict[str, Any]]] = {}
        for name, endpoint in TPEX_ENDPOINTS.items():
            bundle[name] = await self.fetch(name, endpoint)
        return bundle

"""TPEx (OTC market) OpenAPI client — Stage 3 Item 2.

Taiwan's TPEx (Taipei Exchange / 證券櫃檯買賣中心) lists companies with codes
in the 3xxx, 5xxx–9xxx ranges.  Financial-statement data is available from
the same MOPS endpoints as TWSE (exchange-agnostic); only exchange-specific
data (daily price, P/E ratios) comes from the TPEx base URL.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from credit_report.config import TWSE_BUNDLE_CACHE_TTL, TWSE_VERIFY_SSL

logger = logging.getLogger(__name__)

TPEX_BASE_URL = "https://www.tpex.org.tw/openapi/v1"
# MOPS financial data lives on the TWSE OpenAPI host — it covers both TWSE and TPEx listings.
TWSE_OPENAPI_BASE_URL = "https://openapi.twse.com.tw/v1"
MOPS_OPEN_DATA_CSV_BASE_URL = "https://mopsfin.twse.com.tw/opendata"

# Full-URL endpoint table.  MOPS financial-statement endpoints use the TWSE OpenAPI
# host (exchange-agnostic data); TPEx-specific market data uses the TPEx host.
TPEX_ENDPOINTS: dict[str, str] = {
    "company_profile":          f"{TWSE_OPENAPI_BASE_URL}/opendata/t187ap03_L",
    "monthly_revenue":          f"{TWSE_OPENAPI_BASE_URL}/opendata/t187ap05_L",
    "income_statement_general": f"{TWSE_OPENAPI_BASE_URL}/opendata/t187ap06_L_ci",
    "balance_sheet_general":    f"{TWSE_OPENAPI_BASE_URL}/opendata/t187ap07_L_ci",
    "cash_flow_general":        f"{TWSE_OPENAPI_BASE_URL}/opendata/t187ap08_L_ci",
    "dividend":                 f"{TWSE_OPENAPI_BASE_URL}/opendata/t187ap45_L",
    "valuation_ratios":         f"{TPEX_BASE_URL}/tpexValueInfo/reportPE",
    "daily_trading":            f"{TPEX_BASE_URL}/tpexValueInfo/dailyPrice",
}

# In-process cache: {stock_code: (fetched_at_monotonic, bundle)}
# TTL controlled by TWSE_BUNDLE_CACHE_TTL (default 3h), same as TWSEOpenAPIClient.
_bundle_cache: dict[str, tuple[float, dict[str, list[dict[str, Any]]]]] = {}


@dataclass
class TPExOpenAPIClient:
    base_url: str = TPEX_BASE_URL
    timeout_seconds: float = 30.0
    exchange_name: str = field(default="TPEx", init=False)

    async def fetch(self, name: str, endpoint: str) -> list[dict[str, Any]]:
        # All endpoints in TPEX_ENDPOINTS are full URLs.
        url = endpoint if endpoint.startswith("http") else (
            f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        )
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
                    raise ValueError(f"Non-JSON response from {name}: content-type={content_type!r}")
                data = resp.json()
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("TPEx OpenAPI fetch failed name=%s url=%s: %s", name, url, exc)

        # CSV mirror fallback for TWSE OpenAPI /opendata/ endpoints.
        if "/opendata/" in url:
            dataset = url.rsplit("/", 1)[-1]
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
        now = time.monotonic()
        cached = _bundle_cache.get(stock_code)
        if cached is not None:
            cached_at, bundle = cached
            if now - cached_at < TWSE_BUNDLE_CACHE_TTL:
                logger.debug("fetch_company_bundle (TPEx): cache hit stock=%s age=%.0fs", stock_code, now - cached_at)
                return bundle

        bundle: dict[str, list[dict[str, Any]]] = {}
        for name, endpoint in TPEX_ENDPOINTS.items():
            bundle[name] = await self.fetch(name, endpoint)
        # monthly_average uses the same daily-price source as daily_trading on TPEx;
        # reuse the already-fetched result to avoid a duplicate HTTP request.
        bundle["monthly_average"] = bundle.get("daily_trading", [])

        _bundle_cache[stock_code] = (now, bundle)
        return bundle

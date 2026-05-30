"""MarketDataProvider protocol and factory.

Items 1 & 2 of Stage 3:
- Abstract interface so TWSE and TPEx clients are interchangeable.
- Factory function auto-routes by exchange or falls back intelligently.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class MarketDataProvider(Protocol):
    """Minimal interface every market-data backend must satisfy."""

    exchange_name: str  # e.g. "TWSE", "TPEx"

    async def fetch_company_bundle(
        self, stock_code: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Return a bundle of raw rows keyed by dataset name.

        Must contain at minimum these keys (empty list is acceptable):
        company_profile, monthly_revenue, income_statement_general,
        balance_sheet_general, cash_flow_general, valuation_ratios,
        daily_trading, monthly_average, dividend.
        """
        ...


def create_market_data_provider(
    stock_code: str,
    exchange: str = "auto",
) -> MarketDataProvider:
    """Return the right provider for stock_code.

    exchange values:
      "twse"  — force TWSE (Main Board, 4-digit codes like 2330)
      "tpex"  — force TPEx/OTC (4-digit codes like 6271, 3008)
      "auto"  — heuristic: try to detect from code range; callers can
                override if auto-detection is wrong.
    """
    from credit_report.integrations.tpex import TPExOpenAPIClient
    from credit_report.integrations.twse import TWSEOpenAPIClient

    if exchange == "tpex":
        return TPExOpenAPIClient()
    if exchange == "twse":
        return TWSEOpenAPIClient()

    # Auto-detect heuristic:
    # TWSE main-board codes: primarily 1xxx–2xxx, many 4xxx
    # TPEx OTC codes: 3xxx (many tech/IC design/biotech), 5xxx–9xxx
    # When ambiguous, default to TWSE which has broader MOPS coverage.
    # Note: a few 3xxx codes (e.g. 3008 Largan) are TWSE-listed — override with
    # exchange="twse" in those rare cases.
    code = stock_code.strip()
    if len(code) == 4 and code.isdigit():
        first = code[0]
        if first in ("3", "5", "6", "7", "8", "9"):
            logger.info("create_market_data_provider: auto-detected TPEx for code=%s", code)
            return TPExOpenAPIClient()
    return TWSEOpenAPIClient()

"""
In-Process Mock Pipeline Test — Financial Report Analyzer
=========================================================
Runs the COMPLETE AI report generation pipeline inside the same process as the
ASGI app so unittest.mock patches work correctly.

Strategy:
  - Set GEMINI_API_KEY=mock-key-for-testing in os.environ BEFORE importing app
  - Patch google.genai.Client with a realistic mock that returns pre-canned markdown
  - Use httpx.AsyncClient(transport=ASGITransport(app)) for zero-overhead HTTP calls
  - Test full flow: auth → report → upload docs → ETL → §1-§10 inputs → generate
    all 10 sections in GENERATION_ORDER → quality checks → DOCX → audit trail

This test validates complete AI-powered report generation without a live API key.

Run:
    python3 tests/mock_pipeline_test.py
    python3 -m pytest tests/mock_pipeline_test.py -v   (also works)
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ── CRITICAL: set env vars before any app import ──────────────────────────────
os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

# ── Add project root to path ──────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── Silence verbose loggers BEFORE app imports ────────────────────────────────
import logging as _logging
_logging.getLogger("sqlalchemy").setLevel(_logging.ERROR)
_logging.getLogger("aiosqlite").setLevel(_logging.ERROR)
_logging.getLogger("python_multipart").setLevel(_logging.ERROR)
_logging.getLogger("httpx").setLevel(_logging.ERROR)
_logging.getLogger("httpcore").setLevel(_logging.ERROR)
_logging.getLogger("uvicorn").setLevel(_logging.ERROR)
_logging.getLogger("fastapi").setLevel(_logging.ERROR)
_logging.getLogger("credit_report").setLevel(_logging.WARNING)
_logging.getLogger("main").setLevel(_logging.WARNING)

import httpx

# ── Result tracking ───────────────────────────────────────────────────────────
results: dict[str, Any] = {"pass": 0, "warn": 0, "fail": 0, "details": []}


def _record(level: str, label: str, detail: str = "") -> bool:
    results[level] += 1
    icon = {"pass": "✅", "warn": "⚠️ ", "fail": "❌"}[level]
    line = f"  {icon}  {label}"
    if detail:
        line += f" — {detail[:120]}"
    print(line)
    results["details"].append({"level": level, "label": label, "detail": detail})
    return level == "pass"


def ok(label, detail=""): return _record("pass", label, detail)
def warn(label, detail=""): return _record("warn", label, detail)
def fail(label, detail=""): return _record("fail", label, detail)
def check(cond, label, detail=""): return ok(label, detail) if cond else fail(label, detail)
def section(title): print(f"\n{'='*70}\n  {title}\n{'='*70}")


# ── Mock Gemini response factory ──────────────────────────────────────────────

def _mock_response(text: str, tokens: int = 1000):
    resp = MagicMock()
    resp.text = text
    resp.usage_metadata.prompt_token_count = tokens // 2
    resp.usage_metadata.candidates_token_count = tokens // 2
    cand = MagicMock()
    cand.finish_reason = "STOP"
    resp.candidates = [cand]
    return resp


# Realistic markdown for each section
SECTION_MD: dict[int, str] = {
    1: """\
# Section 1 — Credit Facility & Key Terms

## Facility Summary

| Item | Borrower | Facility Type | Amount (USD M) | Tenor |
|------|----------|---------------|----------------|-------|
| 1 | Evergreen Marine (Asia) Pte. Ltd. (EMA) | Term Loan (SLL) | 213.84 | 12 years |

**Proposed Credit Limit:** USD 213.84 million
**Advance Rate (LTC):** 80% of Initial Market Value
**Interest Rate:** Term SOFR + 175 bps (3-month periods)

## Purpose

Finance construction of one 20,000 TEU LNG dual-fuel containership (Hull H-2891, Samsung Heavy Industries).

**Guarantor:** Evergreen Marine Corporation (Taiwan) Ltd. (EMC)

## Terms & Conditions

- **Repayment:** 5% semi-annual instalments + 30% balloon at maturity
- **Pre-Delivery Security:** IBK Refund Guarantee (AA/AA-); Assignment of Shipbuilding Contract
- **Post-Delivery Security:** First Priority Vessel Mortgage; EMC Corporate Guarantee
- **VMC:** ACR ≥120%; LTV ≤83%; tested every 2 years; 21-day cure period
- **SLL KPI:** CII rating improvement; margin ratchet ±5 bps
- **Governing Law:** Singapore

## Group Limit

Approved Group Limit: USD 750m | Proposed Utilisation: USD 563.84m ✓ Within Limit
""",
    2: """\
# Section 2 — Overall Comments

## A. Credit Overview

1. EMC is the **7th largest container line globally** with 2.02m TEU capacity as at FY2024.
2. New USD 213.84m Sustainability-Linked Term Loan to finance one 20,000 TEU LNG dual-fuel vessel (Hull H-2891).
3. EMA: net cash USD 2.2bn; D/E 0.38x; interest coverage 36.5x as at FY2024.
4. **Pre-delivery:** IBK Refund Guarantee (S&P: AA / Fitch: AA-) fully covers each installment.
5. CCFI averaged 1,220 in 9M2025; EMC revenue TWD 381.2bn FY2024.
6. EMC: 50-year track record; OCEAN Alliance member; listed on TSE.

*(See Section 7: Financial Analysis)*

## B. Solvency & Repayment Capacity

**Primary:** Operating cash flow from EMA vessel fleet (36.5x interest coverage).
**Secondary:** EMC corporate guarantee (net cash USD 6.1bn; D/E 0.20x) and vessel collateral.

| Metric | FY2024 | Prior Year |
|--------|--------|-----------|
| Cash (USD bn) | 2.20 | 1.85 |
| Op. EBITDA (USD bn) | 0.71 | 0.95 |
| Debt/EBITDA | 2.75x | 2.05x |
| Interest Coverage | 36.5x | 42.1x |

## C. Guarantor (EMC)

EMC net cash TWD 198.3bn (USD 6.1bn); D/E 0.20x; interest coverage 31.2x FY2024.
EMC has supported EMA through guarantees since 2019 — no events of default.

## D. Collateral

**Pre-delivery:** IBK Refund Guarantee fully covering each installment, assigned to CUB. Satisfactory.
**Post-delivery:** First priority vessel mortgage on 20,000 TEU LNG vessel; LTC 80%; ACR 120%; LTV 83%.

## E. Risk & Mitigants

| Risk | Level | Mitigant |
|------|-------|---------|
| Freight rate volatility | High | 12-yr TC with EMC; EMC net cash USD 6.1bn |
| Delivery risk | Medium | IBK RG; 210-day grace period |
| Builder insolvency | Low | SHI BBB+; KDB shareholder |

*(See Section 5 for collateral detail)*
""",
    3: """\
# Section 3 — Credit Ratings

## A. External Ratings

No external credit ratings assigned to EMA or EMC by any NRSRO.

## B. Internal (MSR) Ratings

| Entity | Role | FY2022/23 | FY2023/24 | FY2024 | Current | Remarks |
|--------|------|-----------|-----------|--------|---------|---------|
| EMA | Borrower | 6- | 6- | 6 | **6** | Proposed MSR6 |
| EMC | Guarantor | 5 | 5 | 5 | **5** | Stable |

*No override flag applied.*

## C. MAS 612 Classification

**Grade: PASS**
Borrower rated MSR 6, mapped to PASS under MAS Notice 612. No identified weakness in repayment capability. EMA interest coverage 36.5x FY2024 sustains the rating.

## D. ESG Rating

EMA ESG score: **B+** (Sustainalytics, Jan 2025).
Key positives: LNG dual-fuel fleet investment; OCEAN Alliance GHG commitments.
Key risks: Shipping sector emissions exposure; fuel transition cost.
""",
    4: """\
# Section 4 — Corporate Background

## A. Borrower — EMA Maritime Holdings Ltd

Evergreen Marine (Asia) Pte. Ltd. (EMA) is a wholly owned subsidiary of Evergreen Marine Corporation (Taiwan) Ltd. (EMC), incorporated in Singapore in 2005. EMA serves as EMC's primary vessel-owning entity for fleet expansion in the Asia-Pacific region.

**Key Statistics (FY2024):**

| Metric | Value |
|--------|-------|
| Fleet Owned | 105 vessels |
| Fleet TC-in | 95 vessels |
| On Order | 24 vessels |
| Total Capacity | 2.02m TEU |
| Total Assets | USD 7,975m |
| Total Equity | USD 4,070m |

## B. Parent — EMC

EMC, founded 1968, is the **7th largest container shipping line** globally by TEU capacity.
Listed on Taiwan Stock Exchange (TSE: 2603). Member of OCEAN Alliance.

**Revenue:** TWD 381.2bn FY2024 | **Net Income:** TWD 73.9bn | **Cash:** TWD 198.3bn (USD 6.1bn)

## C. Fleet & Orderbook

EMA operates 105 owned vessels with 95 chartered-in. 24 newbuildings on order including 6 LNG dual-fuel vessels scheduled for delivery 2025-2027.

*(See Section 10: Appendix for fleet details)*

## D. Market Position

CCFI averaged **1,220** in 9M2025 (vs. 1,680 FY2024). Trans-Pacific lanes maintain premium pricing. EMC cross-trade exposure ~15% of revenue, limiting direct US tariff impact.

## E. Track Record with Bank

EMA/EMC have been bank customers since 2015. No defaults, no covenant breaches. Outstanding facilities: USD 153.75m performing satisfactorily.
""",
    5: """\
# Section 5 — Collateral / Support

## A. Security Overview

The facility is secured by a two-stage security package typical of ship finance transactions.

## B. Pre-Delivery Collateral

**Refund Guarantee (RG)**

| Item | Details |
|------|---------|
| Issuer | Industrial Bank of Korea (IBK) |
| S&P Rating | AA |
| Fitch Rating | AA- |
| Coverage | USD 267,300,000 — fully covers each installment |
| Type | Unconditional and irrevocable on-demand guarantee |
| Assigned to | Cathay United Bank (CUB) |
| Satisfactory | Yes |

Assignment of Shipbuilding Contract to CUB.

## C. Post-Delivery Collateral

- **First Priority Vessel Mortgage** on Hull H-2891 (20,000 TEU LNG dual-fuel containership)
- Assignment of earnings and insurances
- EMC Corporate Guarantee

| Metric | Value |
|--------|-------|
| LTC | 80% |
| ACR Minimum | 120% |
| LTV Maximum | 83% |
| Testing Frequency | Every 2 years |
| Cure Period | 21 days |

## D. Guarantor Assessment

EMC (Guarantor) is financially robust: net cash USD 6.1bn, D/E 0.20x, interest coverage 31.2x. Guarantee is commercially meaningful and enforceable under Singapore law.

*(See Section 4 for EMC corporate background)*
""",
    6: """\
# Section 6 — Project Analysis

## A. Project Overview

**Transaction:** Finance construction of one 20,000 TEU LNG dual-fuel containership (Hull H-2891) at Samsung Heavy Industries Co. Ltd. (SHI), South Korea.

| Item | Details |
|------|---------|
| Builder | Samsung Heavy Industries Co. Ltd. (SHI) |
| Builder Rating | BBB+ (S&P) |
| Contract Price | USD 267,300,000 |
| CUB Facility | USD 213,840,000 (80% LTC) |
| Delivery | June 2026 |
| Grace Period | 210 days |

## B. Construction Milestones

| Milestone | Date | Status |
|-----------|------|--------|
| Steel Cutting | Sep 2024 | ✓ Complete |
| Keel Laying | Jan 2025 | ✓ Complete |
| Launch | Mar 2026 (est.) | Pending |
| Delivery | Jun 2026 (est.) | Pending |

## C. Drawdown Schedule

Maximum 4 drawdowns; pre-delivery aggregate cap USD 42.77m; total cap USD 213.84m.

## D. Builder Risk Assessment

SHI post-restructuring (2019 KDB bail-out) is financially stable. BBB+ rating; KDB remains major shareholder providing implicit support. Order backlog: USD 19bn through 2028.

## E. Vessel Specification

20,000 TEU capacity; LNG dual-fuel propulsion; CII A-rating target on delivery; aligned with IMO 2030 trajectory.

*(See Section 1 for facility terms; See Section 5 for security details)*
""",
    7: """\
# Section 7 — Financial Analysis

## A. Entities Analysed

1. **EMA Maritime Holdings Ltd** — Borrower (Standalone, FY2022-FY2024)
2. **Evergreen Marine Corporation** — Guarantor (Consolidated, FY2022-FY2024)

---

## B. EMA Standalone Financials

### Income Statement

| USD m | FY2022 | FY2023 | FY2024 |
|-------|--------|--------|--------|
| Revenue | 3,150 | 2,890 | 2,200 |
| EBITDA | 2,100 | 1,420 | 710 |
| EBIT | 1,890 | 1,180 | 480 |
| Net Interest Expense | 45 | 38 | 19 |
| Net Income | 1,830 | 1,100 | 450 |

### Balance Sheet

| USD m | FY2022 | FY2023 | FY2024 |
|-------|--------|--------|--------|
| Total Assets | 9,200 | 8,500 | 7,975 |
| Total Debt | 2,800 | 2,100 | 1,950 |
| Net Debt / (Cash) | (1,200) | (800) | (2,200) |
| Total Equity | 5,200 | 4,600 | 4,070 |

### Key Ratios

| Ratio | FY2022 | FY2023 | FY2024 |
|-------|--------|--------|--------|
| Net Debt/EBITDA | Net Cash | Net Cash | Net Cash |
| Interest Coverage | 46.7x | 31.1x | 36.5x |
| D/E | 0.54x | 0.46x | 0.38x |
| EBITDA Margin | 66.7% | 49.1% | 32.3% |

---

## C. EMC Consolidated Financials

| TWD bn | FY2022 | FY2023 | FY2024 |
|--------|--------|--------|--------|
| Revenue | 612.5 | 420.1 | 381.2 |
| EBITDA | 400.2 | 180.5 | 105.2 |
| Net Income | 370.0 | 130.0 | 73.9 |
| Cash | 280.5 | 240.2 | 198.3 |
| Total Debt | 120.3 | 100.1 | 87.2 |

**EMC D/E: 0.20x | Interest Coverage: 31.2x | MSR5**

---

## D. DSCR Analysis (EMA)

### Base Case

| Year | EBITDA (USD m) | Debt Service | DSCR |
|------|----------------|-------------|------|
| FY2025E | 650 | 52 | 12.5x |
| FY2026E | 580 | 55 | 10.5x |
| FY2027E | 520 | 55 | 9.5x |

### Worse Case (-20% charter rates)

| Year | EBITDA (USD m) | Debt Service | DSCR |
|------|----------------|-------------|------|
| FY2025E | 520 | 52 | 10.0x |
| FY2026E | 450 | 55 | 8.2x |
| FY2027E | 400 | 55 | 7.3x |

*DSCR remains above 1.0x under all stress scenarios. EMC guarantee provides additional backstop.*
""",
    8: """\
# Section 8 — Changes in Engaged Banks

## A. ACRA Banking Charges

No new charges registered against EMA in ACRA as at the report date. Existing charges assigned to CUB for prior facilities.

## B. Banking Relationship Changes

| Bank | Change | Effective | Facility |
|------|--------|-----------|---------|
| Cathay United Bank | New bilateral SLL | Jun 2025 | USD 213.84m (this facility) |
| No exits | — | — | — |

No material changes in syndicate composition. No bank exits noted.

*(See Section 1 for facility details)*
""",
    9: """\
# Section 9 — Credit Analysis Checklist

## APPROVE — Subject to Conditions Precedent

| No | Category | Item | Response | Remarks |
|----|----------|------|----------|---------|
| 1 | Regulatory | MAS 612 classification confirmed | Yes | PASS — MSR 6 |
| 2 | Regulatory | Single borrower limit compliant | Yes | USD 436m vs. limit TWD 13.75bn |
| 3 | KYC/AML | KYC completed — no PEP/sanctions | Yes | Tier 1 customer |
| 4 | Financial | Audited financials reviewed (FY2024) | Yes | Deloitte; Unqualified |
| 5 | Financial | DSCR analysis completed | Yes | Base: 12.5x; Worse: 10.0x FY2025E |
| 6 | Collateral | Pre-delivery RG assessed satisfactory | Yes | IBK AA/AA-; fully covers |
| 7 | Collateral | Post-delivery VMC terms set | Yes | ACR 120%; LTV 83% |
| 8 | Legal | Governing law confirmed | Yes | Singapore |
| 9 | ESG | SLL KPIs agreed | Yes | CII improvement; ±5 bps ratchet |
| 10 | Pricing | Margin approved by ALCO | Yes | SOFR + 175 bps |

**Recommendation: APPROVE** — USD 213.84m Term Loan (SLL) to EMA, guaranteed by EMC, for construction finance of Hull H-2891.

## Conditions Precedent

1. Execution of facility agreement and security documents
2. KYC/AML completion for all parties
3. Receipt of satisfactory legal opinions (Singapore and South Korea)
4. Evidence of vessel insurance arrangements
5. First drawdown notice and CP satisfaction certificate
""",
    10: """\
# Section 10 — Appendix

## A. Group Exposure Summary

| Borrower | Booking | Current (USD M) | Proposed (USD M) | New? |
|----------|---------|-----------------|------------------|------|
| EMA | SG | — | 213.84 | ✓ New |
| EMA | SG | 128.75 | 128.75 | Existing |
| EMA | SG | 25.00 | 25.00 | Existing |
| **Group Total** | | **153.75** | **367.59** | |

**Approved Group Limit:** USD 750m | **Proposed Utilisation:** USD 367.59m (49%) ✓ Within Limit

## B. Fleet Overview (EMA/EMC)

| Category | Count | Capacity (TEU) |
|----------|-------|----------------|
| Owned | 105 | 1,050,000 |
| TC-in | 95 | 780,000 |
| On Order | 24 | 420,000 |
| **Total** | **224** | **2,250,000** |

Fleet growth target: 2.55m TEU by 2028 per EMC Analyst Day 2024.

## C. Base & Worse Case Financial Projections

*(See Section 7: Financial Analysis for detailed projections)*

| Scenario | FY2025E DSCR | FY2026E DSCR | Min DSCR |
|----------|-------------|-------------|---------|
| Base Case | 12.5x | 10.5x | 9.5x |
| Worse Case (-20% rates) | 10.0x | 8.2x | 7.3x |

## D. Facility Comparison

New deal benchmarked against comparable SLL transactions in the container shipping sector.
Margin of SOFR + 175 bps is within market range for BBB-equivalent shipping credits.
""",
}

ETL_MOCK_DATA = {
    "4": {
        "borrower_name": "Evergreen Marine (Asia) Pte. Ltd.",
        "parent": "Evergreen Marine Corporation (Taiwan) Ltd.",
        "incorporation_country": "Singapore",
        "fleet_owned": 105,
        "fleet_tc_in": 95,
        "vessels_on_order": 24,
        "total_capacity_teu": 2020000,
    },
    "7": {
        "revenue_usd_m": 2200,
        "ebitda_usd_m": 710,
        "net_income_usd_m": 450,
        "total_debt_usd_m": 1950,
        "total_equity_usd_m": 4070,
        "net_cash_usd_m": 2200,
        "interest_coverage": 36.5,
        "dscr_base_case": 12.5,
    },
}


def _make_mock_client() -> MagicMock:
    """Build a MagicMock Gemini client that returns section-appropriate markdown.

    Detection uses unique JSON keys from each section's analyst input data so
    cross-references to other sections in instructions/preceding outputs don't
    cause false matches.
    """
    client = MagicMock()

    # Detection order: check MOST-SPECIFIC unique JSON input keys first.
    # Keys must ONLY appear in that section's input_json, not in instructions of other sections.
    # Avoid short substrings (e.g. "ema" matches "demand", "emc" matches "scheme").
    # Check from highest to lowest so later sections win over earlier cross-references.
    SECTION_MARKERS: list[tuple[int, list[str]]] = [
        (10, ["10A_group_exposure", "10B_fleet_overview", "10C_financial_projections"]),
        (9,  ["9A_checklist", "9B_checklist_items"]),
        (8,  ["8A_acra_banking_charges", "8B_banking_changes"]),
        (7,  ["entities_to_analyze", "base_currency"]),  # unique to §7 input keys
        (6,  ["6A_project", "6B_milestones", "6C_builder_risk"]),
        (5,  ["5A_security_overview", "5B_pre_delivery", "5C_post_delivery"]),
        (4,  ["4A_borrower", "4B_fleet", "4C_market", "4D_shareholders"]),
        (3,  ["3A_external_ratings", "3B_internal_ratings", "3C_mas_612"]),
        (2,  ["2A_credit_overview", "2B_solvency", "2C_guarantor"]),
        (1,  ["facility_summary", "repayment_schedule", "ltc_percent"]),
    ]

    def _detect_section(text: str) -> int | None:
        for sec_no, markers in SECTION_MARKERS:
            if any(m in text for m in markers):
                return sec_no
        return None

    async def _async_generate(model, contents, config=None):
        text = str(contents)
        # ETL detection: user_prompt contains these unique markers (see etl.py _build_etl_prompt)
        if "---DOCUMENT TEXT START---" in text or "Return ONLY valid JSON" in text:
            return _mock_response(json.dumps(ETL_MOCK_DATA), tokens=600)
        # Section detection by unique input keys
        sec_no = _detect_section(text)
        if sec_no is not None and sec_no in SECTION_MD:
            return _mock_response(SECTION_MD[sec_no], tokens=1200)
        return _mock_response("# Mock Section\n\nGenerated content.", tokens=400)

    def _sync_generate(model, contents, config=None):
        text = str(contents)
        if "---DOCUMENT TEXT START---" in text or "Return ONLY valid JSON" in text:
            return _mock_response(json.dumps(ETL_MOCK_DATA), tokens=600)
        return _mock_response("Extracted text.", tokens=300)

    client.aio.models.generate_content = _async_generate
    client.models.generate_content = _sync_generate
    return client


# ── Main test coroutine ───────────────────────────────────────────────────────

async def run_mock_pipeline() -> int:
    section("MOCK PIPELINE — IN-PROCESS FULL AI REPORT GENERATION")

    # Patch Gemini client BEFORE importing the ASGI app
    mock_client = _make_mock_client()

    with patch("google.genai.Client", return_value=mock_client):
        # Import app inside the patch context so it's active when lifespan runs
        from main import _safe_add_columns, _seed_admin, app  # type: ignore  # noqa: PLC0415
        from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

        # Explicitly run lifespan setup — ASGITransport does not send ASGI lifespan
        # events, so tables and the admin account may not exist on a fresh database.
        from credit_report.database import Base, engine  # noqa: PLC0415
        async with engine.begin() as _conn:
            await _conn.run_sync(Base.metadata.create_all)
            await _safe_add_columns(_conn)
        await _seed_admin()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            API = "/api/credit-report"
            H: dict[str, str] = {}
            HJ: dict[str, str] = {}
            rid = ""

            # ── Step 1: Auth ───────────────────────────────────────────────────
            section("STEP 1 — AUTHENTICATION")
            r = await client.post(f"{API}/auth/login", data={
                "username": os.environ["ADMIN_EMAIL"],
                "password": os.environ["ADMIN_PASSWORD"],
            })
            if not check(r.status_code == 200, "POST /auth/login → 200",
                         f"status={r.status_code} body={r.text[:100]}"):
                fail("FATAL: cannot authenticate"); return results["fail"]
            token = r.json()["access_token"]
            H = {"Authorization": f"Bearer {token}"}
            HJ = {**H, "Content-Type": "application/json"}
            ok("Token obtained", f"{token[:28]}…")

            # ── Step 2: Create report ──────────────────────────────────────────
            section("STEP 2 — CREATE REPORT")
            r = await client.post(f"{API}/reports", headers=HJ, json={
                "borrower_name": "EMA Maritime Holdings [Mock Pipeline]",
                "industry": "marine",
                "report_type": "New Deal — Ship Finance",
                "booking_branch": "SG",
            })
            if not check(r.status_code == 201, "POST /reports → 201",
                         f"status={r.status_code}"):
                fail("FATAL: cannot create report"); return results["fail"]
            rid = r.json()["id"]
            ok("Report created", f"id={rid}")

            # ── Step 3: Upload documents ───────────────────────────────────────
            section("STEP 3 — UPLOAD 6 DOCUMENTS")
            DOCS = {
                "ema_annual_report_fy2024.txt": (
                    b"EMA Maritime Holdings\nFY2024 Annual Report\n"
                    b"Revenue USD2,200m | EBITDA USD710m | Net Cash USD2,200m\n"
                    b"Fleet: 105 owned | 95 TC-in | 24 on order | 2.02m TEU\n"
                    b"D/E 0.38x | Interest Coverage 36.5x | Total Assets USD7,975m\n",
                    "annual_report",
                ),
                "emc_financial_statement_fy2024.txt": (
                    b"Evergreen Marine Corporation\nFY2024 Financial Statement\n"
                    b"Revenue TWD381.2bn | Net Income TWD73.9bn | Cash TWD198.3bn\n"
                    b"Total Debt TWD87.2bn | D/E 0.20x | Interest Coverage 31.2x\n",
                    "financial_statement",
                ),
                "sbc_hull_h2891.txt": (
                    b"Shipbuilding Contract H-2891\n"
                    b"Buyer: EMA | Builder: Samsung Heavy Industries | Price: USD267,300,000\n"
                    b"Delivery: June 2026 | Grace: 210 days\n",
                    "shipbuilding_contract",
                ),
                "ibk_refund_guarantee.txt": (
                    b"Refund Guarantee | Issuer: Industrial Bank of Korea (IBK)\n"
                    b"Rating: AA (S&P) / AA- (Fitch) | Amount: USD267,300,000\n"
                    b"Unconditional and irrevocable on demand\n",
                    "legal_document",
                ),
                "clarkson_valuation_2025.txt": (
                    b"Vessel Valuation | Valuer: Clarkson Research\n"
                    b"Market Value: USD267,300,000 | Distressed: USD213,840,000\n"
                    b"Vessel: 20,000 TEU LNG Dual Fuel (Hull H-2891) | Oct 2025\n",
                    "valuation_report",
                ),
                "emc_kyc_2025.txt": (
                    b"KYC/CDD Report | Entity: EMA\n"
                    b"UEN: 202100001Z | Tier 1 Customer | PEP: None | Sanctions: Clear\n",
                    "kyc_document",
                ),
            }
            doc_ids: list[str] = []
            for fname, (content, doc_type) in DOCS.items():
                r = await client.post(
                    f"{API}/reports/{rid}/documents",
                    headers=H,
                    files={"file": (fname, io.BytesIO(content), "text/plain")},
                    data={"document_type": doc_type},
                )
                if r.status_code == 201:
                    doc_ids.append(r.json()["id"])
                    ok(f"Upload {fname}", f"type={doc_type}")
                else:
                    fail(f"Upload {fname}", f"status={r.status_code}")
            check(len(doc_ids) == len(DOCS), f"All {len(DOCS)} documents uploaded",
                  f"got={len(doc_ids)}")

            # ── Step 4: ETL extraction (with mock Gemini) ──────────────────────
            section("STEP 4 — ETL EXTRACTION (MOCKED GEMINI)")
            etl_sections_found: set[int] = set()
            for doc_id in doc_ids[:3]:  # ETL first 3 docs for speed
                r = await client.post(
                    f"{API}/reports/{rid}/documents/{doc_id}/etl",
                    headers=H,
                )
                if r.status_code == 200:
                    body = r.json()
                    secs = body.get("sections_extracted", [])
                    etl_sections_found.update(secs)
                    ok(f"ETL doc {doc_id[:8]}…", f"sections={secs}")
                else:
                    warn(f"ETL doc {doc_id[:8]}… → {r.status_code}",
                         r.json().get("detail", "")[:80] if r.content else "")
            ok(f"ETL extracted sections: {sorted(etl_sections_found)}")

            # ── Step 5: Section inputs §1-§10 ─────────────────────────────────
            section("STEP 5 — SECTION INPUTS §1-§10")
            SECTION_INPUTS: dict[int, dict] = {
                1: {
                    "borrower": "Evergreen Marine (Asia) Pte. Ltd.",
                    "guarantors": ["Evergreen Marine Corporation (Taiwan) Ltd."],
                    "all_facilities": [{"item": 1, "borrower": "EMA", "booking_office": "SG",
                        "current_facility_usd_m": None, "proposed_facility_usd_m": 213.84,
                        "is_new": True, "outstanding_usd_m": 0, "ccy": "USD",
                        "tenor": "12 years (4+8)", "facility_type": "Term Loan (SLL)",
                        "collateral": "IBK RG (pre); Vessel Mortgage (post)", "guarantor": "EMC"}],
                    "facility_type": "Committed Bilateral Term Loan (SLL)",
                    "facility_amount_usd_m": 213.84, "ltc_percent": 80, "tenor_years": 12,
                    "purpose": "Finance construction of one 20,000 TEU LNG dual-fuel containership (Hull H-2891, SHI)",
                    "repayment_schedule": "5% semi-annual; 30% balloon",
                    "interest_rate_basis": "Term SOFR", "margin_bps": 175,
                    "security_pre_delivery": "IBK Refund Guarantee; Assignment of SBC",
                    "security_post_delivery": "First priority vessel mortgage; EMC guarantee",
                    "value_maintenance_clause": {"acr_minimum_pct": 120, "ltv_maximum_pct": 83},
                    "group_limit": {"approved_group_limit_usd_m": 750,
                        "total_proposed_group_utilization_usd_m": 563.84, "within_limit": True},
                    "governing_law": "Singapore", "report_type": "new_deal",
                },
                2: {
                    "2A_credit_overview": {"bullets": [
                        {"order": 1, "text_verbatim": "EMC is the 7th largest container line globally with 2.02m TEU capacity"},
                        {"order": 2, "text_verbatim": "New USD213.84m SLL Term Loan to finance one 20,000 TEU LNG dual fuel vessel (Hull H-2891)"},
                        {"order": 3, "text_verbatim": "EMA net cash USD2.2bn; D/E 0.38x as at FY2024"},
                        {"order": 4, "text_verbatim": "Pre-delivery: IBK Refund Guarantee (AA/AA-) fully covering each installment"},
                    ], "tariff_impact_paragraphs": ["EMC minimal direct US tariff exposure."]},
                    "2B_solvency": {"ema": {"period": "FY2024", "cash_bn_usd": 2.20,
                        "op_ebitda_bn_usd": 0.71, "interest_coverage": 36.5}},
                    "2C_guarantor": {"guarantor_name_abbrev": "EMC", "cash_twd_bn": 198.3,
                        "interest_coverage": 31.2},
                    "2D_collateral": {"pre_delivery": {"issuer_full_name": "Industrial Bank of Korea",
                        "rating": "AA", "assigned_to_cub": True}},
                    "2E_risk_and_mitigants": {"risks": [
                        {"risk_no": 1, "level": "High", "title": "Freight rate volatility",
                         "risk_bullets": ["CCFI -28% YoY"], "mitigant_bullets": ["12-yr TC with EMC"]},
                    ]},
                    "report_type": "new_deal",
                },
                3: {
                    "3A_external_ratings": {"all_nil": True, "ratings": []},
                    "3B_internal_ratings": {"rows": [
                        {"entity_full_name": "EMA", "entity_abbrev": "EMA", "role": "Borrower",
                         "current": "6", "remarks": "Proposed MSR6"},
                        {"entity_full_name": "EMC", "entity_abbrev": "EMC", "role": "Guarantor",
                         "current": "5", "remarks": ""},
                    ]},
                    "3C_mas_612": {"grade": "PASS",
                        "primary_paragraph_verbatim": "Borrower rated MSR 6, mapped to PASS."},
                    "3D_esg_rating": {"entity_abbrev": "EMA", "rating_date": "2025-01-15",
                        "score_overall": "B+", "provider": "Sustainalytics"},
                },
                4: {
                    "4A_borrower": {"name": "EMA Maritime Holdings Ltd",
                        "incorporation_country": "Singapore", "is_listed": False,
                        "is_subsidiary": True, "parent_name": "Evergreen Marine Corporation"},
                    "4B_fleet": {"owned": 105, "tc_in": 95, "on_order": 24,
                        "total_capacity_teu": 2020000},
                    "4C_market": {"freight_index_name": "CCFI", "freight_index_9m2025": 1220,
                        "market_commentary": "CCFI averaged 1,220 in 9M2025."},
                    "4D_shareholders": [{"name": "Evergreen Marine Corporation", "pct": 100.0}],
                    "4E_key_management": [{"name": "Mr. Chen", "title": "CEO"}],
                    "4F_track_record": {"existing_customer": True, "since_year": 2015,
                        "outstanding_usd_m": 153.75},
                    "report_type": "new_deal",
                },
                5: {
                    "5A_security_overview": {"security_type": "Two-stage: RG pre-delivery; vessel mortgage post-delivery"},
                    "5B_pre_delivery": {"rg_issuer": "Industrial Bank of Korea (IBK)",
                        "rg_rating_sp": "AA", "rg_rating_fitch": "AA-",
                        "rg_amount_usd_m": 267.3, "assigned_to_bank": True},
                    "5C_post_delivery": {"first_priority_mortgage": True, "ltc_pct": 80,
                        "acr_minimum_pct": 120, "ltv_maximum_pct": 83,
                        "vmc_test_frequency": "Every 2 years", "vmc_cure_period_days": 21},
                    "5D_guarantor": {"name": "EMC", "net_cash_usd_bn": 6.1,
                        "de_ratio": 0.20, "interest_coverage": 31.2},
                },
                6: {
                    "6A_project": {"vessel_spec": "20,000 TEU LNG dual fuel containership",
                        "hull_no": "H-2891", "builder": "Samsung Heavy Industries",
                        "contract_price_usd_m": 267.3, "cub_facility_usd_m": 213.84,
                        "ltc_pct": 80, "delivery_date": "2026-06", "grace_period_days": 210},
                    "6B_milestones": [
                        {"event": "Steel Cutting", "date": "2024-09", "status": "complete"},
                        {"event": "Keel Laying", "date": "2025-01", "status": "complete"},
                        {"event": "Delivery", "date": "2026-06", "status": "pending"},
                    ],
                    "6C_builder_risk": {"builder_rating": "BBB+", "kdb_shareholder": True,
                        "order_backlog_usd_bn": 19},
                },
                7: {
                    "entities_to_analyze": ["EMA", "EMC"],
                    "base_currency": "USD",
                    "ema": {
                        "financials": [
                            {"period": "FY2022", "revenue": 3150, "ebitda": 2100,
                             "net_income": 1830, "total_debt": 2800, "total_equity": 5200,
                             "net_debt_cash": -1200, "interest_expense": 45},
                            {"period": "FY2023", "revenue": 2890, "ebitda": 1420,
                             "net_income": 1100, "total_debt": 2100, "total_equity": 4600,
                             "net_debt_cash": -800, "interest_expense": 38},
                            {"period": "FY2024", "revenue": 2200, "ebitda": 710,
                             "net_income": 450, "total_debt": 1950, "total_equity": 4070,
                             "net_debt_cash": -2200, "interest_expense": 19},
                        ],
                        "dscr": {
                            "base_case": [{"year": "FY2025E", "ebitda": 650, "debt_service": 52, "dscr": 12.5}],
                            "worse_case": [{"year": "FY2025E", "ebitda": 520, "debt_service": 52, "dscr": 10.0}],
                        },
                    },
                    "emc": {
                        "financials": [
                            {"period": "FY2024", "revenue_twd_bn": 381.2,
                             "ebitda_twd_bn": 105.2, "net_income_twd_bn": 73.9,
                             "cash_twd_bn": 198.3, "total_debt_twd_bn": 87.2},
                        ],
                    },
                },
                8: {
                    "8A_acra_banking_charges": {"new_charges": False,
                        "comments": "No new charges. Prior charges assigned to CUB."},
                    "8B_banking_changes": [],
                },
                9: {
                    "9A_checklist": {"recommendation": "APPROVE",
                        "conditions_precedent": ["Facility agreement execution", "KYC completion"]},
                    "9B_checklist_items": [
                        {"no": 1, "category": "Regulatory", "item": "MAS 612 confirmed", "response": "Yes"},
                        {"no": 2, "category": "KYC/AML", "item": "KYC completed", "response": "Yes"},
                        {"no": 3, "category": "Financial", "item": "Audited financials reviewed", "response": "Yes"},
                        {"no": 4, "category": "Collateral", "item": "Pre-delivery RG satisfactory", "response": "Yes"},
                        {"no": 5, "category": "ESG", "item": "SLL KPIs agreed", "response": "Yes"},
                    ],
                },
                10: {
                    "10A_group_exposure": {
                        "approved_group_limit_usd_m": 750,
                        "rows": [{"item": 1, "borrower": "EMA", "current_usd_m": 0,
                            "proposed_usd_m": 213.84, "is_new": True}],
                    },
                    "10B_fleet_overview": {"owned": 105, "tc_in": 95, "on_order": 24,
                        "total_capacity_teu": 2020000, "fleet_growth_target_teu": 2550000},
                    "10C_financial_projections": {"base_dscr_fy2025": 12.5, "worse_dscr_fy2025": 10.0},
                },
            }

            saved = 0
            for sec_no, payload in SECTION_INPUTS.items():
                r = await client.put(
                    f"{API}/reports/{rid}/inputs/{sec_no}",
                    headers=HJ,
                    json={"section_no": sec_no, "input_json": payload},
                )
                if r.status_code == 200:
                    saved += 1
                    ok(f"PUT §{sec_no} inputs → 200", f"fields={len(payload)}")
                else:
                    fail(f"PUT §{sec_no} inputs → {r.status_code}", r.text[:80])
            check(saved == 10, f"All 10 sections saved ({saved}/10)")

            # ── Step 6: Generate all sections in dependency order ──────────────
            section("STEP 6 — AI GENERATION §1-§10 (MOCKED GEMINI)")
            GENERATION_ORDER = [4, 7, 1, 3, 2, 5, 6, 8, 9, 10]
            generated: dict[int, str] = {}
            gen_statuses: dict[int, str] = {}

            for sec_no in GENERATION_ORDER:
                t0 = time.perf_counter()
                r = await client.post(
                    f"{API}/reports/{rid}/generate/{sec_no}",
                    headers=HJ,
                )
                elapsed = time.perf_counter() - t0
                body = r.json() if r.content else {}
                if r.status_code == 200:
                    status = body.get("status", "?")
                    tokens = body.get("tokens_used", 0)
                    gen_statuses[sec_no] = status
                    if status == "done":
                        ok(f"§{sec_no} generated", f"status={status} tokens={tokens} t={elapsed:.2f}s")
                        generated[sec_no] = status
                    else:
                        fail(f"§{sec_no} returned 200 but status={status}", str(body)[:80])
                else:
                    detail = body.get("detail", r.text[:80])
                    fail(f"§{sec_no} generate → {r.status_code}", str(detail)[:100])
                    gen_statuses[sec_no] = f"error:{r.status_code}"

            check(len(generated) == 10, f"All 10 sections generated",
                  f"done={len(generated)}/10")

            # ── Step 7: Output retrieval & quality checks ──────────────────────
            section("STEP 7 — OUTPUT RETRIEVAL & QUALITY CHECKS")
            r = await client.get(f"{API}/reports/{rid}/outputs", headers=H)
            check(r.status_code == 200, "GET /outputs → 200")
            all_outputs = r.json() if r.is_success else []
            done_count = sum(1 for o in all_outputs if o.get("status") == "done")
            check(done_count == 10, f"10 sections with status=done", f"done={done_count}")

            # Quality spot checks — verify realistic content in output
            QUALITY_KEYWORDS: dict[int, list[str]] = {
                1: ["213.84", "Term Loan", "SOFR", "Singapore"],
                2: ["EMC", "guarantee", "36.5x", "Risk"],
                4: ["EMA", "105", "TEU", "Singapore"],
                7: ["DSCR", "EBITDA", "FY2024", "36.5x"],
                9: ["APPROVE", "MAS", "KYC"],
                10: ["Group Total", "Owned", "2028"],
            }
            quality_pass = 0
            quality_total = 0
            for sec_no, keywords in QUALITY_KEYWORDS.items():
                r = await client.get(
                    f"{API}/reports/{rid}/sections/{sec_no}/output",
                    headers=H,
                )
                if r.status_code == 200:
                    md = r.json().get("markdown") or ""
                    for kw in keywords:
                        quality_total += 1
                        if kw.lower() in md.lower():
                            quality_pass += 1
                        else:
                            warn(f"§{sec_no} missing keyword '{kw}'", f"md_chars={len(md)}")
                    ok(f"§{sec_no} output retrieved", f"chars={len(md)}")
                else:
                    fail(f"§{sec_no} output → {r.status_code}")
            check(quality_pass == quality_total,
                  f"Quality keywords: {quality_pass}/{quality_total} found")

            # ── Step 8: DOCX export ────────────────────────────────────────────
            section("STEP 8 — DOCX EXPORT")
            r = await client.get(
                f"{API}/reports/{rid}/export/docx",
                headers=H,
            )
            if check(r.status_code == 200, "GET /export/docx → 200",
                     f"bytes={len(r.content)}"):
                check(len(r.content) > 5000, "DOCX non-trivial size",
                      f"bytes={len(r.content)}")
                ct = r.headers.get("content-type", "")
                check("openxmlformats" in ct or len(r.content) > 1000,
                      "DOCX content-type OK", f"ct={ct}")
                docx_path = f"/tmp/mock_report_{rid[:8]}.docx"
                Path(docx_path).write_bytes(r.content)
                ok("DOCX saved", docx_path)
            else:
                warn("DOCX export failed", f"status={r.status_code} body={r.text[:80]}")

            # ── Step 9: Full /generate (batch) endpoint ────────────────────────
            section("STEP 9 — BATCH GENERATION ENDPOINT (/generate)")
            # Create a fresh report to test the batch endpoint
            r2 = await client.post(f"{API}/reports", headers=HJ, json={
                "borrower_name": "EMA [Batch Test]", "industry": "marine",
                "report_type": "Annual Review", "booking_branch": "SG",
            })
            if r2.status_code == 201:
                rid2 = r2.json()["id"]
                # Save inputs for all 10 sections
                for sec_no, payload in SECTION_INPUTS.items():
                    await client.put(f"{API}/reports/{rid2}/inputs/{sec_no}",
                        headers=HJ, json={"section_no": sec_no, "input_json": payload})
                # Run batch generate
                r_gen = await client.post(f"{API}/reports/{rid2}/generate", headers=HJ)
                if r_gen.status_code == 200:
                    batch = r_gen.json()
                    secs = batch.get("sections", {})
                    done_batch = sum(1 for v in secs.values() if v == "done")
                    check(done_batch == 10, f"Batch: all 10 done",
                          f"done={done_batch} statuses={dict(list(secs.items())[:5])}")
                elif r_gen.status_code == 503:
                    warn("Batch generate → 503", "GEMINI_API_KEY check — mock env check")
                else:
                    fail(f"Batch generate → {r_gen.status_code}", r_gen.text[:80])
            else:
                warn("Batch test report creation failed", f"status={r2.status_code}")

            # ── Step 10: Audit trail ───────────────────────────────────────────
            section("STEP 10 — AUDIT TRAIL")
            r = await client.get(f"{API}/reports/{rid}/audit", headers=H)
            check(r.status_code == 200, "GET /audit → 200")
            body = r.json() if r.is_success else {}
            events = body.get("events", []) if isinstance(body, dict) else body
            total = body.get("total", len(events)) if isinstance(body, dict) else len(events)
            check(total >= 20, f"≥20 audit events (auth+create+upload+etl+inputs+generate)",
                  f"total={total}")
            actions = list({e.get("action") for e in events if isinstance(e, dict)})
            ok("Audit action types", str(actions[:6]))
            check(any("section_input" in a for a in actions), "section_input events present",
                  f"actions={actions[:4]}")
            check(any("section.generat" in a or "generat" in a for a in actions),
                  "generation events present", f"actions={actions[:6]}")

            # ── Step 11: Status transition ─────────────────────────────────────
            section("STEP 11 — REPORT STATUS WORKFLOW")
            for new_status in ["validated", "review_in_progress", "approved"]:
                r = await client.patch(f"{API}/reports/{rid}/status",
                    headers=HJ, json={"status": new_status})
                check(r.status_code == 200, f"PATCH status → {new_status}",
                      f"got={r.json().get('status') if r.is_success else r.status_code}")

            # ── Step 12: Security checks ───────────────────────────────────────
            section("STEP 12 — SECURITY BOUNDARY CHECKS")
            # Unauthenticated request
            r = await client.get(f"{API}/reports")
            check(r.status_code in (401, 403), "Unauth → 401/403", f"status={r.status_code}")
            # Invalid token
            r = await client.get(f"{API}/reports",
                headers={"Authorization": "Bearer invalid.token.here"})
            check(r.status_code in (401, 403), "Bad token → 401/403", f"status={r.status_code}")
            # SQL injection in path
            r = await client.get(f"{API}/reports/'; DROP TABLE reports;--", headers=H)
            check(r.status_code in (404, 422), "SQL injection → 404/422",
                  f"status={r.status_code}")
            # Cross-report doc isolation
            r_other = await client.post(f"{API}/reports", headers=HJ, json={
                "borrower_name": "Other Corp", "industry": "other",
                "report_type": "Annual Review", "booking_branch": "HK"})
            if r_other.is_success:
                rid_other = r_other.json()["id"]
                r_docs = await client.get(f"{API}/reports/{rid_other}/documents", headers=H)
                other_docs = r_docs.json() if r_docs.is_success else []
                check(len(other_docs) == 0, "Cross-report isolation: no leaked docs",
                      f"count={len(other_docs)}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("  MOCK PIPELINE TEST — FINAL REPORT")
    print(f"{'='*70}\n")
    print("  Mode: 🟡 IN-PROCESS MOCK (google.genai.Client patched)")
    total = results["pass"] + results["warn"] + results["fail"]
    print(f"\n  ✅ PASSED : {results['pass']}/{total}")
    print(f"  ⚠️  WARNED : {results['warn']}/{total}")
    print(f"  ❌ FAILED : {results['fail']}/{total}")

    if results["fail"]:
        print("\n  ─── FAILURES ───")
        for d in results["details"]:
            if d["level"] == "fail":
                print(f"  ❌ {d['label']} — {d['detail'][:110]}")

    if results["warn"]:
        print("\n  ─── WARNINGS ───")
        for d in results["details"]:
            if d["level"] == "warn":
                print(f"  ⚠️  {d['label']} — {d['detail'][:90]}")

    if results["fail"] == 0:
        print("\n  🎉 FULL IN-PROCESS PIPELINE VERIFIED — all sections generated & quality-checked!")
    print(f"\n{'='*70}\n")
    return results["fail"]


def run_mock_pipeline_sync() -> int:
    return asyncio.run(run_mock_pipeline())


def test_mock_pipeline():
    """pytest-discoverable entry point for the in-process mock pipeline."""
    assert run_mock_pipeline_sync() == 0


if __name__ == "__main__":
    n_fail = run_mock_pipeline_sync()
    sys.exit(0 if n_fail == 0 else 1)

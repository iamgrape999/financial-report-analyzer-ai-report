from __future__ import annotations

import os
from pathlib import Path

ENVIRONMENT: str = os.getenv("ENVIRONMENT", os.getenv("APP_ENV", "development")).lower()
IS_PRODUCTION: bool = ENVIRONMENT in {"prod", "production"} or os.getenv("RENDER", "").lower() == "true"

# ── Database ─────────────────────────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./data/credit_report.db",
)
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
AUTO_CREATE_TABLES: bool = os.getenv("AUTO_CREATE_TABLES", "false" if IS_PRODUCTION else "true").lower() == "true"

# ── LLM ──────────────────────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CREDIT_REPORT_MODEL: str = os.getenv("CREDIT_REPORT_MODEL", "claude-sonnet-4-6")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
# GEMINI_MODEL is the base default; override OCR/ETL independently via their own env vars.
# OCR needs native PDF bytes support → keep on a capable vision model (Flash or Pro).
# ETL is text-in/JSON-out → can use a cheaper model (e.g. gemini-2.5-flash-lite).
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_OCR_MODEL: str = os.getenv("GEMINI_OCR_MODEL", GEMINI_MODEL)
GEMINI_ETL_MODEL: str = os.getenv("GEMINI_ETL_MODEL", GEMINI_MODEL)
CR_SECTION_MAX_TOKENS: int = int(os.getenv("CR_SECTION_MAX_TOKENS", "8192"))
CR_MAX_CONCURRENT_GENERATIONS: int = int(os.getenv("CR_MAX_CONCURRENT_GENERATIONS", "2"))
# Per-user per-day token budget for the analyst baseline role (default 4M ≈ 8 full reports).
# Reviewer/approver roles get 2×; admin gets 5×. See generation/quota.py _ROLE_LIMITS.
DAILY_TOKEN_LIMIT: int = int(os.getenv("DAILY_TOKEN_LIMIT", "4000000"))
LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "180"))

SECTION_MAX_OUTPUT_TOKENS: dict[int | str, int] = {
    1: 16384,   # §1 facility table + T&Cs (21 fields) + deal comparison + account strategy
    2: 16384,   # §2 five mandatory tables (T1-T5); 8192 default cuts off after T1 only
    3: 12288,   # §3 MSR table (multi-entity override remarks) + MAS 612 (4 paragraphs) + ESG
    5: 12288,   # §5 RG milestone table (8 col) + amortisation (up to 24 rows) + guarantor dual-currency
    6: 12288,   # §6 payment table (11 col) + construction risks (3-5 bullets each) + force majeure
    7: 16384,   # §7 full financial analysis with multi-year tables
    10: 16384,  # §10 appendix
    4: 12288,   # §4 corporate background
    9: 12288,   # §9 credit analysis checklist
    "default": 8192,
}

# ── Storage ──────────────────────────────────────────────────────────────────────────────────────
CREDIT_REPORTS_ROOT: Path = Path(os.getenv("CREDIT_REPORTS_ROOT", "./data/credit_reports"))
CR_MAX_CHUNKS_PER_SECTION: int = int(os.getenv("CR_MAX_CHUNKS_PER_SECTION", "12"))
CREDIT_REPORT_MAX_UPLOAD_MB: int = int(os.getenv("CREDIT_REPORT_MAX_UPLOAD_MB", "50"))

CORS_ALLOW_ORIGINS: str = os.getenv("CORS_ALLOW_ORIGINS", "*")

# ── Auth ──────────────────────────────────────────────────────────────────────────────────────
DEFAULT_SECRET_KEY = "dev-secret-key-change-in-production"
SECRET_KEY: str = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY)
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS: int = 7

# ── PromptOps ──────────────────────────────────────────────────────────────────────────────────────
PROMPT_AUTO_DEPLOY: bool = os.getenv("PROMPT_AUTO_DEPLOY", "false").lower() == "true"
GOLDEN_DATASET_ROOT: Path = Path(os.getenv("GOLDEN_DATASET_ROOT", "./data/golden_datasets"))

# ── Paths ──────────────────────────────────────────────────────────────────────────────────────
MODULE_ROOT: Path = Path(__file__).parent
INDUSTRY_TEMPLATES_ROOT: Path = MODULE_ROOT / "industry_templates"

# ── Generation ordering ────────────────────────────────────────────────────────────────────────────────────────────
GENERATION_ORDER: list[int] = [4, 7, 1, 3, 2, 5, 6, 8, 9, 10]

SECTION_HARD_DEPENDENCIES: dict[int, list[int]] = {
    2: [7],
    3: [7],
    5: [1],
    6: [1, 5],
    9: [1, 2, 3, 4, 5, 6, 7, 8],
    10: [7, 1],
}

SECTION_SOFT_DEPENDENCIES: dict[int, list[int]] = {
    3: [4],
    2: [4],
}

# ── Evidence retrieval keywords per section ──────────────────────────────────────────────────────────────────────────────────
SECTION_RETRIEVAL_KEYWORDS: dict[int, list[str]] = {
    1: ["facility", "loan", "tenor", "collateral", "guarantor", "margin", "SOFR", "LIBOR",
        "commitment", "repayment", "covenant", "Banking Act", "33-3", "drawdown", "tranche",
        "sustainability-linked", "SLL", "KPI", "value maintenance", "ACR", "group limit"],
    2: ["solvency", "repayment", "guarantor", "collateral", "risk", "DSCR", "recommendation",
        "approve", "decline", "LTC", "ACR", "summary", "executive", "overall",
        "NII", "net interest income", "downturn", "balloon", "account strategy"],
    3: ["rating", "MSR", "masterscale", "MAS 612", "ESG", "sanctions", "OFAC",
        "MSCI", "Sustainalytics", "Taiwan CG", "corporate governance", "climate",
        "Poseidon", "internal rating", "watch list", "country risk", "industry risk"],
    4: ["corporate", "management", "shareholders", "operations", "fleet", "beneficial owner",
        "incorporation", "company", "customers", "revenue", "charter", "shipping",
        "holding", "board of directors", "annual report", "market position",
        "公司現況", "company overview", "集團總運能", "全球航線", "子公司",
        "自有碼頭", "terminal", "全球排名", "市佔率", "碼頭數",
        "燃油成本", "港埠費用", "船艙租", "成本結構", "法人說明會",
        "TEU", "container", "UBN", "listing", "peer comparison", "market share",
        "SCFI", "CCFI", "運價指數", "freight index", "海洋聯盟", "OCEAN ALLIANCE",
        "CMA CGM", "COSCO", "OOCL", "Ocean Alliance", "alliance",
        "GDP", "IMF", "經濟展望", "economic outlook", "WTI", "BRENT", "油價",
        "地緣政治", "geopolitical", "蘇伊士運河", "Suez", "紅海", "Red Sea",
        "遠東北美", "遠東歐洲", "Far East", "trade lane", "東西航線",
        "運力增長", "capacity growth", "newbuilding", "新船", "ALPHALINER",
        "市場運價", "航運市場供需", "週運力"],
    5: ["collateral", "mortgage", "refund guarantee", "ACR", "LTV", "LTC",
        "valuation", "vessel value", "Clarkson", "BRS", "distressed",
        "insurance", "P&I", "hull", "market value", "advance rate",
        "value maintenance clause", "balloon", "responsible person", "guarantee scope"],
    6: ["project", "vessel", "hull number", "shipbuilding", "milestone", "delivery",
        "shipyard", "keel laying", "launch", "contract price", "charter party",
        "deadweight", "DWT", "class society", "flag state", "construction",
        "DNV GL", "dock", "CGT", "IMO", "drawdown", "construction progress"],
    7: ["financial", "revenue", "EBITDA", "net income", "debt", "cash flow", "NTD",
        "USD", "balance sheet", "income statement", "interest expense", "leverage",
        "tangible leverage", "DSCR", "ROA", "ROE", "audit", "IFRS", "depreciation",
        "capex", "free cash flow", "profit", "loss", "equity",
        "CCFI", "SCFI", "EMC", "EMA", "standalone", "consolidated"],
    8: ["charge", "ACRA", "UEN", "chargee", "charge date", "outstanding", "satisfied",
        "bank relationship", "engaged bank", "credit facility", "committed",
        "exposure", "limit", "banking pattern", "credit pattern"],
    9: ["KYC", "AML", "sanctions", "OFAC", "PEP", "compliance", "checklist",
        "conditions precedent", "CP", "covenant", "ACR covenant", "listing requirement",
        "insurance requirement", "negative pledge", "change of control",
        "approval authority", "credit committee", "recommendation"],
    10: ["DSCR projection", "appendix", "fleet schedule", "repayment schedule",
         "sensitivity analysis", "stress test", "BDI", "market overview",
         "capacity", "projection", "LTV schedule", "ACR schedule",
         "group exposure", "MSR", "fleet growth", "base case", "worse case",
         "blocking data", "data gaps", "QA",
         "運費", "freight rate", "運量", "volume", "TEU", "油價", "fuel cost",
         "月份", "monthly", "單季", "quarterly revenue", "各單季",
         "合併運費", "價量", "流動比率", "負債比率",
         "USD/TEU", "USD/TON", "萬TEU"],
    11: ["rating", "buy", "hold", "sell", "neutral", "target price", "目標價",
         "price target", "analyst", "EPS", "每股盈餘", "投資建議", "investment recommendation",
         "earnings per share", "forecast", "estimate", "projected", "预估", "預估",
         "revenue estimate", "quarterly", "1Q", "2Q", "3Q", "4Q", "valuation",
         "PBR", "PER", "upside", "downside", "research report", "個股報告",
         "broker", "investment bank", "securities", "群益", "capital", "元大",
         "年度預測", "季度預測", "EPS forecast", "dividend yield", "殖利率",
         "資產負債表", "balance sheet", "損益表", "income statement", "現金流量表",
         "cash flow", "比率分析", "ratio analysis", "毛利率", "營業利益", "稅後純益",
         "流動比率", "負債比率", "ROA", "ROE", "應收帳款", "ESG", "碳排放",
         "季度損益表", "quarterly income", "EBITDA", "資本支出", "現金增資"],
}

# ── Continuation tokens ────────────────────────────────────────────────────────────────────────────────────────────
CONTINUATION_END_TOKENS: dict[int, str | None] = {
    1: "[§1 CONTINUED IN NEXT OUTPUT]",
    2: "[§2 CONTINUED IN NEXT OUTPUT]",
    3: "[§3 CONTINUED IN NEXT OUTPUT]",
    4: "[§4 CONTINUED IN NEXT OUTPUT]",
    5: "[§5 CONTINUED IN NEXT OUTPUT]",
    6: "[§6 CONTINUED IN NEXT OUTPUT]",
    7: "[§7 CONTINUED — PART 2 FOLLOWS]",
    8: None,
    9: "[§9 CONTINUED — PART 2 FOLLOWS]",
    10: "[§10 CONTINUED — PART 2]",
}

CONTINUATION_RESUME_TOKENS: dict[int, str | None] = {
    1: "[§1 CONTINUED]",
    2: "[§2 CONTINUED]",
    3: "[§3 CONTINUED]",
    4: "[§4 CONTINUED]",
    5: "[§5 CONTINUED]",
    6: "[§6 CONTINUED]",
    7: "[§7 CONTINUED]",
    8: None,
    9: "[§9 CONTINUED]",
    10: "[§10 CONTINUED]",
}


def validate_runtime_security() -> None:
    """Fail fast when production security-sensitive settings are unsafe."""
    import logging as _logging
    _cfg_logger = _logging.getLogger(__name__)
    if IS_PRODUCTION and SECRET_KEY == DEFAULT_SECRET_KEY:
        raise RuntimeError("SECRET_KEY must be set to a strong non-default value in production")
    if not IS_PRODUCTION and SECRET_KEY == DEFAULT_SECRET_KEY:
        _cfg_logger.warning(
            "SECRET_KEY is still set to the default development value. "
            "Set a strong SECRET_KEY before deploying to production."
        )
    if IS_PRODUCTION and CORS_ALLOW_ORIGINS.strip() in ("*", ""):
        _cfg_logger.warning(
            "CORS_ALLOW_ORIGINS is set to wildcard '*' in production. "
            "Set CORS_ALLOW_ORIGINS to your actual frontend origin (e.g. https://your-app.onrender.com) "
            "to prevent cross-origin attacks."
        )


def parse_cors_origins(raw_origins: str | None = None) -> list[str]:
    """Parse comma-separated CORS origins, removing blanks and duplicates."""
    raw = CORS_ALLOW_ORIGINS if raw_origins is None else raw_origins
    origins: list[str] = []
    for origin in raw.split(","):
        cleaned = origin.strip()
        if cleaned and cleaned not in origins:
            origins.append(cleaned)
    return origins or ["*"]

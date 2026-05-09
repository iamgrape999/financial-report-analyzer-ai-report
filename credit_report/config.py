from __future__ import annotations

import os
from pathlib import Path

# ── Database ─────────────────────────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./data/credit_report.db",
)
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ── LLM ──────────────────────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CREDIT_REPORT_MODEL: str = os.getenv("CREDIT_REPORT_MODEL", "claude-sonnet-4-6")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CR_SECTION_MAX_TOKENS: int = int(os.getenv("CR_SECTION_MAX_TOKENS", "8192"))
CR_MAX_CONCURRENT_GENERATIONS: int = int(os.getenv("CR_MAX_CONCURRENT_GENERATIONS", "2"))
DAILY_TOKEN_LIMIT: int = int(os.getenv("DAILY_TOKEN_LIMIT", "4000000"))

SECTION_MAX_OUTPUT_TOKENS: dict[int | str, int] = {
    7: 16384,
    10: 16384,
    4: 12288,
    9: 12288,
    "default": 8192,
}

# ── Storage ──────────────────────────────────────────────────────────────────────────────────────
CREDIT_REPORTS_ROOT: Path = Path(os.getenv("CREDIT_REPORTS_ROOT", "./data/credit_reports"))
CR_MAX_CHUNKS_PER_SECTION: int = int(os.getenv("CR_MAX_CHUNKS_PER_SECTION", "12"))
CREDIT_REPORT_MAX_UPLOAD_MB: int = int(os.getenv("CREDIT_REPORT_MAX_UPLOAD_MB", "50"))

# ── Auth ──────────────────────────────────────────────────────────────────────────────────────
SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
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
    1: ["facility", "tenor", "collateral", "guarantor", "regulatory", "Banking Act", "33-3"],
    2: ["solvency", "repayment", "guarantor", "collateral", "risk", "tariff", "DSCR"],
    3: ["rating", "MSR", "MAS 612", "default", "ESG", "sanctions"],
    4: ["corporate", "management", "shareholders", "operations", "fleet"],
    5: ["collateral", "mortgage", "refund guarantee", "ACR", "LTV", "IBK"],
    6: ["project", "vessel", "hull", "shipbuilding", "milestone", "delivery"],
    7: ["financial", "revenue", "EBITDA", "debt", "cash flow", "NTD"],
    8: ["ACRA", "charge", "bank", "lender", "mortgage"],
    9: ["checklist", "compliance", "KYC", "AML", "sanctions"],
    10: ["appendix", "capacity", "projection", "DSCR", "FY2025"],
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

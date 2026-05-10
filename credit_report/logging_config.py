"""Centralised logging configuration for the credit-report service.

Call setup_logging() once at application startup (in main.py lifespan).
Every module then uses:  logger = logging.getLogger(__name__)
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time
import uuid
from pathlib import Path

from credit_report.config import IS_PRODUCTION

LOG_DIR = Path("./data/logs")
LOG_FILE = LOG_DIR / "app.log"
ERROR_LOG_FILE = LOG_DIR / "errors.log"

_FMT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str | None = None) -> None:
    """Configure root logger with console + rotating file handlers."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    effective_level = level or ("WARNING" if IS_PRODUCTION else "DEBUG")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # root always DEBUG; handlers filter
    root.handlers.clear()

    formatter = logging.Formatter(_FMT, _DATE_FMT)

    # ── Console handler ──────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(effective_level)
    root.addHandler(console)

    # ── Rotating app log (all levels) ────────────────────────────────────────
    app_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=10,
        encoding="utf-8",
    )
    app_handler.setFormatter(formatter)
    app_handler.setLevel(logging.DEBUG)
    root.addHandler(app_handler)

    # ── Error-only log (WARNING+) ─────────────────────────────────────────────
    err_handler = logging.handlers.RotatingFileHandler(
        ERROR_LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    err_handler.setFormatter(formatter)
    err_handler.setLevel(logging.WARNING)
    root.addHandler(err_handler)

    # ── Quiet noisy third-party libraries ────────────────────────────────────
    for lib in ("httpx", "httpcore", "google", "urllib3", "multipart"):
        logging.getLogger(lib).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if not IS_PRODUCTION else logging.WARNING
    )

    logging.getLogger(__name__).info(
        "Logging configured | level=%s | app_log=%s | error_log=%s | production=%s",
        effective_level,
        LOG_FILE,
        ERROR_LOG_FILE,
        IS_PRODUCTION,
    )

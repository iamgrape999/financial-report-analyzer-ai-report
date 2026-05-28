from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from credit_report import router as credit_report_router
from credit_report.config import IS_PRODUCTION, validate_runtime_security
from credit_report.database import AsyncSessionLocal, Base, engine
from credit_report.logging_config import setup_logging

# Import all models so Base.metadata knows about every table
import credit_report.models  # noqa: F401
import credit_report.security.models  # noqa: F401
import credit_report.audit.events  # noqa: F401
import credit_report.fact_store.models  # noqa: F401
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.block_ast.models  # noqa: F401
import credit_report.generation.models  # noqa: F401

from sqlalchemy import text

from credit_report.security.models import User
from credit_report.security.auth import hash_password


async def _safe_add_columns(conn) -> None:
    """Add new columns to existing tables without failing if they already exist."""
    # section_documents: document_type, file_format, etl_status columns (Sprint 2)
    new_cols = [
        ("section_documents", "document_type", "VARCHAR(50) DEFAULT 'other'"),
        ("section_documents", "file_format",   "VARCHAR(10)"),
        ("section_documents", "etl_status",    "VARCHAR(20) DEFAULT 'pending'"),
    ]
    for table, col, col_def in new_cols:
        try:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"))
            logger.info("_safe_add_columns: added %s.%s", table, col)
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                pass  # expected on subsequent startups
            else:
                logger.warning("_safe_add_columns: unexpected error adding %s.%s: %s", table, col, e)

    # Widen varchar-limited columns to TEXT on PostgreSQL.
    # create_all never alters existing columns, so older production DBs retain
    # the original VARCHAR(255) for display_value and VARCHAR(100) for column_id/row_id.
    # AI-generated table cells can exceed those limits, causing StringDataRightTruncationError.
    # ALTER to TEXT is instant on PostgreSQL (no rewrite) and a no-op if already TEXT.
    # SQLite does not enforce VARCHAR lengths, so these statements will fail there — caught below.
    _widen = [
        "ALTER TABLE table_cells ALTER COLUMN display_value TYPE TEXT",
        "ALTER TABLE table_cells ALTER COLUMN column_id    TYPE TEXT",
        "ALTER TABLE table_cells ALTER COLUMN row_id       TYPE TEXT",
    ]
    for stmt in _widen:
        try:
            await conn.execute(text(stmt))
        except Exception as e:
            msg = str(e).lower()
            if "syntax error" in msg or "not supported" in msg or "unsupported" in msg or "near" in msg:
                pass  # SQLite doesn't support ALTER COLUMN TYPE — expected
            else:
                logger.warning("_safe_add_columns: unexpected error widening column: %s", e)

    # Deduplicate section_inputs and section_outputs: keep the row with the lowest id
    # per (report_id, section_no). This runs index-first: if the unique index already
    # exists (subsequent startups), CREATE ... IF NOT EXISTS is a no-op and no data is
    # touched. Only when the index cannot be created because duplicate rows exist (first
    # deployment against a database that already has legacy concurrent-write duplicates)
    # does the one-time DELETE run before creating the index.
    for tbl in ("section_inputs", "section_outputs"):
        idx_name = f"uq_{tbl}_report_section"
        try:
            # IF NOT EXISTS → no-op when index is already there (all subsequent startups).
            # Fails with duplicate-key error only when legacy duplicate rows block creation.
            await conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} "
                f"ON {tbl} (report_id, section_no)"
            ))
            logger.info("_safe_add_columns: unique constraint on %s verified", tbl)
        except Exception:
            # Legacy duplicate rows are blocking index creation — clean up once.
            try:
                await conn.execute(text(
                    f"DELETE FROM {tbl} WHERE id NOT IN ("
                    f"  SELECT MIN(id) FROM {tbl} GROUP BY report_id, section_no"
                    f")"
                ))
                await conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} "
                    f"ON {tbl} (report_id, section_no)"
                ))
                logger.info("_safe_add_columns: one-time deduplicated and indexed %s", tbl)
            except Exception as e:
                logger.warning("_safe_add_columns: dedup %s failed: %s", tbl, e)

# Initialise logging before anything else logs
setup_logging()
logger = logging.getLogger(__name__)


async def _seed_admin() -> None:
    email = os.getenv("ADMIN_EMAIL", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        logger.info("ADMIN_EMAIL / ADMIN_PASSWORD not set — skipping admin seed")
        return
    # ADMIN_BOOTSTRAP_OVERRIDE=true re-syncs credentials from env on every restart.
    # Without it this function is first-run-only: it creates the account but never
    # silently overwrites a password that may have been changed in production.
    allow_override = os.getenv("ADMIN_BOOTSTRAP_OVERRIDE", "").lower() == "true"
    async with AsyncSessionLocal() as session:
        # Exact-match first, then case-insensitive fallback
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            result2 = await session.execute(select(User).where(User.email.ilike(email)))
            user = result2.scalar_one_or_none()
            if user:
                logger.info("_seed_admin: found existing account via case-insensitive match stored=%r env=%r",
                            user.email, email)

        if user:
            if allow_override:
                user.hashed_password = hash_password(password)
                user.is_active = True
                user.role = "admin"
                await session.commit()
                logger.info("_seed_admin: ADMIN_BOOTSTRAP_OVERRIDE — synced credentials for admin=%s", user.email)
            else:
                logger.info(
                    "_seed_admin: admin account already exists email=%s — skipping credential sync "
                    "(set ADMIN_BOOTSTRAP_OVERRIDE=true to force env-var sync)",
                    user.email,
                )
        else:
            session.add(User(
                id=str(uuid.uuid4()),
                email=email,
                hashed_password=hash_password(password),
                role="admin",
                is_active=True,
            ))
            await session.commit()
            logger.info("_seed_admin: created new admin account email=%s", email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== Service starting (production=%s) ===", IS_PRODUCTION)
    try:
        validate_runtime_security()
    except RuntimeError as e:
        logger.critical("Security validation failed: %s", e)
        raise

    async with engine.begin() as conn:
        try:
            await asyncio.wait_for(conn.run_sync(Base.metadata.create_all), timeout=30.0)
        except asyncio.TimeoutError:
            logger.critical("Database create_all timed out after 30 s — aborting startup")
            raise RuntimeError("Database DDL timeout during startup")
        logger.info("Database tables created / verified")
        try:
            await asyncio.wait_for(_safe_add_columns(conn), timeout=60.0)
        except asyncio.TimeoutError:
            logger.critical("_safe_add_columns timed out after 60 s — aborting startup")
            raise RuntimeError("Database schema upgrade timeout during startup")
        logger.info("Database schema upgrade checks complete")

    await _seed_admin()

    from credit_report.config import CREDIT_REPORTS_ROOT, DATABASE_URL
    logger.info(
        "=== Service ready === db_backend=%s db_url_prefix=%s credit_reports_root=%s",
        "sqlite" if "sqlite" in DATABASE_URL else "postgresql",
        DATABASE_URL[:30],
        CREDIT_REPORTS_ROOT,
    )
    yield
    logger.info("=== Service shutting down ===")


app = FastAPI(
    title="Financial Report Analyzer",
    description="AI-powered credit report generation pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
# allow_credentials=True is invalid with wildcard origin; use credential-less for wildcard
_allow_creds = "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / response logging middleware ─────────────────────────────────────
# Only trust x-forwarded-for when the server is behind a known reverse proxy.
# Without explicit proxy config, use the socket-level client IP to prevent
# log-poisoning via spoofed headers.
_TRUSTED_PROXY = os.getenv("TRUSTED_PROXY_IPS", "")


def _client_ip(request: Request) -> str:
    """Return the most-trustworthy client IP for logging and audit."""
    if _TRUSTED_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


@app.middleware("http")
async def log_requests(request: Request, call_next):
    req_id = str(uuid.uuid4())[:8]
    start = time.perf_counter()

    # Log every incoming request (skip static assets to reduce noise)
    path = request.url.path
    if not path.startswith("/static"):
        logger.info(
            "[%s] → %s %s | ip=%s | ua=%s",
            req_id,
            request.method,
            path,
            _client_ip(request),
            request.headers.get("user-agent", "")[:60],
        )

    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        logger.exception(
            "[%s] ← UNHANDLED EXCEPTION %s %s | %.1fms | %s",
            req_id, request.method, path, elapsed, exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": req_id},
            headers={"X-Request-ID": req_id},
        )

    elapsed = (time.perf_counter() - start) * 1000
    if not path.startswith("/static"):
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(
            level,
            "[%s] ← %s %s %s | %.1fms",
            req_id, response.status_code, request.method, path, elapsed,
        )

    response.headers["X-Request-ID"] = req_id
    return response


app.include_router(credit_report_router)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/app")


@app.get("/app", include_in_schema=False)
async def ui():
    return FileResponse(
        "static/index.html",
        headers={
            # no-cache forces the browser to revalidate every load (the SPA is a single
            # file with no asset hashing, so a stale cache pins the user to old buggy JS).
            "Cache-Control": "no-cache, must-revalidate",
            # Restrict resource loading to same origin + CDN allowlist.
            # 'unsafe-inline' is required for the single-file SPA's inline scripts/styles.
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none';"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
    )


@app.get("/health", tags=["health"])
async def health():
    import pathlib
    from credit_report.config import CREDIT_REPORTS_ROOT

    # DB connectivity check
    db_ok = False
    db_error = None
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        db_error = str(e)

    # Disk write test
    disk_ok = False
    disk_error = None
    try:
        probe = pathlib.Path(CREDIT_REPORTS_ROOT) / ".health_probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
        disk_ok = True
    except Exception as e:
        disk_error = str(e)

    healthy = db_ok and disk_ok
    body = {
        "ok": healthy,
        "service": "financial-report-analyzer",
        "checks": {
            "db": {"ok": db_ok, **({"error": db_error} if db_error else {})},
            "disk": {"ok": disk_ok, **({"error": disk_error} if disk_error else {})},
        },
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)

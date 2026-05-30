from __future__ import annotations

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
from credit_report.security.auth import hash_password, verify_password


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
        except Exception:
            pass  # Column already exists — expected on subsequent startups

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
        except Exception:
            pass  # SQLite: unsupported; PostgreSQL already TEXT: no-op

    # Deduplicate section_inputs and section_outputs: keep the row with the lowest id
    # per (report_id, section_no). Duplicate rows arise from concurrent generation
    # requests both inserting when no row existed yet (race condition).
    for tbl in ("section_inputs", "section_outputs"):
        try:
            await conn.execute(text(
                f"DELETE FROM {tbl} WHERE id NOT IN ("
                f"  SELECT MIN(id) FROM {tbl} GROUP BY report_id, section_no"
                f")"
            ))
            try:
                await conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{tbl}_report_section "
                    f"ON {tbl} (report_id, section_no)"
                ))
                logger.info("_safe_add_columns: unique index created on %s", tbl)
            except Exception:
                pass  # Index may already exist
        except Exception as e:
            logger.warning("_safe_add_columns: dedup %s failed (non-critical): %s", tbl, e)

# Initialise logging before anything else logs
setup_logging()
logger = logging.getLogger(__name__)


async def _seed_admin() -> None:
    email = os.getenv("ADMIN_EMAIL", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        logger.info("ADMIN_EMAIL / ADMIN_PASSWORD not set — skipping admin seed")
        return
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
            # Only rehash when the env var password differs from the stored hash.
            # This avoids the expensive pbkdf2_sha256 hash on every startup when
            # ADMIN_PASSWORD hasn't changed, and makes deliberate rotation explicit.
            if not verify_password(password, user.hashed_password):
                user.hashed_password = hash_password(password)
                logger.info("_seed_admin: ADMIN_PASSWORD changed — hash updated for admin=%s", user.email)
            user.is_active = True
            user.role = "admin"
            await session.commit()
            logger.info("_seed_admin: synced role/status for admin=%s", user.email)
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
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created / verified")
        # Safe column additions for section_documents table (idempotent)
        await _safe_add_columns(conn)
        logger.info("Database schema upgrade checks complete")

    await _seed_admin()
    logger.info("=== Service ready ===")
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
            request.headers.get("x-forwarded-for", request.client.host if request.client else "?"),
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
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    elapsed = (time.perf_counter() - start) * 1000
    if not path.startswith("/static"):
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(
            level,
            "[%s] ← %s %s %s | %.1fms",
            req_id, response.status_code, request.method, path, elapsed,
        )

    return response


app.include_router(credit_report_router)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/app")


@app.get("/app", include_in_schema=False)
async def ui():
    return FileResponse("static/index.html")


@app.get("/health", tags=["health"])
async def health():
    return {"ok": True, "service": "financial-report-analyzer", "production": IS_PRODUCTION}

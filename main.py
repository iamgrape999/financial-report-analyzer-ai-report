from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from credit_report import router as credit_report_router
from credit_report.config import AUTO_CREATE_TABLES, parse_cors_origins, validate_runtime_security
from credit_report.database import AsyncSessionLocal, Base, engine

# Import all models so Base.metadata knows about every table
import credit_report.models  # noqa: F401
import credit_report.security.models  # noqa: F401
import credit_report.audit.events  # noqa: F401
import credit_report.fact_store.models  # noqa: F401
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.block_ast.models  # noqa: F401
import credit_report.generation.models  # noqa: F401

from credit_report.security.models import User
from credit_report.security.auth import hash_password

UI_DIR = Path(__file__).parent / "credit_report" / "ui"


async def _seed_admin() -> None:
    email = os.getenv("ADMIN_EMAIL", "")
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        if result.scalar_one_or_none():
            return
        session.add(User(
            id=str(uuid.uuid4()),
            email=email,
            hashed_password=hash_password(password),
            role="admin",
            is_active=True,
        ))
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_security()
    if AUTO_CREATE_TABLES:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    await _seed_admin()
    yield


app = FastAPI(
    title="Financial Report Analyzer",
    description="AI-powered credit report generation pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

cors_origins = parse_cors_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials="*" not in cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(credit_report_router)


@app.get("/app", include_in_schema=False)
async def app_index():
    """Redirect the bare UI path to the mounted static app index."""
    return RedirectResponse(url="/app/")


app.mount("/app", StaticFiles(directory=UI_DIR, html=True), name="credit-report-ui")


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse(url="/app/")


@app.get("/health", tags=["health"])
async def health():
    return {"ok": True, "service": "financial-report-analyzer"}

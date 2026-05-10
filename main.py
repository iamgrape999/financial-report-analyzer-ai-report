from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from credit_report import router as credit_report_router
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return {"ok": True, "service": "financial-report-analyzer"}

"""
pytest configuration — set environment variables before any test module is imported.

pytest loads conftest.py files before collecting/importing test modules, so this
ensures credit_report.config reads the correct test values when imported by any suite.
"""
import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")


@pytest.fixture(scope="session", autouse=True)
def create_test_environment():
    """Create required data directories and database tables before any tests run.

    Tests that use AsyncSessionLocal() directly (rather than the ASGI app) need
    the tables to already exist. In CI the database is always fresh, so we must
    run CREATE ALL here rather than relying on the FastAPI startup lifespan.
    """
    for d in ["data", "data/credit_reports", "data/memory", "data/logs"]:
        Path(d).mkdir(parents=True, exist_ok=True)

    async def _create_tables_and_seed() -> None:
        # Import all model modules so their metadata is registered before create_all
        import credit_report.models  # noqa: F401
        import credit_report.calculation_engine.models  # noqa: F401
        import credit_report.fact_store.models  # noqa: F401
        import credit_report.block_ast.models  # noqa: F401
        import credit_report.security.models  # noqa: F401
        import credit_report.audit.events  # noqa: F401
        from credit_report.database import Base, engine

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Seed the admin user so tests that call login("admin@example.com")
        # work against a fresh CI database as well as an existing local database.
        from main import _seed_admin
        await _seed_admin()

    asyncio.run(_create_tables_and_seed())

"""Prepare the database for Render startup.

PostgreSQL deployments are upgraded through Alembic before the app starts. The
default Render/local configuration uses SQLite when ``DATABASE_URL`` is unset;
that database is managed by SQLAlchemy ``create_all`` because some historical
Alembic migrations use PostgreSQL-style DDL that SQLite cannot execute.

Older deployments may also have application tables but no ``alembic_version``
row. Do not stamp those schemas as ``head`` automatically: doing so can skip
constraint/data migrations that still need an operator-reviewed migration path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from credit_report.config import DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_APP_TABLES = {
    "reports",
    "users",
    "section_inputs",
    "section_outputs",
    "audit_events",
    "canonical_facts",
}


async def _table_state() -> tuple[bool, bool]:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as conn:
            table_names = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            has_version_table = "alembic_version" in table_names
            has_version_row = False
            if has_version_table:
                result = await conn.execute(text("SELECT COUNT(*) FROM alembic_version"))
                has_version_row = int(result.scalar_one()) > 0
    finally:
        await engine.dispose()
    return has_version_row, bool(table_names & _APP_TABLES)


def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def _database_url_was_configured() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def main() -> None:
    if _is_sqlite_url(DATABASE_URL):
        if _database_url_was_configured():
            logger.warning("Skipping Alembic migrations for SQLite DATABASE_URL; app startup will use create_all")
        else:
            logger.info("DATABASE_URL is unset; using default SQLite and skipping Alembic migrations")
        return

    has_alembic_version, has_app_tables = asyncio.run(_table_state())
    if has_app_tables and not has_alembic_version:
        logger.error(
            "Existing unversioned schema detected; refusing to stamp Alembic head automatically. "
            "Run a reviewed baseline/migration procedure before enabling startup migrations."
        )
        raise SystemExit(1)

    cfg = _alembic_config()
    logger.info("Running Alembic upgrade head")
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    main()

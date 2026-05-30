"""Prepare the database for Render startup.

Fresh databases are upgraded through Alembic normally. Older deployments of this
app created tables with SQLAlchemy ``create_all`` before Alembic was run; those
schemas have application tables but no ``alembic_version`` table. In that case,
running ``alembic upgrade head`` from base attempts to recreate existing tables,
so we stamp the current schema as the baseline before applying future upgrades.
"""
from __future__ import annotations

import asyncio
import logging
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


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def main() -> None:
    has_alembic_version, has_app_tables = asyncio.run(_table_state())
    cfg = _alembic_config()
    if has_app_tables and not has_alembic_version:
        logger.info("Existing unversioned schema detected; stamping Alembic baseline at head")
        command.stamp(cfg, "head")
    logger.info("Running Alembic upgrade head")
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    main()

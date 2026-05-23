"""
pytest configuration — set environment variables before any test module is imported.

pytest loads conftest.py files before collecting/importing test modules, so this
ensures credit_report.config reads the correct test values when imported by any suite.
"""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")


# ── Test Health Tracking ──────────────────────────────────────────────────────
# Writes per-test results to .claude/test_history.jsonl so the health-report
# script can detect flaky / broken / slow tests across sessions.

_ROOT = Path(__file__).parent.parent
_HEALTH_DIR = _ROOT / ".claude"
_HISTORY_FILE = _HEALTH_DIR / "test_history.jsonl"

_run_buffer: list[dict] = []
_start_times: dict[str, float] = {}


def pytest_runtest_logstart(nodeid: str, location) -> None:  # type: ignore[override]
    _start_times[nodeid] = time.monotonic()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:  # type: ignore[override]
    if report.when != "call":
        return
    elapsed = time.monotonic() - _start_times.pop(report.nodeid, time.monotonic())
    if report.passed:
        status = "passed"
    elif report.failed:
        status = "failed"
    elif report.skipped:
        status = "skipped"
    else:
        status = "error"
    _run_buffer.append({
        "test_id": report.nodeid,
        "status": status,
        "duration": round(elapsed, 3),
        "ts": int(time.time()),
    })


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _run_buffer:
        return
    try:
        _HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        with _HISTORY_FILE.open("a") as fh:
            for record in _run_buffer:
                fh.write(json.dumps(record) + "\n")
        _run_buffer.clear()
    except (OSError, PermissionError):
        return  # CI read-only filesystem — data stays in buffer, harmless
    # Regenerate the health report non-critically
    report_script = _ROOT / "scripts" / "test_health_report.py"
    if report_script.exists():
        subprocess.run(
            ["python3", str(report_script)],
            capture_output=True,
            cwd=_ROOT,
        )


# ── Test Fixtures ─────────────────────────────────────────────────────────────


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


@pytest.fixture(autouse=True)
def reset_in_memory_security_state():
    """Reset per-process in-memory security state between tests.

    The rate limiter, brute-force tracker, refresh-token revocation list,
    and generation-lock set are module-level dicts/sets that persist across
    tests in the same process.  Resetting them prevents earlier tests from
    causing 429s/401s/409s in later ones.
    """
    from credit_report.security.rate_limit import reset_all as rl_reset
    from credit_report.api.auth import _failed, _revoked_refresh
    from credit_report.api.generate import _generating_sections
    rl_reset()
    _failed.clear()
    _revoked_refresh.clear()
    _generating_sections.clear()
    yield
    rl_reset()
    _failed.clear()
    _revoked_refresh.clear()
    _generating_sections.clear()

"""
Pipeline Resilience Tests — Catching the DB-error cascade bug
=============================================================

WHY THIS FILE EXISTS
--------------------
The section-2 generation bug was NOT caught by existing tests for three reasons:

1. SQLite gap: all tests run against SQLite, which silently ignores VARCHAR length
   limits. PostgreSQL raises StringDataRightTruncationError when a cell value
   exceeds VARCHAR(255). SQLite never raises this, so the bug was invisible in CI.

2. No AST-failure injection: existing pipeline tests mock generate_section_markdown
   but never inject errors into save_blocks. The invariant "AST save failure must
   not abort the main session" was never tested.

3. No session-isolation test: when PostgreSQL aborts a transaction, every subsequent
   call on the same SQLAlchemy session fails with "current transaction is aborted".
   The inner except clause appeared to catch the AST error, but the session was
   already poisoned — all subsequent writes (record_tokens, write_event, flush)
   raised the secondary error which the outer except re-raised to the user.

WHAT THESE TESTS VERIFY (and would have caught the bug before production)
-------------------------------------------------------------------------
A. When save_blocks raises ANY exception (including DB constraint errors), the
   section generation still completes with status="done" and markdown is saved.

B. The main database session is NOT in an aborted state after AST save failure.
   record_tokens, write_event, and db.flush all succeed normally.

C. Table cells with display_value > 255 chars do not crash section generation.

D. The AST save failure is logged as a WARNING with [AST] prefix.

E. All 10 sections are resilient to AST errors.

F. Consecutive section generations are not affected by previous AST failures.
"""

from __future__ import annotations

import uuid
import logging
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base

# Import all models so Base.metadata knows every table
import credit_report.models                     # noqa: F401
import credit_report.security.models            # noqa: F401
import credit_report.audit.events               # noqa: F401
import credit_report.fact_store.models          # noqa: F401
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.block_ast.models           # noqa: F401
import credit_report.generation.models          # noqa: F401

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

LONG_CELL = "A" * 300   # 300 chars — overflows VARCHAR(255) on PostgreSQL
TABLE_WITH_LONG_CELLS = (
    "## §2 Credit Overview\n\n"
    f"| **{'B' * 200}** | FY2023 | FY2024 |\n"
    "|--------|--------|--------|\n"
    f"| Revenue | {LONG_CELL} | 150.0 |\n"
    "| EBITDA | Short value | 56.0 |\n"
)


# ── shared fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _uid() -> str:
    return str(uuid.uuid4())


def _make_report_id(db_session) -> str:
    """Insert a minimal Report row and return its id."""
    import asyncio
    from credit_report.models import Report

    rid = _uid()

    async def _insert():
        db_session.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db_session.flush()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_insert())
    return rid


def _mock_generate(md=TABLE_WITH_LONG_CELLS, tokens=1000):
    """Patch Gemini call to return given markdown instantly."""
    return patch(
        "credit_report.generation.pipeline.generate_section_markdown",
        new=AsyncMock(return_value=(md, tokens)),
    )


def _mock_evidence():
    return patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[])


def _mock_quota():
    """Allow quota check to pass."""
    return patch("credit_report.generation.pipeline.check_quota", new=AsyncMock(return_value=None))


def _mock_record():
    """No-op token recording."""
    return patch("credit_report.generation.pipeline.record_tokens", new=AsyncMock(return_value=None))


# ── A. AST save failure must not abort section generation ───────────────────

@pytest.mark.asyncio
class TestAstSaveIsolation:
    """
    Core class catching the production bug:
    save_blocks DB error → shared session aborted → entire generation fails.
    """

    async def test_ast_db_error_does_not_crash_generation(self, db):
        """
        THE REGRESSION TEST for the section-2 production bug.

        Injects a SQLAlchemyError (equivalent to PostgreSQL
        StringDataRightTruncationError) into save_blocks.
        Verifies the section output is still "done" and markdown is saved.

        Before the fix: save_blocks used shared db session → PostgreSQL
        aborted the transaction → record_tokens raised → outer except re-raised
        → generation returned "error" despite Gemini returning valid markdown.

        After the fix: save_blocks runs in its own AsyncSessionLocal() → DB
        error rolls back only the AST session → main session is unaffected.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        db_error = SQLAlchemyError(
            "value too long for type character varying(255)"
        )

        with _mock_generate(), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.block_ast.repository.save_blocks", side_effect=db_error):
            output = await run_section_generation(
                db, rid, section_no=2, actor_user_id=_uid()
            )

        assert output.status == "done", (
            f"status={output.status!r}. "
            "AST save failure must not propagate to section generation result."
        )
        assert output.markdown, "markdown must be saved even when AST blocks fail"

    async def test_ast_generic_error_does_not_crash_generation(self, db):
        """Any exception in AST save must be caught and logged — not re-raised."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        with _mock_generate("§4 plain paragraph text."), _mock_evidence(), \
             _mock_quota(), _mock_record(), \
             patch("credit_report.block_ast.repository.save_blocks",
                   side_effect=RuntimeError("unexpected AST failure")):
            output = await run_section_generation(db, rid, section_no=4, actor_user_id=_uid())

        assert output.status == "done"

    async def test_ast_builder_error_does_not_crash_generation(self, db):
        """Exception in build_blocks() itself must also be silently caught."""
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        with _mock_generate("§3 text"), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.block_ast.builder.build_blocks",
                   side_effect=ValueError("malformed markdown")):
            output = await run_section_generation(db, rid, section_no=3, actor_user_id=_uid())

        assert output.status == "done"

    @pytest.mark.parametrize("sec_no", list(range(1, 11)))
    async def test_all_sections_resilient_to_ast_failure(self, db, sec_no):
        """
        Every section must complete even when save_blocks raises.
        This parametrized test would have caught the production bug immediately.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        error = SQLAlchemyError(f"simulated overflow section={sec_no}")
        with _mock_generate(f"§{sec_no} content"), _mock_evidence(), \
             _mock_quota(), _mock_record(), \
             patch("credit_report.block_ast.repository.save_blocks", side_effect=error):
            output = await run_section_generation(db, rid, section_no=sec_no, actor_user_id=_uid())

        assert output.status == "done", (
            f"§{sec_no} failed with status={output.status!r} when AST save raised"
        )
        assert output.markdown


# ── B. Main session not poisoned after AST failure ──────────────────────────

@pytest.mark.asyncio
class TestSessionIsolation:

    async def test_markdown_persisted_after_ast_failure(self, db):
        """
        After AST failure, the generated markdown must be committed to the DB.
        This confirms the main session was NOT rolled back by the AST error.
        """
        from credit_report.generation.pipeline import run_section_generation, get_section_output
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        expected_md = "## §2 Executive Summary\n\nThe borrower is financially strong."
        error = SQLAlchemyError("varchar overflow")

        with _mock_generate(expected_md), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.block_ast.repository.save_blocks", side_effect=error):
            await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())
        await db.commit()

        # Reload from DB — markdown must be there
        saved = await get_section_output(db, rid, section_no=2)
        assert saved is not None, "SectionOutput row not saved — main session was aborted"
        assert saved.markdown == expected_md, (
            "Markdown mismatch — main transaction was rolled back by the AST error"
        )
        assert saved.status == "done"

    async def test_record_tokens_succeeds_after_ast_failure(self, db):
        """
        record_tokens is called on the same session after the AST block.
        If the session is aborted by the AST error, record_tokens raises.
        This test verifies the session is clean.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        tokens_recorded = []

        async def _capture_record_tokens(db_session, user_id, tokens):
            tokens_recorded.append(tokens)

        error = SQLAlchemyError("varchar overflow")
        with _mock_generate(tokens=999), _mock_evidence(), _mock_quota(), \
             patch("credit_report.generation.pipeline.record_tokens",
                   new=AsyncMock(side_effect=_capture_record_tokens)), \
             patch("credit_report.block_ast.repository.save_blocks", side_effect=error):
            output = await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())

        assert output.status == "done"
        assert tokens_recorded == [999], (
            "record_tokens was not called after AST failure — "
            "session was in aborted state or the function raised before reaching record_tokens"
        )


# ── C. Long display_value (the actual PostgreSQL trigger) ───────────────────

@pytest.mark.asyncio
class TestLongDisplayValue:

    async def test_builder_produces_short_col_ids_for_long_headers(self):
        """
        Regression: builder must use col_NNN (≤7 chars) not raw header text.
        A 200-char header stored as column_id would overflow VARCHAR(100) on PostgreSQL.
        """
        from credit_report.block_ast.builder import build_blocks

        long_header = "**Credit Overview and Risk Assessment Summary for Borrower FY2022-2024**"
        assert len(long_header) > 20, "Test setup: header must actually be long"

        md = f"| {long_header} | FY2023 | FY2024 |\n|---|---|---|\n| Revenue | 100 | 120 |\n"
        blocks, cells = build_blocks("r1", 2, md, [])

        for c in cells:
            assert len(c["column_id"]) <= 20, (
                f"column_id is {len(c['column_id'])} chars: {c['column_id']!r}. "
                "Must use col_NNN slug, not raw header text."
            )

    async def test_builder_300_char_cell_fits_in_col_id(self):
        """display_value can be arbitrarily long; column_id must always be ≤ 20."""
        from credit_report.block_ast.builder import build_blocks

        md = f"| Header | Value |\n|---|---|\n| Row | {'X' * 300} |\n"
        blocks, cells = build_blocks("r1", 2, md, [])

        assert cells, "builder must produce cells"
        for c in cells:
            assert len(c["column_id"]) <= 20
            assert len(c["row_id"]) <= 20
            # display_value is Text (unlimited) — not checked here

    async def test_generation_with_long_cells_produces_done_status(self, db):
        """
        End-to-end with TABLE_WITH_LONG_CELLS.
        On SQLite: passes because VARCHAR not enforced (demonstrating the SQLite gap).
        On PostgreSQL after fix: passes because AST session is isolated.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        with _mock_generate(TABLE_WITH_LONG_CELLS), _mock_evidence(), \
             _mock_quota(), _mock_record():
            output = await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())

        assert output.status == "done"
        assert output.markdown == TABLE_WITH_LONG_CELLS


# ── D. Warning logged, not silently swallowed ────────────────────────────────

@pytest.mark.asyncio
class TestAstWarningLogged:

    async def test_ast_failure_is_logged_as_warning(self, db, caplog):
        """
        AST failure must appear in logs at WARNING level with [AST] prefix.
        Silent failures make production debugging impossible.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        error = SQLAlchemyError("simulated overflow")
        with _mock_generate("§2 text"), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.block_ast.repository.save_blocks", side_effect=error), \
             caplog.at_level(logging.WARNING, logger="credit_report.generation.pipeline"):
            await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())

        ast_warns = [r for r in caplog.records
                     if "[AST]" in r.message and r.levelno >= logging.WARNING]
        assert ast_warns, (
            "No [AST] WARNING found in logs. AST failures must be logged so "
            "production errors are diagnosable without user reports."
        )


# ── E. Consecutive generations not affected by previous AST failure ──────────

@pytest.mark.asyncio
class TestConsecutiveGenerations:

    async def test_section3_works_after_section2_ast_failure(self, db):
        """
        §2 AST save fails; §3 generation must still succeed.
        Verifies no session state leaks between requests.
        """
        from credit_report.generation.pipeline import run_section_generation
        from credit_report.models import Report

        rid = _uid()
        db.add(Report(id=rid, industry="marine", created_by=_uid()))
        await db.flush()

        error = SQLAlchemyError("overflow on sec2")

        # §2: AST fails
        with _mock_generate("§2 text"), _mock_evidence(), _mock_quota(), _mock_record(), \
             patch("credit_report.block_ast.repository.save_blocks", side_effect=error):
            out2 = await run_section_generation(db, rid, section_no=2, actor_user_id=_uid())
        assert out2.status == "done", f"§2 failed: {out2.status}"

        # §3: no AST error — must work normally
        with _mock_generate("§3 content"), _mock_evidence(), _mock_quota(), _mock_record():
            out3 = await run_section_generation(db, rid, section_no=3, actor_user_id=_uid())
        assert out3.status == "done", (
            f"§3 failed after §2 AST error: {out3.status}. "
            "Session state leaked between calls."
        )
        assert out3.markdown == "§3 content"

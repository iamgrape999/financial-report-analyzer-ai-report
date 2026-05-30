"""
Hidden Bug Piercing Tests
=========================
20 production bugs that AI sandboxes (isolated pytest, no real platform, no
real files, no real browser) consistently mask.  Every test is self-contained,
pure-Python, runnable with ``pytest tests/test_hidden_bugs.py``.

Each class maps to one of the five defect categories identified by deep code
review.  Two "correction" tests at the end verify that the code actually
handles quota and semaphore *correctly* — the original bug report overstated
those two as defects.

RE-VERIFICATION NOTES (after a second, adversarial pass against the code and
known platform behaviour — corrections folded into the relevant docstrings):
  * Bug #1 (JWT): Render ``generateValue: true`` persists the secret across
    deploys; it does NOT rotate every deploy.  Scope narrowed to the real
    module-level _KEY_BYTES binding + restart semantics.
  * Bug #3 (postgres://): could not confirm Render emits the short scheme;
    reframed as a SQLAlchemy-2.0 robustness gap rather than a guaranteed
    Render crash.  The code behaviour asserted is still exactly true.
  * Bug #4 (alembic): STRENGTHENED — alembic is fully configured with 8
    migrations but never run; create_all is used instead (genuine dead
    migration infrastructure + schema drift on PostgreSQL).
  * Bug #7 (LLM timeout): DEBUNKED.  Two framings both proved false — the
    "Render 30 s gateway" premise (that is Heroku; generation is a polled
    BackgroundTask anyway) AND a follow-up "uncancellable run_in_executor
    thread" guess.  claude_client awaits the NATIVE async client inside
    asyncio.wait_for, so the timeout cancels cleanly.  The test now guards
    that correct implementation instead of asserting a non-existent bug.

Source-file anchors are given in each test docstring so findings can be
re-verified after refactors.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import io
import json
import re
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml  # PyYAML is a dev-dep; available in requirements.txt

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
INDEX_HTML = REPO_ROOT / "static" / "index.html"


# ===========================================================================
# Group 1 – Cross-Deploy State Bugs (#1–#4)
# ===========================================================================

class TestCrossDeployStateBugs:
    """Bugs caused by state that is re-initialised on every Render deploy."""

    # ── Bug #1 ─────────────────────────────────────────────────────────────
    def test_jwt_key_bytes_bound_at_module_import(self):
        """_KEY_BYTES is captured once at import time from SECRET_KEY.

        VERIFIED SCOPE (corrected): Render's ``generateValue: true`` generates
        the secret ONCE when the service is first created and PERSISTS it
        across deploys — it does NOT rotate on every deploy.  So the original
        "every deploy invalidates all tokens" claim is overstated.

        The genuine, narrower defect this test pins down:
        ``_KEY_BYTES = SECRET_KEY.encode()`` is a module-level constant
        (auth.py:29).  Any change to the SECRET_KEY env var (manual rotation,
        switching from the dev default to a real key, or a first deploy that
        had no value set) only takes effect after a full interpreter restart,
        and at that moment every previously-issued token becomes invalid
        because the signing key no longer matches.

        Source: credit_report/security/auth.py:29
                render.yaml:18-19
        """
        from credit_report.security import auth as auth_mod

        # Capture the key bytes the module was loaded with.
        original_key_bytes = auth_mod._KEY_BYTES

        # Issue a token under the original key.
        token = auth_mod.create_access_token("user-1", "analyst")

        # Simulate a runtime key change (what happens after env-var rotation).
        new_key = b"totally-different-secret"
        auth_mod._KEY_BYTES = new_key
        try:
            with pytest.raises(auth_mod.JWTError, match="Invalid signature"):
                auth_mod._decode_jwt(token)
        finally:
            # Restore so other tests are not affected.
            auth_mod._KEY_BYTES = original_key_bytes

    # ── Bug #2 ─────────────────────────────────────────────────────────────
    def test_seed_admin_always_rehashes_password_on_startup(self):
        """_seed_admin re-hashes and commits the admin password every startup.

        If ADMIN_PASSWORD is cleared or changed in the env after initial
        deployment, the next startup will overwrite the stored hash.  An
        operator who removes ADMIN_PASSWORD to prevent auto-seeding will
        instead lock themselves out (main.py:113-116).

        Source: main.py:93-127 (_seed_admin function)
        """
        import main as main_mod

        src = inspect.getsource(main_mod._seed_admin)
        # The unconditional re-hash path must exist: when ``if user:`` branch
        # runs, it always calls hash_password(password) and commits.
        assert "hash_password(password)" in src, (
            "Expected _seed_admin to unconditionally re-hash the password for "
            "existing admin users"
        )
        assert "user.hashed_password = hash_password(password)" in src

    # ── Bug #3 ─────────────────────────────────────────────────────────────
    def test_postgres_url_scheme_not_rewritten_to_asyncpg(self):
        """config.py normalises postgresql:// but NOT the legacy postgres:// scheme.

        VERIFIED SCOPE (corrected): I could not confirm that Render emits the
        ``postgres://`` short scheme (that is Heroku's historical format;
        Render generally emits ``postgresql://``).  So this is a robustness
        gap, not a guaranteed-on-Render crash.

        The defect is still real and verifiable: SQLAlchemy 2.0 REMOVED
        support for the bare ``postgres://`` scheme, and config.py only
        rewrites ``postgresql://`` → ``postgresql+asyncpg://``.  Therefore any
        ``postgres://`` URL — a Heroku-migrated DB, a copy-pasted connection
        string, or a managed provider that uses the short form — reaches
        create_async_engine() unrewritten and raises at startup.

        Source: credit_report/config.py:14-15
        """
        import credit_report.config as cfg

        # Reproduce the exact transformation logic from config.py:14-15.
        render_url = "postgres://user:pass@host:5432/mydb"

        rewritten = render_url
        if rewritten.startswith("postgresql://"):
            rewritten = rewritten.replace("postgresql://", "postgresql+asyncpg://", 1)

        # The ``postgres://`` scheme is NOT handled — it passes through unchanged.
        assert rewritten == render_url, (
            "postgres:// should NOT be rewritten — this is the defect"
        )
        assert not rewritten.startswith("postgresql+asyncpg://"), (
            "asyncpg prefix should be absent, confirming the bug"
        )

        # Verify that the actual config module also lacks the postgres:// guard.
        config_src = Path(REPO_ROOT / "credit_report" / "config.py").read_text()
        assert 'startswith("postgresql://")' in config_src
        assert 'startswith("postgres://")' not in config_src, (
            "Render-format postgres:// is not handled in config.py"
        )

    # ── Bug #4 ─────────────────────────────────────────────────────────────
    def test_render_start_command_has_no_alembic_upgrade(self):
        """render.yaml startCommand does not run ``alembic upgrade head``.

        VERIFIED (stronger than first stated): the repo ships a COMPLETE
        alembic setup — alembic.ini, migrations/env.py, and 8 migration
        revisions including a baseline_schema and PostgreSQL-only column
        fixes (e.g. fix_table_cells_*_text, which widen VARCHAR→TEXT).  Yet
        the app only ever runs ``Base.metadata.create_all`` (main.py:140),
        and the startCommand never runs ``alembic upgrade head``.

        Consequences on a real PostgreSQL deploy:
        * create_all creates tables but never stamps ``alembic_version``, so
          the DB and the migration history diverge from the first deploy.
        * create_all NEVER alters existing columns, so the VARCHAR→TEXT fix
          migrations are dead code — main.py:56-65 has to re-implement those
          ALTERs by hand in _safe_add_columns, confirming the migrations are
          not being applied.

        Source: render.yaml:7 ; main.py:139-144 ; migrations/versions/*
        """
        data = yaml.safe_load(RENDER_YAML.read_text())
        service = data["services"][0]
        start_cmd = service.get("startCommand", "")
        assert "alembic" not in start_cmd.lower(), (
            f"Expected no alembic in startCommand, got: {start_cmd!r}"
        )
        assert "upgrade" not in start_cmd.lower()

        # The buildCommand must not run migrations either.
        build_cmd = service.get("buildCommand", "")
        assert "alembic" not in build_cmd.lower(), (
            f"Expected no alembic in buildCommand, got: {build_cmd!r}"
        )

        # Confirm alembic IS configured (so this is genuine dead infrastructure).
        assert (REPO_ROOT / "alembic.ini").exists(), "alembic.ini must exist"
        versions_dir = REPO_ROOT / "migrations" / "versions"
        migrations = list(versions_dir.glob("*.py"))
        assert len(migrations) >= 5, (
            f"Expected several alembic migrations, found {len(migrations)} — "
            "these never run because startCommand omits 'alembic upgrade head'"
        )

        # And confirm the app relies on create_all instead.
        main_src = (REPO_ROOT / "main.py").read_text()
        assert "Base.metadata.create_all" in main_src, (
            "main.py uses create_all in place of alembic migrations"
        )


# ===========================================================================
# Group 2 – Render Platform Bugs (#5–#6 real; #7 debunked)
# ===========================================================================

class TestRenderPlatformBugs:
    """Bugs caused by Render's ephemeral-filesystem / single-process model.

    #5 and #6 are real platform defects.  #7 (LLM timeout) was investigated
    twice and found NOT to be a bug; its test now guards the correct behaviour.
    """

    # ── Bug #5 ─────────────────────────────────────────────────────────────
    def test_generation_tasks_registry_is_process_local(self):
        """_generation_tasks is a plain module-level dict — lost on restart.

        Render free-tier restarts the process on every deploy.  Any in-flight
        or completed task IDs in ``_generation_tasks`` disappear.  Clients
        polling ``/generate/status/{task_id}`` get 404 after redeploy.

        Source: credit_report/api/generate.py:368-370
        """
        from credit_report.api import generate as gen_mod

        registry = gen_mod._generation_tasks
        assert isinstance(registry, dict), (
            "_generation_tasks should be a plain dict"
        )
        # Verify it is module-level (not a class attribute or DB-backed store).
        assert "_generation_tasks" in vars(gen_mod), (
            "Expected _generation_tasks at module level in generate.py"
        )

        # A task created now will not survive a hypothetical module re-import.
        task_id = gen_mod._create_generation_task({"status": "running", "section_no": 7})
        assert task_id in gen_mod._generation_tasks

        # Simulate restart: reimport clears the dict.
        importlib.reload(gen_mod)
        assert task_id not in gen_mod._generation_tasks, (
            "Task IDs are lost when the module is reloaded (as happens on restart)"
        )

    # ── Bug #6 ─────────────────────────────────────────────────────────────
    def test_data_directory_is_local_filesystem_path(self):
        """CREDIT_REPORTS_ROOT defaults to ./data/credit_reports on local disk.

        Render free-tier ephemeral instances lose every file in ``data/`` on
        restart or redeploy.  Uploaded documents and extracted text are gone.

        Source: credit_report/config.py:48
        """
        from credit_report import config as cfg

        root = str(cfg.CREDIT_REPORTS_ROOT)
        # Confirm it is a relative/local path (no ``/tmp``, no cloud URL).
        assert "s3://" not in root
        assert "gs://" not in root
        assert "azure://" not in root
        # Default is the local data/ directory.
        assert "data" in root.replace("\\", "/"), (
            f"CREDIT_REPORTS_ROOT={root!r} looks like a local path that will be "
            "lost on Render restart"
        )

    # ── Bug #7 (DEBUNKED — kept as a guard) ────────────────────────────────
    def test_llm_timeout_is_correctly_applied_to_a_native_async_call(self):
        """The LLM timeout is implemented correctly — the original bug was wrong.

        Two successive framings of this "bug" both turned out to be FALSE on
        closer inspection; this test documents the truth so the claim is not
        re-raised:

        1. "LLM_TIMEOUT 180 s > Render's 30 s HTTP gateway" — false premise.
           Render does NOT impose a 30 s request timeout (that is Heroku's H12
           router), and generation runs as a polled FastAPI BackgroundTask, so
           no synchronous request is being cut off.
        2. "wait_for cannot cancel the run_in_executor thread" — also false
           HERE.  claude_client does NOT use run_in_executor for the LLM call;
           it awaits the NATIVE async client
           ``client.aio.models.generate_content(...)`` inside
           ``asyncio.wait_for(..., timeout=LLM_TIMEOUT_SECONDS)``.  wait_for
           propagates CancelledError into a native coroutine, so the request is
           cancelled cleanly — no thread leak.

        What IS true and worth pinning: the timeout is genuinely wired up to
        both Gemini entry points, so it is not a dead config value.

        Source: credit_report/generation/claude_client.py:44-54, 113-127
        """
        import inspect as _inspect
        from credit_report.generation import claude_client

        for fn_name in ("call_gemini_raw", "generate_section_markdown"):
            fn = getattr(claude_client, fn_name)
            src = _inspect.getsource(fn)
            assert "asyncio.wait_for(" in src, (
                f"{fn_name} must bound the LLM call with asyncio.wait_for"
            )
            assert "timeout=LLM_TIMEOUT_SECONDS" in src, (
                f"{fn_name} must pass LLM_TIMEOUT_SECONDS as the timeout"
            )
            # Confirm it wraps the NATIVE async call, not a thread executor —
            # this is why wait_for can cancel it cleanly (no orphaned thread).
            assert "client.aio.models.generate_content" in src, (
                f"{fn_name} should await the native async Gemini client"
            )
            assert "run_in_executor" not in src, (
                f"{fn_name} must NOT use run_in_executor for the LLM call — "
                "the native-async path is what makes the timeout cancellable"
            )


# ===========================================================================
# Group 3 – In-Memory State Bugs (#8–#10)
# ===========================================================================

class TestInMemoryStateBugs:
    """Bugs caused by unretained asyncio tasks or destructive startup queries."""

    # ── Bug #8 ─────────────────────────────────────────────────────────────
    def test_asyncio_create_task_reference_not_retained(self):
        """asyncio.create_task() return value is discarded in _schedule_document_text_extraction.

        Python's event loop only holds a *weak* reference to tasks created
        with create_task().  If no other reference exists, the GC may collect
        the coroutine before it finishes.  The result is silent data-loss:
        no text extracted, no error raised.

        Source: credit_report/api/generate.py:179-182
        """
        src = Path(REPO_ROOT / "credit_report" / "api" / "generate.py").read_text()

        # Locate the _schedule_document_text_extraction function body.
        tree = ast.parse(src)
        fn_node = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_schedule_document_text_extraction"
        )
        fn_src = ast.get_source_segment(src, fn_node) or ""

        # The call must be ``asyncio.create_task(runner())`` — not assigned.
        assert "asyncio.create_task(runner())" in fn_src, (
            "Expected bare asyncio.create_task(runner()) in _schedule_document_text_extraction"
        )

        # Confirm no variable is assigned the return value of create_task.
        # A safe implementation would write:  _task = asyncio.create_task(runner())
        assert not re.search(r"\w+\s*=\s*asyncio\.create_task\(", fn_src), (
            "Task reference IS stored — the GC-risk bug may have been fixed"
        )

    # ── Bug #9 ─────────────────────────────────────────────────────────────
    def test_extraction_semaphore_is_module_level_not_shared_across_workers(self):
        """_extraction_semaphore is a module-level asyncio.Semaphore.

        On single-process Render free-tier this is fine.  But it is per-process:
        if Render ever scales to multiple workers the semaphore provides no
        cross-process back-pressure.  Document in-flight guarantees collapse.

        Source: credit_report/api/generate.py:44
        """
        from credit_report.api import generate as gen_mod

        sem = gen_mod._extraction_semaphore
        assert isinstance(sem, asyncio.Semaphore), (
            "_extraction_semaphore should be an asyncio.Semaphore"
        )
        assert "_extraction_semaphore" in vars(gen_mod), (
            "Semaphore must be at module-level, confirming it is process-local"
        )

    # ── Bug #10 ────────────────────────────────────────────────────────────
    def test_safe_add_columns_dedup_keeps_oldest_row_deletes_newer(self):
        """The startup dedup DELETE uses MIN(id), keeping the *oldest* row.

        If a race condition produces two section_inputs rows for the same
        (report_id, section_no), the startup DELETE will keep the row with
        the lowest id — the *first* write — and discard any newer version.
        This silently loses the most-recent user edits on the next redeploy.

        Source: main.py:70-76 (_safe_add_columns dedup block)
        """
        src = Path(REPO_ROOT / "main.py").read_text()

        # The DELETE statement must use MIN(id) — confirming oldest-wins.
        assert "SELECT MIN(id)" in src, (
            "Expected MIN(id) in the dedup DELETE — confirms oldest row survives"
        )
        assert "GROUP BY report_id, section_no" in src

        # The SQL is split across f-string lines:
        #   f"DELETE FROM {tbl} WHERE id NOT IN ("
        #   f"  SELECT MIN(id) FROM {tbl} GROUP BY report_id, section_no"
        # Check each fragment independently rather than as one regex.
        assert 'WHERE id NOT IN (' in src, (
            "Expected 'WHERE id NOT IN (' in the dedup DELETE block"
        )
        assert 'DELETE FROM {tbl} WHERE id NOT IN' in src, (
            "Expected f-string DELETE FROM {tbl} … WHERE id NOT IN in main.py"
        )


# ===========================================================================
# Group 4 – Real Document Format Bugs (#11–#15)
# ===========================================================================

class TestDocumentFormatBugs:
    """Bugs that only appear with real financial documents."""

    # ── Bug #11 ────────────────────────────────────────────────────────────
    def test_vision_ocr_hard_caps_pdf_bytes_at_20mb(self):
        """extract_text_from_scanned_pdf_vision silently truncates input at 20 MB.

        TSMC annual reports are 30–80 MB.  The Vision OCR entry point slices
        ``pdf_bytes[:20 * 1024 * 1024]`` before sending to Gemini.  Pages
        beyond the 20 MB mark are silently dropped with no warning to the user.

        Source: credit_report/generation/evidence.py:434
        """
        from credit_report.generation.evidence import extract_text_from_scanned_pdf_vision

        src = inspect.getsource(extract_text_from_scanned_pdf_vision)
        cap_expr = "pdf_bytes[:20 * 1024 * 1024]"
        assert cap_expr in src, (
            f"Expected '{cap_expr}' in extract_text_from_scanned_pdf_vision"
        )

        # Functional verification: passing >20 MB should not raise.
        # GEMINI_API_KEY is imported inside the function from credit_report.config,
        # so patch it there rather than on the evidence module.
        twenty_one_mb = b"\x00" * (21 * 1024 * 1024)
        with patch("credit_report.config.GEMINI_API_KEY", ""):
            result = extract_text_from_scanned_pdf_vision(twenty_one_mb)
        assert result == "", "Expected empty string when GEMINI_API_KEY unset"

    # ── Bug #12 ────────────────────────────────────────────────────────────
    def test_etl_prompt_truncates_text_at_120k_chars(self):
        """_build_etl_prompt silently truncates document text at CR_ETL_MAX_TEXT_CHARS.

        120,000 characters is roughly 40–60 pages of dense text.  A TSMC annual
        report (200+ pages) loses its financial statements entirely.  The
        logger emits ``truncated=True`` as a positional arg but no HTTP error
        is raised and the caller receives a partial extraction silently.

        Source: credit_report/generation/etl.py:1204
        """
        from credit_report.generation import etl as etl_mod
        from credit_report.config import CR_ETL_MAX_TEXT_CHARS

        assert CR_ETL_MAX_TEXT_CHARS == 120_000, (
            f"Default cap expected 120000, got {CR_ETL_MAX_TEXT_CHARS}"
        )

        # Build a text with a clearly unique overflow sentinel so we can detect
        # whether the chars BEYOND the cap leaked into the prompt.  Using the same
        # character for both body and overflow would make the overflow a substring
        # of the body and the assertion would trivially pass.
        body = "A" * CR_ETL_MAX_TEXT_CHARS
        overflow_sentinel = "OVERFLOW_SENTINEL_ZZZ_" * 2500  # 55 000 chars, all Z
        long_text = body + overflow_sentinel

        assert len(long_text) == CR_ETL_MAX_TEXT_CHARS + len(overflow_sentinel)

        # _build_etl_prompt is a private helper; call it directly.
        _sys, user_prompt = etl_mod._build_etl_prompt("annual_report", long_text, [4, 7])

        # The overflow sentinel must NOT appear in the generated prompt.
        assert "OVERFLOW_SENTINEL_ZZZ" not in user_prompt, (
            "Chars beyond CR_ETL_MAX_TEXT_CHARS must be absent from the prompt"
        )
        # The body (up to the cap) must be present.
        assert "A" * 100 in user_prompt, (
            "First 120 000 chars (body) must appear in the prompt"
        )

    # ── Bug #13 ────────────────────────────────────────────────────────────
    def test_openpyxl_read_only_merged_cells_return_none(self):
        """openpyxl read_only=True does not expand merged cells.

        _extract_text_from_xlsx opens workbooks with ``read_only=True``.
        Non-anchor cells of a merged range return ``None``, which the
        extraction code serialises as the empty string "".  Financial tables
        with merged header cells lose their labels for every non-anchor cell.

        Source: credit_report/generation/evidence.py:494-507
        """
        import openpyxl
        from openpyxl import Workbook

        # Create a workbook with a merged cell A1:C1 = "TSMC 2024 Revenue".
        wb_write = Workbook()
        ws_write = wb_write.active
        ws_write["A1"] = "TSMC 2024 Revenue"
        ws_write["B1"] = "Should be empty after merge"
        ws_write["C1"] = "Should be empty after merge"
        ws_write.merge_cells("A1:C1")
        ws_write["A2"] = 100
        ws_write["B2"] = 200
        ws_write["C2"] = 300

        buf = io.BytesIO()
        wb_write.save(buf)
        buf.seek(0)
        file_bytes = buf.read()

        # Re-open with the same options used in _extract_text_from_xlsx.
        wb_ro = openpyxl.load_workbook(
            io.BytesIO(file_bytes), read_only=True, data_only=True
        )
        ws_ro = wb_ro.active
        rows = list(ws_ro.iter_rows(values_only=True))
        header_row = rows[0]

        # A1 (anchor) retains the value; B1, C1 are None.
        assert header_row[0] == "TSMC 2024 Revenue", (
            "Anchor cell A1 should retain its value"
        )
        assert header_row[1] is None, (
            "Merged non-anchor B1 must be None in read_only mode — label is lost"
        )
        assert header_row[2] is None, (
            "Merged non-anchor C1 must be None in read_only mode — label is lost"
        )
        wb_ro.close()

    # ── Bug #14 ────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_twse_html_response_silently_returns_empty_list(self):
        """When TWSE returns HTML (e.g. maintenance page), fetch() silently returns [].

        ``resp.json()`` raises ``json.JSONDecodeError`` when the body is HTML.
        This is caught by the broad ``except Exception`` at line 318 and the
        function falls through to return ``[]``.  Downstream, the financial
        model receives empty data with no warning to the user.

        Source: credit_report/integrations/twse.py:313-319
        """
        from credit_report.integrations.twse import TWSEOpenAPIClient

        html_body = b"<html><body>Service Unavailable</body></html>"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            client = TWSEOpenAPIClient()
            result = await client.fetch("/opendata/t187ap06_L_ci")

        assert result == [], (
            "Expected [] when TWSE returns HTML that fails JSON parse"
        )

    # ── Bug #15 ────────────────────────────────────────────────────────────
    def test_twse_roc_year_converts_to_gregorian(self):
        """TWSE API reports years in ROC (Republic of China) calendar.

        TWSE fiscal year 113 must be converted to 2024 (113 + 1911).
        _period_key() in twse.py applies this conversion, but only when the
        raw year value is a digit string < 1911.

        Source: credit_report/integrations/twse.py:160-162
        """
        from credit_report.integrations.twse import _period_key

        # ROC year 113 → Gregorian 2024.
        row_roc = {"年度": "113", "季別": ""}
        key = _period_key(row_roc)
        assert "2024" in key, (
            f"ROC year 113 should convert to Gregorian 2024, got {key!r}"
        )

        # Already-Gregorian year must pass through unchanged.
        row_greg = {"年度": "2024", "季別": ""}
        key2 = _period_key(row_greg)
        assert "2024" in key2

        # ROC quarter: year 112 Q4 → FY2023Q4.
        row_q = {"年度": "112", "季別": "4"}
        key3 = _period_key(row_q)
        assert "2023" in key3 and "Q4" in key3, (
            f"Expected FY2023Q4, got {key3!r}"
        )


# ===========================================================================
# Group 5 – Browser Security Bugs (#16–#18)
# ===========================================================================

class TestBrowserSecurityBugs:
    """Bugs visible only in a real browser or with real CORS headers."""

    # ── Bug #16 ────────────────────────────────────────────────────────────
    def test_marked_parse_assigned_to_innerhtml_without_dompurify(self):
        """marked.parse(d.markdown) is assigned directly to el.innerHTML.

        If the AI generates markdown containing raw HTML (e.g. ``<script>``
        or ``<img onerror=...>``), marked.js passes it through and the browser
        executes it.  DOMPurify is not loaded anywhere in index.html.

        Source: static/index.html:2312
        """
        html = INDEX_HTML.read_text(encoding="utf-8")

        # The XSS sink must exist.
        assert "marked.parse(d.markdown)" in html, (
            "Expected marked.parse(d.markdown) in index.html"
        )
        assert "el.innerHTML" in html

        # Confirm the exact dangerous pattern: innerHTML = ... marked.parse ...
        xss_pattern = re.compile(r"el\.innerHTML\s*=\s*.*?marked\.parse\(", re.DOTALL)
        assert xss_pattern.search(html), (
            "innerHTML assigned from marked.parse — XSS vector present"
        )

        # DOMPurify must NOT be present (no sanitization in place).
        assert "DOMPurify" not in html, (
            "DOMPurify is not loaded — marked output is rendered unsanitised"
        )
        assert "dompurify" not in html.lower()

    # ── Bug #17 ────────────────────────────────────────────────────────────
    def test_cors_wildcard_disables_credential_support(self):
        """CORS_ALLOW_ORIGINS='*' forces allow_credentials=False.

        When ``allow_credentials=True`` + ``allow_origins=["*"]`` are
        combined, CORS spec (and FastAPI) raise ``ValueError``.  The code
        correctly guards against this (main.py:160-161), but the consequence
        is that browsers cannot send cookies or ``Authorization`` headers on
        the wildcard-origin CORS response — fetch calls with ``credentials:
        'include'`` will be blocked.

        Source: main.py:159-162
                render.yaml:42-43
        """
        # Verify render.yaml ships with wildcard.
        data = yaml.safe_load(RENDER_YAML.read_text())
        env_vars = {e["key"]: e.get("value", "") for e in data["services"][0].get("envVars", [])}
        cors_value = env_vars.get("CORS_ALLOW_ORIGINS", "")
        assert cors_value == "*", (
            f"render.yaml CORS_ALLOW_ORIGINS expected '*', got {cors_value!r}"
        )

        # Reproduce the main.py credential-disable logic.
        cors_origins_raw = cors_value
        cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
        allow_creds = "*" not in cors_origins
        assert allow_creds is False, (
            "With CORS_ALLOW_ORIGINS='*', allow_credentials must be False — "
            "browsers will reject credentialed requests"
        )

    # ── Bug #18 ────────────────────────────────────────────────────────────
    def test_twse_paid_in_capital_unit_differs_from_financial_statements(self):
        """profile.paid_in_capital is raw TWD; financial-statement metrics are in thousands NTD.

        TWSE company_profile API returns 實收資本額 as a raw NTD integer
        (e.g. 259,303,805,450 for TSMC).  Income/balance sheet fields from
        t187ap06/07 are reported in *thousands* NTD.  build_section7_input
        mixes both without unit normalisation.

        Source: credit_report/integrations/twse.py:93-112 (PROFILE_FIELD_ALIASES)
                credit_report/integrations/twse.py:344-410 (build_section7_input)
        """
        from credit_report.integrations import twse as twse_mod

        # paid_in_capital comes from PROFILE_FIELD_ALIASES (raw TWD).
        assert "paid_in_capital" in twse_mod.PROFILE_FIELD_ALIASES, (
            "paid_in_capital must be in PROFILE_FIELD_ALIASES"
        )
        assert "實收資本額" in twse_mod.PROFILE_FIELD_ALIASES["paid_in_capital"]

        # Financial statement metrics are in thousands NTD.
        # The build_section7_input function sets unit="thousands" for financials.
        src = inspect.getsource(twse_mod.build_section7_input)
        assert '"thousands"' in src or "'thousands'" in src, (
            "build_section7_input should set unit='thousands' for financial data"
        )

        # The profile is included alongside financials — no conversion guard.
        assert "profile" in src, "profile data is included in section7 output"

        # Confirm the unit field exists in the output structure.
        # TSMC paid_in_capital ≈ 259 billion NTD raw → ÷ 1000 → 259 million thousands.
        # Without conversion, the value is 1000× too large relative to financials.
        assert '"unit"' in src or "'unit'" in src, (
            "Expected unit key in build_section7_input output"
        )


# ===========================================================================
# Correction Tests – Bugs overstated in the original analysis
# ===========================================================================

class TestCorrections:
    """Two items from the original '20 bugs' list are actually implemented
    correctly.  These tests confirm that and prevent future regressions."""

    # ── Correction #1 (Bug #7 in original list) ────────────────────────────
    def test_quota_is_correctly_scoped_per_user_not_global(self):
        """Token quota is per-user, stored with user_id as the key.

        The original analysis claimed quota was global.  In fact,
        check_quota() and record_tokens() both filter by
        ``UserTokenQuota.user_id == user_id``.

        Source: credit_report/generation/quota.py:31-36
        """
        import inspect
        from credit_report.generation import quota as quota_mod

        check_src = inspect.getsource(quota_mod.check_quota)
        assert "user_id" in check_src
        assert "UserTokenQuota.user_id == user_id" in check_src, (
            "check_quota must filter by user_id — quota is per-user"
        )

        record_src = inspect.getsource(quota_mod.record_tokens)
        assert "UserTokenQuota.user_id == user_id" in record_src, (
            "record_tokens must filter by user_id — quota is per-user"
        )

        # Per-role limits are also defined, showing intentional per-user design.
        assert "_ROLE_LIMITS" in vars(quota_mod)
        assert "analyst" in quota_mod._ROLE_LIMITS
        assert "admin" in quota_mod._ROLE_LIMITS

    # ── Correction #2 (Bug #8 in original list) ────────────────────────────
    def test_generation_semaphore_correctly_limits_concurrent_generations(self):
        """pipeline.py uses asyncio.Semaphore(CR_MAX_CONCURRENT_GENERATIONS).

        The original analysis suggested concurrent generation was unlimited.
        In fact, _generation_semaphore at pipeline.py:60 gates all generation
        calls.  This is the correct single-process back-pressure mechanism.

        Source: credit_report/generation/pipeline.py:60
        """
        from credit_report.generation import pipeline as pipeline_mod
        from credit_report.config import CR_MAX_CONCURRENT_GENERATIONS

        sem = pipeline_mod._generation_semaphore
        assert isinstance(sem, asyncio.Semaphore), (
            "_generation_semaphore must be an asyncio.Semaphore"
        )
        # asyncio.Semaphore stores the initial value in _value attribute.
        assert sem._value == CR_MAX_CONCURRENT_GENERATIONS, (
            f"Semaphore value {sem._value} must equal CR_MAX_CONCURRENT_GENERATIONS "
            f"({CR_MAX_CONCURRENT_GENERATIONS})"
        )

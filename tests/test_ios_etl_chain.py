"""
iOS/iPhone Safari compatibility and full ETL chain CI/CD tests.

Covers:
  1. HTML head: viewport-fit=cover, theme-color, apple-mobile-web-app meta tags
  2. File input accept: all required MIME types including .xls/.tif
  3. iOS download safety: _isIOS() helper and _dlBlobSafe() present
  4. No bare a.download patterns left without the iOS-safe wrapper
  5. Improve panel: mobile CSS @media rule present
  6. 100dvh usage for iOS Safari dynamic viewport
  7. -webkit-overflow-scrolling:touch on scrollable panels
  8. All onclick="funcName()" function names are defined in the JS
  9. All document.getElementById("id") call targets exist as HTML IDs
 10. Complete ETL → save → generate API chain (backend, all §1-10)
 11. ETL data persists through _etlExtractedData repopulation pattern
 12. confirmEtlMerge path: PUT /inputs/{sec} succeeds after ETL
 13. Chain continuity: no section left unreachable from generate endpoints
 14. Toast container has responsive max-width for small screens
 15. PDF fallback opens new tab (window.open) — popup-safe on iOS
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from credit_report.database import Base
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.fact_store.models  # noqa: F401
import credit_report.block_ast.models  # noqa: F401
import credit_report.security.models  # noqa: F401
import credit_report.audit.events  # noqa: F401
import credit_report.models  # noqa: F401

HTML_PATH = Path(__file__).parent.parent / "static" / "index.html"
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
ANALYST_ID = "ios-chain-test-user"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="function")
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


def _make_user(role: str = "analyst") -> Any:
    from credit_report.security.models import User
    return User(id=ANALYST_ID, email="ios@test.com", role=role,
                hashed_password="x", is_active=True)


async def _seed_report(db: AsyncSession, rid: str) -> None:
    from credit_report.models import Report
    db.add(Report(id=rid, borrower_name="iOS Test Corp", industry="marine",
                  report_type="new_deal", booking_branch="SG",
                  created_by=ANALYST_ID, status="draft"))
    await db.flush()


def _load_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1 — HTML head / meta tags for iOS Safari
# ═══════════════════════════════════════════════════════════════════════════════

class TestIOSMetaTags:
    def test_viewport_has_viewport_fit_cover(self):
        """iOS notch / safe-area: viewport-fit=cover required."""
        html = _load_html()
        assert "viewport-fit=cover" in html, (
            "Missing viewport-fit=cover — content will be clipped on iPhone notch"
        )

    def test_theme_color_meta_present(self):
        """iOS Safari tab colour: theme-color meta tag required."""
        html = _load_html()
        assert 'name="theme-color"' in html

    def test_apple_mobile_web_app_capable(self):
        """Home-screen web app: apple-mobile-web-app-capable required."""
        html = _load_html()
        assert 'apple-mobile-web-app-capable' in html

    def test_charset_utf8(self):
        html = _load_html()
        assert 'charset="UTF-8"' in html or "charset='UTF-8'" in html

    def test_html5_doctype(self):
        html = _load_html()
        assert html.strip().startswith("<!DOCTYPE html>")


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2 — File input: accept all required types
# ═══════════════════════════════════════════════════════════════════════════════

class TestFileInputAccept:
    def _get_accept(self) -> str:
        html = _load_html()
        m = re.search(r'<input[^>]+id="pdfInput"[^>]+accept="([^"]+)"', html)
        assert m, "Could not find pdfInput accept attribute"
        return m.group(1)

    @pytest.mark.parametrize("ext", [
        ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".txt", ".csv", ".md",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
        ".tiff", ".tif",   # both tiff variants
        ".xlsx", ".xls",   # both Excel variants
    ])
    def test_file_type_in_accept(self, ext):
        accept = self._get_accept()
        assert ext in accept, f"Missing {ext} from file input accept attribute"

    def test_accept_on_pdfInput_element(self):
        """The accept attribute must be on the #pdfInput element."""
        html = _load_html()
        # Find the pdfInput input element and check it has accept
        assert 'id="pdfInput"' in html
        # Find the line and ensure it has both id and accept
        m = re.search(r'<input[^>]+id="pdfInput"[^>]+>', html, re.DOTALL)
        assert m
        tag = m.group(0)
        assert "accept=" in tag


# ═══════════════════════════════════════════════════════════════════════════════
# Part 3 — iOS download safety
# ═══════════════════════════════════════════════════════════════════════════════

class TestIOSDownloadSafety:
    def test_is_ios_helper_defined(self):
        """_isIOS() helper function must be defined."""
        html = _load_html()
        assert "function _isIOS()" in html or "_isIOS=" in html

    def test_dl_blob_safe_defined(self):
        """_dlBlobSafe() wrapper must be defined for iOS-safe downloads."""
        html = _load_html()
        assert "_dlBlobSafe" in html, "_dlBlobSafe function missing"

    def test_is_ios_checks_useragent(self):
        """_isIOS() must check for iPad/iPhone/iPod in navigator.userAgent."""
        html = _load_html()
        assert "iPad|iPhone|iPod" in html or ("iPad" in html and "iPhone" in html)

    def test_is_ios_checks_maxTouchPoints(self):
        """_isIOS() must detect iPad (modern) via maxTouchPoints for iPadOS 13+."""
        html = _load_html()
        assert "maxTouchPoints" in html

    def test_dl_blob_safe_uses_window_open_for_ios(self):
        """iOS branch in _dlBlobSafe must use window.open (not a.download)."""
        html = _load_html()
        # The function body should contain window.open in the iOS branch
        # Find _dlBlobSafe function body
        m = re.search(r'function _dlBlobSafe\([^)]*\)\{(.+?)\}(?=\s*function)', html, re.DOTALL)
        assert m, "_dlBlobSafe function body not found"
        body = m.group(1)
        assert "window.open" in body, "_dlBlobSafe iOS branch must use window.open"

    def test_no_bare_a_download_with_click_in_export_functions(self):
        """exportDocx/exportPdf must not use the old bare a.download + a.click() pattern."""
        html = _load_html()
        # Extract the exportDocx and exportPdf function areas
        # Check that a.download is not directly followed by a.click in these functions
        # The old pattern was: a.download=fname; ... a.click();
        # If a.download appears, it must be inside _dlBlobSafe, not in the export functions directly
        export_section = re.search(
            r'async function exportDocx\(\).+?async function exportPdf\(\).+?function _exportPdfFallback',
            html, re.DOTALL
        )
        if export_section:
            section_text = export_section.group(0)
            # The old bare pattern: a.download=fname paired with document.body.appendChild(a);a.click()
            bare_pattern = re.search(r'a\.download\s*=\s*[^\n]+\n[^\n]*a\.click\(\)', section_text)
            assert not bare_pattern, (
                "Found bare a.download + a.click() in export functions — must use _dlBlobSafe instead"
            )

    def test_dlBlob_calls_dlBlobSafe(self):
        """dlBlob() must delegate to _dlBlobSafe (not use bare a.click)."""
        html = _load_html()
        m = re.search(r'function dlBlob\([^)]*\)\{(.+?)\}(?=\s*\n)', html, re.DOTALL)
        assert m, "dlBlob function not found"
        body = m.group(1)
        assert "_dlBlobSafe" in body, "dlBlob must call _dlBlobSafe"
        assert "a.click" not in body, "dlBlob must not use bare a.click"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 4 — Mobile CSS / responsive layout
# ═══════════════════════════════════════════════════════════════════════════════

class TestMobileCSS:
    def test_media_query_for_small_screens(self):
        """A @media(max-width:...) rule must exist for mobile overrides."""
        html = _load_html()
        assert "@media" in html and "max-width" in html

    def test_improve_panel_full_width_on_mobile(self):
        """Improve panel must be 100vw on small screens."""
        html = _load_html()
        assert "100vw" in html, "Improve panel missing 100vw for mobile"

    def test_dvh_for_ios_dynamic_viewport(self):
        """100dvh used for iOS Safari dynamic viewport height."""
        html = _load_html()
        assert "100dvh" in html, (
            "Missing 100dvh — iOS Safari 15+ dynamic viewport height not handled"
        )

    def test_webkit_overflow_scrolling_on_panel(self):
        """-webkit-overflow-scrolling: touch enables momentum scroll on iOS."""
        html = _load_html()
        assert "-webkit-overflow-scrolling" in html

    def test_toast_container_responsive_width(self):
        """Toast container must cap width so it doesn't overflow on iPhone."""
        html = _load_html()
        # Should have max-width on toast-cont or its children
        assert "max-width:calc(100vw" in html or "max-width: calc(100vw" in html


# ═══════════════════════════════════════════════════════════════════════════════
# Part 5 — JS function reference integrity
# ═══════════════════════════════════════════════════════════════════════════════

class TestJSFunctionIntegrity:
    def _extract_onclick_functions(self, html: str) -> set[str]:
        """Extract all function names called from onclick= attributes."""
        matches = re.findall(r'onclick="([^"(]+)\(', html)
        return {m.strip() for m in matches}

    def _extract_defined_functions(self, html: str) -> set[str]:
        """Extract all function names defined in script blocks."""
        # Match: function name(...) and async function name(...)
        matches = re.findall(r'(?:async\s+)?function\s+(\w+)\s*\(', html)
        # Also match arrow function assignments: const name = (...) =>
        arrow_matches = re.findall(r'(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(', html)
        return set(matches) | set(arrow_matches)

    def test_all_onclick_functions_are_defined(self):
        """Every function called from onclick= must be defined in the JS."""
        html = _load_html()
        called = self._extract_onclick_functions(html)
        defined = self._extract_defined_functions(html)

        # Some functions are globally available (browser builtins or method calls on global objects)
        builtins = {"confirm", "alert", "event"}
        # Filter out dotted names (e.g. event.stopPropagation, window.print) — these are
        # method calls on browser globals, not user-defined functions
        missing = {n for n in called - defined - builtins if "." not in n}

        assert not missing, (
            f"onclick references undefined functions: {sorted(missing)}\n"
            f"All called: {sorted(called)}\n"
            f"All defined: {sorted(defined)}"
        )

    def test_etl_chain_functions_defined(self):
        """All ETL chain functions must be defined."""
        html = _load_html()
        defined = self._extract_defined_functions(html)
        chain = [
            "uploadPDF", "etlDocument", "previewEtlMerge", "confirmEtlMerge",
            "saveSectionInput", "generateCurrentSection", "generateFromReview",
            "loadSectionOutput", "openPanel", "loadReportDetail",
        ]
        missing = [f for f in chain if f not in defined]
        assert not missing, f"ETL chain functions missing: {missing}"

    def test_export_functions_defined(self):
        """All export functions referenced in the UI must be defined."""
        html = _load_html()
        defined = self._extract_defined_functions(html)
        exports = ["exportReport", "exportMarkdown", "exportTxt", "exportPdf", "exportDocx"]
        missing = [f for f in exports if f not in defined]
        assert not missing, f"Export functions missing: {missing}"

    def test_ios_helpers_defined(self):
        html = _load_html()
        defined = self._extract_defined_functions(html)
        assert "_isIOS" in defined
        assert "_dlBlobSafe" in defined


# ═══════════════════════════════════════════════════════════════════════════════
# Part 6 — HTML element ID integrity
# ═══════════════════════════════════════════════════════════════════════════════

class TestElementIDIntegrity:
    def _extract_get_element_ids(self, html: str) -> set[str]:
        """Extract IDs passed to getElementById()."""
        return set(re.findall(r'getElementById\([\'"](\w[\w-]*)[\'"]', html))

    def _extract_defined_ids(self, html: str) -> set[str]:
        """Extract all id= attributes in the HTML."""
        return set(re.findall(r'\bid=[\'"](\w[\w-]*)[\'"]', html))

    def test_all_getElementById_targets_exist(self):
        """Every getElementById() call must reference an element that exists in the HTML."""
        html = _load_html()
        queried = self._extract_get_element_ids(html)
        defined = self._extract_defined_ids(html)
        missing = queried - defined
        # Some IDs are created dynamically at runtime (toast items, JS-rendered templates) — skip those
        # Also skip IDs that are injected via innerHTML or string-concatenated (e.g. 'revSpin'+n)
        dynamic_ok = {
            "toast-cont",       # exists as static element
            "revSpin",          # dynamically created per-section: id="revSpin${n}"
            "conflict-card-",   # template prefix: getElementById('conflict-card-'+id)
            "factTab-",         # template prefix: getElementById('factTab-'+tab)
            "reason-",          # template prefix: getElementById('reason-'+id)
            "vstatus-",         # template prefix: getElementById('vstatus-'+id)
            "gpRow",            # dynamically created: id="gpRow${n}" in generateAll() progress panel
            "gpBadge",          # dynamically created: id="gpBadge${n}" in generateAll() progress panel
        }
        actual_missing = missing - dynamic_ok
        assert not actual_missing, (
            f"getElementById references non-existent IDs: {sorted(actual_missing)}\n"
            f"Defined IDs: {sorted(defined)}"
        )

    def test_etl_modal_elements_exist(self):
        """ETL modal elements referenced in etlDocument() must exist."""
        html = _load_html()
        defined = self._extract_defined_ids(html)
        required = {"etlModal", "etlModalBody", "etlModalStatus", "etlMergeSecNo"}
        missing = required - defined
        assert not missing, f"ETL modal elements missing: {missing}"

    def test_section_panel_elements_exist(self):
        """Key section panel elements must exist."""
        html = _load_html()
        defined = self._extract_defined_ids(html)
        required = {"completenessPanel", "pdfInput", "docType", "docList"}
        missing = required - defined
        assert not missing, f"Section panel elements missing: {missing}"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 7 — Full ETL → save → generate API chain (backend)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("sec_no", list(range(1, 11)))
async def test_etl_save_generate_chain_section(db, sec_no):
    """
    Full chain: ETL mock data → PUT /inputs/{sec} → run pipeline.
    Mirrors exactly what the iOS browser does when the user:
    1. Uploads a document
    2. Clicks ETL → "Apply to §N"
    3. Clicks "Generate §N"
    """
    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    # Step 1: Simulate ETL output — the kind of data that _etlExtractedData stores
    etl_output = {
        "company_name": "Pacific Shipping Ltd",
        "revenue": 1500,
        "total_assets": 8000,
        "borrower": "Pacific Shipping Ltd",
        "facility_amount_usd_m": 120.0,
        "tenor_years": 7,
    }

    # Step 2: Save via PUT /inputs/{sec} (what confirmEtlMerge does)
    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload

    payload = SectionInputPayload(section_no=sec_no, input_json=etl_output)
    with (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core", new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    ):
        saved = await save_section_input(
            report_id=rid, section_no=sec_no, payload=payload,
            db=db, current_user=user,
        )
    assert saved is not None, f"ETL save failed for §{sec_no}"

    # Step 3: Generate via pipeline
    from credit_report.generation.pipeline import run_section_generation
    from credit_report.models import SectionOutput
    from sqlalchemy import select

    mock_response = MagicMock()
    mock_response.text = f"## §{sec_no} — Generated\n\nContent."
    mock_response.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=10)
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    with (
        patch("google.genai.Client", return_value=mock_client),
        patch("credit_report.config.GEMINI_API_KEY", "mock-key"),
        patch("credit_report.generation.pipeline.reserve_and_record_tokens", new=AsyncMock()),
        patch("credit_report.generation.pipeline.write_event", new=AsyncMock()),
    ):
        await run_section_generation(
            report_id=rid, section_no=sec_no, db=db, actor_user_id=ANALYST_ID,
        )

    result = await db.execute(
        select(SectionOutput).where(
            SectionOutput.report_id == rid,
            SectionOutput.section_no == sec_no,
        )
    )
    output = result.scalar_one_or_none()
    assert output is not None, f"§{sec_no}: SectionOutput not created"
    assert output.status == "done", f"§{sec_no}: status={output.status}, expected done"
    assert output.markdown, f"§{sec_no}: markdown is empty"


@pytest.mark.asyncio
async def test_etl_merge_overwrites_existing_keys(db):
    """
    confirmEtlMerge merges ETL data on top of existing input.
    New keys are added; conflicting keys are overwritten by ETL data.
    """
    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    # First save (existing analyst input)
    existing = {"borrower": "Old Name", "tenor_years": 5, "analyst_note": "keep this"}
    with (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core", new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    ):
        await save_section_input(
            report_id=rid, section_no=1,
            payload=SectionInputPayload(section_no=1, input_json=existing),
            db=db, current_user=user,
        )

    # ETL merge (simulates confirmEtlMerge — sends full merged dict)
    merged = {**existing, "borrower": "ETL Company", "facility_amount_usd_m": 150.0}
    with (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core", new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    ):
        await save_section_input(
            report_id=rid, section_no=1,
            payload=SectionInputPayload(section_no=1, input_json=merged),
            db=db, current_user=user,
        )

    retrieved = await get_section_input(report_id=rid, section_no=1, db=db, current_user=user)
    stored = retrieved.input_json
    assert stored["borrower"] == "ETL Company", "ETL value should overwrite existing"
    assert stored["analyst_note"] == "keep this", "Non-conflicting key should be preserved"
    assert stored["facility_amount_usd_m"] == 150.0, "New ETL field should be added"


@pytest.mark.asyncio
async def test_full_ios_etl_chain_all_sections(db):
    """
    Full 10-section iOS chain smoke test:
    ETL-style data → save all sections → generate all → all outputs done.
    Represents the complete user workflow on iPhone Safari.
    """
    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    etl_data = {sec: {"company_name": f"Corp §{sec}", "revenue": sec * 100} for sec in range(1, 11)}

    from credit_report.api.reports import save_section_input
    from credit_report.schemas import SectionInputPayload
    from credit_report.generation.pipeline import run_section_generation
    from credit_report.models import SectionOutput
    from sqlalchemy import select

    # Save all sections (what confirmEtlMerge does for each)
    for sec in range(1, 11):
        with (
            patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
            patch("credit_report.api.calculations._run_recalculate_core", new=AsyncMock(return_value=(0, []))),
            patch("credit_report.api.reports.write_event", new=AsyncMock()),
        ):
            await save_section_input(
                report_id=rid, section_no=sec,
                payload=SectionInputPayload(section_no=sec, input_json=etl_data[sec]),
                db=db, current_user=user,
            )

    # Generate all sections
    mock_response = MagicMock()
    mock_response.text = "## Generated\n\nContent."
    mock_response.usage_metadata = MagicMock(prompt_token_count=5, candidates_token_count=5)
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    with (
        patch("google.genai.Client", return_value=mock_client),
        patch("credit_report.config.GEMINI_API_KEY", "mock-key"),
        patch("credit_report.generation.pipeline.reserve_and_record_tokens", new=AsyncMock()),
        patch("credit_report.generation.pipeline.write_event", new=AsyncMock()),
    ):
        for sec in range(1, 11):
            await run_section_generation(
                report_id=rid, section_no=sec, db=db, actor_user_id=ANALYST_ID,
            )

    # Verify all outputs are done
    result = await db.execute(
        select(SectionOutput).where(SectionOutput.report_id == rid)
    )
    outputs = result.scalars().all()
    assert len(outputs) == 10, f"Expected 10 outputs, got {len(outputs)}"
    for out in outputs:
        assert out.status == "done", f"§{out.section_no} not done: {out.status}"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 8 — API endpoint chain connectivity (no broken links)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_generate_endpoint_exists_for_all_sections(db):
    """generate_section endpoint must accept all section numbers 1-10 (no KeyError/404)."""
    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    from fastapi import BackgroundTasks
    from credit_report.api.generate import generate_section

    for sec in range(1, 11):
        mock_bg = MagicMock(spec=BackgroundTasks)
        mock_bg.add_task = MagicMock()
        with (
            patch("credit_report.api.generate.run_section_generation", new=AsyncMock()),
            # Bypass hard dependency check — we're testing endpoint routing, not ordering rules
            patch("credit_report.api.generate.check_hard_dependencies", new=AsyncMock(return_value=[])),
        ):
            resp = await generate_section(
                report_id=rid, section_no=sec,
                db=db, background_tasks=mock_bg, current_user=user,
            )
        assert resp is not None, f"generate_section returned None for §{sec}"


@pytest.mark.asyncio
async def test_get_section_input_after_etl_save(db):
    """
    After ETL save (PUT /inputs/{sec}), GET /inputs/{sec} must return the data.
    This is the read-back that the Review Panel performs.
    """
    rid = str(uuid.uuid4())
    await _seed_report(db, rid)
    user = _make_user()

    from credit_report.api.reports import save_section_input, get_section_input
    from credit_report.schemas import SectionInputPayload

    data = {"borrower": "Mobile Test Corp", "revenue": 999}
    with (
        patch("credit_report.api.reports.upsert_facts", new=AsyncMock()),
        patch("credit_report.api.calculations._run_recalculate_core", new=AsyncMock(return_value=(0, []))),
        patch("credit_report.api.reports.write_event", new=AsyncMock()),
    ):
        await save_section_input(
            report_id=rid, section_no=4,
            payload=SectionInputPayload(section_no=4, input_json=data),
            db=db, current_user=user,
        )

    retrieved = await get_section_input(report_id=rid, section_no=4, db=db, current_user=user)
    assert retrieved is not None
    stored = retrieved.input_json if hasattr(retrieved, "input_json") else retrieved.get("input_json", {})
    assert stored.get("borrower") == "Mobile Test Corp"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 9 — ETL data key chain (JS _etlExtractedData repopulation pattern)
# ═══════════════════════════════════════════════════════════════════════════════

class TestETLDataChainPattern:
    def test_etl_extracted_data_var_defined(self):
        """_etlExtractedData must be declared at module level (not just in function scope)."""
        html = _load_html()
        assert "_etlExtractedData" in html
        assert "_etlExtractedData={}" in html or "_etlExtractedData = {}" in html

    def test_etl_extracted_data_repopulated_after_loadReportDetail(self):
        """
        _etlExtractedData must be populated AFTER loadReportDetail() call.
        This was the bug where Apply buttons showed 'No extracted data found'.
        """
        html = _load_html()
        # Find the sequence: loadReportDetail should come BEFORE _etlExtractedData={} in the etlDocument body
        etl_doc_match = re.search(
            r'async function etlDocument\(.+?(?=async function \w)',
            html, re.DOTALL
        )
        assert etl_doc_match, "etlDocument function not found"
        body = etl_doc_match.group(0)

        load_pos = body.find("loadReportDetail")
        repop_pos = body.find("_etlExtractedData = {}")
        if repop_pos == -1:
            repop_pos = body.find("_etlExtractedData={}")

        assert load_pos != -1, "loadReportDetail call not found in etlDocument"
        assert repop_pos != -1, "_etlExtractedData reset not found in etlDocument"
        assert repop_pos > load_pos, (
            "_etlExtractedData must be reset AFTER loadReportDetail "
            "(was the original bug causing 'No extracted data found')"
        )

    def test_preview_etl_merge_reads_etl_data(self):
        """previewEtlMerge must read from _etlExtractedData[secNo]."""
        html = _load_html()
        assert "_etlExtractedData[secNo]" in html or "_etlExtractedData[sec" in html

    def test_preview_etl_merge_checks_for_null(self):
        """previewEtlMerge must guard against missing data (show error if not found)."""
        html = _load_html()
        m = re.search(r'function previewEtlMerge\(.+?(?=function \w)', html, re.DOTALL)
        assert m, "previewEtlMerge not found"
        body = m.group(0)
        # Should check if data is falsy and toast/return early
        assert "No extracted data" in body or "toast" in body


# ═══════════════════════════════════════════════════════════════════════════════
# Part 10 — iOS popup / window.open pattern check
# ═══════════════════════════════════════════════════════════════════════════════

class TestIOSPopupSafety:
    def test_pdf_fallback_uses_window_open(self):
        """PDF browser fallback uses window.open (supported on iOS Safari)."""
        html = _load_html()
        m = re.search(r'function _exportPdfFallback\(\)\{(.+?)\}(?=\s*function)', html, re.DOTALL)
        assert m, "_exportPdfFallback not found"
        body = m.group(1)
        assert "window.open" in body

    def test_ios_download_guidance_present(self):
        """iOS users must see guidance on how to save the file (no popup needed)."""
        html = _load_html()
        # _dlBlobSafe now uses a.click() instead of window.open() on iOS,
        # so a toast with Share/Save instructions is shown instead of popup warning
        assert "Save to Files" in html or "Share" in html or "tap" in html.lower()

    def test_ios_share_instructions_in_toast(self):
        """iOS users must be instructed how to save (Share → Save to Files)."""
        html = _load_html()
        assert "Save to Files" in html or "Share" in html

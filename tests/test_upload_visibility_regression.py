"""Regression tests for the "uploaded document disappears from the UI" bug.

Two layers are covered:

1. Backend guarantee — a document is durably committed and immediately visible via
   GET /documents right after the 201 from POST /documents (no background-task or
   commit-timing race makes it vanish).

2. Frontend safeguards (static HTML source assertions) — loadReportDetail must NOT
   silently blank the document list when the documents fetch fails, and uploadPDF
   must optimistically render the freshly-uploaded doc so it never "disappears"
   even if the follow-up reload fails.
"""
import io
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app

RPTS = "/api/credit-report/reports"
AUTH = "/api/credit-report/auth"
HTML = (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def admin_hdrs(ac):
    r = await ac.post(f"{AUTH}/login", data={"username": "admin@example.com", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── Backend: uploaded doc is immediately listable ────────────────────────────

@pytest.mark.asyncio
async def test_uploaded_doc_visible_immediately(ac, admin_hdrs):
    r = await ac.post(RPTS, json={"borrower_name": "VisCo", "industry": "shipping"}, headers=admin_hdrs)
    rid = r.json()["id"]
    fake_pdf = b"%PDF-1.4 annual report revenue 500M"
    up = await ac.post(
        f"{RPTS}/{rid}/documents",
        files={"file": ("2330_annual.pdf", io.BytesIO(fake_pdf), "application/pdf")},
        data={"document_type": "annual_report"},
        headers=admin_hdrs,
    )
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]

    lst = await ac.get(f"{RPTS}/{rid}/documents", headers=admin_hdrs)
    assert lst.status_code == 200, lst.text
    ids = [d["id"] for d in lst.json()]
    assert doc_id in ids, f"Uploaded doc {doc_id} must be visible immediately; got {ids}"


@pytest.mark.asyncio
async def test_uploaded_doc_persists_across_two_list_calls(ac, admin_hdrs):
    """A second GET (what loadReportDetail does on reload) still sees the doc."""
    r = await ac.post(RPTS, json={"borrower_name": "VisCo2", "industry": "shipping"}, headers=admin_hdrs)
    rid = r.json()["id"]
    up = await ac.post(
        f"{RPTS}/{rid}/documents",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4 hello"), "application/pdf")},
        data={"document_type": "other"},
        headers=admin_hdrs,
    )
    doc_id = up.json()["id"]
    for _ in range(2):
        lst = await ac.get(f"{RPTS}/{rid}/documents", headers=admin_hdrs)
        assert doc_id in [d["id"] for d in lst.json()]


# ── Frontend: no silent blanking + optimistic render ─────────────────────────

def test_render_doc_list_helper_exists():
    assert "function renderDocList(" in HTML, "renderDocList helper must exist"


def test_load_report_detail_does_not_blank_on_doc_fetch_failure():
    # The old code did `const docs=docRes.ok?...:[]` which silently blanked the list.
    assert "docFetchFailed" in HTML, "loadReportDetail must track a doc-fetch failure flag"
    assert "const docs=docRes.ok?await docRes.json():[]" not in HTML, (
        "Old silent-blank pattern for documents must be removed"
    )


def test_upload_optimistically_renders_new_doc():
    # After a successful upload, the new doc is pushed into uploadedDocs and rendered
    # immediately so it never disappears even if the follow-up reload fails.
    assert "uploadedDocs.unshift(d)" in HTML, (
        "uploadPDF must optimistically insert the uploaded doc"
    )
    assert "renderDocList(false)" in HTML, "uploadPDF must re-render the doc list after upload"


def test_same_report_reload_preserves_cached_docs():
    # loadReportDetail must only clear uploadedDocs when switching to a different report.
    assert "_switchingReport" in HTML, (
        "loadReportDetail must preserve cached docs on same-report reload"
    )


# ── TWSE import message clarity ──────────────────────────────────────────────

def test_twse_zero_written_message_is_not_confusing():
    """The old "0 field(s) written into none (34 skipped)" message is replaced by
    three clear branches (wrote / all already filled / no usable data)."""
    # The confusing literal must be gone.
    assert "written into ${secList} (${d.fields_skipped} skipped" not in HTML, (
        "Old confusing 'written into none' TWSE message must be removed"
    )
    # The "all matching fields already filled" branch must exist (English + Chinese).
    assert "already filled, so nothing was overwritten" in HTML
    assert "皆已填寫" in HTML
    # The "no usable values" branch must NOT say "no data" but "source not obtained".
    assert "no usable values for the targeted sections" in HTML
    assert "資料來源未成功提供" in HTML

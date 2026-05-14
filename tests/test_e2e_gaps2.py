"""
CI/CD Gap Coverage Tests — Round 2
====================================
Covers 14 scenarios identified in the second-pass audit:

  G1  TestETLErrorPaths          — ETL 404 (doc not found) + non-owner analyst 403
  G2  TestDocumentRBAC           — upload & delete by non-owner analyst → 403
  G3  TestSubmitRecallOwnerCheck — submit-for-review & recall by non-owner → 403
  G4  TestImportJsonRBAC         — import-section-json by non-owner analyst → 403
  G5  TestConflictAutoTrigger    — two conflicting facts → FactConflict auto-created
  G6  TestStatusTransitions      — PATCH status → review_in_progress; 404 not found
  G7  TestFactLockEdgeCases      — approve version mismatch → 409; analyst deprecate → 403
  G8  TestStatsEmpty             — block stats on report with no blocks → all-zero
  G9  TestRecalcNoFacts          — recalculate with no facts → 0 computed
"""
from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from main import app  # noqa: E402

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
REPORTS = f"{BASE}/reports"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_gemini(return_text: str = "## Section\n\nContent."):
    mock_resp = MagicMock()
    mock_resp.text = return_text
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
    return patch("google.genai.Client", return_value=mock_client)


async def _login(ac, email, password="admin123"):
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def _auth_headers(ac, email="admin@example.com", password="admin123"):
    tok = await _login(ac, email, password)
    return {"Authorization": f"Bearer {tok['access_token']}"}


async def _create_report(ac, hdrs, borrower="Gap2 Test Co"):
    r = await ac.post(f"{REPORTS}", json={"borrower_name": borrower, "industry": "marine"}, headers=hdrs)
    assert r.status_code == 201, r.text
    return r.json()


async def _register_user(ac, admin_hdrs, email, role="analyst", password="Pass1234!"):
    r = await ac.post(
        f"{AUTH}/register",
        json={"email": email, "password": password, "role": role},
        headers=admin_hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _save_input(ac, hdrs, report_id, sec, data):
    r = await ac.put(
        f"{REPORTS}/{report_id}/inputs/{sec}",
        json={"section_no": sec, "input_json": data},
        headers=hdrs,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _upload_txt(ac, hdrs, report_id, content=b"test content", filename="doc.txt"):
    r = await ac.post(
        f"{REPORTS}/{report_id}/documents",
        files={"file": (filename, content, "text/plain")},
        data={"document_type": "other"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _make_report_in_review(ac, admin_hdrs):
    """Create a report with a done section and submit for review; return report dict."""
    from credit_report.database import AsyncSessionLocal
    from credit_report.models import SectionOutput

    rpt = await _create_report(ac, admin_hdrs, borrower=f"ReviewCo {uuid.uuid4().hex[:6]}")
    await _save_input(ac, admin_hdrs, rpt["id"], 4, {"x": 1})

    async with AsyncSessionLocal() as db:
        db.add(SectionOutput(
            id=str(uuid.uuid4()),
            report_id=rpt["id"],
            section_no=4,
            markdown="## §4\n\nContent.",
            status="done",
            tokens_used=100,
        ))
        await db.commit()

    r = await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
    assert r.status_code == 200, r.text
    return rpt


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def admin_hdrs(ac):
    return await _auth_headers(ac)


@pytest_asyncio.fixture
async def report(ac, admin_hdrs):
    return await _create_report(ac, admin_hdrs)


@pytest_asyncio.fixture
async def other_analyst(ac, admin_hdrs):
    """Register a second analyst (non-owner of admin's reports)."""
    email = f"other_{uuid.uuid4().hex[:8]}@test.com"
    await _register_user(ac, admin_hdrs, email, role="analyst")
    hdrs = await _auth_headers(ac, email, "Pass1234!")
    return {"email": email, "headers": hdrs}


# ══════════════════════════════════════════════════════════════════════════════
# G1 — ETL error paths
# ══════════════════════════════════════════════════════════════════════════════

class TestETLErrorPaths:

    async def test_etl_doc_not_found_returns_404(self, ac, admin_hdrs, report):
        """POST /documents/{non_existent_id}/etl → 404."""
        fake_doc_id = str(uuid.uuid4())
        r = await ac.post(
            f"{REPORTS}/{report['id']}/documents/{fake_doc_id}/etl",
            headers=admin_hdrs,
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    async def test_etl_non_owner_analyst_forbidden(self, ac, admin_hdrs, report, other_analyst):
        """Non-owner analyst cannot run ETL on another user's report → 403."""
        doc = await _upload_txt(ac, admin_hdrs, report["id"])

        r = await ac.post(
            f"{REPORTS}/{report['id']}/documents/{doc['id']}/etl",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# G2 — Document upload / delete RBAC (non-owner analyst)
# ══════════════════════════════════════════════════════════════════════════════

class TestDocumentRBAC:

    async def test_upload_document_non_owner_forbidden(self, ac, admin_hdrs, report, other_analyst):
        """Non-owner analyst cannot upload a document to another user's report → 403."""
        r = await ac.post(
            f"{REPORTS}/{report['id']}/documents",
            files={"file": ("x.txt", b"data", "text/plain")},
            data={"document_type": "other"},
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403

    async def test_delete_document_non_owner_forbidden(self, ac, admin_hdrs, report, other_analyst):
        """Non-owner analyst cannot delete a document from another user's report → 403."""
        doc = await _upload_txt(ac, admin_hdrs, report["id"])

        r = await ac.delete(
            f"{REPORTS}/{report['id']}/documents/{doc['id']}",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# G3 — submit-for-review & recall owner check
# ══════════════════════════════════════════════════════════════════════════════

class TestSubmitRecallOwnerCheck:

    async def test_submit_for_review_non_owner_forbidden(self, ac, admin_hdrs, report, other_analyst):
        """Non-owner analyst cannot submit another user's report for review → 403."""
        r = await ac.post(
            f"{REPORTS}/{report['id']}/submit-for-review",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403

    async def test_recall_non_owner_forbidden(self, ac, admin_hdrs, other_analyst):
        """Non-owner analyst cannot recall another user's report → 403.

        The owner check fires before the status check, so even a draft report
        returns 403 (not 409) for a non-owner.
        """
        rpt = await _create_report(ac, admin_hdrs, borrower=f"RecallCo {uuid.uuid4().hex[:6]}")
        r = await ac.post(
            f"{REPORTS}/{rpt['id']}/recall",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# G4 — import-section-json RBAC (non-owner analyst)
# ══════════════════════════════════════════════════════════════════════════════

class TestImportJsonRBAC:

    async def test_import_section_json_non_owner_forbidden(self, ac, admin_hdrs, report, other_analyst):
        """Non-owner analyst cannot import JSON into another user's report → 403."""
        payload = {"revenue": 5000}
        json_bytes = json.dumps(payload).encode()

        r = await ac.post(
            f"{REPORTS}/{report['id']}/import-section-json",
            files={"file": ("data.json", json_bytes, "application/json")},
            data={"section_no": "4"},
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# G5 — Conflict auto-trigger (fact upsert detection)
# ══════════════════════════════════════════════════════════════════════════════

class TestConflictAutoTrigger:

    async def test_conflict_created_when_facts_disagree(self, ac, admin_hdrs, report):
        """Upserting two facts with the same metric key but different source & value
        (> 2% delta) should automatically create an open FactConflict visible via the
        GET /facts/conflicts endpoint."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_facts

        rid = report["id"]

        # First fact: analyst input source, value = 5000
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [{
                "report_id": rid,
                "metric_name": "total_assets",
                "entity": "AutoConflictCo",
                "period": "FY2024",
                "value": 5000.0,
                "source_type": "analyst_input_json",
                "source_priority": 2,
            }])
            await db.commit()

        # Second fact: different source, value = 4200 (16% lower — triggers conflict)
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [{
                "report_id": rid,
                "metric_name": "total_assets",
                "entity": "AutoConflictCo",
                "period": "FY2024",
                "value": 4200.0,
                "source_type": "pdf_extraction",
                "source_priority": 3,
            }])
            await db.commit()

        # Verify the conflict is visible via API
        r = await ac.get(
            f"{REPORTS}/{rid}/facts/conflicts",
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        conflicts = r.json()
        auto_conflicts = [
            c for c in conflicts
            if c["metric_name"] == "total_assets" and c["entity"] == "AutoConflictCo"
        ]
        assert len(auto_conflicts) >= 1, "Expected at least one auto-created conflict"
        assert auto_conflicts[0]["status"] == "open"
        assert auto_conflicts[0]["fact_a_id"] is not None
        assert auto_conflicts[0]["fact_b_id"] is not None


# ══════════════════════════════════════════════════════════════════════════════
# G6 — PATCH /reports/{id}/status additional transitions
# ══════════════════════════════════════════════════════════════════════════════

class TestStatusTransitions:

    async def test_patch_status_to_review_in_progress_direct(self, ac, admin_hdrs, report):
        """Admin can PATCH a report status directly to review_in_progress (no section
        completion requirement for the raw PATCH endpoint, unlike submit-for-review)."""
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/status",
            json={"status": "review_in_progress"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "review_in_progress"

    async def test_patch_status_report_not_found(self, ac, admin_hdrs):
        """PATCH status on a non-existent report returns 404."""
        r = await ac.patch(
            f"{REPORTS}/{uuid.uuid4()}/status",
            json={"status": "validated"},
            headers=admin_hdrs,
        )
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# G7 — Fact locking / RBAC edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestFactLockEdgeCases:

    async def _seed_fact(self, report_id: str, state: str = "validated") -> str:
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.models import CanonicalFact

        fid = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(CanonicalFact(
                id=fid, report_id=report_id,
                metric_name=f"lock_test_{fid[:8]}", entity="LockCo", period="FY2024",
                value=1000.0,
                state=state, source_type="analyst_input_json",
                source_priority=2, version=1,
            ))
            await db.commit()
        return fid

    async def test_approve_fact_version_mismatch_returns_409(self, ac, admin_hdrs, report):
        """Approving a fact with a stale expected_version returns 409 (optimistic lock)."""
        fact_id = await self._seed_fact(report["id"], state="validated")

        email = f"rev2_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, role="reviewer")
        rev_hdrs = await _auth_headers(ac, email, "Pass1234!")

        # Send wrong version (99 instead of 1)
        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/{fact_id}/approve",
            json={"expected_version": 99},
            headers=rev_hdrs,
        )
        assert r.status_code == 409
        assert "version" in r.json()["detail"].lower()

    async def test_deprecate_fact_by_analyst_forbidden(self, ac, admin_hdrs, report):
        """Analyst cannot deprecate a fact — deprecate requires reviewer role → 403."""
        fact_id = await self._seed_fact(report["id"], state="validated")

        email = f"ana3_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, role="analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")

        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/{fact_id}/deprecate",
            params={"reason": "Analyst should not be able to do this"},
            headers=ana_hdrs,
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# G8 — Block stats on an empty report
# ══════════════════════════════════════════════════════════════════════════════

class TestStatsEmpty:

    async def test_block_stats_no_blocks_returns_zeros(self, ac, admin_hdrs, report):
        """GET /blocks/stats on a report with no blocks returns all-zero counters."""
        r = await ac.get(
            f"{REPORTS}/{report['id']}/blocks/stats",
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        data = r.json()

        # All numeric counters must be present and zero
        assert data["total_blocks"] == 0
        assert data["pending"] == 0
        assert data["passed"] == 0
        assert data["failed"] == 0
        assert data["stale"] == 0
        assert data["total_cells"] == 0
        assert data["bound_cells"] == 0
        assert data["unbound_cells"] == 0
        assert data["binding_rate_pct"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# G9 — Recalculate with no facts
# ══════════════════════════════════════════════════════════════════════════════

class TestRecalcNoFacts:

    async def test_recalculate_no_facts_returns_zero_computed(self, ac, admin_hdrs):
        """POST /recalculate on a fresh report with no facts returns
        calculations_computed=0 and entity_period_pairs=0."""
        rpt = await _create_report(ac, admin_hdrs, borrower=f"EmptyCalcCo {uuid.uuid4().hex[:6]}")

        r = await ac.post(
            f"{REPORTS}/{rpt['id']}/recalculate",
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["calculations_computed"] == 0
        assert data["entity_period_pairs"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# G10 — Bonus: save section input non-owner check
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveInputNonOwner:

    async def test_save_section_input_non_owner_forbidden(self, ac, admin_hdrs, report, other_analyst):
        """Non-owner analyst cannot PUT section input on another user's report → 403."""
        r = await ac.put(
            f"{REPORTS}/{report['id']}/inputs/4",
            json={"section_no": 4, "input_json": {"x": 1}},
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# G11 — Bonus: batch generate non-owner check
# ══════════════════════════════════════════════════════════════════════════════

class TestBatchGenerateNonOwner:

    async def test_generate_full_report_non_owner_forbidden(self, ac, admin_hdrs, other_analyst):
        """Non-owner analyst cannot trigger batch generation on another user's report → 403."""
        rpt = await _create_report(ac, admin_hdrs, borrower=f"BatchGenCo {uuid.uuid4().hex[:6]}")
        await _save_input(ac, admin_hdrs, rpt["id"], 4, {"x": 1})

        r = await ac.post(
            f"{REPORTS}/{rpt['id']}/generate",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403

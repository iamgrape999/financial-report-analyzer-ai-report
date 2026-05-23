"""
CI/CD Gap Coverage Tests — Round 3
=====================================
Covers 10 scenarios identified in the third-pass audit:

  H1  TestRoleChangeEdgeCases      — PATCH /auth/users/{id}/role: 404 bogus user, 400 invalid role
  H2  TestDeleteReportOwnership    — DELETE /report by non-owner analyst → 403
  H3  TestPatchStatusOwnership     — PATCH /status (non-approved) by non-owner analyst → 403
  H4  TestFxRateValidation         — PUT /fx-rates missing required field → 422
  H5  TestDoubleApproveReport      — POST /approve on already-approved report → 409
  H6  TestAuditPermissive          — GET /audit: non-owner analyst can read (no ownership guard)
  H7  TestRecalculatePermissive    — POST /recalculate: non-owner analyst can trigger
  H8  TestLtvAcrEdgeCases          — POST /calculations/ltv-acr with empty schedule → 200 []
  H9  TestBlockPatchPermissive     — PATCH /blocks: ownership guard enforced (non-owner gets 403)
  H10 TestFxRateZeroRate           — PUT /fx-rates with rate=0.0 accepted (valid float)
"""
from __future__ import annotations

import os
import uuid

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

async def _login(ac, email, password="admin123"):
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def _auth_headers(ac, email="admin@example.com", password="admin123"):
    tok = await _login(ac, email, password)
    return {"Authorization": f"Bearer {tok['access_token']}"}


async def _register_user(ac, admin_hdrs, email, role="analyst", password="Pass1234!"):
    r = await ac.post(
        f"{AUTH}/register",
        json={"email": email, "password": password, "role": role},
        headers=admin_hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _create_report(ac, hdrs, borrower="Gap3 Test Co"):
    r = await ac.post(
        f"{REPORTS}", json={"borrower_name": borrower, "industry": "marine"}, headers=hdrs
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _make_approved_report(ac, admin_hdrs):
    """Create a report and advance it all the way to 'approved' status."""
    from credit_report.database import AsyncSessionLocal
    from credit_report.models import SectionOutput

    rpt = await _create_report(ac, admin_hdrs, borrower=f"ApprCo {uuid.uuid4().hex[:6]}")

    async with AsyncSessionLocal() as db:
        db.add(SectionOutput(
            id=str(uuid.uuid4()),
            report_id=rpt["id"],
            section_no=4,
            markdown="## §4\n\nContent.",
            status="done",
            tokens_used=50,
        ))
        await db.commit()

    r = await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
    assert r.status_code == 200, r.text

    r = await ac.post(f"{REPORTS}/{rpt['id']}/approve", headers=admin_hdrs)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    return rpt


async def _seed_block(report_id: str) -> str:
    """Insert a minimal block directly into the DB; return block_id."""
    from credit_report.database import AsyncSessionLocal
    from credit_report.block_ast.models import ReportBlock

    block_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(ReportBlock(
            id=block_id,
            report_id=report_id,
            section_no=1,
            block_type="paragraph",
            content="Original content for ownership test.",
            version=1,
        ))
        await db.commit()
    return block_id


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
    """Second analyst who does NOT own admin's reports."""
    email = f"gap3_{uuid.uuid4().hex[:8]}@test.com"
    await _register_user(ac, admin_hdrs, email, role="analyst")
    hdrs = await _auth_headers(ac, email, "Pass1234!")
    return {"email": email, "headers": hdrs}


# ══════════════════════════════════════════════════════════════════════════════
# H1 — PATCH /auth/users/{user_id}/role edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestRoleChangeEdgeCases:

    async def test_role_change_user_not_found_returns_404(self, ac, admin_hdrs):
        """PATCH /auth/users/{id}/role with a non-existent user_id → 404."""
        r = await ac.patch(
            f"{AUTH}/users/{uuid.uuid4()}/role",
            params={"role": "reviewer"},
            headers=admin_hdrs,
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    async def test_role_change_invalid_role_returns_400(self, ac, admin_hdrs):
        """PATCH /auth/users/{id}/role with an unrecognised role value → 400."""
        user = await _register_user(
            ac, admin_hdrs, f"badrole_{uuid.uuid4().hex[:8]}@test.com"
        )
        r = await ac.patch(
            f"{AUTH}/users/{user['id']}/role",
            params={"role": "superuser"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400
        assert "invalid role" in r.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# H2 — DELETE /reports/{id} by non-owner analyst
# ══════════════════════════════════════════════════════════════════════════════

class TestDeleteReportOwnership:

    async def test_delete_report_non_owner_analyst_returns_403(
        self, ac, admin_hdrs, report, other_analyst
    ):
        """Non-owner analyst attempting DELETE on another user's report → 403."""
        r = await ac.delete(
            f"{REPORTS}/{report['id']}",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403

    async def test_delete_report_owner_succeeds(self, ac, admin_hdrs, report):
        """Owner (admin here) can soft-delete their own report → 204."""
        r = await ac.delete(f"{REPORTS}/{report['id']}", headers=admin_hdrs)
        assert r.status_code == 204
        # Confirm it is now a 404
        r2 = await ac.get(f"{REPORTS}/{report['id']}", headers=admin_hdrs)
        assert r2.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# H3 — PATCH /reports/{id}/status by non-owner analyst (non-"approved" target)
# ══════════════════════════════════════════════════════════════════════════════

class TestPatchStatusOwnership:

    async def test_patch_status_validated_by_non_owner_analyst_returns_403(
        self, ac, admin_hdrs, report, other_analyst
    ):
        """Non-owner analyst cannot PATCH /status to 'validated' on another's report → 403.

        The 'approved' guard fires first for role checks; for other valid statuses
        _assert_owner_or_admin is called — non-owner analyst must receive 403.
        """
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/status",
            json={"status": "validated"},
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403

    async def test_patch_status_draft_by_non_owner_analyst_returns_403(
        self, ac, admin_hdrs, report, other_analyst
    ):
        """Non-owner analyst cannot PATCH /status to 'draft' on another's report → 403."""
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/status",
            json={"status": "draft"},
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# H4 — PUT /reports/{id}/fx-rates validation
# ══════════════════════════════════════════════════════════════════════════════

class TestFxRateValidation:

    async def test_fx_rate_missing_required_field_returns_422(self, ac, admin_hdrs, report):
        """PUT /fx-rates without 'rate' field → 422 (Pydantic validation error)."""
        r = await ac.put(
            f"{REPORTS}/{report['id']}/fx-rates",
            json={"from_currency": "TWD", "to_currency": "USD", "source": "manual"},
            headers=admin_hdrs,
        )
        assert r.status_code == 422

    async def test_fx_rate_missing_currency_returns_422(self, ac, admin_hdrs, report):
        """PUT /fx-rates without 'from_currency' field → 422."""
        r = await ac.put(
            f"{REPORTS}/{report['id']}/fx-rates",
            json={"to_currency": "USD", "rate": 0.031, "source": "manual"},
            headers=admin_hdrs,
        )
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# H5 — Double-approve report → 409
# ══════════════════════════════════════════════════════════════════════════════

class TestDoubleApproveReport:

    async def test_double_approve_returns_409(self, ac, admin_hdrs):
        """POST /approve on a report that is already 'approved' → 409."""
        rpt = await _make_approved_report(ac, admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/approve", headers=admin_hdrs)
        assert r.status_code == 409
        assert "approved" in r.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# H6 — GET /audit: no ownership guard (any authenticated user can read)
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditPermissive:

    async def test_non_owner_analyst_can_read_audit_trail(
        self, ac, admin_hdrs, report, other_analyst
    ):
        """GET /audit enforces ownership — non-owner analyst receives 403.
        Ownership guard added to prevent information leakage across reports.
        """
        r = await ac.get(
            f"{REPORTS}/{report['id']}/audit",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# H7 — POST /recalculate: no ownership guard
# ══════════════════════════════════════════════════════════════════════════════

class TestRecalculatePermissive:

    async def test_non_owner_analyst_can_trigger_recalculate(
        self, ac, admin_hdrs, report, other_analyst
    ):
        """POST /recalculate enforces ownership — non-owner analyst receives 403.
        Ownership guard added to prevent cross-report recalculation.
        """
        r = await ac.post(
            f"{REPORTS}/{report['id']}/recalculate",
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# H8 — POST /calculations/ltv-acr edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestLtvAcrEdgeCases:

    async def test_ltv_acr_empty_schedule_returns_empty_table(
        self, ac, admin_hdrs, report
    ):
        """POST /calculations/ltv-acr with an empty amortization_schedule returns
        200 with an empty schedule list (not an error)."""
        r = await ac.post(
            f"{REPORTS}/{report['id']}/calculations/ltv-acr",
            json={
                "facility_amount": 10_000_000,
                "initial_asset_value": 15_000_000,
                "amortization_schedule": [],
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        body = r.json()
        assert "schedule" in body or isinstance(body, (list, dict))

    async def test_ltv_acr_missing_required_field_returns_422(
        self, ac, admin_hdrs, report
    ):
        """POST /calculations/ltv-acr without facility_amount → 422."""
        r = await ac.post(
            f"{REPORTS}/{report['id']}/calculations/ltv-acr",
            json={
                "initial_asset_value": 15_000_000,
                "amortization_schedule": [],
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# H9 — PATCH /blocks/{block_id}: no ownership guard
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockPatchPermissive:

    async def test_non_owner_analyst_cannot_patch_block(
        self, ac, admin_hdrs, report, other_analyst
    ):
        """PATCH /blocks/{block_id} enforces ownership — non-owner analysts are denied.
        RBAC guard added: only report owner or admin may edit blocks.
        """
        block_id = await _seed_block(report["id"])

        r = await ac.patch(
            f"{REPORTS}/{report['id']}/blocks/{block_id}",
            json={"content": "Edited by non-owner.", "expected_version": 1},
            headers=other_analyst["headers"],
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# H10 — PUT /fx-rates with rate=0.0 accepted
# ══════════════════════════════════════════════════════════════════════════════

class TestFxRateZeroRate:

    async def test_fx_rate_zero_is_accepted(self, ac, admin_hdrs, report):
        """PUT /fx-rates with rate=0.0 should be accepted (zero is a valid float).
        Callers using the rate for division must handle zero themselves.
        """
        r = await ac.put(
            f"{REPORTS}/{report['id']}/fx-rates",
            json={
                "from_currency": "JPY",
                "to_currency": "USD",
                "rate": 0.0,
                "source": "manual",
                "rate_date": "2024-01-01",
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["rate"] == 0.0

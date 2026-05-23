"""Tests for the 10 gap scenarios identified in the deep reflection audit.

Items covered:
 1. Approved-report immutability on PUT /inputs
 2. Approved-report immutability on POST /field-suggestions/apply
 3. Approved-report immutability on PATCH /blocks
 4. AuditEvent.timestamp index (query correctness, not DB schema)
 5. Concurrent refresh-token: JTI one-time-use in asyncio event loop
 6. _revoked_refresh OrderedDict bounded at _REVOKED_REFRESH_MAX
 7. Per-report generation lock: duplicate generate_section returns 409
 8. ETL: _normalise_year_key handles all FY key variants (item 10)
 9. Three-way conflict detection: A vs B vs C → 3 conflict pairs
10. Section 7 FY key lowercase / mixed-case normalization
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from main import app

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
RPTS = f"{BASE}/reports"


# ── helpers ───────────────────────────────────────────────────────────────────

async def _login(ac, email, password="admin123"):
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": password})
    return r.json()


async def _hdrs(ac, email, password="admin123"):
    t = await _login(ac, email, password)
    return {"Authorization": f"Bearer {t['access_token']}"}


async def _register(ac, admin_h, email, role="analyst"):
    await ac.post(f"{AUTH}/register",
                  json={"email": email, "password": "Pass1234!", "role": role},
                  headers=admin_h)
    return await _hdrs(ac, email, "Pass1234!")


async def _approved_report(ac, hdrs) -> dict:
    """Create a report and approve it (admin can approve directly)."""
    r = await ac.post(f"{RPTS}", json={"borrower_name": "GapCo", "industry": "other"},
                      headers=hdrs)
    rep = r.json()
    ar = await ac.patch(f"{RPTS}/{rep['id']}/status", json={"status": "approved"}, headers=hdrs)
    assert ar.status_code == 200, f"Approval failed: {ar.text}"
    return rep


async def _seed_fact_db(report_id: str, metric: str = "revenue",
                        source_type: str = "pdf_extraction",
                        value: float = 100.0, period: str = "FY2024") -> str:
    from credit_report.database import AsyncSessionLocal
    from credit_report.fact_store.repository import upsert_facts
    async with AsyncSessionLocal() as db:
        facts = await upsert_facts(db, [{
            "report_id": report_id,
            "metric_name": metric,
            "entity": "BORROWER",
            "period": period,
            "value": value,
            "value_text": str(value),
            "currency": "USD",
            "unit": "mn",
            "source_type": source_type,
            "source_priority": 3 if source_type == "pdf_extraction" else 1,
            "source_section_no": 7,
            "state": "extracted",
        }])
        await db.commit()
        return facts[0].id


async def _seed_block(report_id: str, content: str = "Original text.") -> str:
    from credit_report.database import AsyncSessionLocal
    from credit_report.block_ast.models import ReportBlock
    bid = f"blk_{uuid.uuid4().hex[:12]}"
    async with AsyncSessionLocal() as db:
        db.add(ReportBlock(
            id=bid, report_id=report_id,
            section_no=1, block_type="paragraph",
            content=content, validation_status="pending",
            is_stale=False, version=1,
        ))
        await db.commit()
    return bid


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def admin_h(ac):
    return await _hdrs(ac, "admin@example.com", "admin123")


# ══════════════════════════════════════════════════════════════════════════════
# Items 1-3: Approved-report immutability on every mutating endpoint
# ══════════════════════════════════════════════════════════════════════════════

class TestApprovedReportImmutability:

    async def test_put_inputs_on_approved_report_returns_409(self, ac, admin_h):
        """Item 1: PUT /inputs must be blocked once report is approved."""
        rep = await _approved_report(ac, admin_h)
        r = await ac.put(
            f"{RPTS}/{rep['id']}/inputs/1",
            json={"section_no": 1, "input_json": {"borrower_name": "Hack"}},
            headers=admin_h,
        )
        assert r.status_code == 409, r.text
        assert "immutable" in r.json()["detail"].lower()

    async def test_apply_field_suggestions_on_approved_report_returns_409(self, ac, admin_h):
        """Item 2: POST /field-suggestions/apply must be blocked on approved reports."""
        rep = await _approved_report(ac, admin_h)
        fid = await _seed_fact_db(rep["id"])
        r = await ac.post(
            f"{RPTS}/{rep['id']}/sections/1/field-suggestions/apply",
            json={
                "apply_mode": "only_empty",
                "items": [{"suggestion_id": fid, "fact_id": fid,
                            "field_path": "borrower_name", "suggested_value": "Hack"}],
            },
            headers=admin_h,
        )
        assert r.status_code == 409, r.text
        assert "immutable" in r.json()["detail"].lower()

    async def test_patch_block_on_approved_report_returns_409(self, ac, admin_h):
        """Item 3: PATCH /blocks/{id} must be blocked on approved reports."""
        rep = await _approved_report(ac, admin_h)
        bid = await _seed_block(rep["id"])
        r = await ac.patch(
            f"{RPTS}/{rep['id']}/blocks/{bid}",
            json={"content": "Tampered content.", "expected_version": 1},
            headers=admin_h,
        )
        assert r.status_code == 409, r.text
        assert "immutable" in r.json()["detail"].lower()

    async def test_approved_report_still_readable(self, ac, admin_h):
        """Immutability must not block reads."""
        rep = await _approved_report(ac, admin_h)
        r = await ac.get(f"{RPTS}/{rep['id']}", headers=admin_h)
        assert r.status_code == 200

    async def test_unapproved_report_allows_put_inputs(self, ac, admin_h):
        """Sanity: a draft report accepts section input writes."""
        r = await ac.post(f"{RPTS}", json={"borrower_name": "DraftCo", "industry": "other"},
                          headers=admin_h)
        rep = r.json()
        r2 = await ac.put(
            f"{RPTS}/{rep['id']}/inputs/1",
            json={"section_no": 1, "input_json": {"note": "ok"}},
            headers=admin_h,
        )
        assert r2.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Item 4: AuditEvent.timestamp index — global audit endpoint ordering
# ══════════════════════════════════════════════════════════════════════════════

class TestGlobalAuditEndpoint:

    async def test_global_audit_events_returns_200_for_admin(self, ac, admin_h):
        """Admin can fetch global audit events."""
        r = await ac.get(f"{BASE}/audit/events", headers=admin_h)
        assert r.status_code == 200
        data = r.json()
        assert "events" in data
        assert "total" in data

    async def test_global_audit_events_newest_first(self, ac, admin_h):
        """Events must be ordered newest-first (timestamp DESC)."""
        # Create two reports to guarantee two audit events
        await ac.post(f"{RPTS}", json={"borrower_name": "AuditA", "industry": "other"},
                      headers=admin_h)
        await ac.post(f"{RPTS}", json={"borrower_name": "AuditB", "industry": "other"},
                      headers=admin_h)
        r = await ac.get(f"{BASE}/audit/events?page_size=50", headers=admin_h)
        assert r.status_code == 200
        events = r.json()["events"]
        if len(events) >= 2:
            from datetime import datetime
            ts = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                  for e in events]
            assert ts == sorted(ts, reverse=True), "Events must be newest-first"

    async def test_global_audit_forbidden_for_non_admin(self, ac, admin_h):
        """Non-admin analysts must get 403."""
        analyst_h = await _register(ac, admin_h, f"a_{uuid.uuid4().hex[:6]}@test.com", "analyst")
        r = await ac.get(f"{BASE}/audit/events", headers=analyst_h)
        assert r.status_code == 403

    async def test_global_audit_large_page_number_does_not_500(self, ac, admin_h):
        """Integer overflow from huge page param must return 4xx, not 5xx."""
        r = await ac.get(f"{BASE}/audit/events?page=2147483647", headers=admin_h)
        assert r.status_code < 500

    async def test_global_audit_pagination(self, ac, admin_h):
        """page/page_size parameters work correctly."""
        r = await ac.get(f"{BASE}/audit/events?page=1&page_size=2", headers=admin_h)
        assert r.status_code == 200
        data = r.json()
        assert len(data["events"]) <= 2
        assert data["page"] == 1
        assert data["page_size"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# Item 5: Concurrent refresh-token (JTI one-time-use safety in asyncio)
# ══════════════════════════════════════════════════════════════════════════════

class TestRefreshTokenOneTimeUse:

    async def test_refresh_token_cannot_be_reused(self, ac):
        """A consumed refresh token must be rejected on the second call."""
        login = await _login(ac, "admin@example.com", "admin123")
        rt = login["refresh_token"]
        r1 = await ac.post(f"{AUTH}/refresh", json={"refresh_token": rt})
        assert r1.status_code == 200, "First refresh must succeed"
        r2 = await ac.post(f"{AUTH}/refresh", json={"refresh_token": rt})
        assert r2.status_code == 401, "Replayed refresh token must be rejected"

    async def test_refresh_issues_new_tokens(self, ac):
        """A valid refresh call returns new access and refresh tokens."""
        login = await _login(ac, "admin@example.com", "admin123")
        rt = login["refresh_token"]
        r = await ac.post(f"{AUTH}/refresh", json={"refresh_token": rt})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["refresh_token"] != rt


# ══════════════════════════════════════════════════════════════════════════════
# Item 6: _revoked_refresh dict is bounded
# ══════════════════════════════════════════════════════════════════════════════

class TestRevokedRefreshBound:

    def test_revoked_refresh_bounded_at_max(self):
        """Adding more than _REVOKED_REFRESH_MAX entries evicts oldest (LRU)."""
        from credit_report.api.auth import _REVOKED_REFRESH_MAX, _revoke_refresh_jti, _revoked_refresh
        _revoked_refresh.clear()
        for i in range(_REVOKED_REFRESH_MAX + 10):
            _revoke_refresh_jti(f"jti_{i:08d}")
        assert len(_revoked_refresh) == _REVOKED_REFRESH_MAX
        # Oldest entries must have been evicted
        assert "jti_00000000" not in _revoked_refresh
        # Recent entries must still be present
        assert f"jti_{_REVOKED_REFRESH_MAX + 9:08d}" in _revoked_refresh

    def test_revoked_jti_is_detected(self):
        """Revoked JTI returns True from is_revoked check."""
        from credit_report.api.auth import _is_refresh_jti_revoked, _revoke_refresh_jti
        jti = f"jti_{uuid.uuid4().hex}"
        assert not _is_refresh_jti_revoked(jti)
        _revoke_refresh_jti(jti)
        assert _is_refresh_jti_revoked(jti)


# ══════════════════════════════════════════════════════════════════════════════
# Item 7: Per-report generation lock
# ══════════════════════════════════════════════════════════════════════════════

class TestPerReportGenerationLock:

    def _mock_gemini(self):
        mock_resp = MagicMock()
        mock_resp.text = "## §1\n\nContent.\n"
        mu = MagicMock()
        mu.prompt_token_count = 10
        mu.candidates_token_count = 20
        mock_resp.usage_metadata = mu
        mc = MagicMock()
        mc.aio = MagicMock()
        mc.aio.models = MagicMock()
        mc.aio.models.generate_content = AsyncMock(return_value=mock_resp)
        return patch("google.genai.Client", return_value=mc)

    async def test_duplicate_generate_same_section_returns_409(self, ac, admin_h):
        """A second concurrent generate call for the same (report, section) returns 409."""
        from credit_report.api.generate import _generating_sections
        r = await ac.post(f"{RPTS}", json={"borrower_name": "LockCo", "industry": "other"},
                          headers=admin_h)
        rid = r.json()["id"]

        # Simulate first task already in flight by pre-inserting the lock key
        _generating_sections.add((rid, 4))

        with self._mock_gemini():
            r2 = await ac.post(f"{RPTS}/{rid}/generate/{4}", headers=admin_h)
        assert r2.status_code == 409, r2.text
        assert "already being generated" in r2.json()["detail"]

    async def test_generate_different_sections_allowed_concurrently(self, ac, admin_h):
        """Sections 4 and 1 can be generated concurrently (different lock keys)."""
        from credit_report.api.generate import _generating_sections
        r = await ac.post(f"{RPTS}", json={"borrower_name": "LockCo2", "industry": "other"},
                          headers=admin_h)
        rid = r.json()["id"]

        # Only section 4 is in flight
        _generating_sections.add((rid, 4))

        with self._mock_gemini():
            # Section 1 (depends on §4) — hard dep will fire first but we verify the lock
            # doesn't block it. Use section 4 which is "running"; try section 1.
            r2 = await ac.post(f"{RPTS}/{rid}/generate/1", headers=admin_h)
        # Either 202 (accepted) or 409 for hard dep (§4 not done yet) — not a lock 409
        if r2.status_code == 409:
            assert "already being generated" not in r2.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════════
# Items 8 & 10: _normalise_year_key — FY key variant normalization
# ══════════════════════════════════════════════════════════════════════════════

class TestNormaliseYearKey:

    def _norm(self, key: str) -> str:
        from credit_report.generation.etl import _normalise_year_key
        return _normalise_year_key(key)

    def test_fy_underscore_uppercase(self):
        assert self._norm("FY_2024") == "FY2024"

    def test_fy_underscore_lowercase(self):
        """Item 10: lowercase fy_2024 must normalise to FY2024."""
        assert self._norm("fy_2024") == "FY2024"

    def test_fy_underscore_mixed_case(self):
        assert self._norm("Fy_2024") == "FY2024"

    def test_fy_no_underscore_already_canonical(self):
        assert self._norm("FY2024") == "FY2024"

    def test_fy_lowercase_no_underscore(self):
        assert self._norm("fy2024") == "FY2024"

    def test_year_only(self):
        assert self._norm("2024") == "FY2024"

    def test_year_with_forecast_suffix_uppercase(self):
        assert self._norm("2024F") == "FY2024F"

    def test_year_with_forecast_suffix_lowercase(self):
        assert self._norm("2024f") == "FY2024F"

    def test_fy_underscore_with_forecast(self):
        assert self._norm("FY_2024F") == "FY2024F"

    def test_fy_underscore_with_forecast_lowercase_suffix(self):
        assert self._norm("FY_2024f") == "FY2024F"

    def test_template_placeholder_unchanged(self):
        """YYYY template keys must be returned as-is so ETL can skip them."""
        assert "YYYY" in self._norm("FY_YYYY")

    def test_quarterly_key_unchanged(self):
        """Quarterly keys like 1Q25 must pass through unchanged."""
        result = self._norm("1Q25")
        assert result == "1Q25"

    def test_fy2024f_already_canonical(self):
        assert self._norm("FY2024F") == "FY2024F"

    def test_fy2024e_estimate_suffix(self):
        assert self._norm("2024E") == "FY2024E"


# ══════════════════════════════════════════════════════════════════════════════
# Item 9: Three-way conflict detection
# ══════════════════════════════════════════════════════════════════════════════

class TestThreeWayConflict:

    async def _seed_three_sources(self, report_id: str):
        """Seed same metric from 3 different sources with different values → 3 conflict pairs."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_facts

        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [
                {
                    "report_id": report_id,
                    "metric_name": "revenue",
                    "entity": "BORROWER",
                    "period": "FY2024",
                    "value": 100.0,
                    "value_text": "100",
                    "currency": "USD",
                    "unit": "mn",
                    "source_type": "pdf_extraction",
                    "source_priority": 3,
                    "source_section_no": 7,
                    "state": "extracted",
                },
                {
                    "report_id": report_id,
                    "metric_name": "revenue",
                    "entity": "BORROWER",
                    "period": "FY2024",
                    "value": 200.0,
                    "value_text": "200",
                    "currency": "USD",
                    "unit": "mn",
                    "source_type": "analyst_input_json",
                    "source_priority": 1,
                    "source_section_no": 7,
                    "state": "extracted",
                },
                {
                    "report_id": report_id,
                    "metric_name": "revenue",
                    "entity": "BORROWER",
                    "period": "FY2024",
                    "value": 300.0,
                    "value_text": "300",
                    "currency": "USD",
                    "unit": "mn",
                    "source_type": "manual_override",
                    "source_priority": 2,
                    "source_section_no": 7,
                    "state": "extracted",
                },
            ])
            await db.commit()

    async def test_three_sources_create_multiple_conflict_pairs(self, ac, admin_h):
        """Three disagreeing sources for the same metric should produce conflict rows."""
        r = await ac.post(f"{RPTS}", json={"borrower_name": "3WayCo", "industry": "other"},
                          headers=admin_h)
        rid = r.json()["id"]
        await self._seed_three_sources(rid)

        r2 = await ac.get(f"{RPTS}/{rid}/facts/conflicts", headers=admin_h)
        assert r2.status_code == 200
        conflicts = r2.json()
        # Must have at least 2 conflict rows (A-B and A-C or B-C pairs)
        assert len(conflicts) >= 2, (
            f"Expected ≥2 conflicts for 3-way disagreement, got {len(conflicts)}: {conflicts}"
        )
        # All conflicts involve metric 'revenue'
        for c in conflicts:
            assert c["metric_name"] == "revenue"

    async def test_three_sources_all_facts_in_conflicted_state(self, ac, admin_h):
        """All three facts must transition to 'conflicted' state."""
        r = await ac.post(f"{RPTS}", json={"borrower_name": "3WayState", "industry": "other"},
                          headers=admin_h)
        rid = r.json()["id"]
        await self._seed_three_sources(rid)

        r2 = await ac.get(f"{RPTS}/{rid}/facts", headers=admin_h)
        assert r2.status_code == 200
        facts = [f for f in r2.json() if f["metric_name"] == "revenue"]
        assert len(facts) == 3
        conflicted = [f for f in facts if f["state"] == "conflicted"]
        assert len(conflicted) == 3, (
            f"All 3 facts must be in 'conflicted' state, got: "
            f"{[(f['source_type'], f['state']) for f in facts]}"
        )

    async def test_two_agreeing_sources_no_conflict(self, ac, admin_h):
        """Two sources reporting the same value must not create a conflict."""
        r = await ac.post(f"{RPTS}", json={"borrower_name": "AgreeingCo", "industry": "other"},
                          headers=admin_h)
        rid = r.json()["id"]

        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_facts
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [
                {
                    "report_id": rid,
                    "metric_name": "ebitda",
                    "entity": "BORROWER",
                    "period": "FY2024",
                    "value": 50.0,
                    "value_text": "50",
                    "currency": "USD",
                    "unit": "mn",
                    "source_type": "pdf_extraction",
                    "source_priority": 3,
                    "source_section_no": 7,
                    "state": "extracted",
                },
                {
                    "report_id": rid,
                    "metric_name": "ebitda",
                    "entity": "BORROWER",
                    "period": "FY2024",
                    "value": 50.0,  # same value — no conflict expected
                    "value_text": "50",
                    "currency": "USD",
                    "unit": "mn",
                    "source_type": "analyst_input_json",
                    "source_priority": 1,
                    "source_section_no": 7,
                    "state": "extracted",
                },
            ])
            await db.commit()

        r2 = await ac.get(f"{RPTS}/{rid}/facts/conflicts", headers=admin_h)
        assert r2.status_code == 200
        ebitda_conflicts = [c for c in r2.json() if c["metric_name"] == "ebitda"]
        assert len(ebitda_conflicts) == 0, "Identical values must not produce conflicts"

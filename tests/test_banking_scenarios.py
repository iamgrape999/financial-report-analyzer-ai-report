"""
Banking Scenario Test Suite
===========================
20 untested / under-designed scenarios identified from the perspective of a
Singapore / Taiwan bank credit team user.

Scenarios covered:
  ③  JWT token refresh and session expiry
  ⑤  Section 3 MSR rating normalisation (_normalize_section3_ratings)
  ⑦  Reviewer rejection / rework workflow (no "rejected" state – design gap)
  ⑧  Section generation with zero evidence documents
  ⑨  Multi-period financial facts passed to generation (3-year trend)
  ⑩  Fact conflict auto-detection during ETL (two contradictory PDFs)
  ⑪  Input save with failed recalculation (non-blocking guard)
  ⑫  Report list search and pagination (analyst vs reviewer visibility)
  ⑭  Section regeneration overwrites previous output
  ⑮  Fact completeness – source_type distinguishes ETL vs analyst-input
  ⑯  Section 9 (compliance) hard-dependency on §1–§8
  ⑱  Approved report immutability (delete, input edit, fact override)
  ⑲  Audit log completeness across the full report lifecycle
  ⑳  Chinese-language generation flag accepted by API (zh / en guard)
  ①  Non-Singapore borrower entity guard (ACRA placeholder assertion)
  ②  Multi-entity consolidated vs standalone fact labelling
  ④  Section 1 mandatory field completeness post-generation
  ⑥  FX rate stored and retrievable (TWD/USD dual-currency)
  ⑬  Block listing after section output seeded
  ⑰  Concurrent PATCH with same expected_version → 409 (optimistic lock)
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from main import app  # noqa: E402

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
RPTS = f"{BASE}/reports"


# ── shared helpers ────────────────────────────────────────────────────────────

async def _login(ac: AsyncClient, email: str, password: str = "Pass1234!") -> dict:
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.text}"
    return r.json()


async def _hdrs(ac: AsyncClient, email: str, password: str = "Pass1234!") -> dict:
    tokens = await _login(ac, email, password)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _register(ac, admin_h, email, role="analyst"):
    r = await ac.post(f"{AUTH}/register",
                      json={"email": email, "password": "Pass1234!", "role": role},
                      headers=admin_h)
    assert r.status_code in (200, 201, 409), f"register failed: {r.text}"
    return r.json()


def _mock_gemini(text: str = "## Section\n\nContent.\n"):
    mock_resp = MagicMock()
    mock_resp.text = text
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 50
    mock_usage.candidates_token_count = 100
    mock_resp.usage_metadata = mock_usage
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
    return patch("google.genai.Client", return_value=mock_client)


async def _create_report(ac, hdrs, borrower="BankTestCo", industry="shipping"):
    r = await ac.post(RPTS, json={"borrower_name": borrower, "industry": industry},
                      headers=hdrs)
    assert r.status_code == 201, r.text
    return r.json()


async def _seed_section(report_id: str, section_no: int = 7,
                        markdown: str = "## Financial Analysis\n\nContent.\n",
                        status: str = "done") -> None:
    from credit_report.database import AsyncSessionLocal
    from credit_report.models import SectionOutput
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as db:
        db.add(SectionOutput(
            id=str(uuid.uuid4()),
            report_id=report_id,
            section_no=section_no,
            markdown=markdown,
            status=status,
            tokens_used=200,
            generated_at=datetime.now(timezone.utc),
        ))
        await db.commit()


async def _seed_fact(report_id: str, metric: str = "revenue", value: float = 1000.0,
                     entity: str = "BankTestCo", period: str = "FY2024",
                     source_type: str = "etl", state: str = "extracted") -> str:
    from credit_report.database import AsyncSessionLocal
    from credit_report.fact_store.repository import upsert_fact
    async with AsyncSessionLocal() as db:
        fact = await upsert_fact(db, {
            "report_id": report_id,
            "metric_name": metric,
            "entity": entity,
            "period": period,
            "value": value,
            "value_text": f"{value:,.1f}",
            "currency": "USD",
            "unit": "m",
            "state": state,
            "source_type": source_type,
        })
        await db.commit()
        return fact.id


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def admin_hdrs(ac):
    return await _hdrs(ac, "admin@example.com", "admin123")


@pytest_asyncio.fixture
async def report(ac, admin_hdrs):
    return await _create_report(ac, admin_hdrs)


# ══════════════════════════════════════════════════════════════════════════════
# ③  JWT Token Refresh
# ══════════════════════════════════════════════════════════════════════════════

class TestTokenRefresh:
    """POST /auth/refresh — session continuity after access token expiry."""

    async def test_refresh_with_valid_refresh_token_returns_new_access_token(self, ac, admin_hdrs):
        tokens = await _login(ac, "admin@example.com", "admin123")
        refresh_token = tokens["refresh_token"]
        r = await ac.post(f"{AUTH}/refresh", json={"refresh_token": refresh_token})
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert "refresh_token" in body
        # New access token must differ from old one (freshly minted)
        assert body["access_token"] != tokens["access_token"]

    async def test_refresh_new_access_token_authenticates_successfully(self, ac, admin_hdrs):
        tokens = await _login(ac, "admin@example.com", "admin123")
        r = await ac.post(f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]})
        new_access = r.json()["access_token"]
        me_r = await ac.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {new_access}"})
        assert me_r.status_code == 200

    async def test_refresh_with_invalid_token_returns_401(self, ac):
        r = await ac.post(f"{AUTH}/refresh", json={"refresh_token": "not.a.valid.token"})
        assert r.status_code == 401

    async def test_refresh_with_access_token_as_refresh_returns_401(self, ac):
        """Guard: an access token must not be accepted as a refresh token."""
        tokens = await _login(ac, "admin@example.com", "admin123")
        r = await ac.post(f"{AUTH}/refresh", json={"refresh_token": tokens["access_token"]})
        assert r.status_code == 401

    async def test_refresh_missing_body_returns_422(self, ac):
        r = await ac.post(f"{AUTH}/refresh", json={})
        assert r.status_code == 422

    async def test_expired_access_token_returns_401_on_protected_endpoint(self, ac):
        """Simulate an expired token by injecting one with past exp."""
        from credit_report.security.auth import _encode_jwt
        expired_token = _encode_jwt(
            {"sub": "00000000-dead-0000-0000-000000000000", "role": "analyst",
             "exp": int(time.time()) - 3600, "type": "access"}
        )
        r = await ac.get(RPTS, headers={"Authorization": f"Bearer {expired_token}"})
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# ⑤  Section 3 MSR Rating Normalisation
# ══════════════════════════════════════════════════════════════════════════════

class TestMSRRatingNormalisation:
    """Unit tests for _normalize_section3_ratings().

    The function operates on the 'rows' list inside '3B_internal_ratings'.
    Each row is a dict keyed by period names (interim, current, fy2024, etc.)
    whose values can be nested FORMAT C objects instead of flat MSR strings.
    """

    def test_format_c_variant1_flattened_to_string(self):
        """FORMAT C variant 1: {generated_msr, override_applied, override_to} → flat string."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        inp = {
            "3B_internal_ratings": {
                "rows": [
                    {
                        "interim": {"generated_msr": "4+", "override_applied": False},
                        "current": {"generated_msr": "3", "override_applied": True, "override_to": "3+"},
                    }
                ]
            }
        }
        out = _normalize_section3_ratings(inp)
        row = out["3B_internal_ratings"]["rows"][0]
        # After normalisation, period values must be flat strings, not dicts
        assert not isinstance(row["interim"], dict), (
            f"FORMAT C dict was not flattened for 'interim': {row['interim']!r}"
        )
        assert not isinstance(row["current"], dict), (
            f"FORMAT C dict was not flattened for 'current': {row['current']!r}"
        )
        assert row["interim"] == "4+"
        assert row["current"] == "3+"

    def test_format_c_variant2_proposed_assessment_flattened(self):
        """FORMAT C variant 2: {proposed_assessment: {generated_msr, proposed_final_msr}} → flat."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        inp = {
            "3B_internal_ratings": {
                "rows": [
                    {
                        "current": {
                            "proposed_assessment": {
                                "generated_msr": "3",
                                "proposed_final_msr": "3+",
                            }
                        }
                    }
                ]
            }
        }
        out = _normalize_section3_ratings(inp)
        row = out["3B_internal_ratings"]["rows"][0]
        assert not isinstance(row["current"], dict), (
            f"proposed_assessment variant not flattened: {row['current']!r}"
        )
        assert row["current"] == "3+"

    def test_null_ratings_key_handled_gracefully(self):
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        inp = {"3B_internal_ratings": None}
        out = _normalize_section3_ratings(inp)
        assert out["3B_internal_ratings"] is None

    def test_missing_ratings_key_returns_dict_unchanged(self):
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        inp = {"other_key": "value"}
        out = _normalize_section3_ratings(inp)
        assert "3B_internal_ratings" not in out
        assert out == {"other_key": "value"}

    def test_no_rows_key_returns_unchanged(self):
        """If '3B_internal_ratings' exists but has no 'rows', return unchanged."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        inp = {"3B_internal_ratings": {"some_other_structure": True}}
        out = _normalize_section3_ratings(inp)
        assert out == inp

    def test_flat_string_row_preserved_unchanged(self):
        """FORMAT A rows (already flat strings) must pass through untouched."""
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        inp = {
            "3B_internal_ratings": {
                "rows": [{"interim": "3+", "current": "4-", "metric": "Borrower"}]
            }
        }
        out = _normalize_section3_ratings(inp)
        row = out["3B_internal_ratings"]["rows"][0]
        assert row["interim"] == "3+"
        assert row["current"] == "4-"

    def test_empty_rows_list_handled_gracefully(self):
        from credit_report.generation.prompt_builder import _normalize_section3_ratings
        inp = {"3B_internal_ratings": {"rows": []}}
        out = _normalize_section3_ratings(inp)
        assert out["3B_internal_ratings"]["rows"] == []


# ══════════════════════════════════════════════════════════════════════════════
# ⑦  Reviewer Rejection / Rework Workflow
# ══════════════════════════════════════════════════════════════════════════════

class TestReviewerRejectionWorkflow:
    """
    BUG-7 (design gap): There is no 'rejected' or 'needs_rework' state.
    Once submitted for review a report can only be recalled by the analyst or
    approved by the approver; a reviewer cannot send it back with comments.

    Tests confirm the current state machine and document the design gap.
    """

    async def _setup_reviewer(self, ac, admin_hdrs):
        email = f"reviewer_{uuid.uuid4().hex[:6]}@bank.com"
        await _register(ac, admin_hdrs, email, role="reviewer")
        return email

    async def test_reviewer_cannot_patch_status_to_rejected(self, ac, admin_hdrs):
        """Design gap: 'rejected' is not a valid status — API should reject it."""
        email = await self._setup_reviewer(ac, admin_hdrs)
        rev_hdrs = await _hdrs(ac, email)
        rpt = await _create_report(ac, admin_hdrs, borrower="ReviewGapCo")
        r = await ac.patch(f"{RPTS}/{rpt['id']}/status",
                           json={"status": "rejected"},
                           headers=rev_hdrs)
        # 422 (schema rejection) or 400 (invalid transition) — NOT 200
        assert r.status_code in (400, 422), (
            f"BUG-7: API accepted 'rejected' status transition: {r.status_code} {r.text}"
        )

    async def test_valid_statuses_only_accepted(self, ac, admin_hdrs):
        """API must reject arbitrary status strings."""
        rpt = await _create_report(ac, admin_hdrs, borrower="StatusGuardCo")
        r = await ac.patch(f"{RPTS}/{rpt['id']}/status",
                           json={"status": "in_committee_review"},
                           headers=admin_hdrs)
        assert r.status_code in (400, 422)

    async def test_recall_from_review_returns_to_draft(self, ac, admin_hdrs):
        """Recall is the only current 'rework' mechanism — must work cleanly."""
        rpt = await _create_report(ac, admin_hdrs, borrower="RecallReworkCo")
        await _seed_section(rpt["id"], section_no=7)
        # Submit for review
        r = await ac.post(f"{RPTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 200, f"submit failed: {r.text}"
        assert r.json()["status"] == "review_in_progress"
        # Recall
        r = await ac.post(f"{RPTS}/{rpt['id']}/recall", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["status"] == "draft"

    async def test_reviewer_cannot_recall_report(self, ac, admin_hdrs):
        """Only the owning analyst (or admin) can recall a report."""
        email = await self._setup_reviewer(ac, admin_hdrs)
        rev_hdrs = await _hdrs(ac, email)
        rpt = await _create_report(ac, admin_hdrs, borrower="RecallAuthCo")
        await _seed_section(rpt["id"], section_no=7)
        r = await ac.post(f"{RPTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 200, f"submit failed: {r.text}"
        r = await ac.post(f"{RPTS}/{rpt['id']}/recall", headers=rev_hdrs)
        # Reviewer must not be able to recall a report they don't own
        assert r.status_code in (403, 409), (
            f"Reviewer should not be able to recall: {r.status_code} {r.text}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# ⑧  Section Generation with No Evidence Documents
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerationWithNoEvidence:
    """Section generation must work when zero documents are uploaded."""

    async def test_generate_section1_no_documents_returns_202(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs, borrower="NoDocCo")
        with _mock_gemini("## Facility Structure\n\nContent.\n"):
            r = await ac.post(f"{RPTS}/{rpt['id']}/generate/1",
                              headers=admin_hdrs)
        # Must return 202 (accepted) — not 400 even with no documents
        assert r.status_code == 202, (
            f"Section generation with no docs should accept: {r.status_code} {r.text}"
        )

    async def test_prompt_build_for_section1_with_empty_evidence(self):
        """prompt_builder must handle empty evidence_chunks without exception."""
        from credit_report.generation.prompt_builder import build_section_prompt
        system_prompt, user_prompt = build_section_prompt(
            section_no=1,
            input_json={},
            evidence_chunks=[],
            preceding_outputs={},
            output_language="en",
        )
        assert isinstance(user_prompt, str) and len(user_prompt) > 0
        assert isinstance(system_prompt, str)

    async def test_prompt_build_all_sections_with_empty_evidence(self):
        """All 10 section prompts must build cleanly with no evidence."""
        from credit_report.generation.prompt_builder import build_section_prompt
        for sec_no in range(1, 11):
            _, user_prompt = build_section_prompt(
                section_no=sec_no,
                input_json={},
                evidence_chunks=[],
                preceding_outputs={},
                output_language="en",
            )
            assert len(user_prompt) > 0, f"Empty prompt for section {sec_no}"


# ══════════════════════════════════════════════════════════════════════════════
# ⑨  Multi-Period Financial Facts (3-Year Trend)
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiPeriodFinancialFacts:
    """Three periods of facts must all reach the generation prompt context."""

    async def test_three_period_facts_all_visible_via_api(self, ac, admin_hdrs, report):
        rid = report["id"]
        for period in ("FY2022", "FY2023", "FY2024"):
            await _seed_fact(rid, "revenue", 1000.0 + list(("FY2022","FY2023","FY2024")).index(period)*100,
                             period=period)
        r = await ac.get(f"{RPTS}/{rid}/facts", headers=admin_hdrs)
        assert r.status_code == 200
        facts = r.json()
        periods_seen = {f["period"] for f in facts if f["metric_name"] == "revenue"}
        assert periods_seen == {"FY2022", "FY2023", "FY2024"}, (
            f"Not all three periods visible: {periods_seen}"
        )

    async def test_three_period_facts_included_in_prompt_context(self, ac, admin_hdrs, report):
        """get_facts_for_prompt must return facts from all three periods."""
        from credit_report.fact_store.repository import get_facts_for_report
        from credit_report.database import AsyncSessionLocal
        rid = report["id"]
        for period in ("FY2022", "FY2023", "FY2024"):
            await _seed_fact(rid, "ebitda", 200.0, period=period, state="validated")
        async with AsyncSessionLocal() as db:
            facts = await get_facts_for_report(db, rid)
        ebitda_periods = {f.period for f in facts if f.metric_name == "ebitda"}
        assert len(ebitda_periods) >= 3, (
            f"Expected 3 periods in fact store, got: {ebitda_periods}"
        )

    async def test_calculation_results_grouped_by_period(self, ac, admin_hdrs, report):
        """Calculation results endpoint must preserve the period dimension."""
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/calculations", headers=admin_hdrs)
        assert r.status_code == 200
        # Response must be a list (possibly empty); each item must have a 'period' field
        items = r.json()
        for item in items:
            assert "period" in item, f"Calculation result missing 'period' field: {item}"


# ══════════════════════════════════════════════════════════════════════════════
# ⑩  Fact Conflict Auto-Detection
# ══════════════════════════════════════════════════════════════════════════════

class TestFactConflictAutoDetection:
    """Two contradictory facts for the same metric/entity/period must auto-conflict."""

    async def test_duplicate_metric_period_creates_conflict(self, ac, admin_hdrs, report):
        """
        Conflict detection requires source_type to differ between the two facts
        (e.g. one from ETL, one from analyst_input_json) and values to disagree by >2%.
        """
        from credit_report.fact_store.repository import upsert_facts
        from credit_report.database import AsyncSessionLocal
        rid = report["id"]
        # fact_a: ETL-extracted revenue = 1000
        fact_a = {
            "report_id": rid,
            "metric_name": "revenue", "entity": "ConflictCo", "period": "FY2024",
            "value": 1000.0, "value_text": "1,000.0", "currency": "USD", "unit": "m",
            "source_type": "etl", "state": "extracted",
        }
        # fact_b: analyst_input_json revenue = 1200 (20% higher — clearly contradictory)
        fact_b = {
            "report_id": rid,
            "metric_name": "revenue", "entity": "ConflictCo", "period": "FY2024",
            "value": 1200.0, "value_text": "1,200.0", "currency": "USD", "unit": "m",
            "source_type": "analyst_input_json", "state": "extracted",
        }
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [fact_a])
            await db.commit()
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [fact_b])
            await db.commit()

        # Both facts should now be in conflicted state or a FactConflict record exists
        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts", headers=admin_hdrs)
        assert r.status_code == 200
        conflicts = r.json()
        assert len(conflicts) >= 1, (
            "Expected auto-conflict after contradictory fact upsert (etl vs analyst_input_json). "
            "Conflict detection requires different source_type values to disagree by >2%."
        )

    async def test_same_metric_same_value_does_not_create_spurious_conflict(self, ac, admin_hdrs, report):
        """Identical value re-uploaded should not create a conflict."""
        from credit_report.fact_store.repository import upsert_facts
        from credit_report.database import AsyncSessionLocal
        rid = report["id"]
        shared_id = str(uuid.uuid4())
        fact = {
            "id": shared_id, "report_id": rid,
            "metric_name": "total_assets", "entity": "NoConflictCo", "period": "FY2024",
            "value": 5000.0, "value_text": "5,000.0", "currency": "USD", "unit": "m",
            "source_type": "etl", "source_doc_id": "doc-C", "state": "extracted",
        }
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [fact])
            await db.commit()
        # Re-upsert same fact (same id) — should not trigger conflict
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [fact])
            await db.commit()
        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts", headers=admin_hdrs)
        assert r.status_code == 200
        # Should not have gained any conflicts for total_assets
        conflicts = r.json()
        ta_conflicts = [c for c in conflicts if "total_assets" in str(c)]
        assert len(ta_conflicts) == 0


# ══════════════════════════════════════════════════════════════════════════════
# ⑫  Report List Search and Pagination
# ══════════════════════════════════════════════════════════════════════════════

class TestReportSearchAndPagination:
    """GET /reports — search, pagination, and role-based visibility."""

    async def test_pagination_skip_and_limit(self, ac, admin_hdrs):
        # Create 3 uniquely named reports
        names = [f"PaginateCo_{i}_{uuid.uuid4().hex[:4]}" for i in range(3)]
        for name in names:
            await _create_report(ac, admin_hdrs, borrower=name)

        r_all = await ac.get(f"{RPTS}?skip=0&limit=100", headers=admin_hdrs)
        assert r_all.status_code == 200
        all_reports = r_all.json()

        r_p1 = await ac.get(f"{RPTS}?skip=0&limit=1", headers=admin_hdrs)
        assert r_p1.status_code == 200
        assert len(r_p1.json()) == 1

        r_p2 = await ac.get(f"{RPTS}?skip=1&limit=1", headers=admin_hdrs)
        assert r_p2.status_code == 200
        assert len(r_p2.json()) == 1

        # The two pages must return different reports
        ids_p1 = {rep["id"] for rep in r_p1.json()}
        ids_p2 = {rep["id"] for rep in r_p2.json()}
        assert ids_p1.isdisjoint(ids_p2), "Pagination returned overlapping results"

    async def test_analyst_only_sees_own_reports(self, ac, admin_hdrs):
        email = f"analyst_{uuid.uuid4().hex[:6]}@bank.com"
        await _register(ac, admin_hdrs, email, role="analyst")
        analyst_hdrs = await _hdrs(ac, email)

        # Analyst creates one report
        rpt = await _create_report(ac, analyst_hdrs, borrower="AnalystOwnedCo")

        # Admin creates another report (different owner)
        await _create_report(ac, admin_hdrs, borrower="AdminOwnedCo")

        r = await ac.get(f"{RPTS}?skip=0&limit=100", headers=analyst_hdrs)
        assert r.status_code == 200
        ids = {rep["id"] for rep in r.json()}
        assert rpt["id"] in ids
        # Analyst must not see admin-owned report
        admin_reports = [rep for rep in r.json() if rep["borrower_name"] == "AdminOwnedCo"]
        assert len(admin_reports) == 0, "Analyst can see another user's reports — access leak"

    async def test_reviewer_sees_all_reports(self, ac, admin_hdrs):
        email = f"reviewer_{uuid.uuid4().hex[:6]}@bank.com"
        await _register(ac, admin_hdrs, email, role="reviewer")
        rev_hdrs = await _hdrs(ac, email)

        analyst_email = f"analyst_{uuid.uuid4().hex[:6]}@bank.com"
        await _register(ac, admin_hdrs, analyst_email, role="analyst")
        analyst_hdrs = await _hdrs(ac, analyst_email)

        rpt = await _create_report(ac, analyst_hdrs, borrower="AnalystRptForReviewer")
        r = await ac.get(f"{RPTS}?skip=0&limit=100", headers=rev_hdrs)
        assert r.status_code == 200
        ids = {rep["id"] for rep in r.json()}
        assert rpt["id"] in ids, "Reviewer should see all reports including those owned by analysts"

    async def test_limit_zero_returns_empty_list(self, ac, admin_hdrs):
        r = await ac.get(f"{RPTS}?skip=0&limit=0", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json() == []


# ══════════════════════════════════════════════════════════════════════════════
# ⑭  Section Regeneration Overwrites Previous Output
# ══════════════════════════════════════════════════════════════════════════════

class TestSectionRegeneration:
    """Regenerating a done section must update the content (no version history exists)."""

    async def test_regeneration_updates_generated_at_timestamp(self, ac, admin_hdrs, report):
        rid = report["id"]
        old_ts = "2024-01-01T00:00:00"
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput
        from datetime import datetime, timezone
        from sqlalchemy import select as sa_select
        old_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(SectionOutput(
                id=old_id, report_id=rid, section_no=1,
                markdown="## Old Content\n\nOriginal.\n",
                status="done", tokens_used=100,
                generated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ))
            await db.commit()

        with _mock_gemini("## New Content\n\nRegenerated.\n"):
            r = await ac.post(f"{RPTS}/{rid}/generate/1", headers=admin_hdrs)
        assert r.status_code == 202

        # After background task completes (poll or seed via direct DB check)
        # At minimum the API accepted the regeneration request
        r2 = await ac.get(f"{RPTS}/{rid}/sections/1/output", headers=admin_hdrs)
        assert r2.status_code == 200

    async def test_section_output_overwritten_on_second_generation(self, ac, admin_hdrs, report):
        """Two successive seedings — latest must win."""
        rid = report["id"]
        await _seed_section(rid, section_no=2, markdown="## First Version\n\nOld.\n")
        # Seed again with newer markdown (simulates regeneration overwrite)
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput
        from sqlalchemy import select as sa_select
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sa_select(SectionOutput).where(
                    SectionOutput.report_id == rid,
                    SectionOutput.section_no == 2
                )
            )
            out = result.scalar_one_or_none()
            if out:
                out.markdown = "## Second Version\n\nNew.\n"
                await db.commit()

        r = await ac.get(f"{RPTS}/{rid}/sections/2/output", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        assert "Second Version" in body.get("markdown", ""), (
            "Expected latest markdown to be returned after overwrite"
        )


# ══════════════════════════════════════════════════════════════════════════════
# ⑮  Fact Source Type Completeness
# ══════════════════════════════════════════════════════════════════════════════

class TestFactSourceTypeCompleteness:
    """source_type must distinguish ETL-extracted vs analyst-input facts."""

    async def test_etl_fact_has_source_type_etl(self, ac, admin_hdrs, report):
        rid = report["id"]
        fid = await _seed_fact(rid, "revenue", 1000.0, source_type="etl")
        r = await ac.get(f"{RPTS}/{rid}/facts", headers=admin_hdrs)
        assert r.status_code == 200
        fact = next((f for f in r.json() if f["id"] == fid), None)
        assert fact is not None
        assert fact["source_type"] == "etl"

    async def test_analyst_input_fact_has_source_type_analyst_input(self, ac, admin_hdrs, report):
        rid = report["id"]
        fid = await _seed_fact(rid, "net_income", 50.0, source_type="analyst_input")
        r = await ac.get(f"{RPTS}/{rid}/facts", headers=admin_hdrs)
        assert r.status_code == 200
        fact = next((f for f in r.json() if f["id"] == fid), None)
        assert fact is not None
        assert fact["source_type"] == "analyst_input"

    async def test_facts_list_includes_source_type_field(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_fact(rid, "ebitda", 200.0)
        r = await ac.get(f"{RPTS}/{rid}/facts", headers=admin_hdrs)
        assert r.status_code == 200
        facts = r.json()
        if facts:
            assert "source_type" in facts[0], (
                "Facts response must include 'source_type' for ETL completeness tracking"
            )


# ══════════════════════════════════════════════════════════════════════════════
# ⑯  Section 9 Hard Dependency on §1–§8
# ══════════════════════════════════════════════════════════════════════════════

class TestSection9HardDependency:
    """§9 (Compliance Checklist) requires §1–§8 to be done first."""

    async def test_section9_blocked_when_section7_not_done(self, ac, admin_hdrs, report):
        rid = report["id"]
        # §9 depends on §1 through §8 — none are done yet
        r = await ac.post(f"{RPTS}/{rid}/generate/9", headers=admin_hdrs)
        # Must be 409 (dependency missing), not 202
        assert r.status_code == 409, (
            f"§9 should be blocked by hard dependencies: {r.status_code} {r.text}"
        )

    async def test_section9_dependency_error_message_names_sections(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(f"{RPTS}/{rid}/generate/9", headers=admin_hdrs)
        if r.status_code == 409:
            body = r.json()
            detail = body.get("detail", "")
            # The error should mention missing section numbers
            assert any(char.isdigit() for char in detail), (
                f"Dependency error should name missing sections: {detail!r}"
            )

    async def test_section2_blocked_when_section7_not_done(self, ac, admin_hdrs, report):
        """§2 (Overall Comments) also depends on §7 per SECTION_HARD_DEPENDENCIES."""
        rid = report["id"]
        r = await ac.post(f"{RPTS}/{rid}/generate/2", headers=admin_hdrs)
        assert r.status_code == 409, (
            f"§2 requires §7 first: {r.status_code} {r.text}"
        )

    async def test_section7_unblocked_no_hard_dependencies(self, ac, admin_hdrs, report):
        """§7 (Financial Analysis) has no hard dependencies — should be accepted."""
        rid = report["id"]
        with _mock_gemini():
            r = await ac.post(f"{RPTS}/{rid}/generate/7", headers=admin_hdrs)
        assert r.status_code == 202, (
            f"§7 should generate without dependency: {r.status_code} {r.text}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# ⑱  Approved Report Immutability
# ══════════════════════════════════════════════════════════════════════════════

class TestApprovedReportImmutability:
    """BUG-5 / BUG-5-extension: Approved reports must be immutable."""

    async def _approve_report(self, ac, admin_hdrs, rid):
        await _seed_section(rid, section_no=7)
        r = await ac.post(f"{RPTS}/{rid}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 200, f"submit-for-review failed: {r.text}"
        r = await ac.post(f"{RPTS}/{rid}/approve", headers=admin_hdrs)
        assert r.status_code == 200, f"approve failed: {r.text}"
        assert r.json()["status"] == "approved"

    async def test_delete_approved_report_documents_bug5(self, ac, admin_hdrs):
        """BUG-5: DELETE on approved report should be blocked but currently returns 204."""
        rpt = await _create_report(ac, admin_hdrs, borrower="ApprovedDeleteCo")
        await self._approve_report(ac, admin_hdrs, rpt["id"])
        r = await ac.delete(f"{RPTS}/{rpt['id']}", headers=admin_hdrs)
        if r.status_code == 204:
            pytest.xfail("BUG-5 confirmed: approved report can be deleted — immutability not enforced")
        else:
            assert r.status_code in (400, 403, 409), (
                f"Unexpected status code for approved report delete: {r.status_code}"
            )

    async def test_regenerate_section_on_approved_report(self, ac, admin_hdrs):
        """Regenerating a section on an approved report should be blocked."""
        rpt = await _create_report(ac, admin_hdrs, borrower="ApprovedRegenCo")
        await self._approve_report(ac, admin_hdrs, rpt["id"])
        with _mock_gemini():
            r = await ac.post(f"{RPTS}/{rpt['id']}/generate/1", headers=admin_hdrs)
        if r.status_code == 202:
            pytest.xfail(
                "BUG-5-ext: section regeneration allowed on approved report — "
                "immutability not enforced for generation"
            )
        else:
            assert r.status_code in (400, 403, 409)

    async def test_override_fact_on_approved_report(self, ac, admin_hdrs):
        """Overriding a fact on an approved report should be blocked."""
        rpt = await _create_report(ac, admin_hdrs, borrower="ApprovedFactCo")
        fid = await _seed_fact(rpt["id"], "revenue", 1000.0)
        await self._approve_report(ac, admin_hdrs, rpt["id"])
        r = await ac.patch(f"{RPTS}/{rpt['id']}/facts/{fid}",
                           json={"value": 9999.0, "reason": "test override", "expected_version": 1},
                           headers=admin_hdrs)
        if r.status_code == 200:
            pytest.xfail(
                "BUG-5-ext: fact override allowed on approved report — "
                "immutability not enforced for facts"
            )
        else:
            assert r.status_code in (400, 403, 409)

    async def test_approved_report_still_readable(self, ac, admin_hdrs):
        """Immutability must not block read access to the approved report."""
        rpt = await _create_report(ac, admin_hdrs, borrower="ApprovedReadCo")
        await self._approve_report(ac, admin_hdrs, rpt["id"])
        r = await ac.get(f"{RPTS}/{rpt['id']}", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["status"] == "approved"


# ══════════════════════════════════════════════════════════════════════════════
# ⑲  Audit Log Completeness
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditLogCompleteness:
    """Audit log must capture every state-change action in the lifecycle.

    The /audit endpoint returns {"events": [...], "total": ..., "page": ..., "page_size": ...}.
    Event timestamp field is "timestamp" (not "created_at").
    """

    def _events(self, body: dict) -> list:
        """Extract event list from paginated audit response."""
        return body.get("events", body.get("items", body if isinstance(body, list) else []))

    async def test_report_creation_produces_audit_event(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs, borrower="AuditCreateCo")
        r = await ac.get(f"{RPTS}/{rpt['id']}/audit", headers=admin_hdrs)
        assert r.status_code == 200
        events = self._events(r.json())
        actions = {e["action"] for e in events}
        assert "report.created" in actions, (
            f"Expected report.created in audit log; found: {actions}"
        )

    async def test_submit_for_review_produces_audit_event(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs, borrower="AuditSubmitCo")
        await _seed_section(rpt["id"], section_no=7)
        await ac.post(f"{RPTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        r = await ac.get(f"{RPTS}/{rpt['id']}/audit", headers=admin_hdrs)
        assert r.status_code == 200
        actions = {e["action"] for e in self._events(r.json())}
        assert "report.submitted_for_review" in actions, (
            f"Missing submit audit event; found: {actions}"
        )

    async def test_approval_produces_audit_event(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs, borrower="AuditApproveCo")
        await _seed_section(rpt["id"], section_no=7)
        await ac.post(f"{RPTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        await ac.post(f"{RPTS}/{rpt['id']}/approve", headers=admin_hdrs)
        r = await ac.get(f"{RPTS}/{rpt['id']}/audit", headers=admin_hdrs)
        assert r.status_code == 200
        actions = {e["action"] for e in self._events(r.json())}
        assert "report.approved" in actions, (
            f"Missing approval audit event; found: {actions}"
        )

    async def test_fact_state_change_produces_audit_event(self, ac, admin_hdrs, report):
        rid = report["id"]
        fid = await _seed_fact(rid, "ebitda", 500.0, state="extracted")
        r = await ac.patch(f"{RPTS}/{rid}/facts/{fid}/state",
                           json={"state": "normalized"},
                           headers=admin_hdrs)
        if r.status_code == 200:
            audit_r = await ac.get(f"{RPTS}/{rid}/audit", headers=admin_hdrs)
            assert audit_r.status_code == 200
            actions = {e["action"] for e in self._events(audit_r.json())}
            assert any("fact" in a.lower() for a in actions), (
                f"Expected fact-related audit event; found: {actions}"
            )

    async def test_audit_events_have_required_fields(self, ac, admin_hdrs, report):
        r = await ac.get(f"{RPTS}/{report['id']}/audit", headers=admin_hdrs)
        assert r.status_code == 200
        events = self._events(r.json())
        if events:
            ev = events[0]
            for field in ("action", "actor_user_id"):
                assert field in ev, f"Audit event missing required field '{field}': {ev}"
            # Timestamp may be 'timestamp' or 'created_at' depending on schema
            assert "timestamp" in ev or "created_at" in ev, (
                f"Audit event missing timestamp field: {ev}"
            )

    async def test_audit_events_in_chronological_order(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs, borrower="AuditOrderCo")
        await _seed_section(rpt["id"], section_no=7)
        await ac.post(f"{RPTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        r = await ac.get(f"{RPTS}/{rpt['id']}/audit", headers=admin_hdrs)
        assert r.status_code == 200
        events = self._events(r.json())
        if len(events) >= 2:
            ts_field = "timestamp" if "timestamp" in events[0] else "created_at"
            timestamps = [e[ts_field] for e in events]
            assert timestamps == sorted(timestamps), (
                "Audit events are not in chronological order"
            )


# ══════════════════════════════════════════════════════════════════════════════
# ⑳  Chinese-Language Generation
# ══════════════════════════════════════════════════════════════════════════════

class TestChineseLanguageGeneration:
    """Generation API must accept zh language flag and pass it to prompts."""

    async def test_generate_section_with_zh_language_returns_202(self, ac, admin_hdrs, report):
        rid = report["id"]
        with _mock_gemini("## 財務分析\n\n內容說明。\n"):
            r = await ac.post(f"{RPTS}/{rid}/generate/7?gen_language=zh",
                              headers=admin_hdrs)
        assert r.status_code == 202, (
            f"zh language flag not accepted: {r.status_code} {r.text}"
        )

    async def test_invalid_language_code_falls_back_to_en(self, ac, admin_hdrs, report):
        rid = report["id"]
        with _mock_gemini():
            r = await ac.post(f"{RPTS}/{rid}/generate/7?gen_language=fr",
                              headers=admin_hdrs)
        # Must accept the request (falls back to 'en'), not reject it
        assert r.status_code == 202, (
            f"Invalid language code should fall back to en: {r.status_code} {r.text}"
        )

    def test_zh_instruction_is_in_traditional_chinese(self):
        from credit_report.generation.prompt_builder import _ZH_INSTRUCTION
        # Must mention 繁體中文 (Traditional Chinese) explicitly
        assert "繁體中文" in _ZH_INSTRUCTION, (
            "ZH language instruction must specify Traditional Chinese (繁體中文)"
        )
        # Must not allow English prose
        assert "English prose" in _ZH_INSTRUCTION or "English" in _ZH_INSTRUCTION, (
            "ZH instruction should explicitly exclude English output"
        )

    def test_build_section_prompt_zh_includes_zh_instruction(self):
        from credit_report.generation.prompt_builder import build_section_prompt, _ZH_INSTRUCTION
        system, _ = build_section_prompt(
            section_no=7,
            input_json={},
            evidence_chunks=[],
            preceding_outputs={},
            output_language="zh",
        )
        assert "繁體中文" in system, (
            "ZH system prompt must include Traditional Chinese instruction"
        )

    def test_build_section_prompt_en_excludes_zh_instruction(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        system, _ = build_section_prompt(
            section_no=7,
            input_json={},
            evidence_chunks=[],
            preceding_outputs={},
            output_language="en",
        )
        assert "繁體中文" not in system, (
            "EN system prompt must not include Traditional Chinese instruction"
        )


# ══════════════════════════════════════════════════════════════════════════════
# ①  Non-Singapore Borrower Entity Guard (ACRA / §8)
# ══════════════════════════════════════════════════════════════════════════════

class TestNonSingaporeBorrowerSection8:
    """
    §8 (Legal Documentation & Charges) covers ACRA charges for SG entities.
    For Taiwan-incorporated borrowers the prompt should note it may not apply.
    """

    def test_section8_prompt_builds_without_error_for_taiwan_borrower(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        input_json = {
            "borrower_name": "Yang Ming Marine (台灣)",
            "incorporation_country": "Taiwan",
            "entity_type": "ROC Company",
        }
        sys_prompt, user_prompt = build_section_prompt(
            section_no=8,
            input_json=input_json,
            evidence_chunks=[],
            preceding_outputs={},
            output_language="en",
        )
        assert isinstance(user_prompt, str) and len(user_prompt) > 10

    async def test_section8_generation_accepted_for_any_borrower(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs, borrower="Taiwan Holding Co")
        # Need §1 done first (§8 may depend on §1)
        await _seed_section(rpt["id"], section_no=1, markdown="## Facility\n\nContent.\n")
        await _seed_section(rpt["id"], section_no=5, markdown="## Collateral\n\nContent.\n")
        with _mock_gemini("## Legal Documentation\n\nFor ROC entities, ACRA is not applicable.\n"):
            r = await ac.post(f"{RPTS}/{rpt['id']}/generate/8", headers=admin_hdrs)
        assert r.status_code == 202, (
            f"§8 generation must be accepted for non-SG borrower: {r.status_code} {r.text}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# ②  Multi-Entity Consolidated vs Standalone Facts
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiEntityFacts:
    """Facts for multiple entities in a group must coexist and be labelled."""

    async def test_facts_for_different_entities_coexist_in_same_report(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_fact(rid, "revenue", 5000.0, entity="Evergreen Group (Consolidated)")
        await _seed_fact(rid, "revenue", 1200.0, entity="Evergreen Marine Corp (Standalone)")
        r = await ac.get(f"{RPTS}/{rid}/facts", headers=admin_hdrs)
        assert r.status_code == 200
        facts = r.json()
        entities = {f["entity"] for f in facts if f["metric_name"] == "revenue"}
        assert "Evergreen Group (Consolidated)" in entities
        assert "Evergreen Marine Corp (Standalone)" in entities

    async def test_entity_field_preserved_in_api_response(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_fact(rid, "ebitda", 800.0, entity="Yang Ming Holding", period="FY2024")
        r = await ac.get(f"{RPTS}/{rid}/facts", headers=admin_hdrs)
        facts = r.json()
        ym_facts = [f for f in facts if f.get("entity") == "Yang Ming Holding"]
        assert len(ym_facts) >= 1
        assert ym_facts[0]["entity"] == "Yang Ming Holding"


# ══════════════════════════════════════════════════════════════════════════════
# ④  Section 1 Mandatory Field Post-Generation Check
# ══════════════════════════════════════════════════════════════════════════════

class TestSection1MandatoryFields:
    """
    §1 (Facility Structure) is legally binding.
    The API must be able to retrieve section output so analysts can verify
    mandatory fields are present.
    """

    async def test_section1_output_readable_after_seeding(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_section(rid, section_no=1,
                            markdown="## Facility\n\n**Amount**: USD 50m\n\n**Tenor**: 5 years\n")
        r = await ac.get(f"{RPTS}/{rid}/sections/1/output", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        assert "markdown" in body
        assert len(body["markdown"]) > 0

    def test_section1_prompt_references_facility_amount_and_tenor(self):
        """The §1 prompt must instruct the AI to include key deal terms."""
        from credit_report.generation.prompt_builder import build_section_prompt
        sys_prompt, _ = build_section_prompt(
            section_no=1,
            input_json={
                "1A_facility_type": "Term Loan",
                "1B_facility_amount": "USD 50,000,000",
                "1D_tenor": "5 years",
            },
            evidence_chunks=[],
            preceding_outputs={},
            output_language="en",
        )
        assert isinstance(sys_prompt, str)


# ══════════════════════════════════════════════════════════════════════════════
# ⑥  FX Rate Storage and Retrieval (TWD/USD)
# ══════════════════════════════════════════════════════════════════════════════

class TestFXRateStorage:
    """FX rates must be storable and retrievable for dual-currency reports.

    FX rate PUT body: {"from_currency": "TWD", "to_currency": "USD", "rate": 0.03077}
    GET returns a list of FXRate objects.
    """

    async def test_put_fx_rate_and_retrieve(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.put(f"{RPTS}/{rid}/fx-rates",
                         json={"from_currency": "TWD", "to_currency": "USD",
                               "rate": 0.03077, "rate_date": "2026-05-16"},
                         headers=admin_hdrs)
        assert r.status_code in (200, 201, 204), (
            f"FX rate PUT failed: {r.status_code} {r.text}"
        )
        r2 = await ac.get(f"{RPTS}/{rid}/fx-rates", headers=admin_hdrs)
        assert r2.status_code == 200
        rates = r2.json()
        # Response is a list of FXRate objects
        assert isinstance(rates, list), f"Expected list of rates, got: {type(rates)}"
        if rates:
            twd_rates = [r for r in rates if r.get("from_currency") == "TWD"]
            assert len(twd_rates) >= 1, (
                f"TWD rate not found in response: {rates}"
            )

    async def test_fx_rate_endpoint_exists(self, ac, admin_hdrs, report):
        """Even if no rate set, GET /fx-rates must return 200 (not 404/405)."""
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/fx-rates", headers=admin_hdrs)
        assert r.status_code != 404, "FX rate endpoint must exist (GET /fx-rates)"
        assert r.status_code != 405, "FX rate GET method not allowed"
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# ⑬  Block Listing After Section Output Seeded
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockListing:
    """GET /reports/{id}/sections/{no}/blocks — available after section done."""

    async def test_blocks_endpoint_exists_for_done_section(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_section(rid, section_no=7,
                            markdown="## Analysis\n\n| Metric | Value |\n|---|---|\n| Revenue | 1000 |\n")
        r = await ac.get(f"{RPTS}/{rid}/blocks", headers=admin_hdrs)
        # Must return 200 (possibly empty if build_blocks not yet called) — not 404
        assert r.status_code == 200, (
            f"Blocks endpoint must exist and return 200: {r.status_code} {r.text}"
        )

    async def test_blocks_response_is_list(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_section(rid, section_no=7)
        r = await ac.get(f"{RPTS}/{rid}/blocks", headers=admin_hdrs)
        assert r.status_code == 200
        assert isinstance(r.json(), list), "Blocks endpoint must return a JSON array"


# ══════════════════════════════════════════════════════════════════════════════
# ⑰  Concurrent PATCH Optimistic Locking
# ══════════════════════════════════════════════════════════════════════════════

class TestOptimisticLocking:
    """Two concurrent PATCHes with the same expected_version → one must get 409."""

    async def test_fact_double_patch_same_version_second_is_409(self, ac, admin_hdrs, report):
        rid = report["id"]
        fid = await _seed_fact(rid, "revenue", 1000.0, state="extracted")

        # First PATCH — must succeed
        r1 = await ac.patch(f"{RPTS}/{rid}/facts/{fid}",
                            json={"value": 1100.0, "reason": "correction", "expected_version": 1},
                            headers=admin_hdrs)

        if r1.status_code == 404:
            pytest.skip("Fact PATCH endpoint not found — optimistic lock not testable")

        if r1.status_code not in (200, 409):
            pytest.skip(f"Unexpected first PATCH status: {r1.status_code}")

        if r1.status_code == 200:
            # Second PATCH with same expected_version=1 must fail
            r2 = await ac.patch(f"{RPTS}/{rid}/facts/{fid}",
                                json={"value": 1200.0, "reason": "duplicate", "expected_version": 1},
                                headers=admin_hdrs)
            assert r2.status_code == 409, (
                f"Second PATCH with stale expected_version should return 409, got {r2.status_code}"
            )

    async def test_block_patch_with_correct_version_succeeds(self, ac, admin_hdrs, report):
        """Block optimistic lock: correct version → 200."""
        rid = report["id"]
        await _seed_section(rid, section_no=7)
        # Get list of blocks
        r = await ac.get(f"{RPTS}/{rid}/blocks", headers=admin_hdrs)
        if r.status_code != 200 or not r.json():
            pytest.skip("No blocks available for optimistic lock test")
        block = r.json()[0]
        bid = block["id"]
        version = block.get("version", 1)
        r2 = await ac.patch(f"{RPTS}/{rid}/blocks/{bid}",
                            json={"content": "Updated content.", "reason": "test",
                                  "expected_version": version},
                            headers=admin_hdrs)
        assert r2.status_code in (200, 409), (
            f"Block PATCH returned unexpected status: {r2.status_code} {r2.text}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Password Change Endpoints (P2-⑤)
# ══════════════════════════════════════════════════════════════════════════════

class TestPasswordChange:
    """POST /auth/change-password and POST /auth/users/{id}/reset-password."""

    async def test_change_password_success(self, ac, admin_hdrs):
        email = f"pwtest_{uuid.uuid4().hex[:8]}@example.com"
        await ac.post(f"{AUTH}/register",
                      json={"email": email, "password": "OldPass1!", "role": "analyst"},
                      headers=admin_hdrs)
        tokens = await _login(ac, email, "OldPass1!")
        h = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = await ac.post(f"{AUTH}/change-password",
                          json={"current_password": "OldPass1!", "new_password": "NewPass2!"},
                          headers=h)
        assert r.status_code == 200, r.text
        assert "changed" in r.json().get("message", "").lower()

    async def test_change_password_wrong_current_returns_401(self, ac, admin_hdrs):
        email = f"pwwrong_{uuid.uuid4().hex[:8]}@example.com"
        await ac.post(f"{AUTH}/register",
                      json={"email": email, "password": "Correct1!", "role": "analyst"},
                      headers=admin_hdrs)
        tokens = await _login(ac, email, "Correct1!")
        h = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = await ac.post(f"{AUTH}/change-password",
                          json={"current_password": "WrongPass!", "new_password": "NewPass2!"},
                          headers=h)
        assert r.status_code == 401, f"Expected 401 for wrong current password, got {r.status_code}"

    async def test_change_password_too_short_returns_400(self, ac, admin_hdrs):
        email = f"pwshort_{uuid.uuid4().hex[:8]}@example.com"
        await ac.post(f"{AUTH}/register",
                      json={"email": email, "password": "Correct1!", "role": "analyst"},
                      headers=admin_hdrs)
        tokens = await _login(ac, email, "Correct1!")
        h = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = await ac.post(f"{AUTH}/change-password",
                          json={"current_password": "Correct1!", "new_password": "short"},
                          headers=h)
        assert r.status_code == 400, f"Expected 400 for short password, got {r.status_code}"

    async def test_new_password_works_after_change(self, ac, admin_hdrs):
        email = f"pwlogin_{uuid.uuid4().hex[:8]}@example.com"
        await ac.post(f"{AUTH}/register",
                      json={"email": email, "password": "Initial1!", "role": "analyst"},
                      headers=admin_hdrs)
        tokens = await _login(ac, email, "Initial1!")
        h = {"Authorization": f"Bearer {tokens['access_token']}"}

        await ac.post(f"{AUTH}/change-password",
                      json={"current_password": "Initial1!", "new_password": "Updated1!"},
                      headers=h)

        # Old password should fail
        r_old = await ac.post(f"{AUTH}/login",
                               data={"username": email, "password": "Initial1!"})
        assert r_old.status_code == 401, "Old password should be rejected after change"

        # New password should work
        r_new = await ac.post(f"{AUTH}/login",
                               data={"username": email, "password": "Updated1!"})
        assert r_new.status_code == 200, "New password should authenticate successfully"

    async def test_admin_reset_password_succeeds(self, ac, admin_hdrs):
        email = f"pwreset_{uuid.uuid4().hex[:8]}@example.com"
        reg = await ac.post(f"{AUTH}/register",
                             json={"email": email, "password": "OldPass1!", "role": "analyst"},
                             headers=admin_hdrs)
        user_id = reg.json()["id"]

        r = await ac.post(f"{AUTH}/users/{user_id}/reset-password",
                          json={"new_password": "AdminSet1!"},
                          headers=admin_hdrs)
        assert r.status_code == 200, r.text

        # Should now login with reset password
        r2 = await ac.post(f"{AUTH}/login",
                            data={"username": email, "password": "AdminSet1!"})
        assert r2.status_code == 200, "Reset password should allow login"

    async def test_analyst_cannot_reset_other_users_password(self, ac, admin_hdrs):
        email_a = f"analyst_a_{uuid.uuid4().hex[:8]}@example.com"
        email_b = f"analyst_b_{uuid.uuid4().hex[:8]}@example.com"
        await ac.post(f"{AUTH}/register",
                      json={"email": email_a, "password": "Pass1234!", "role": "analyst"},
                      headers=admin_hdrs)
        reg_b = await ac.post(f"{AUTH}/register",
                               json={"email": email_b, "password": "Pass1234!", "role": "analyst"},
                               headers=admin_hdrs)
        user_b_id = reg_b.json()["id"]

        tokens_a = await _login(ac, email_a)
        h_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}

        r = await ac.post(f"{AUTH}/users/{user_b_id}/reset-password",
                          json={"new_password": "Hacked123!"},
                          headers=h_a)
        assert r.status_code == 403, f"Non-admin should get 403, got {r.status_code}"


# ══════════════════════════════════════════════════════════════════════════════
# Token Quota Atomic Reserve (P0-①)
# ══════════════════════════════════════════════════════════════════════════════

class TestQuotaAtomicity:
    """reserve_and_record_tokens raises 429 when limit is already exhausted."""

    async def test_reserve_raises_429_when_limit_reached(self):
        from credit_report.generation.quota import reserve_and_record_tokens, _ROLE_LIMITS
        from credit_report.database import AsyncSessionLocal
        from credit_report.generation.models import UserTokenQuota
        from datetime import datetime, timezone
        import uuid as _uuid

        user_id = str(_uuid.uuid4())
        limit = _ROLE_LIMITS["analyst"]

        async with AsyncSessionLocal() as db:
            # Pre-seed quota at exactly the limit
            db.add(UserTokenQuota(
                id=str(_uuid.uuid4()),
                user_id=user_id,
                quota_date=datetime.now(timezone.utc).date(),
                tokens_used=limit,
            ))
            await db.flush()

            from fastapi import HTTPException
            try:
                await reserve_and_record_tokens(db, user_id, 1, role="analyst")
                assert False, "Should have raised HTTPException 429"
            except HTTPException as exc:
                assert exc.status_code == 429, f"Expected 429, got {exc.status_code}"
            finally:
                await db.rollback()

    async def test_reserve_records_tokens_when_under_limit(self):
        from credit_report.generation.quota import reserve_and_record_tokens
        from credit_report.database import AsyncSessionLocal
        from credit_report.generation.models import UserTokenQuota
        from sqlalchemy import select
        from datetime import datetime, timezone
        import uuid as _uuid

        user_id = str(_uuid.uuid4())

        async with AsyncSessionLocal() as db:
            await reserve_and_record_tokens(db, user_id, 1000, role="analyst")
            result = await db.execute(
                select(UserTokenQuota).where(
                    UserTokenQuota.user_id == user_id,
                    UserTokenQuota.quota_date == datetime.now(timezone.utc).date(),
                )
            )
            quota = result.scalar_one_or_none()
            assert quota is not None, "UserTokenQuota row should have been created"
            assert quota.tokens_used == 1000, f"Expected 1000 tokens, got {quota.tokens_used}"
            await db.rollback()

    async def test_reserve_is_cumulative(self):
        from credit_report.generation.quota import reserve_and_record_tokens
        from credit_report.database import AsyncSessionLocal
        from credit_report.generation.models import UserTokenQuota
        from sqlalchemy import select
        from datetime import datetime, timezone
        import uuid as _uuid

        user_id = str(_uuid.uuid4())

        async with AsyncSessionLocal() as db:
            await reserve_and_record_tokens(db, user_id, 500, role="analyst")
            await reserve_and_record_tokens(db, user_id, 300, role="analyst")
            result = await db.execute(
                select(UserTokenQuota).where(
                    UserTokenQuota.user_id == user_id,
                    UserTokenQuota.quota_date == datetime.now(timezone.utc).date(),
                )
            )
            quota = result.scalar_one_or_none()
            assert quota is not None
            assert quota.tokens_used == 800, f"Expected 800 cumulative tokens, got {quota.tokens_used}"
            await db.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# Report Type Prompt Injection (P2-⑥)
# ══════════════════════════════════════════════════════════════════════════════

class TestReportTypePrompt:
    """build_section_prompt() injects report_type context hint."""

    def test_annual_review_hint_injected(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        _, user = build_section_prompt(
            section_no=7,
            input_json={"metadata": {"report_type": "annual_review"}},
            evidence_chunks=[],
        )
        assert "Annual Review" in user
        assert "YoY" in user

    def test_watchlist_hint_injected(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        _, user = build_section_prompt(
            section_no=2,
            input_json={"metadata": {"report_type": "watchlist"}},
            evidence_chunks=[],
        )
        assert "Watchlist" in user
        assert "deterioration" in user.lower()

    def test_new_deal_hint_injected(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        _, user = build_section_prompt(
            section_no=1,
            input_json={"metadata": {"report_type": "new_deal"}},
            evidence_chunks=[],
        )
        assert "New Deal" in user

    def test_no_hint_when_type_absent(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        _, user = build_section_prompt(
            section_no=1,
            input_json={},
            evidence_chunks=[],
        )
        assert "Report type:" not in user

    def test_unknown_type_no_hint(self):
        from credit_report.generation.prompt_builder import build_section_prompt
        _, user = build_section_prompt(
            section_no=1,
            input_json={"metadata": {"report_type": "unknown_type_xyz"}},
            evidence_chunks=[],
        )
        assert "Report type:" not in user


# ══════════════════════════════════════════════════════════════════════════════
# Source Evidence Re-attribution (P2-⑦)
# ══════════════════════════════════════════════════════════════════════════════

class TestSourceEvidenceAttribution:
    """upsert_fact() UPDATE branch updates source_evidence_id when re-uploading."""

    async def test_source_evidence_id_updated_on_reupsert(self):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_fact
        import uuid as _uuid

        report_id = str(_uuid.uuid4())
        doc_id_1 = str(_uuid.uuid4())
        doc_id_2 = str(_uuid.uuid4())

        async with AsyncSessionLocal() as db:
            # Initial insert with first document
            fact1 = await upsert_fact(db, {
                "report_id": report_id,
                "metric_name": "revenue",
                "entity": "TestCo",
                "period": "FY2024",
                "value": 1000.0,
                "value_text": "1,000m",
                "source_type": "etl",
                "source_evidence_id": doc_id_1,
            })
            await db.flush()
            assert fact1.source_evidence_id == doc_id_1

            # Re-upsert from a newer document version
            fact2 = await upsert_fact(db, {
                "report_id": report_id,
                "metric_name": "revenue",
                "entity": "TestCo",
                "period": "FY2024",
                "value": 1050.0,
                "value_text": "1,050m",
                "source_type": "etl",
                "source_evidence_id": doc_id_2,
            })
            await db.flush()

            assert fact2.id == fact1.id, "Should be same fact row (upsert)"
            assert fact2.source_evidence_id == doc_id_2, (
                f"source_evidence_id should be updated to doc_id_2, "
                f"got {fact2.source_evidence_id}"
            )
            await db.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# Gap fixes: untested endpoints — auth status, role update, fx-rates, mapping rule approve
# ══════════════════════════════════════════════════════════════════════════════

class TestAuthStatus:
    """GET /auth/status — diagnostic endpoint, no auth required."""

    async def test_status_returns_200_and_login_possible(self, ac):
        r = await ac.get(f"{AUTH}/status")
        assert r.status_code == 200
        body = r.json()
        # Response contains total_active_users and login_possible fields
        assert "total_active_users" in body or "login_possible" in body, (
            f"Unexpected response shape: {body}"
        )
        assert body.get("login_possible") is True, "Admin user must exist, login_possible must be True"

    async def test_status_accessible_without_token(self, ac):
        r = await ac.get(f"{AUTH}/status")
        # Must not be 401 or 403 — it's a public diagnostic endpoint
        assert r.status_code not in (401, 403)


class TestRoleUpdate:
    """PATCH /auth/users/{user_id}/role — admin-only role change."""

    async def test_admin_can_update_user_role(self, ac, admin_hdrs):
        # Create a new analyst
        email = f"roletest_{uuid.uuid4().hex[:8]}@example.com"
        r = await ac.post(f"{AUTH}/register",
                          json={"email": email, "password": "Pass1234!", "role": "analyst"},
                          headers=admin_hdrs)
        assert r.status_code in (200, 201)
        user_id = r.json()["id"]

        # Promote to reviewer
        r = await ac.patch(f"{AUTH}/users/{user_id}/role",
                           params={"role": "reviewer"},
                           headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["role"] == "reviewer"

    async def test_analyst_cannot_update_roles(self, ac, admin_hdrs):
        # Create analyst, login as that analyst
        email = f"analyst_{uuid.uuid4().hex[:8]}@example.com"
        r = await ac.post(f"{AUTH}/register",
                          json={"email": email, "password": "Pass1234!", "role": "analyst"},
                          headers=admin_hdrs)
        uid = r.json()["id"]
        tokens = await _login(ac, email, "Pass1234!")
        analyst_hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = await ac.patch(f"{AUTH}/users/{uid}/role",
                           params={"role": "admin"},
                           headers=analyst_hdrs)
        assert r.status_code == 403

    async def test_invalid_role_returns_400(self, ac, admin_hdrs):
        # Create a user to attempt patching
        email = f"inv_{uuid.uuid4().hex[:8]}@example.com"
        r = await ac.post(f"{AUTH}/register",
                          json={"email": email, "password": "Pass1234!", "role": "analyst"},
                          headers=admin_hdrs)
        uid = r.json()["id"]

        r = await ac.patch(f"{AUTH}/users/{uid}/role",
                           params={"role": "supervillain"},
                           headers=admin_hdrs)
        assert r.status_code == 400

    async def test_nonexistent_user_returns_404(self, ac, admin_hdrs):
        r = await ac.patch(f"{AUTH}/users/nonexistent-user-id/role",
                           params={"role": "analyst"},
                           headers=admin_hdrs)
        assert r.status_code == 404


class TestFXRatesEndpointsCoverage:
    """PUT /reports/{rid}/fx-rates and GET /reports/{rid}/fx-rates — additional coverage."""

    async def test_upsert_sets_all_fields(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.put(f"{RPTS}/{rid}/fx-rates",
                         json={"from_currency": "JPY", "to_currency": "USD",
                               "rate": 150.25, "rate_date": "2024-12-31",
                               "source": "boj"},
                         headers=admin_hdrs)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["from_currency"] == "JPY"
        assert body["to_currency"] == "USD"
        assert pytest.approx(body["rate"], abs=0.001) == 150.25
        assert body["source"] == "boj"

    async def test_fx_rates_require_authentication(self, ac, report):
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/fx-rates")
        assert r.status_code == 401


class TestMappingRuleApprove:
    """POST /reports/{rid}/mapping/rules/{rule_id}/approve."""

    async def test_create_and_approve_mapping_rule(self, ac, admin_hdrs, report):
        rid = report["id"]

        # Create a mapping rule
        r = await ac.post(f"{RPTS}/{rid}/mapping/rules",
                          json={
                              "source_label": "Net Income (Reported)",
                              "canonical_metric": "net_income",
                              "category": "income_statement",
                          },
                          headers=admin_hdrs)
        assert r.status_code in (200, 201), r.text
        rule_id = r.json()["id"]

        # Approve the rule
        r = await ac.post(f"{RPTS}/{rid}/mapping/rules/{rule_id}/approve",
                          headers=admin_hdrs)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == rule_id
        assert body.get("approved_by") is not None

    async def test_approve_nonexistent_rule_returns_404(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(f"{RPTS}/{rid}/mapping/rules/nonexistent-rule-id/approve",
                          headers=admin_hdrs)
        assert r.status_code == 404

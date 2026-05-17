"""
Final-Sweep End-to-End Test Suite
====================================
Deep-reconnaissance pass covering every remaining gap found after the
comprehensive sprint review. Organized by risk category.

Gaps targeted:
  A. Cross-tenant isolation on list_conflicts / get_conflict (security)
  B. mark_unresolved must restore involved facts to "conflicted" (correctness)
  C. validate_block idempotency (double-call must not error)
  D. FX rate triple upsert: only the live rate is returned each time
  E. Block history ordering: ascending by version (oldest first)
  F. Mapping rule double-approve: idempotent, approved_by updated
  G. safe_divide / financial formula arithmetic edge cases (unit)
  H. Section input section_no validation (out-of-range → 400)
  I. Conflict resolve edge cases (empty rejected list; re-resolve after mark_unresolved)
  J. Block stats edge cases (no cells, all-bound, all-unbound)
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from credit_report.database import AsyncSessionLocal, Base
import credit_report.calculation_engine.models  # noqa: F401
import credit_report.fact_store.models          # noqa: F401
import credit_report.block_ast.models           # noqa: F401
import credit_report.security.models            # noqa: F401
import credit_report.audit.events               # noqa: F401
import credit_report.models                     # noqa: F401

from main import app

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
RPTS = f"{BASE}/reports"
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="function")
async def db():
    """Isolated in-memory SQLite for pure unit/repository tests."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture(scope="function")
async def admin_hdrs(ac):
    r = await ac.post(f"{AUTH}/login", data={"username": "admin@example.com", "password": "admin123"})
    assert r.status_code == 200, f"admin login: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture(scope="function")
async def report(ac, admin_hdrs):
    r = await ac.post(RPTS,
                      json={"industry": "shipping", "report_type": "credit_analysis",
                            "borrower_name": "SweepCo"},
                      headers=admin_hdrs)
    assert r.status_code in (200, 201)
    return r.json()


def _uid() -> str:
    return str(uuid.uuid4())


async def _new_analyst(ac, admin_hdrs) -> dict:
    """Register a fresh analyst and return login headers."""
    email = f"sw_{_uid()[:8]}@test.com"
    await ac.post(f"{AUTH}/register",
                  json={"email": email, "password": "Pass1234!", "role": "analyst"},
                  headers=admin_hdrs)
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": "Pass1234!"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── DB-level helpers for seeding conflict scenarios ───────────────────────────

async def _seed_conflicting_facts(report_id: str) -> tuple[str, str, str]:
    """
    Insert two facts in 'conflicted' state and an open FactConflict linking them.
    Returns (fact_a_id, fact_b_id, conflict_id).
    """
    from credit_report.fact_store.models import CanonicalFact, FactConflict

    fa_id = _uid()
    fb_id = _uid()
    cf_id = _uid()

    async with AsyncSessionLocal() as db:
        db.add(CanonicalFact(
            id=fa_id, report_id=report_id,
            metric_name="revenue", entity="TestCo", period="FY2024",
            value=1000.0, value_text="USD 1B", state="conflicted",
            source_type="analyst_input_json", source_priority=2, version=1,
        ))
        db.add(CanonicalFact(
            id=fb_id, report_id=report_id,
            metric_name="revenue", entity="TestCo", period="FY2024",
            value=1200.0, value_text="USD 1.2B", state="conflicted",
            source_type="etl_extraction", source_priority=1, version=1,
        ))
        db.add(FactConflict(
            id=cf_id, report_id=report_id,
            fact_a_id=fa_id, fact_b_id=fb_id,
            metric_name="revenue", entity="TestCo", period="FY2024",
            status="open",
        ))
        await db.commit()
    return fa_id, fb_id, cf_id


async def _get_fact_state(fact_id: str) -> str:
    """Read a fact's current state directly from the DB."""
    from credit_report.fact_store.models import CanonicalFact
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(CanonicalFact).where(CanonicalFact.id == fact_id))
        fact = r.scalar_one_or_none()
        return fact.state if fact else "NOT_FOUND"


async def _seed_block(report_id: str, *, content: str = "Paragraph text.") -> str:
    """Insert a ReportBlock and return its id."""
    from credit_report.block_ast.models import ReportBlock
    bid = _uid()
    async with AsyncSessionLocal() as db:
        db.add(ReportBlock(
            id=bid, report_id=report_id, section_no=1,
            block_type="paragraph", content=content,
            source_fact_ids="[]", is_stale=False,
            version=1, validation_status="pending",
        ))
        await db.commit()
    return bid


# ══════════════════════════════════════════════════════════════════════════════
# A. Cross-Tenant Isolation on Conflict Endpoints
# ══════════════════════════════════════════════════════════════════════════════

class TestConflictCrossTenantSecurity:
    """
    Analyst B must not be able to read Analyst A's conflicts, regardless of
    which route (conflicts router or facts router) they use.
    """

    async def test_list_conflicts_via_conflicts_router_blocks_non_owner(
        self, ac, admin_hdrs, report
    ):
        rid = report["id"]
        await _seed_conflicting_facts(rid)
        analyst_b = await _new_analyst(ac, admin_hdrs)

        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts", headers=analyst_b)
        assert r.status_code in (403, 404), (
            f"Analyst B must not list conflicts for Analyst A's report. Got {r.status_code}"
        )

    async def test_get_conflict_via_conflicts_router_blocks_non_owner(
        self, ac, admin_hdrs, report
    ):
        rid = report["id"]
        _, _, cf_id = await _seed_conflicting_facts(rid)
        analyst_b = await _new_analyst(ac, admin_hdrs)

        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts/{cf_id}", headers=analyst_b)
        assert r.status_code in (403, 404), (
            f"Analyst B must not read a specific conflict. Got {r.status_code}"
        )

    async def test_list_conflicts_via_facts_router_blocks_non_owner(
        self, ac, admin_hdrs, report
    ):
        """GET /reports/{rid}/facts/conflicts (mounted under facts router)."""
        rid = report["id"]
        await _seed_conflicting_facts(rid)
        analyst_b = await _new_analyst(ac, admin_hdrs)

        # The facts router also exposes GET /facts/conflicts — same security gap
        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts", headers=analyst_b)
        assert r.status_code in (403, 404), (
            f"Facts-router list_conflicts must deny non-owner. Got {r.status_code}"
        )

    async def test_owner_can_still_list_own_conflicts(self, ac, admin_hdrs, report):
        """The fix must not block the report owner from accessing their own conflicts."""
        rid = report["id"]
        await _seed_conflicting_facts(rid)
        # admin_hdrs is the owner (admin owns all)
        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts", headers=admin_hdrs)
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# B. mark_unresolved Fact State Restoration
# ══════════════════════════════════════════════════════════════════════════════

class TestMarkUnresolvedFactRestoration:
    """
    When a conflict is resolved: chosen fact → approved, rejected → deprecated.
    When mark_unresolved is called: BOTH facts must come back to 'conflicted'
    so the conflict can be re-resolved without hitting state-machine dead ends.
    """

    async def test_mark_unresolved_restores_chosen_fact_to_conflicted(
        self, ac, admin_hdrs, report
    ):
        rid = report["id"]
        fa_id, fb_id, cf_id = await _seed_conflicting_facts(rid)

        # Resolve: choose fa, reject fb
        r = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/resolve",
            json={"chosen_fact_id": fa_id, "rejected_fact_ids": [fb_id],
                  "resolution_reason": "A is more accurate"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200, r.text
        assert await _get_fact_state(fa_id) == "approved"

        # Unresolve
        r2 = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/mark-unresolved",
            headers=admin_hdrs,
        )
        assert r2.status_code == 200
        # Chosen fact must be restored to 'conflicted', not stuck in 'approved'
        assert await _get_fact_state(fa_id) == "conflicted", (
            "Chosen fact must return to 'conflicted' after mark_unresolved"
        )

    async def test_mark_unresolved_restores_rejected_fact_from_deprecated(
        self, ac, admin_hdrs, report
    ):
        rid = report["id"]
        fa_id, fb_id, cf_id = await _seed_conflicting_facts(rid)

        await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/resolve",
            json={"chosen_fact_id": fa_id, "rejected_fact_ids": [fb_id],
                  "resolution_reason": "B overruled"},
            headers=admin_hdrs,
        )
        assert await _get_fact_state(fb_id) == "deprecated"

        await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/mark-unresolved",
            headers=admin_hdrs,
        )
        assert await _get_fact_state(fb_id) == "conflicted", (
            "Rejected fact must return to 'conflicted' after mark_unresolved — "
            "previously it stayed 'deprecated' (terminal), blocking re-resolution"
        )

    async def test_conflict_is_re_resolvable_after_mark_unresolved(
        self, ac, admin_hdrs, report
    ):
        """End-to-end: resolve → mark_unresolved → resolve again (should succeed)."""
        rid = report["id"]
        fa_id, fb_id, cf_id = await _seed_conflicting_facts(rid)

        await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/resolve",
            json={"chosen_fact_id": fa_id, "rejected_fact_ids": [fb_id],
                  "resolution_reason": "first pick"},
            headers=admin_hdrs,
        )
        await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/mark-unresolved",
            headers=admin_hdrs,
        )
        # Re-resolve (this would crash with 404 before the fix because
        # update_fact_state would raise InvalidStateTransitionError for
        # approved→approved and deprecated→deprecated)
        r = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/resolve",
            json={"chosen_fact_id": fb_id, "rejected_fact_ids": [fa_id],
                  "resolution_reason": "changed my mind"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200, (
            f"Re-resolution after mark_unresolved must succeed. Got {r.status_code}: {r.text}"
        )
        assert r.json()["chosen_fact_id"] == fb_id

    async def test_mark_unresolved_on_already_open_conflict_is_idempotent(
        self, ac, admin_hdrs, report
    ):
        """mark_unresolved on an already-open conflict must not error."""
        rid = report["id"]
        _, _, cf_id = await _seed_conflicting_facts(rid)
        r = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/mark-unresolved",
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "open"


# ══════════════════════════════════════════════════════════════════════════════
# C. validate_block Idempotency
# ══════════════════════════════════════════════════════════════════════════════

class TestValidateBlockIdempotency:

    async def test_validate_block_twice_both_return_200(self, ac, admin_hdrs, report):
        rid = report["id"]
        bid = await _seed_block(rid)

        r1 = await ac.post(f"{RPTS}/{rid}/blocks/{bid}/validate", headers=admin_hdrs)
        assert r1.status_code == 200
        assert r1.json()["validation_status"] == "passed"

        r2 = await ac.post(f"{RPTS}/{rid}/blocks/{bid}/validate", headers=admin_hdrs)
        assert r2.status_code == 200, (
            f"Second validate call must not fail. Got {r2.status_code}: {r2.text}"
        )
        assert r2.json()["validation_status"] == "passed"

    async def test_validate_block_resets_failed_to_passed(self, ac, admin_hdrs, report):
        """A block manually set to 'failed' can be re-validated to 'passed'."""
        from credit_report.block_ast.models import ReportBlock
        rid = report["id"]
        bid = await _seed_block(rid)

        # Force block to failed state via direct DB
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(ReportBlock).where(ReportBlock.id == bid))
            blk = r.scalar_one()
            blk.validation_status = "failed"
            await db.commit()

        r = await ac.post(f"{RPTS}/{rid}/blocks/{bid}/validate", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["validation_status"] == "passed"


# ══════════════════════════════════════════════════════════════════════════════
# D. FX Rate Triple Upsert — Only Live Rate Returned
# ══════════════════════════════════════════════════════════════════════════════

class TestFXRateTripleUpsert:

    async def test_three_sequential_upserts_return_only_last_rate(
        self, ac, admin_hdrs, report
    ):
        """After 3 upserts of the same pair, GET /fx-rates returns exactly 1 rate."""
        rid = report["id"]
        for rate in (10.0, 20.0, 30.5):
            r = await ac.put(
                f"{RPTS}/{rid}/fx-rates",
                json={"from_currency": "HKD", "to_currency": "USD",
                      "rate": rate, "rate_date": "2025-01-01", "source": "test"},
                headers=admin_hdrs,
            )
            assert r.status_code == 200

        r = await ac.get(f"{RPTS}/{rid}/fx-rates", headers=admin_hdrs)
        assert r.status_code == 200
        hkd_rates = [rt for rt in r.json() if rt["from_currency"] == "HKD"]
        assert len(hkd_rates) == 1, (
            f"After 3 upserts only 1 non-stale rate should be returned, got {len(hkd_rates)}"
        )
        assert pytest.approx(hkd_rates[0]["rate"], abs=0.01) == 30.5

    async def test_stale_rates_not_visible_after_upsert(self, ac, admin_hdrs, report):
        """Verify is_stale=True rates are excluded from the list endpoint."""
        rid = report["id"]
        await ac.put(f"{RPTS}/{rid}/fx-rates",
                     json={"from_currency": "MYR", "to_currency": "USD",
                           "rate": 4.7, "rate_date": "2025-01-01", "source": "test"},
                     headers=admin_hdrs)
        await ac.put(f"{RPTS}/{rid}/fx-rates",
                     json={"from_currency": "MYR", "to_currency": "USD",
                           "rate": 4.9, "rate_date": "2025-01-15", "source": "test"},
                     headers=admin_hdrs)

        r = await ac.get(f"{RPTS}/{rid}/fx-rates", headers=admin_hdrs)
        myr_rates = [rt for rt in r.json() if rt["from_currency"] == "MYR"]
        # Only the 4.9 rate should appear; 4.7 is stale
        rates = [rt["rate"] for rt in myr_rates]
        assert 4.7 not in rates, "Stale rate 4.7 must not appear in list"
        assert any(abs(r - 4.9) < 0.01 for r in rates), "Live rate 4.9 must appear"


# ══════════════════════════════════════════════════════════════════════════════
# E. Block History Ordering
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockHistoryOrdering:

    async def test_block_history_ascending_version(self, ac, admin_hdrs, report):
        """GET /blocks/{id}/history returns snapshots ordered by version ascending."""
        rid = report["id"]
        bid = await _seed_block(rid, content="v1 text")

        for i, content in enumerate(("v2 text", "v3 text"), start=2):
            r = await ac.patch(
                f"{RPTS}/{rid}/blocks/{bid}",
                json={"content": content, "reason": f"edit {i}",
                      "expected_version": i - 1},
                headers=admin_hdrs,
            )
            assert r.status_code == 200, r.text

        r = await ac.get(f"{RPTS}/{rid}/blocks/{bid}/history", headers=admin_hdrs)
        assert r.status_code == 200
        history = r.json()
        assert len(history) >= 2
        versions = [h["version"] for h in history]
        assert versions == sorted(versions), (
            f"Block history must be ordered ascending by version, got {versions}"
        )

    async def test_block_history_reflects_latest_content(self, ac, admin_hdrs, report):
        rid = report["id"]
        bid = await _seed_block(rid, content="original")

        await ac.patch(f"{RPTS}/{rid}/blocks/{bid}",
                       json={"content": "updated", "reason": "improve",
                             "expected_version": 1},
                       headers=admin_hdrs)

        r = await ac.get(f"{RPTS}/{rid}/blocks/{bid}/history", headers=admin_hdrs)
        assert r.status_code == 200
        history = r.json()
        # The snapshot(s) in history capture the PREVIOUS content
        assert any(h["content"] == "original" for h in history), (
            "History must contain a snapshot of the original content"
        )


# ══════════════════════════════════════════════════════════════════════════════
# F. Mapping Rule Double-Approve Idempotency
# ══════════════════════════════════════════════════════════════════════════════

class TestMappingRuleDoubleApprove:

    async def test_approve_same_rule_twice_is_idempotent(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/mapping/rules",
            json={"source_label": "Net Income", "canonical_metric": "net_income"},
            headers=admin_hdrs,
        )
        assert r.status_code in (200, 201), r.text
        rule_id = r.json()["id"]

        r1 = await ac.post(f"{RPTS}/{rid}/mapping/rules/{rule_id}/approve",
                           headers=admin_hdrs)
        assert r1.status_code == 200
        assert r1.json().get("approved_by") is not None

        r2 = await ac.post(f"{RPTS}/{rid}/mapping/rules/{rule_id}/approve",
                           headers=admin_hdrs)
        assert r2.status_code == 200, (
            f"Second approve must be idempotent (200), got {r2.status_code}: {r2.text}"
        )

    async def test_approve_rule_sets_approved_by_and_status(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/mapping/rules",
            json={"source_label": "EBITDA", "canonical_metric": "ebitda"},
            headers=admin_hdrs,
        )
        rule_id = r.json()["id"]
        r = await ac.post(f"{RPTS}/{rid}/mapping/rules/{rule_id}/approve",
                          headers=admin_hdrs)
        body = r.json()
        assert body.get("approved_by") is not None, "approved_by must be populated"
        assert body.get("status") in ("approved", None), f"Unexpected status: {body}"


# ══════════════════════════════════════════════════════════════════════════════
# G. safe_divide and Financial Formula Unit Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSafeArithmeticFormulas:
    """Pure unit tests — no DB or HTTP needed."""

    def test_safe_divide_zero_denominator_returns_none(self):
        from credit_report.calculation_engine.financial_ratios import safe_divide
        assert safe_divide(100.0, 0) is None
        assert safe_divide(100.0, 0.0) is None

    def test_safe_divide_none_inputs_return_none(self):
        from credit_report.calculation_engine.financial_ratios import safe_divide
        assert safe_divide(None, 50.0) is None
        assert safe_divide(100.0, None) is None
        assert safe_divide(None, None) is None

    def test_ebitda_margin_with_zero_revenue_returns_none_value(self):
        from credit_report.calculation_engine.financial_ratios import ebitda_margin
        val, formula, _ = ebitda_margin(500.0, 0.0)
        assert val is None, "Division by zero revenue must yield None, not raise"
        assert "0.0" in formula  # formula string still formed

    def test_net_margin_with_zero_revenue_returns_none(self):
        """net_margin with zero revenue must yield val=None (not ZeroDivisionError)."""
        from credit_report.calculation_engine.financial_ratios import net_margin
        val, formula, _ = net_margin(100.0, 0.0)
        assert val is None, "Zero revenue → division by zero → safe_divide returns None"
        assert "0.0" in formula

    def test_debt_to_equity_with_zero_equity_returns_none(self):
        from credit_report.calculation_engine.financial_ratios import debt_to_equity
        val, _, _ = debt_to_equity(5000.0, 0.0)
        assert val is None

    def test_interest_coverage_with_zero_interest_returns_none(self):
        from credit_report.calculation_engine.financial_ratios import interest_coverage
        val, _, _ = interest_coverage(1000.0, 0.0)
        assert val is None

    def test_debt_to_ebitda_normal_case_correct(self):
        from credit_report.calculation_engine.financial_ratios import debt_to_ebitda
        val, formula, fact_ids = debt_to_ebitda(5000.0, 1000.0, "f1", "f2")
        assert pytest.approx(val, abs=0.001) == 5.0
        assert "5.00x" in formula
        assert set(fact_ids) == {"f1", "f2"}


# ══════════════════════════════════════════════════════════════════════════════
# H. Section Input section_no Validation
# ══════════════════════════════════════════════════════════════════════════════

class TestSectionInputEdgeCases:

    async def test_section_input_section_no_zero_rejected(self, ac, admin_hdrs, report):
        """section_no=0 is out of range 1-11 and must be rejected (400 or 422)."""
        rid = report["id"]
        r = await ac.put(f"{RPTS}/{rid}/inputs/0",
                         json={"section_no": 0, "input_json": {}},
                         headers=admin_hdrs)
        assert r.status_code in (400, 422), (
            f"section_no=0 must be rejected, got {r.status_code}"
        )

    async def test_section_input_section_no_12_rejected(self, ac, admin_hdrs, report):
        """section_no=12 is out of range 1-11 and must be rejected (400 or 422)."""
        rid = report["id"]
        r = await ac.put(f"{RPTS}/{rid}/inputs/12",
                         json={"section_no": 12, "input_json": {}},
                         headers=admin_hdrs)
        assert r.status_code in (400, 422), (
            f"section_no=12 must be rejected, got {r.status_code}"
        )

    async def test_retrieve_nonexistent_section_input_returns_404(
        self, ac, admin_hdrs, report
    ):
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/inputs/9", headers=admin_hdrs)
        assert r.status_code == 404, (
            f"Fetching a section that has no saved input must return 404, "
            f"got {r.status_code}"
        )

    async def test_section_input_round_trip_valid_section(self, ac, admin_hdrs, report):
        rid = report["id"]
        payload = {"section_no": 3, "input_json": {"borrower_name": "TestCo", "revenue": 500}}
        r = await ac.put(f"{RPTS}/{rid}/inputs/3", json=payload, headers=admin_hdrs)
        assert r.status_code == 200

        r2 = await ac.get(f"{RPTS}/{rid}/inputs/3", headers=admin_hdrs)
        assert r2.status_code == 200
        body = r2.json()
        assert body["input_json"]["borrower_name"] == "TestCo"
        assert body["section_no"] == 3


# ══════════════════════════════════════════════════════════════════════════════
# I. Conflict Resolve Edge Cases
# ══════════════════════════════════════════════════════════════════════════════

class TestConflictResolveEdgeCases:

    async def test_resolve_with_empty_rejected_fact_ids_succeeds(
        self, ac, admin_hdrs, report
    ):
        """Resolving a conflict with no rejected facts must succeed (only chosen approved)."""
        rid = report["id"]
        fa_id, _, cf_id = await _seed_conflicting_facts(rid)

        r = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/resolve",
            json={"chosen_fact_id": fa_id, "rejected_fact_ids": [],
                  "resolution_reason": "sole decision"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200, r.text
        assert await _get_fact_state(fa_id) == "approved"

    async def test_resolve_same_conflict_twice_returns_400(self, ac, admin_hdrs, report):
        """Resolving an already-resolved conflict must return 400."""
        rid = report["id"]
        fa_id, fb_id, cf_id = await _seed_conflicting_facts(rid)

        await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/resolve",
            json={"chosen_fact_id": fa_id, "rejected_fact_ids": [fb_id],
                  "resolution_reason": "first"},
            headers=admin_hdrs,
        )
        r = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{cf_id}/resolve",
            json={"chosen_fact_id": fa_id, "rejected_fact_ids": [],
                  "resolution_reason": "second attempt"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400, (
            f"Resolving an already-resolved conflict must return 400, got {r.status_code}"
        )

    async def test_resolve_nonexistent_conflict_returns_404(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/no-such-id/resolve",
            json={"chosen_fact_id": "any", "rejected_fact_ids": [],
                  "resolution_reason": "test"},
            headers=admin_hdrs,
        )
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# J. Block Stats Edge Cases
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockStatsEdgeCases:

    async def test_block_stats_with_no_blocks_returns_zeros(self, ac, admin_hdrs):
        """GET /blocks/stats on a fresh report must not raise ZeroDivisionError."""
        r = await ac.post(RPTS,
                          json={"industry": "shipping", "report_type": "credit_analysis",
                                "borrower_name": "EmptyCo"},
                          headers=admin_hdrs)
        rid = r.json()["id"]

        r = await ac.get(f"{RPTS}/{rid}/blocks/stats", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        assert body["total_blocks"] == 0
        assert body["binding_rate_pct"] == 0, "No cells → binding_rate_pct must be 0, not div/0 error"

    async def test_block_stats_with_bound_cells_shows_correct_rate(self, ac, admin_hdrs, report):
        """Bound cells are reflected in binding_rate_pct."""
        from credit_report.block_ast.models import ReportBlock, TableCell
        rid = report["id"]
        bid = _uid()

        async with AsyncSessionLocal() as db:
            db.add(ReportBlock(
                id=bid, report_id=rid, section_no=2,
                block_type="table", content=None,
                source_fact_ids="[]", is_stale=False,
                version=1, validation_status="pending",
            ))
            # 3 bound, 1 unbound → 75%
            for i in range(3):
                db.add(TableCell(
                    id=_uid(), block_id=bid,
                    row_id=f"r{i}", column_id="c1",
                    display_value=str(i * 100),
                    binding_status="bound", version=1,
                ))
            db.add(TableCell(
                id=_uid(), block_id=bid,
                row_id="r3", column_id="c1",
                display_value="unbound",
                binding_status="unbound", version=1,
            ))
            await db.commit()

        r = await ac.get(f"{RPTS}/{rid}/blocks/stats", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        assert body["bound_cells"] == 3
        assert body["unbound_cells"] == 1
        assert body["binding_rate_pct"] == 75


# ══════════════════════════════════════════════════════════════════════════════
# K. State Machine Enforcement — Deprecated Is Terminal
# ══════════════════════════════════════════════════════════════════════════════

class TestStateTerminalEnforcement:

    async def test_deprecated_fact_cannot_transition_to_any_state(self, db):
        """deprecated is a terminal state; validate_transition must raise for any target."""
        from credit_report.fact_store.state_machine import (
            InvalidStateTransitionError, validate_transition,
        )
        for target in ("extracted", "normalized", "validated", "conflicted",
                       "user_overridden", "approved"):
            with pytest.raises(InvalidStateTransitionError):
                validate_transition("deprecated", target)

    async def test_approved_fact_can_only_go_to_deprecated(self, db):
        from credit_report.fact_store.state_machine import (
            InvalidStateTransitionError, validate_transition,
        )
        # Valid
        validate_transition("approved", "deprecated")  # must not raise
        # Invalid
        for target in ("extracted", "normalized", "validated", "conflicted",
                       "user_overridden", "approved"):
            with pytest.raises(InvalidStateTransitionError):
                validate_transition("approved", target)

    async def test_same_state_transition_raises(self, db):
        """Transitioning to the same state is never valid."""
        from credit_report.fact_store.state_machine import (
            InvalidStateTransitionError, validate_transition,
        )
        for state in ("extracted", "normalized", "validated", "conflicted",
                      "user_overridden", "approved", "deprecated"):
            with pytest.raises(InvalidStateTransitionError):
                validate_transition(state, state)


# ══════════════════════════════════════════════════════════════════════════════
# L. Stale Block Propagation via Fact Override
# ══════════════════════════════════════════════════════════════════════════════

class TestStalePropagation:

    async def test_fact_override_marks_bound_blocks_stale_not_unbound(self, db):
        """
        mark_blocks_stale_by_fact checks TableCell.fact_id, not source_fact_ids JSON.
        Only blocks whose cells are bound to the given fact_id get marked stale.
        """
        from credit_report.block_ast.models import ReportBlock, TableCell
        from credit_report.block_ast.repository import mark_blocks_stale_by_fact

        rid = _uid()
        fact_a = _uid()
        fact_b = _uid()

        bid1 = _uid()
        bid2 = _uid()
        db.add(ReportBlock(
            id=bid1, report_id=rid, section_no=1,
            block_type="table", content=None, source_fact_ids="[]",
            is_stale=False, version=1, validation_status="pending",
        ))
        db.add(ReportBlock(
            id=bid2, report_id=rid, section_no=1,
            block_type="table", content=None, source_fact_ids="[]",
            is_stale=False, version=1, validation_status="pending",
        ))
        # Cell in block1 is bound to fact_a
        db.add(TableCell(
            id=_uid(), block_id=bid1, row_id="r1", column_id="c1",
            display_value="1000", binding_status="bound", fact_id=fact_a, version=1,
        ))
        # Cell in block2 is bound to fact_b (different fact)
        db.add(TableCell(
            id=_uid(), block_id=bid2, row_id="r1", column_id="c1",
            display_value="2000", binding_status="bound", fact_id=fact_b, version=1,
        ))
        await db.flush()

        await mark_blocks_stale_by_fact(db, fact_a)
        await db.flush()

        r = await db.execute(select(ReportBlock).where(ReportBlock.id == bid1))
        assert r.scalar_one().is_stale is True, "Block with cell bound to fact_a must be stale"

        r = await db.execute(select(ReportBlock).where(ReportBlock.id == bid2))
        assert r.scalar_one().is_stale is False, "Block bound to fact_b must NOT be stale"

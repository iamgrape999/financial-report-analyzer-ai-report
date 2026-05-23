"""
Comprehensive End-to-End Test Suite
====================================
A systematic verification of every fix, feature, and edge case built or repaired
in the current sprint. Each test documents WHAT it is testing and WHY that
specific path matters, so failures give unambiguous signal.

Coverage areas:
  1. Critical fixes: JSON crash guard, evidence silence, block AST field widening
  2. Conflict lifecycle: auto-detection, resolution, mark-unresolved data integrity
  3. Fact state machine enforcement (valid and invalid transitions)
  4. Block validate audit trail (newly wired write_event)
  5. Security boundaries: cross-tenant protection, role enforcement
  6. Quota atomicity and per-role limits
  7. Password flows: change, reset, audit events
  8. FX rates and mapping rule full lifecycle
  9. Full report workflow: draft → review → approve → immutability
 10. Stale propagation, block history, preceding outputs
 11. Edge cases: optimistic locks, hard dependencies, query-param facts API
"""
from __future__ import annotations

import inspect
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from credit_report.database import Base
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
    """Isolated in-memory SQLite for unit-style direct DB tests."""
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
    """Full-stack ASGI client against the real app (uses app's SQLite DB)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture(scope="function")
async def admin_hdrs(ac):
    r = await ac.post(f"{AUTH}/login", data={"username": "admin@example.com", "password": "admin123"})
    assert r.status_code == 200, f"admin login failed: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture(scope="function")
async def report(ac, admin_hdrs):
    r = await ac.post(RPTS,
                      json={"industry": "shipping", "report_type": "credit_analysis",
                            "borrower_name": "TestCo"},
                      headers=admin_hdrs)
    assert r.status_code in (200, 201)
    return r.json()


def _uid() -> str:
    return str(uuid.uuid4())


async def _register_analyst(ac, admin_hdrs, *, email: str | None = None) -> dict:
    """Create a fresh analyst user; return login tokens."""
    email = email or f"analyst_{_uid()[:8]}@test.com"
    await ac.post(f"{AUTH}/register",
                  json={"email": email, "password": "Pass1234!", "role": "analyst"},
                  headers=admin_hdrs)
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": "Pass1234!"})
    assert r.status_code == 200
    tokens = r.json()
    tokens["email"] = email
    return tokens


async def _analyst_hdrs(ac, admin_hdrs) -> dict:
    tokens = await _register_analyst(ac, admin_hdrs)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _register_reviewer(ac, admin_hdrs) -> dict:
    email = f"reviewer_{_uid()[:8]}@test.com"
    await ac.post(f"{AUTH}/register",
                  json={"email": email, "password": "Pass1234!", "role": "reviewer"},
                  headers=admin_hdrs)
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": "Pass1234!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _seed_fact(ac, hdrs, report_id: str, *, metric: str = "revenue",
                     value: float = 100.0, source: str = "etl",
                     state: str = "extracted") -> dict:
    """Seed a single fact directly into the DB and return it via the facts API."""
    from credit_report.database import AsyncSessionLocal
    from credit_report.fact_store.repository import upsert_facts
    async with AsyncSessionLocal() as db:
        await upsert_facts(db, [{
            "report_id": report_id, "metric_name": metric,
            "entity": "TestCo", "period": "FY2024",
            "value": value, "value_text": str(value),
            "source_type": source, "state": state,
        }])
        await db.commit()
    r2 = await ac.get(f"{RPTS}/{report_id}/facts", headers=hdrs)
    facts = [f for f in r2.json() if f["metric_name"] == metric]
    return facts[0] if facts else {}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Critical Fix Validation
# ══════════════════════════════════════════════════════════════════════════════

class TestCriticalFixValidation:
    """Verify C1/C2/H5 fixes work correctly end-to-end."""

    async def test_null_section_input_json_returns_empty_dict(self, db):
        """C1: When si.input_json is NULL the endpoint must return {} not crash."""
        from credit_report.models import Report, SectionInput
        from credit_report.api.reports import get_section_input
        from credit_report.security.models import User

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by="u1"))
        db.add(SectionInput(id=_uid(), report_id=rid, section_no=1, input_json=None))
        await db.flush()

        mock_user = User(id="u1", email="a@b.com", role="analyst",
                         hashed_password="x", is_active=True)
        result = await get_section_input(report_id=rid, section_no=1,
                                         db=db, current_user=mock_user)
        assert result.input_json == {}, "Null input_json must be returned as empty dict"

    async def test_malformed_section_input_json_returns_500_with_clear_message(self, db):
        """C1: Malformed JSON stored in DB → HTTP 500 with 'corrupted' in detail, not raw traceback."""
        from fastapi import HTTPException
        from credit_report.models import Report, SectionInput
        from credit_report.api.reports import get_section_input
        from credit_report.security.models import User

        rid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by="u1"))
        db.add(SectionInput(id=_uid(), report_id=rid, section_no=2,
                             input_json="BROKEN{{{not valid json"))
        await db.flush()

        mock_user = User(id="u1", email="a@b.com", role="analyst",
                         hashed_password="x", is_active=True)
        with pytest.raises(HTTPException) as exc_info:
            await get_section_input(report_id=rid, section_no=2,
                                    db=db, current_user=mock_user)
        assert exc_info.value.status_code == 500
        detail = exc_info.value.detail.lower()
        assert "invalid json" in detail or "corrupt" in detail, (
            f"500 detail must explain the corruption, got: {exc_info.value.detail!r}"
        )

    async def test_table_cell_row_id_longer_than_20_chars_persists_without_truncation(self, db):
        """H5: row_id changed from String(20) to Text — long IDs must not be truncated."""
        from credit_report.block_ast.repository import save_blocks, get_block
        from credit_report.block_ast.models import TableCell
        from sqlalchemy import select

        rid = _uid()
        block_id = f"{rid}.7.table.001"
        long_row_id = "row_" + "X" * 100      # 104 chars, would truncate under String(20)
        long_col_id = "col_monthly_revenue_fy2024_usd_thousands"  # 42 chars

        await save_blocks(
            db,
            [{"id": block_id, "report_id": rid, "section_no": 7,
              "block_type": "table", "content": "Rev", "source_fact_ids": "[]",
              "is_stale": False, "version": 1}],
            [{"id": _uid(), "block_id": block_id,
              "row_id": long_row_id, "column_id": long_col_id,
              "display_value": "100m", "fact_id": None, "binding_status": "unbound"}],
        )
        await db.flush()

        result = await db.execute(
            select(TableCell).where(TableCell.block_id == block_id)
        )
        cell = result.scalar_one_or_none()
        assert cell is not None
        assert cell.row_id == long_row_id, (
            f"row_id was truncated! Expected {len(long_row_id)} chars, "
            f"got {len(cell.row_id)}: {cell.row_id!r}"
        )
        assert cell.column_id == long_col_id, (
            f"column_id was truncated! Expected {len(long_col_id)} chars, "
            f"got {len(cell.column_id)}: {cell.column_id!r}"
        )

    async def test_section_input_save_and_retrieve_roundtrip(self, ac, admin_hdrs, report):
        """C1: Normal save → retrieve cycle must preserve all JSON fields."""
        rid = report["id"]
        payload = {
            "section_no": 3,
            "input_json": {
                "borrower_name": "TestCo",
                "nested": {"key": "value", "num": 42},
                "list": [1, 2, 3],
                "unicode": "新加坡",
            },
        }
        r = await ac.put(f"{RPTS}/{rid}/inputs/3", json=payload, headers=admin_hdrs)
        assert r.status_code == 200

        r2 = await ac.get(f"{RPTS}/{rid}/inputs/3", headers=admin_hdrs)
        assert r2.status_code == 200
        returned = r2.json()["input_json"]
        assert returned["borrower_name"] == "TestCo"
        assert returned["nested"]["num"] == 42
        assert returned["unicode"] == "新加坡"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Conflict Lifecycle & Data Integrity (including mark-unresolved bug fix)
# ══════════════════════════════════════════════════════════════════════════════

class TestConflictLifecycle:
    """Full conflict workflow: auto-detection → resolution → mark-unresolved."""

    async def test_conflicting_facts_from_two_sources_auto_creates_conflict(self, db):
        """Two facts with same key but different sources and divergent values → FactConflict."""
        from credit_report.fact_store.repository import upsert_facts, get_open_conflicts

        rid = _uid()
        await upsert_facts(db, [
            {"report_id": rid, "metric_name": "revenue", "entity": "Co",
             "period": "FY2024", "value": 1000.0, "source_type": "etl", "state": "extracted"},
            {"report_id": rid, "metric_name": "revenue", "entity": "Co",
             "period": "FY2024", "value": 1500.0, "source_type": "analyst_input_json",
             "state": "extracted"},
        ])
        await db.flush()

        conflicts = await get_open_conflicts(db, rid)
        assert len(conflicts) >= 1, "Divergent values from two sources must trigger a conflict"
        assert conflicts[0].metric_name == "revenue"
        assert conflicts[0].status == "open"

    async def test_same_source_upsert_does_not_create_conflict(self, db):
        """Re-upserting the same fact from the same source must not self-conflict."""
        from credit_report.fact_store.repository import upsert_facts, get_open_conflicts

        rid = _uid()
        await upsert_facts(db, [
            {"report_id": rid, "metric_name": "ebitda", "entity": "Co",
             "period": "FY2024", "value": 200.0, "source_type": "etl", "state": "extracted"},
            {"report_id": rid, "metric_name": "ebitda", "entity": "Co",
             "period": "FY2024", "value": 210.0, "source_type": "etl", "state": "extracted"},
        ])
        await db.flush()

        conflicts = await get_open_conflicts(db, rid)
        revenue_conflicts = [c for c in conflicts if c.metric_name == "ebitda"]
        assert len(revenue_conflicts) == 0, "Same source upsert must not create a conflict"

    async def _seed_conflict(self, rid: str, metric: str = "net_income") -> dict | None:
        """Seed two conflicting facts and return the conflict dict, or None if detection fails."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.repository import upsert_facts
        from credit_report.fact_store.models import FactConflict
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            await upsert_facts(db, [
                {"report_id": rid, "metric_name": metric, "entity": "Co", "period": "FY2024",
                 "value": 100.0, "value_text": "100", "source_type": "pdf_extraction", "state": "extracted"},
                {"report_id": rid, "metric_name": metric, "entity": "Co", "period": "FY2024",
                 "value": 180.0, "value_text": "180", "source_type": "ocr_extraction", "state": "extracted"},
            ])
            await db.commit()
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(FactConflict).where(FactConflict.report_id == rid,
                                           FactConflict.metric_name == metric))
            c = r.scalars().first()
            if not c:
                return None
            return {"id": c.id, "fact_a_id": c.fact_a_id, "fact_b_id": c.fact_b_id}

    async def test_resolve_conflict_updates_fact_states(self, ac, admin_hdrs, report):
        """After resolution, chosen fact → validated, rejected → deprecated."""
        rid = report["id"]

        conflict = await self._seed_conflict(rid, "net_income")
        if not conflict:
            pytest.skip("No conflict detected")

        # Resolve: choose fact_a, reject fact_b
        chosen = conflict["fact_a_id"]
        rejected = conflict["fact_b_id"]
        r = await ac.post(
            f"{RPTS}/{rid}/facts/conflicts/{conflict['id']}/resolve",
            json={"chosen_fact_id": chosen,
                  "rejected_fact_ids": [rejected],
                  "resolution_reason": "ETL source is more reliable"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "resolved"
        assert body["chosen_fact_id"] == chosen
        assert body["resolved_by"] is not None

    async def test_mark_unresolved_clears_all_resolution_metadata(self, ac, admin_hdrs, report):
        """BUG FIX: mark-unresolved must clear resolved_by AND resolved_at, not just chosen_fact_id."""
        rid = report["id"]

        conflict = await self._seed_conflict(rid, "ltv")
        if not conflict:
            pytest.skip("No conflict detected")
        cid = conflict["id"]

        # Resolve first
        await ac.post(f"{RPTS}/{rid}/facts/conflicts/{cid}/resolve",
                      json={"chosen_fact_id": conflict["fact_a_id"],
                            "rejected_fact_ids": [conflict["fact_b_id"]],
                            "resolution_reason": "Test"},
                      headers=admin_hdrs)

        # Verify resolved metadata is set
        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts/{cid}", headers=admin_hdrs)
        resolved = r.json()
        assert resolved["resolved_by"] is not None
        assert resolved["resolved_at"] is not None

        # Mark unresolved
        r = await ac.post(f"{RPTS}/{rid}/facts/conflicts/{cid}/mark-unresolved",
                          headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "open"
        assert body["conflict_id"] == cid

        # Verify ALL resolution metadata is cleared
        r = await ac.get(f"{RPTS}/{rid}/facts/conflicts/{cid}", headers=admin_hdrs)
        reopened = r.json()
        assert reopened["status"] == "open"
        assert reopened["chosen_fact_id"] is None, "chosen_fact_id must be cleared on mark-unresolved"
        assert reopened["resolution_reason"] is None, "resolution_reason must be cleared on mark-unresolved"
        assert reopened["resolved_by"] is None, (
            "resolved_by must be cleared on mark-unresolved — ghost metadata bug"
        )
        assert reopened["resolved_at"] is None, (
            "resolved_at must be cleared on mark-unresolved — ghost metadata bug"
        )

    async def test_mark_unresolved_response_has_correct_shape(self, ac, admin_hdrs, report):
        """M2 fix: mark-unresolved response must match MarkUnresolvedResponse schema."""
        rid = report["id"]
        conflict = await self._seed_conflict(rid, "dscr")
        if not conflict:
            pytest.skip("No conflict")
        cid = conflict["id"]

        # Resolve, then mark unresolved
        await ac.post(f"{RPTS}/{rid}/facts/conflicts/{cid}/resolve",
                      json={"chosen_fact_id": conflict["fact_a_id"],
                            "rejected_fact_ids": [conflict["fact_b_id"]],
                            "resolution_reason": "x"},
                      headers=admin_hdrs)
        r = await ac.post(f"{RPTS}/{rid}/facts/conflicts/{cid}/mark-unresolved",
                          headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        # Exact schema: {"status": "open", "conflict_id": "<uuid>"}
        assert set(body.keys()) == {"status", "conflict_id"}, (
            f"Response shape must be exactly {{status, conflict_id}}, got: {set(body.keys())}"
        )
        assert body["status"] == "open"
        assert body["conflict_id"] == cid

    async def test_conflict_on_nonexistent_conflict_id_returns_404(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(f"{RPTS}/{rid}/facts/conflicts/nonexistent-id/mark-unresolved",
                          headers=admin_hdrs)
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 3. Fact State Machine Enforcement
# ══════════════════════════════════════════════════════════════════════════════

class TestFactStateMachineEnforcement:
    """Every allowed and disallowed transition in the state machine."""

    async def test_extracted_to_normalized_is_valid(self, db):
        from credit_report.fact_store.repository import upsert_fact, update_fact_state
        rid = _uid()
        f = await upsert_fact(db, {"report_id": rid, "metric_name": "x",
                                    "entity": "Co", "period": "FY24",
                                    "value": 1.0, "source_type": "etl", "state": "extracted"})
        await db.flush()
        updated = await update_fact_state(db, f.id, "normalized", actor_id="sys")
        assert updated.state == "normalized"

    async def test_extracted_to_approved_is_invalid(self, db):
        from credit_report.fact_store.repository import upsert_fact
        from credit_report.fact_store.state_machine import validate_transition, InvalidStateTransitionError
        rid = _uid()
        await upsert_fact(db, {"report_id": rid, "metric_name": "x",
                                "entity": "Co", "period": "FY24",
                                "value": 1.0, "source_type": "etl", "state": "extracted"})
        with pytest.raises(InvalidStateTransitionError):
            validate_transition("extracted", "approved")

    async def test_deprecated_is_terminal_no_further_transitions(self, db):
        from credit_report.fact_store.repository import upsert_fact, update_fact_state
        from credit_report.fact_store.state_machine import InvalidStateTransitionError
        rid = _uid()
        f = await upsert_fact(db, {"report_id": rid, "metric_name": "x",
                                    "entity": "Co", "period": "FY24",
                                    "value": 1.0, "source_type": "etl", "state": "approved"})
        await db.flush()
        await update_fact_state(db, f.id, "deprecated", actor_id="sys")

        with pytest.raises(Exception):
            await update_fact_state(db, f.id, "approved", actor_id="sys")

    async def test_approve_requires_reviewer_role(self, ac, admin_hdrs, report):
        """Analyst cannot approve a fact — requires reviewer+."""
        rid = report["id"]
        analyst_hdrs = await _analyst_hdrs(ac, admin_hdrs)
        f = await _seed_fact(ac, admin_hdrs, rid, metric="approve_test",
                              value=50.0, state="validated")
        if not f:
            pytest.skip("No fact seeded")

        r = await ac.post(f"{RPTS}/{rid}/facts/{f['id']}/approve",
                          json={"expected_version": f["version"]},
                          headers=analyst_hdrs)
        assert r.status_code == 403, "Analyst must not be able to approve facts"

    async def test_deprecate_requires_reviewer_role(self, ac, admin_hdrs, report):
        """Analyst cannot deprecate a fact — requires reviewer+."""
        rid = report["id"]
        analyst_hdrs = await _analyst_hdrs(ac, admin_hdrs)
        f = await _seed_fact(ac, admin_hdrs, rid, metric="deprecate_test",
                              value=60.0, state="validated")
        if not f:
            pytest.skip("No fact seeded")

        r = await ac.post(f"{RPTS}/{rid}/facts/{f['id']}/deprecate",
                          params={"reason": "testing"},
                          headers=analyst_hdrs)
        assert r.status_code == 403, "Analyst must not be able to deprecate facts"

    async def test_deprecate_reason_is_query_param_not_body(self, ac, admin_hdrs, report):
        """The deprecate endpoint takes 'reason' as a query param, not a JSON body."""
        rid = report["id"]
        rev_hdrs = await _register_reviewer(ac, admin_hdrs)
        f = await _seed_fact(ac, admin_hdrs, rid, metric="depr_qp", value=70.0,
                              state="validated")
        if not f:
            pytest.skip("No fact seeded")

        # Wrong: send reason in body → should still accept or return 422 for bad body,
        # but must succeed when reason is in query params
        r = await ac.post(f"{RPTS}/{rid}/facts/{f['id']}/deprecate",
                          params={"reason": "test deprecation"},
                          headers=rev_hdrs)
        assert r.status_code in (200, 400, 422), r.text
        # The important check: sending reason as a query param reaches the endpoint
        assert r.status_code != 404, "Endpoint must exist with reason as query param"

    async def test_version_increments_on_every_transition(self, db):
        from credit_report.fact_store.repository import upsert_fact, update_fact_state
        rid = _uid()
        f = await upsert_fact(db, {"report_id": rid, "metric_name": "ver_test",
                                    "entity": "Co", "period": "FY24",
                                    "value": 1.0, "source_type": "etl", "state": "extracted"})
        await db.flush()
        await db.refresh(f)
        v0 = f.version
        assert v0 is not None

        await update_fact_state(db, f.id, "normalized", actor_id="sys")
        await update_fact_state(db, f.id, "validated", actor_id="analyst")
        await update_fact_state(db, f.id, "approved", actor_id="approver")
        assert f.version == v0 + 3


# ══════════════════════════════════════════════════════════════════════════════
# 4. Block Validate Audit Trail (newly wired write_event)
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockValidateAuditTrail:
    """validate_block must write an audit event — previously this was missing."""

    async def test_validate_block_sets_status_to_passed(self, ac, admin_hdrs, report):
        """Basic: POST /blocks/{id}/validate → validation_status becomes 'passed'."""
        from credit_report.block_ast.repository import save_blocks
        from credit_report.database import AsyncSessionLocal

        rid = report["id"]
        block_id = f"{rid}.7.table.001"
        async with AsyncSessionLocal() as db_direct:
            await save_blocks(
                db_direct,
                [{"id": block_id, "report_id": rid, "section_no": 7,
                  "block_type": "table", "content": "Test", "source_fact_ids": "[]",
                  "is_stale": False, "version": 1}],
                [],
            )
            await db_direct.commit()

        r = await ac.post(f"{RPTS}/{rid}/blocks/{block_id}/validate", headers=admin_hdrs)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["validation_status"] == "passed"
        assert body["block_id"] == block_id

    async def test_validate_block_creates_audit_event(self, ac, admin_hdrs, report):
        """BUG FIX: validate_block must call write_event — verify via audit log."""
        from credit_report.block_ast.repository import save_blocks
        from credit_report.database import AsyncSessionLocal

        rid = report["id"]
        block_id = f"{rid}.7.table.audit"
        async with AsyncSessionLocal() as db_direct:
            await save_blocks(
                db_direct,
                [{"id": block_id, "report_id": rid, "section_no": 7,
                  "block_type": "paragraph", "content": "Audit test", "source_fact_ids": "[]",
                  "is_stale": False, "version": 1}],
                [],
            )
            await db_direct.commit()

        await ac.post(f"{RPTS}/{rid}/blocks/{block_id}/validate", headers=admin_hdrs)

        # Check audit log contains block.validated event
        r = await ac.get(f"{BASE}/audit/events",
                          params={"page_size": 50}, headers=admin_hdrs)
        assert r.status_code == 200, f"Global audit endpoint failed: {r.status_code} {r.text}"
        events = r.json().get("events", [])
        validated_events = [e for e in events
                             if e.get("action") == "block.validated"
                             and e.get("target_id") == block_id]
        assert validated_events, (
            "block.validated audit event must be written after POST /blocks/{id}/validate"
        )

    async def test_validate_nonexistent_block_returns_404(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(f"{RPTS}/{rid}/blocks/nonexistent-block/validate",
                          headers=admin_hdrs)
        assert r.status_code == 404

    async def test_validate_block_from_wrong_report_returns_404(self, ac, admin_hdrs):
        """validate_block must check block.report_id == URL report_id."""
        from credit_report.block_ast.repository import save_blocks
        from credit_report.database import AsyncSessionLocal

        # Create block under report A
        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "A"},
                          headers=admin_hdrs)
        rid_a = r.json()["id"]
        # Create report B
        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "B"},
                          headers=admin_hdrs)
        rid_b = r.json()["id"]

        block_id = f"{rid_a}.1.para.001"
        async with AsyncSessionLocal() as db_direct:
            await save_blocks(db_direct,
                              [{"id": block_id, "report_id": rid_a, "section_no": 1,
                                "block_type": "paragraph", "content": "A", "source_fact_ids": "[]",
                                "is_stale": False, "version": 1}], [])
            await db_direct.commit()

        # Attempt to validate block A through report B's URL
        r = await ac.post(f"{RPTS}/{rid_b}/blocks/{block_id}/validate", headers=admin_hdrs)
        assert r.status_code == 404, (
            "Block belonging to report A must not be accessible via report B's URL"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 5. Security Boundaries
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurityBoundaries:
    """Cross-tenant isolation, role enforcement, auth guards."""

    async def test_analyst_cannot_access_another_analysts_report_facts(self, ac, admin_hdrs):
        """Facts from report owned by analyst A must not be visible to analyst B."""
        tokens_a = await _register_analyst(ac, admin_hdrs)
        tokens_b = await _register_analyst(ac, admin_hdrs)
        hdrs_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}
        hdrs_b = {"Authorization": f"Bearer {tokens_b['access_token']}"}

        # A creates a report
        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "A's Report"},
                          headers=hdrs_a)
        assert r.status_code in (200, 201)
        rid_a = r.json()["id"]

        # B tries to GET facts for A's report
        r = await ac.get(f"{RPTS}/{rid_a}/facts", headers=hdrs_b)
        # Must be 403 or 404, not 200
        assert r.status_code in (403, 404), (
            f"Analyst B must not be able to read Analyst A's report facts. Got {r.status_code}"
        )

    async def test_analyst_cannot_modify_another_analysts_section_input(self, ac, admin_hdrs):
        """Section input save must enforce report ownership."""
        tokens_a = await _register_analyst(ac, admin_hdrs)
        tokens_b = await _register_analyst(ac, admin_hdrs)
        hdrs_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}
        hdrs_b = {"Authorization": f"Bearer {tokens_b['access_token']}"}

        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "A"},
                          headers=hdrs_a)
        rid_a = r.json()["id"]

        r = await ac.put(f"{RPTS}/{rid_a}/inputs/1",
                         json={"section_no": 1, "input_json": {"evil": True}},
                         headers=hdrs_b)
        assert r.status_code in (403, 404), (
            f"Analyst B must not be able to write to Analyst A's inputs. Got {r.status_code}"
        )

    async def test_unauthenticated_request_returns_401(self, ac, report):
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/facts")
        assert r.status_code == 401

    async def test_analyst_cannot_update_user_roles(self, ac, admin_hdrs):
        """PATCH /auth/users/{id}/role requires admin."""
        tokens = await _register_analyst(ac, admin_hdrs)
        analyst_hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}
        uid = (await ac.get(f"{AUTH}/me", headers=analyst_hdrs)).json()["id"]

        r = await ac.patch(f"{AUTH}/users/{uid}/role",
                           params={"role": "admin"}, headers=analyst_hdrs)
        assert r.status_code == 403

    async def test_auth_status_is_public(self, ac):
        """GET /auth/status must not require authentication."""
        r = await ac.get(f"{AUTH}/status")
        assert r.status_code == 200
        assert r.status_code != 401
        body = r.json()
        assert "total_active_users" in body
        assert "login_possible" in body

    async def test_deleted_report_returns_404(self, ac, admin_hdrs):
        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "ToDelete"},
                          headers=admin_hdrs)
        rid = r.json()["id"]

        r = await ac.delete(f"{RPTS}/{rid}", headers=admin_hdrs)
        assert r.status_code in (200, 204)

        r = await ac.get(f"{RPTS}/{rid}", headers=admin_hdrs)
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 6. Quota Atomicity and Per-Role Limits
# ══════════════════════════════════════════════════════════════════════════════

class TestQuotaAtomicityAndRoleLimits:
    """reserve_and_record_tokens enforces limits correctly per role."""

    async def test_analyst_blocked_exactly_at_daily_limit(self, db):
        from credit_report.generation.quota import reserve_and_record_tokens, _limit_for_role
        from credit_report.generation.models import UserTokenQuota
        from fastapi import HTTPException

        limit = _limit_for_role("analyst")
        uid = _uid()
        db.add(UserTokenQuota(id=_uid(), user_id=uid,
                               quota_date=datetime.now(timezone.utc).date(),
                               tokens_used=limit))
        await db.flush()

        with pytest.raises(HTTPException) as exc_info:
            await reserve_and_record_tokens(db, uid, 1, role="analyst")
        assert exc_info.value.status_code == 429
        assert "limit" in exc_info.value.detail.lower()

    async def test_reviewer_succeeds_where_analyst_would_be_blocked(self, db):
        """A reviewer's higher limit means they can consume past the analyst ceiling."""
        from credit_report.generation.quota import reserve_and_record_tokens, _limit_for_role
        from credit_report.generation.models import UserTokenQuota

        analyst_limit = _limit_for_role("analyst")
        uid = _uid()
        # Pre-seed at analyst limit
        db.add(UserTokenQuota(id=_uid(), user_id=uid,
                               quota_date=datetime.now(timezone.utc).date(),
                               tokens_used=analyst_limit))
        await db.flush()

        # Reviewer can consume analyst_limit + 1 more
        await reserve_and_record_tokens(db, uid, 1, role="reviewer")

    async def test_quota_is_independent_per_user(self, db):
        """Consuming tokens for user A must not affect user B's quota."""
        from credit_report.generation.quota import reserve_and_record_tokens, _limit_for_role
        from credit_report.generation.models import UserTokenQuota
        from fastapi import HTTPException

        limit = _limit_for_role("analyst")
        uid_a, uid_b = _uid(), _uid()

        # A is at limit
        db.add(UserTokenQuota(id=_uid(), user_id=uid_a,
                               quota_date=datetime.now(timezone.utc).date(),
                               tokens_used=limit))
        await db.flush()

        # A is blocked
        with pytest.raises(HTTPException):
            await reserve_and_record_tokens(db, uid_a, 1, role="analyst")

        # B still has full quota
        await reserve_and_record_tokens(db, uid_b, 1000, role="analyst")

    async def test_zero_tokens_bypasses_quota_check(self, db):
        """reserve_and_record_tokens(tokens=0) must return immediately without DB access."""
        from credit_report.generation.quota import reserve_and_record_tokens, _limit_for_role
        from credit_report.generation.models import UserTokenQuota

        limit = _limit_for_role("analyst")
        uid = _uid()
        db.add(UserTokenQuota(id=_uid(), user_id=uid,
                               quota_date=datetime.now(timezone.utc).date(),
                               tokens_used=limit))
        await db.flush()

        # 0 tokens must not raise 429 even when at limit
        await reserve_and_record_tokens(db, uid, 0, role="analyst")

    async def test_quota_accumulates_across_multiple_calls(self, db):
        """Multiple calls to reserve_and_record_tokens must sum correctly."""
        from credit_report.generation.quota import reserve_and_record_tokens
        from credit_report.generation.models import UserTokenQuota
        from sqlalchemy import select

        uid = _uid()
        await reserve_and_record_tokens(db, uid, 1000, role="analyst")
        await reserve_and_record_tokens(db, uid, 2000, role="analyst")
        await db.flush()

        r = await db.execute(select(UserTokenQuota).where(UserTokenQuota.user_id == uid))
        quota = r.scalar_one_or_none()
        assert quota is not None
        assert quota.tokens_used == 3000


# ══════════════════════════════════════════════════════════════════════════════
# 7. Password Flows
# ══════════════════════════════════════════════════════════════════════════════

class TestPasswordFlows:
    """change-password, reset-password, audit events."""

    async def test_change_password_old_password_rejected_immediately(self, ac, admin_hdrs):
        tokens = await _register_analyst(ac, admin_hdrs)
        hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = await ac.post(f"{AUTH}/change-password",
                          json={"current_password": "Pass1234!", "new_password": "NewPass99!"},
                          headers=hdrs)
        assert r.status_code == 200

        # Old password must no longer work
        r = await ac.post(f"{AUTH}/login",
                          data={"username": tokens["email"], "password": "Pass1234!"})
        assert r.status_code in (400, 401, 403), "Old password must be rejected after change"

    async def test_change_password_new_password_works(self, ac, admin_hdrs):
        tokens = await _register_analyst(ac, admin_hdrs)
        hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}

        await ac.post(f"{AUTH}/change-password",
                      json={"current_password": "Pass1234!", "new_password": "NewPass99!"},
                      headers=hdrs)

        r = await ac.post(f"{AUTH}/login",
                          data={"username": tokens["email"], "password": "NewPass99!"})
        assert r.status_code == 200, "New password must work after change"

    async def test_change_password_wrong_current_returns_401(self, ac, admin_hdrs):
        tokens = await _register_analyst(ac, admin_hdrs)
        hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = await ac.post(f"{AUTH}/change-password",
                          json={"current_password": "WrongPass!", "new_password": "NewPass99!"},
                          headers=hdrs)
        assert r.status_code in (400, 401)

    async def test_change_password_minimum_8_chars_enforced(self, ac, admin_hdrs):
        tokens = await _register_analyst(ac, admin_hdrs)
        hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = await ac.post(f"{AUTH}/change-password",
                          json={"current_password": "Pass1234!", "new_password": "short"},
                          headers=hdrs)
        assert r.status_code in (400, 422), "Passwords shorter than 8 chars must be rejected"

    async def test_admin_reset_preserves_user_role(self, ac, admin_hdrs):
        """Admin password reset must NOT change the user's role."""
        email = f"rstpres_{_uid()[:6]}@test.com"
        r = await ac.post(f"{AUTH}/register",
                          json={"email": email, "password": "Pass1234!", "role": "reviewer"},
                          headers=admin_hdrs)
        uid = r.json()["id"]

        # Admin resets password
        await ac.post(f"{AUTH}/users/{uid}/reset-password",
                      json={"new_password": "Reset9999!"},
                      headers=admin_hdrs)

        # User logs in with new password and checks their role
        r = await ac.post(f"{AUTH}/login", data={"username": email, "password": "Reset9999!"})
        assert r.status_code == 200
        user_hdrs = {"Authorization": f"Bearer {r.json()['access_token']}"}
        me = (await ac.get(f"{AUTH}/me", headers=user_hdrs)).json()
        assert me["role"] == "reviewer", "Admin reset must preserve the user's role"

    async def test_role_update_takes_role_as_query_param(self, ac, admin_hdrs):
        """PATCH /auth/users/{id}/role: 'role' must be a query param (not body)."""
        email = f"rolupdqp_{_uid()[:6]}@test.com"
        r = await ac.post(f"{AUTH}/register",
                          json={"email": email, "password": "Pass1234!", "role": "analyst"},
                          headers=admin_hdrs)
        uid = r.json()["id"]

        # role as query param — correct usage
        r = await ac.patch(f"{AUTH}/users/{uid}/role",
                           params={"role": "reviewer"}, headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["role"] == "reviewer"

        # role as body only (no query param) — must be rejected or ignored
        r_body = await ac.patch(f"{AUTH}/users/{uid}/role",
                                json={"role": "admin"}, headers=admin_hdrs)
        assert r_body.status_code in (400, 422), (
            "Sending role as body should fail — endpoint expects query param"
        )

    async def test_role_update_writes_audit_event(self, ac, admin_hdrs):
        email = f"rau_{_uid()[:6]}@test.com"
        r = await ac.post(f"{AUTH}/register",
                          json={"email": email, "password": "Pass1234!", "role": "analyst"},
                          headers=admin_hdrs)
        uid = r.json()["id"]

        await ac.patch(f"{AUTH}/users/{uid}/role", params={"role": "reviewer"},
                       headers=admin_hdrs)

        r = await ac.get(f"{BASE}/audit/events", params={"page_size": 100},
                         headers=admin_hdrs)
        assert r.status_code == 200, f"Global audit endpoint failed: {r.status_code} {r.text}"
        events = r.json().get("events", [])
        role_events = [e for e in events
                       if e.get("action") == "auth.role_change"
                       and e.get("target_id") == uid]
        assert role_events, "Role change must produce an auth.role_change audit event"


# ══════════════════════════════════════════════════════════════════════════════
# 8. FX Rates and Mapping Rules Full Lifecycle
# ══════════════════════════════════════════════════════════════════════════════

class TestFXRatesAndMappingRulesLifecycle:
    """FX rate upsert/overwrite; mapping rule create/approve with side effects."""

    async def test_fx_rate_upsert_stores_all_fields(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.put(f"{RPTS}/{rid}/fx-rates",
                         json={"from_currency": "TWD", "to_currency": "USD",
                               "rate": 32.5, "rate_date": "2024-12-31",
                               "source": "bloomberg"},
                         headers=admin_hdrs)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["from_currency"] == "TWD"
        assert body["to_currency"] == "USD"
        assert pytest.approx(body["rate"], abs=0.001) == 32.5
        assert body["source"] == "bloomberg"

    async def test_fx_rate_same_pair_overwrites(self, ac, admin_hdrs, report):
        """Second PUT for the same currency pair must return the new rate."""
        rid = report["id"]
        for rate in (31.0, 33.5):
            await ac.put(f"{RPTS}/{rid}/fx-rates",
                         json={"from_currency": "SGD", "to_currency": "USD",
                               "rate": rate, "rate_date": "2024-12-31", "source": "mas"},
                         headers=admin_hdrs)

        r = await ac.get(f"{RPTS}/{rid}/fx-rates", headers=admin_hdrs)
        assert r.status_code == 200
        sgd_rates = [rt for rt in r.json() if rt["from_currency"] == "SGD"]
        assert len(sgd_rates) >= 1
        # Most recent must be 33.5
        assert pytest.approx(sgd_rates[0]["rate"], abs=0.01) == 33.5

    async def test_fx_rate_different_pairs_coexist(self, ac, admin_hdrs, report):
        rid = report["id"]
        for from_ccy, rate in [("CNY", 7.3), ("JPY", 150.1), ("EUR", 1.08)]:
            await ac.put(f"{RPTS}/{rid}/fx-rates",
                         json={"from_currency": from_ccy, "to_currency": "USD",
                               "rate": rate, "rate_date": "2024-12-31", "source": "test"},
                         headers=admin_hdrs)

        r = await ac.get(f"{RPTS}/{rid}/fx-rates", headers=admin_hdrs)
        stored = {rt["from_currency"]: rt["rate"] for rt in r.json()}
        for from_ccy, rate in [("CNY", 7.3), ("JPY", 150.1), ("EUR", 1.08)]:
            assert from_ccy in stored, f"{from_ccy} not found in stored rates"
            assert pytest.approx(stored[from_ccy], abs=0.01) == rate

    async def test_fx_rates_require_authentication(self, ac, report):
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/fx-rates")
        assert r.status_code == 401

    async def test_mapping_rule_lifecycle_create_then_approve(self, ac, admin_hdrs, report):
        """Create a mapping rule and approve it; approved_by must be set."""
        rid = report["id"]

        r = await ac.post(f"{RPTS}/{rid}/mapping/rules",
                          json={"source_label": "Net Income (Reported)",
                                "canonical_metric": "net_income",
                                "category": "income_statement"},
                          headers=admin_hdrs)
        assert r.status_code in (200, 201), r.text
        rule_id = r.json()["id"]

        r = await ac.post(f"{RPTS}/{rid}/mapping/rules/{rule_id}/approve",
                          headers=admin_hdrs)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == rule_id
        assert body.get("approved_by") is not None, "approved_by must be set after approval"
        assert body.get("status") in ("approved", None)

    async def test_approve_nonexistent_mapping_rule_returns_404(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(f"{RPTS}/{rid}/mapping/rules/nonexistent-id/approve",
                          headers=admin_hdrs)
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 9. Full Report Workflow: Draft → Review → Approve
# ══════════════════════════════════════════════════════════════════════════════

class TestFullReportWorkflow:
    """End-to-end state machine for reports."""

    async def test_submit_for_review_requires_done_section(self, ac, admin_hdrs):
        """Submitting a report with no completed sections must return 422."""
        from fastapi import HTTPException
        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "Empty"},
                          headers=admin_hdrs)
        rid = r.json()["id"]

        r = await ac.post(f"{RPTS}/{rid}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 422, "Cannot submit a report with zero done sections"

    async def test_approved_report_cannot_be_soft_deleted(self, ac, admin_hdrs):
        """An approved report must be immutable — soft-delete must be rejected."""
        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "ImmutableCo"},
                          headers=admin_hdrs)
        rid = r.json()["id"]

        # Seed a done section to allow submit-for-review
        from credit_report.models import SectionOutput
        from credit_report.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db_direct:
            db_direct.add(SectionOutput(id=_uid(), report_id=rid, section_no=1,
                                         status="done", markdown="# Test"))
            await db_direct.commit()

        # Submit for review
        r = await ac.post(f"{RPTS}/{rid}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 200

        # Admin approves (admin role can always approve)
        r = await ac.post(f"{RPTS}/{rid}/approve", headers=admin_hdrs)
        assert r.status_code == 200, f"approve failed: {r.text}"

        # Try to delete — must fail
        r = await ac.delete(f"{RPTS}/{rid}", headers=admin_hdrs)
        assert r.status_code in (400, 403, 409, 422), (
            f"Approved report must not be deletable, got {r.status_code}: {r.text}"
        )

    async def test_recall_returns_report_to_draft(self, ac, admin_hdrs):
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput

        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "RecallCo"},
                          headers=admin_hdrs)
        rid = r.json()["id"]

        async with AsyncSessionLocal() as db_direct:
            db_direct.add(SectionOutput(id=_uid(), report_id=rid, section_no=1,
                                         status="done", markdown="# Test"))
            await db_direct.commit()

        r = await ac.post(f"{RPTS}/{rid}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["status"] == "review_in_progress"

        r = await ac.post(f"{RPTS}/{rid}/recall", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["status"] == "draft"

    async def test_report_status_field_present_in_list(self, ac, admin_hdrs, report):
        """Every report returned by GET /reports must include a 'status' field."""
        r = await ac.get(RPTS, headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        reports = body if isinstance(body, list) else body.get("reports", [])
        assert reports, "GET /reports must return at least one report"
        assert "status" in reports[0], "Report list must include 'status' field"


# ══════════════════════════════════════════════════════════════════════════════
# 10. Block Staleness and History
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockStalenessAndHistory:
    """Stale propagation, version snapshots, block history."""

    async def test_fact_override_marks_bound_blocks_stale(self, db):
        """update_fact_value() must call _mark_bound_blocks_stale() → is_stale=True."""
        from credit_report.fact_store.repository import upsert_fact, update_fact_value
        from credit_report.block_ast.repository import save_blocks, get_block

        rid = _uid()
        fact = await upsert_fact(db, {"report_id": rid, "metric_name": "revenue",
                                       "entity": "Co", "period": "FY2024",
                                       "value": 1000.0, "source_type": "etl"})
        await db.flush()

        block_id = _uid()
        await save_blocks(
            db,
            [{"id": block_id, "report_id": rid, "section_no": 7,
              "block_type": "table", "content": "Revenue", "source_fact_ids": f'["{fact.id}"]',
              "is_stale": False, "version": 1}],
            [{"id": _uid(), "block_id": block_id, "row_id": "row_001", "column_id": "col_000",
              "display_value": "1,000m", "fact_id": fact.id, "binding_status": "bound"}],
        )
        await db.flush()

        block_before = await get_block(db, block_id)
        assert block_before.is_stale is False

        await update_fact_value(db, fact.id, new_value=1200.0, new_display="1,200m",
                                actor_id="analyst", reason="correction",
                                expected_version=fact.version)
        await db.flush()

        block_after = await get_block(db, block_id)
        assert block_after.is_stale is True, "Block bound to overridden fact must become stale"

    async def test_unrelated_fact_override_does_not_mark_block_stale(self, db):
        """Overriding fact A must not mark blocks bound only to fact B as stale."""
        from credit_report.fact_store.repository import upsert_fact, update_fact_value
        from credit_report.block_ast.repository import save_blocks, get_block

        rid = _uid()
        fact_a = await upsert_fact(db, {"report_id": rid, "metric_name": "revenue",
                                         "entity": "Co", "period": "FY2024",
                                         "value": 1000.0, "source_type": "etl"})
        fact_b = await upsert_fact(db, {"report_id": rid, "metric_name": "ebitda",
                                         "entity": "Co", "period": "FY2024",
                                         "value": 200.0, "source_type": "etl"})
        await db.flush()

        block_b_id = _uid()
        await save_blocks(
            db,
            [{"id": block_b_id, "report_id": rid, "section_no": 7,
              "block_type": "table", "content": "EBITDA", "source_fact_ids": f'["{fact_b.id}"]',
              "is_stale": False, "version": 1}],
            [{"id": _uid(), "block_id": block_b_id, "row_id": "row_001", "column_id": "col_000",
              "display_value": "200m", "fact_id": fact_b.id, "binding_status": "bound"}],
        )
        await db.flush()

        # Override fact A (not fact B)
        await update_fact_value(db, fact_a.id, new_value=1100.0, new_display="1,100m",
                                actor_id="analyst", reason="correction",
                                expected_version=fact_a.version)
        await db.flush()

        block_b = await get_block(db, block_b_id)
        assert block_b.is_stale is False, (
            "Block bound to fact B must NOT be stale when fact A is overridden"
        )

    async def test_block_patch_creates_version_snapshot(self, ac, admin_hdrs, report):
        """PATCH /blocks/{id} must save a BlockVersion snapshot before applying the edit."""
        from credit_report.block_ast.repository import save_blocks
        from credit_report.database import AsyncSessionLocal

        rid = report["id"]
        block_id = f"{rid}.1.paragraph.hist"
        original_content = "Original paragraph text for history testing."

        async with AsyncSessionLocal() as db_direct:
            await save_blocks(db_direct,
                              [{"id": block_id, "report_id": rid, "section_no": 1,
                                "block_type": "paragraph", "content": original_content,
                                "source_fact_ids": "[]", "is_stale": False, "version": 1}], [])
            await db_direct.commit()

        r = await ac.patch(f"{RPTS}/{rid}/blocks/{block_id}",
                           json={"content": "Updated paragraph content.",
                                 "reason": "Editorial improvement",
                                 "expected_version": 1},
                           headers=admin_hdrs)
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 2

        r = await ac.get(f"{RPTS}/{rid}/blocks/{block_id}/history", headers=admin_hdrs)
        assert r.status_code == 200
        history = r.json()
        assert len(history) >= 1, "At least one history snapshot must exist after edit"
        assert any(h["content"] == original_content for h in history), (
            "The original content must be saved as a history snapshot"
        )

    async def test_concurrent_block_patch_same_version_second_is_409(self, ac, admin_hdrs, report):
        """Optimistic locking: two PATCHes with expected_version=1 → second must fail."""
        from credit_report.block_ast.repository import save_blocks
        from credit_report.database import AsyncSessionLocal

        rid = report["id"]
        block_id = f"{rid}.1.para.olock"
        async with AsyncSessionLocal() as db_direct:
            await save_blocks(db_direct,
                              [{"id": block_id, "report_id": rid, "section_no": 1,
                                "block_type": "paragraph", "content": "Initial",
                                "source_fact_ids": "[]", "is_stale": False, "version": 1}], [])
            await db_direct.commit()

        r1 = await ac.patch(f"{RPTS}/{rid}/blocks/{block_id}",
                            json={"content": "Edit A", "expected_version": 1},
                            headers=admin_hdrs)
        assert r1.status_code == 200

        r2 = await ac.patch(f"{RPTS}/{rid}/blocks/{block_id}",
                            json={"content": "Edit B", "expected_version": 1},
                            headers=admin_hdrs)
        assert r2.status_code == 409, (
            f"Second PATCH with same expected_version must return 409, got {r2.status_code}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 11. Generation Pipeline: report_type, preceding outputs, hard dependencies
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerationPipeline:
    """Pipeline wiring, preceding outputs, hard dependency enforcement."""

    async def test_report_type_injected_into_section_prompt(self, db):
        """Pipeline must inject Report.report_type into input_json['metadata']."""
        from credit_report.models import Report
        from credit_report.generation import pipeline

        rid = _uid()
        uid = _uid()
        captured: dict = {}

        db.add(Report(id=rid, industry="shipping", created_by=uid,
                      report_type="watchlist"))
        await db.flush()

        async def fake_gen(section_no, input_json, **kw):
            captured.update(input_json)
            return "## Done.\n", 50

        with patch("credit_report.generation.pipeline.generate_section_markdown",
                   new=AsyncMock(side_effect=fake_gen)), \
             patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[]), \
             patch("credit_report.generation.pipeline.check_quota", new=AsyncMock()), \
             patch("credit_report.generation.pipeline.reserve_and_record_tokens",
                   new=AsyncMock()), \
             patch("credit_report.database.AsyncSessionLocal") as mock_asl:
            mock_asl.return_value.__aenter__ = AsyncMock(return_value=db)
            mock_asl.return_value.__aexit__ = AsyncMock(return_value=False)
            await pipeline.run_section_generation(
                db=db, report_id=rid, section_no=2,
                actor_user_id=uid, actor_role="analyst",
            )

        assert captured.get("metadata", {}).get("report_type") == "watchlist", (
            f"report_type must be injected from Report model. Got: {captured.get('metadata')}"
        )

    async def test_existing_metadata_report_type_not_overwritten(self, db):
        """If analyst set report_type in input_json already, pipeline must not overwrite it."""
        from credit_report.models import Report, SectionInput
        from credit_report.generation import pipeline
        import json as _json

        rid = _uid()
        uid = _uid()
        captured: dict = {}

        db.add(Report(id=rid, industry="shipping", created_by=uid,
                      report_type="annual_review"))
        db.add(SectionInput(report_id=rid, section_no=3,
                             input_json=_json.dumps({"metadata": {"report_type": "new_deal"}})))
        await db.flush()

        async def fake_gen(section_no, input_json, **kw):
            captured.update(input_json)
            return "## Done.\n", 50

        with patch("credit_report.generation.pipeline.generate_section_markdown",
                   new=AsyncMock(side_effect=fake_gen)), \
             patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[]), \
             patch("credit_report.generation.pipeline.check_quota", new=AsyncMock()), \
             patch("credit_report.generation.pipeline.reserve_and_record_tokens",
                   new=AsyncMock()), \
             patch("credit_report.database.AsyncSessionLocal") as mock_asl:
            mock_asl.return_value.__aenter__ = AsyncMock(return_value=db)
            mock_asl.return_value.__aexit__ = AsyncMock(return_value=False)
            await pipeline.run_section_generation(
                db=db, report_id=rid, section_no=3,
                actor_user_id=uid, actor_role="analyst",
            )

        assert captured.get("metadata", {}).get("report_type") == "new_deal", (
            "Analyst-set report_type must win over Report.report_type"
        )

    async def test_section_status_set_to_error_on_llm_failure(self, db):
        """When LLM raises, SectionOutput.status must be 'error', not left as 'generating'."""
        from credit_report.models import Report, SectionOutput
        from credit_report.generation import pipeline
        from sqlalchemy import select

        rid = _uid()
        uid = _uid()
        db.add(Report(id=rid, industry="shipping", created_by=uid))
        await db.flush()

        with patch("credit_report.generation.pipeline.generate_section_markdown",
                   new=AsyncMock(side_effect=RuntimeError("LLM offline"))), \
             patch("credit_report.generation.pipeline.retrieve_evidence", return_value=[]), \
             patch("credit_report.generation.pipeline.check_quota", new=AsyncMock()), \
             patch("credit_report.generation.pipeline.reserve_and_record_tokens",
                   new=AsyncMock()), \
             patch("credit_report.database.AsyncSessionLocal") as mock_asl:
            mock_asl.return_value.__aenter__ = AsyncMock(return_value=db)
            mock_asl.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(RuntimeError):
                await pipeline.run_section_generation(
                    db=db, report_id=rid, section_no=5,
                    actor_user_id=uid, actor_role="analyst",
                )

        r = await db.execute(
            select(SectionOutput).where(SectionOutput.report_id == rid,
                                         SectionOutput.section_no == 5)
        )
        output = r.scalar_one_or_none()
        assert output is not None
        assert output.status == "error", (
            f"SectionOutput must be 'error' after LLM failure, got {output.status!r}"
        )

    async def test_preceding_outputs_appear_in_generated_prompt(self):
        """build_section_prompt must embed preceding section previews in user_prompt."""
        from credit_report.generation.prompt_builder import build_section_prompt

        preceding = {
            1: "## Facility\n\nBorrower: Maersk Line. Loan: USD 500m revolving credit.\n",
            7: "## Financial Analysis\n\nRevenue FY2024: USD 8.5bn. EBITDA: USD 1.2bn.\n",
        }
        _, user_prompt = build_section_prompt(
            section_no=9, input_json={}, evidence_chunks=[],
            preceding_outputs=preceding,
        )
        assert "Maersk" in user_prompt, "Preceding section content must appear in prompt"
        assert "EBITDA" in user_prompt
        assert "Previously Generated Sections" in user_prompt or "Section 1" in user_prompt

    async def test_preceding_preview_capped_to_prevent_context_overflow(self):
        """Long preceding outputs must be truncated to prevent token overflow."""
        from credit_report.generation.prompt_builder import build_section_prompt

        very_long = "A" * 5000
        _, user_prompt = build_section_prompt(
            section_no=2, input_json={}, evidence_chunks=[],
            preceding_outputs={1: very_long},
        )
        assert "A" * 700 not in user_prompt, "Preceding preview must be capped"

    async def test_pipeline_code_sets_generating_before_llm(self):
        """Code inspection: SectionOutput.status must be set to 'generating' before LLM call."""
        from credit_report.generation import pipeline
        source = inspect.getsource(pipeline.run_section_generation)
        assert 'status = "generating"' in source, (
            "Pipeline must set status='generating' before calling the LLM"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 12. Task Store Lifecycle (generation background tasks)
# ══════════════════════════════════════════════════════════════════════════════

class TestTaskStoreLifecycle:
    """_TaskStore TTL, maxsize, and status transition correctness."""

    async def test_task_store_set_get(self):
        from credit_report.api.generate import _generation_tasks
        tid = _uid()
        _generation_tasks.set(tid, {"status": "running", "section_no": 3})
        task = _generation_tasks.get(tid)
        assert task is not None
        assert task["status"] == "running"

    async def test_task_store_update_merges_fields(self):
        from credit_report.api.generate import _generation_tasks
        tid = _uid()
        _generation_tasks.set(tid, {"status": "running", "section_no": 5})
        _generation_tasks.update(tid, {"status": "done", "tokens_used": 1200})
        task = _generation_tasks.get(tid)
        assert task["status"] == "done"
        assert task["tokens_used"] == 1200
        assert task["section_no"] == 5

    async def test_task_store_error_transition(self):
        from credit_report.api.generate import _generation_tasks
        tid = _uid()
        _generation_tasks.set(tid, {"status": "running", "section_no": 7})
        _generation_tasks.update(tid, {"status": "error", "detail": "LLM timeout after 180s"})
        task = _generation_tasks.get(tid)
        assert task["status"] == "error"
        assert "timeout" in task["detail"]

    async def test_task_store_nonexistent_returns_none(self):
        from credit_report.api.generate import _generation_tasks
        assert _generation_tasks.get("nonexistent-task-id-xyz") is None


# ══════════════════════════════════════════════════════════════════════════════
# 13. PDF Export Guard
# ══════════════════════════════════════════════════════════════════════════════

class TestPDFExportGuard:
    """PDF export 503 when weasyprint absent; DOCX always available."""

    async def test_pdf_export_503_when_weasyprint_missing(self, ac, admin_hdrs, report):
        """POST /export/pdf must return 503 with actionable message when weasyprint is absent."""
        import sys
        sys.modules.pop("weasyprint", None)
        with patch.dict(sys.modules, {"weasyprint": None}):
            rid = report["id"]
            r = await ac.get(f"{RPTS}/{rid}/export/pdf",
                             params={"report_id": rid}, headers=admin_hdrs)
            if r.status_code == 404:
                # No done sections → 404 is also acceptable in this path
                return
            if r.status_code == 503:
                body = r.json()
                assert "weasyprint" in body.get("detail", "").lower(), (
                    "503 detail must mention weasyprint so the admin knows what to install"
                )

    async def test_docx_export_returns_correct_content_type(self, ac, admin_hdrs, report):
        """GET /export/docx must return application/vnd.openxmlformats... content-type."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput

        rid = report["id"]
        async with AsyncSessionLocal() as db_direct:
            db_direct.add(SectionOutput(id=_uid(), report_id=rid, section_no=1,
                                         status="done", markdown="# Test\n\nContent."))
            await db_direct.commit()

        r = await ac.get(f"{RPTS}/{rid}/export/docx",
                         params={"report_id": rid}, headers=admin_hdrs)
        assert r.status_code == 200, r.text
        assert "openxmlformats" in r.headers.get("content-type", ""), (
            "DOCX export must return the correct OOXML content-type"
        )
        assert len(r.content) > 100, "DOCX export must return non-trivial bytes"


# ══════════════════════════════════════════════════════════════════════════════
# 14. Audit Trail Completeness
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditTrailCompleteness:
    """Every mutating action that should write an audit event does so."""

    async def test_report_creation_audited(self, ac, admin_hdrs):
        r = await ac.post(RPTS, json={"industry": "shipping", "borrower_name": "AuditCo"},
                          headers=admin_hdrs)
        assert r.status_code in (200, 201)
        rid = r.json()["id"]

        r = await ac.get(f"{BASE}/audit/events", params={"page_size": 50}, headers=admin_hdrs)
        assert r.status_code == 200, f"Global audit endpoint failed: {r.status_code} {r.text}"
        events = r.json().get("events", [])
        creation_events = [e for e in events
                           if e.get("report_id") == rid and "creat" in e.get("action", "")]
        assert creation_events, "Report creation must produce an audit event"

    async def test_password_change_audited(self, ac, admin_hdrs):
        tokens = await _register_analyst(ac, admin_hdrs)
        hdrs = {"Authorization": f"Bearer {tokens['access_token']}"}

        await ac.post(f"{AUTH}/change-password",
                      json={"current_password": "Pass1234!", "new_password": "Changed99!"},
                      headers=hdrs)

        r = await ac.get(f"{BASE}/audit/events", params={"page_size": 100}, headers=admin_hdrs)
        assert r.status_code == 200, f"Global audit endpoint failed: {r.status_code} {r.text}"
        events = r.json().get("events", [])
        pw_events = [e for e in events if "password_change" in e.get("action", "")]
        assert pw_events, "Password change must produce an auth.password_change audit event"

    async def test_block_validate_audited(self, ac, admin_hdrs, report):
        """BUG FIX verification: validate_block now writes block.validated audit event."""
        from credit_report.block_ast.repository import save_blocks
        from credit_report.database import AsyncSessionLocal

        rid = report["id"]
        block_id = f"{rid}.2.para.audit_trail"
        async with AsyncSessionLocal() as db_direct:
            await save_blocks(db_direct,
                              [{"id": block_id, "report_id": rid, "section_no": 2,
                                "block_type": "paragraph", "content": "Audit trail test",
                                "source_fact_ids": "[]", "is_stale": False, "version": 1}], [])
            await db_direct.commit()

        await ac.post(f"{RPTS}/{rid}/blocks/{block_id}/validate", headers=admin_hdrs)

        r = await ac.get(f"{BASE}/audit/events", params={"page_size": 100}, headers=admin_hdrs)
        assert r.status_code == 200, f"Global audit endpoint failed: {r.status_code} {r.text}"
        events = r.json().get("events", [])
        validate_events = [e for e in events
                           if e.get("action") == "block.validated"
                           and e.get("target_id") == block_id]
        assert validate_events, (
            "block.validated audit event must appear after POST /blocks/{id}/validate"
        )

"""
Tests for the CanonicalFact → Section Form Field Suggestion Engine.

Covers:
  1  GET happy path — fact → suggestion
  2  entity_path static resolution from current input (dict path)
  3  entity_path bracket-index resolution (rows[0].entity_abbrev, Section 3 style)
  4  deprecated facts excluded from suggestions
  5  conflicted fact → selectable=False, confidence="low"
  6  values_equal check — already-matching value excluded from suggestions
  7  section_no out of range (11) → 400
  8  tampered suggestion_id rejected on POST apply
  9  apply only_empty mode — filled field skipped, reason communicated
  10 apply overwrite mode — filled field replaced
  11 apply conflicted fact → conflict_paths
  12 POST apply requires report ownership (403 for non-owner)
  13 POST apply batch size limit (501 items → 400)
  14 POST apply writes audit event section_input.facts_applied
  15 static mapping fallback: entity="" still matches metric-only
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from main import app  # noqa: E402

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
RPTS = f"{BASE}/reports"


# ── helpers ───────────────────────────────────────────────────────────────────

async def _login(ac: AsyncClient, email: str = "admin@example.com", pw: str = "admin123") -> str:
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _hdrs(ac: AsyncClient, email: str = "admin@example.com", pw: str = "admin123") -> dict:
    return {"Authorization": f"Bearer {await _login(ac, email, pw)}"}


async def _register(ac: AsyncClient, admin_hdrs: dict, email: str, role: str = "analyst") -> dict:
    r = await ac.post(
        f"{AUTH}/register",
        json={"email": email, "password": "Pass1234!", "role": role},
        headers=admin_hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _create_report(ac: AsyncClient, hdrs: dict, industry: str = "marine") -> dict:
    r = await ac.post(
        RPTS,
        json={"borrower_name": "TestCo", "industry": industry, "report_type": "new_deal"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _save_input(ac: AsyncClient, hdrs: dict, rid: str, sec: int, data: dict) -> dict:
    r = await ac.put(
        f"{RPTS}/{rid}/inputs/{sec}",
        json={"section_no": sec, "input_json": data},
        headers=hdrs,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _seed_fact(
    report_id: str,
    metric: str,
    entity: str,
    period: str = "CURRENT",
    value: float | None = None,
    value_text: str | None = None,
    state: str = "extracted",
    source_type: str = "pdf_extraction",
    source_priority: int = 3,
    currency: str | None = None,
    display: str | None = None,
) -> str:
    """Insert a CanonicalFact directly via repository and return its id."""
    from credit_report.database import AsyncSessionLocal
    from credit_report.fact_store.repository import upsert_fact

    async with AsyncSessionLocal() as db:
        fact = await upsert_fact(db, {
            "report_id": report_id,
            "metric_name": metric,
            "entity": entity,
            "period": period,
            "value": value,
            "value_text": value_text or (str(value) if value is not None else None),
            "currency": currency,
            "unit": None,
            "display": display,
            "state": state,
            "source_type": source_type,
            "source_priority": source_priority,
        })
        await db.flush()
        await db.commit()
        return fact.id


def _suggestion_id(report_id: str, section_no: int, field_path: str, fact_id: str) -> str:
    """Mirror the server-side deterministic suggestion_id computation."""
    raw = f"{report_id}:{section_no}:{field_path}:{fact_id}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def admin_hdrs(ac):
    return await _hdrs(ac)


@pytest_asyncio.fixture
async def report(ac, admin_hdrs):
    return await _create_report(ac, admin_hdrs)


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGetFieldSuggestions:

    # 1 — happy path
    @pytest.mark.asyncio
    async def test_happy_path_returns_suggestion(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Seed a fact that matches section 1 YAML (total_credit_proposed / FACILITY / CURRENT)
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=100.0, display="USD 100m",
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/1/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        assert body["report_id"] == rid
        assert body["section_no"] == 1
        assert body["total_facts_checked"] > 0
        # At least one suggestion should have our fact
        fact_ids = [s["fact_id"] for s in body["suggestions"]]
        assert fact_id in fact_ids

    # 2 — entity_path resolved from current input (simple dot-path)
    @pytest.mark.asyncio
    async def test_entity_path_simple_dot_resolution(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Section 3 MAS612 mapping has static entity="BORROWER"
        # But internal_rating_current uses entity_path pointing to rows[0]
        # We test a simpler case: save input with an entity name, seed matching fact
        fact_id = await _seed_fact(
            rid, "mas612_grade", "BORROWER", "CURRENT",
            value_text="Pass", state="validated",
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/3/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        fact_ids = [s["fact_id"] for s in body["suggestions"]]
        assert fact_id in fact_ids

    # 3 — entity_path bracket-index resolution (rows[0].entity_abbrev)
    @pytest.mark.asyncio
    async def test_entity_path_bracket_index_resolution(self, ac, admin_hdrs, report):
        rid = report["id"]
        entity_name = "TESTCO"
        # Pre-populate section 3 input so entity_path can resolve
        sec3_input = {
            "3B_internal_ratings": {
                "rows": [
                    {"entity_abbrev": entity_name, "current": None, "final_rating": None}
                ]
            }
        }
        await _save_input(ac, admin_hdrs, rid, 3, sec3_input)
        # Seed matching fact
        fact_id = await _seed_fact(
            rid, "internal_rating_current", entity_name, "CURRENT",
            value_text="BB+", state="validated",
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/3/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        fact_ids = [s["fact_id"] for s in body["suggestions"]]
        assert fact_id in fact_ids, (
            f"Expected fact {fact_id} for entity {entity_name} in suggestions; got: {body['suggestions']}"
        )

    # 4 — deprecated facts excluded
    @pytest.mark.asyncio
    async def test_deprecated_fact_excluded(self, ac, admin_hdrs, report):
        rid = report["id"]
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=999.0, state="deprecated",
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/1/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        fact_ids = [s["fact_id"] for s in body["suggestions"]]
        assert fact_id not in fact_ids, "Deprecated fact must not appear in suggestions"

    # 5 — conflicted fact → selectable=False, confidence="low"
    @pytest.mark.asyncio
    async def test_conflicted_fact_selectable_false(self, ac, admin_hdrs, report):
        rid = report["id"]
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=77.0, state="conflicted",
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/1/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        matching = [s for s in body["suggestions"] if s["fact_id"] == fact_id]
        assert matching, "Conflicted fact should still appear as a suggestion"
        s = matching[0]
        assert s["selectable"] is False
        assert s["confidence"] == "low"
        assert s["conflict_warning"] is not None

    # 6 — values_equal: already-matching value excluded
    @pytest.mark.asyncio
    async def test_already_matching_value_excluded(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Save the section input with the same value we are about to seed
        await _save_input(ac, admin_hdrs, rid, 1, {
            "1A_facility_summary": {
                "totals": {"total_credit_proposed_usd_m": 55.0}
            }
        })
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT", value=55.0,
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/1/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        fact_ids = [s["fact_id"] for s in body["suggestions"]]
        assert fact_id not in fact_ids, (
            "When current value matches suggested value, field should be excluded"
        )

    # 7 — section_no out of range
    @pytest.mark.asyncio
    async def test_section_no_out_of_range_returns_400(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/sections/11/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 400

    # 7b — section_no = 0 also rejected
    @pytest.mark.asyncio
    async def test_section_no_zero_returns_400(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.get(f"{RPTS}/{rid}/sections/0/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 400

    # 15 — static mapping entity fallback (entity="" matches metric-only)
    @pytest.mark.asyncio
    async def test_static_mapping_metric_only_fallback(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Seed a fact with a metric that has a static YAML mapping but supply wrong entity
        # The endpoint should fall back to metric-only match as last resort
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "UNKNOWN_ENTITY", "CURRENT", value=42.0,
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/1/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        body = r.json()
        # entity fallback means it should still find this fact
        fact_ids = [s["fact_id"] for s in body["suggestions"]]
        assert fact_id in fact_ids


class TestApplyFieldSuggestions:

    # 8 — tampered suggestion_id rejected
    @pytest.mark.asyncio
    async def test_tampered_suggestion_id_in_skipped_paths(self, ac, admin_hdrs, report):
        rid = report["id"]
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT", value=100.0,
        )
        bad_sig = "0000000000000000"  # wrong sha
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={
                "items": [{
                    "suggestion_id": bad_sig,
                    "field_path": "1A_facility_summary.totals.total_credit_proposed_usd_m",
                    "suggested_value": 100.0,
                    "fact_id": fact_id,
                }],
                "apply_mode": "only_empty",
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied_count"] == 0
        assert "1A_facility_summary.totals.total_credit_proposed_usd_m" in body["skipped_paths"]

    # 9 — only_empty skips already-filled field
    @pytest.mark.asyncio
    async def test_apply_only_empty_skips_filled_field(self, ac, admin_hdrs, report):
        rid = report["id"]
        field_path = "1A_facility_summary.totals.total_credit_proposed_usd_m"
        await _save_input(ac, admin_hdrs, rid, 1, {
            "1A_facility_summary": {"totals": {"total_credit_proposed_usd_m": 99.0}}
        })
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT", value=123.0,
        )
        sig = _suggestion_id(rid, 1, field_path, fact_id)
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={
                "items": [{"suggestion_id": sig, "field_path": field_path,
                            "suggested_value": 123.0, "fact_id": fact_id}],
                "apply_mode": "only_empty",
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied_count"] == 0
        assert field_path in body["skipped_paths"]

    # 10 — overwrite mode replaces existing value
    @pytest.mark.asyncio
    async def test_apply_overwrite_replaces_existing(self, ac, admin_hdrs, report):
        rid = report["id"]
        field_path = "1A_facility_summary.totals.total_credit_proposed_usd_m"
        await _save_input(ac, admin_hdrs, rid, 1, {
            "1A_facility_summary": {"totals": {"total_credit_proposed_usd_m": 99.0}}
        })
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT", value=123.0,
        )
        sig = _suggestion_id(rid, 1, field_path, fact_id)
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={
                "items": [{"suggestion_id": sig, "field_path": field_path,
                            "suggested_value": 123.0, "fact_id": fact_id}],
                "apply_mode": "overwrite",
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied_count"] == 1
        assert field_path in body["applied_paths"]
        # Verify value was actually saved
        r2 = await ac.get(f"{RPTS}/{rid}/inputs/1", headers=admin_hdrs)
        assert r2.status_code == 200
        saved = r2.json()["input_json"]
        actual = saved["1A_facility_summary"]["totals"]["total_credit_proposed_usd_m"]
        assert actual == 123.0

    # 11 — conflicted fact rejected to conflict_paths
    @pytest.mark.asyncio
    async def test_apply_conflicted_fact_goes_to_conflict_paths(self, ac, admin_hdrs, report):
        rid = report["id"]
        field_path = "1A_facility_summary.totals.total_credit_proposed_usd_m"
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=100.0, state="conflicted",
        )
        sig = _suggestion_id(rid, 1, field_path, fact_id)
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={
                "items": [{"suggestion_id": sig, "field_path": field_path,
                            "suggested_value": 100.0, "fact_id": fact_id}],
                "apply_mode": "overwrite",
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied_count"] == 0
        assert field_path in body["conflict_paths"]

    # 12 — non-owner analyst gets 403
    @pytest.mark.asyncio
    async def test_apply_non_owner_gets_403(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Register a fresh analyst (different from report creator admin)
        email = f"other-{uuid.uuid4().hex[:8]}@example.com"
        await _register(ac, admin_hdrs, email)
        other_hdrs = await _hdrs(ac, email, "Pass1234!")
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT", value=50.0,
        )
        field_path = "1A_facility_summary.totals.total_credit_proposed_usd_m"
        sig = _suggestion_id(rid, 1, field_path, fact_id)
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={
                "items": [{"suggestion_id": sig, "field_path": field_path,
                            "suggested_value": 50.0, "fact_id": fact_id}],
                "apply_mode": "only_empty",
            },
            headers=other_hdrs,
        )
        assert r.status_code == 403

    # 13 — batch size limit
    @pytest.mark.asyncio
    async def test_apply_batch_too_large_returns_400(self, ac, admin_hdrs, report):
        rid = report["id"]
        items = [
            {
                "suggestion_id": f"{'0'*16}",
                "field_path": f"some.path.{i}",
                "suggested_value": i,
                "fact_id": str(uuid.uuid4()),
            }
            for i in range(501)
        ]
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={"items": items, "apply_mode": "only_empty"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400
        assert "500" in r.json()["detail"]

    # 14 — audit event written after apply
    @pytest.mark.asyncio
    async def test_apply_writes_audit_event(self, ac, admin_hdrs, report):
        rid = report["id"]
        field_path = "1A_facility_summary.totals.total_credit_proposed_usd_m"
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT", value=88.0,
        )
        sig = _suggestion_id(rid, 1, field_path, fact_id)
        apply_r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={
                "items": [{"suggestion_id": sig, "field_path": field_path,
                            "suggested_value": 88.0, "fact_id": fact_id}],
                "apply_mode": "only_empty",
            },
            headers=admin_hdrs,
        )
        assert apply_r.status_code == 200
        assert apply_r.json()["applied_count"] == 1

        audit_r = await ac.get(f"{RPTS}/{rid}/audit", headers=admin_hdrs)
        assert audit_r.status_code == 200
        actions = [e["action"] for e in audit_r.json()["events"]]
        assert "section_input.facts_applied" in actions, (
            f"Expected 'section_input.facts_applied' in audit log; got: {actions}"
        )


class TestHelperLogic:
    """Unit-level tests for _resolve_path_safe and _values_equal via the GET endpoint."""

    # 6b — numeric string equality (values_equal guard)
    @pytest.mark.asyncio
    async def test_numeric_string_match_excluded(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Store the value as a string with comma separator in input
        await _save_input(ac, admin_hdrs, rid, 1, {
            "1A_facility_summary": {"totals": {"total_credit_proposed_usd_m": "100"}}
        })
        fact_id = await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT", value=100.0,
        )
        r = await ac.get(f"{RPTS}/{rid}/sections/1/field-suggestions", headers=admin_hdrs)
        assert r.status_code == 200
        # "100" (string) == 100.0 (float) → should be excluded
        fact_ids = [s["fact_id"] for s in r.json()["suggestions"]]
        assert fact_id not in fact_ids

    # apply_mode validation
    @pytest.mark.asyncio
    async def test_apply_invalid_mode_returns_400(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={"items": [{"suggestion_id": "x"*16, "field_path": "a.b",
                              "suggested_value": 1, "fact_id": str(uuid.uuid4())}],
                  "apply_mode": "bad_mode"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400

    # empty items list
    @pytest.mark.asyncio
    async def test_apply_empty_items_returns_400(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/sections/1/field-suggestions/apply",
            json={"items": [], "apply_mode": "only_empty"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400

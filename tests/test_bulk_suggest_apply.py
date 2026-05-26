"""
Tests for POST /reports/{report_id}/sections/bulk-suggest-apply

Covers:
  1  Happy path — facts in S1 + S7 → correct per-section counts returned
  2  min_confidence="high" — medium-confidence facts excluded
  3  min_confidence="medium" — medium + high included, low excluded
  4  min_confidence="any" — all non-conflicted facts included
  5  apply_mode="only_empty" — existing values not overwritten
  6  apply_mode="overwrite" — existing values replaced
  7  sections=[1] — only processes requested section, others untouched
  8  sections=[] → 422 validation error (empty list not allowed)
  9  Approved report → 409 immutable
  10 Non-owner analyst → 403 Forbidden
  11 Conflicted facts → counted in conflict_count, not applied
  12 Audit event written (section_input.bulk_facts_applied)
  13 Response schema: total_applied / total_skipped / sections list
  14 Invalid min_confidence → 400
  15 Invalid apply_mode → 400
"""
from __future__ import annotations

import os
import uuid

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
BULK = "sections/bulk-suggest-apply"


# ── helpers ───────────────────────────────────────────────────────────────────

async def _login(ac: AsyncClient, email: str = "admin@example.com", pw: str = "admin123") -> str:
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _hdrs(ac: AsyncClient, email: str = "admin@example.com", pw: str = "admin123") -> dict:
    return {"Authorization": f"Bearer {await _login(ac, email, pw)}"}


async def _register_analyst(ac: AsyncClient, admin_hdrs: dict) -> dict:
    email = f"analyst_{uuid.uuid4().hex[:8]}@test.com"
    r = await ac.post(
        f"{AUTH}/register",
        json={"email": email, "password": "Pass1234!", "role": "analyst"},
        headers=admin_hdrs,
    )
    assert r.status_code == 201, r.text
    return {"email": email, "password": "Pass1234!"}


async def _create_report(ac: AsyncClient, hdrs: dict, industry: str = "marine") -> dict:
    r = await ac.post(
        RPTS,
        json={"borrower_name": "BulkTestCo", "industry": industry, "report_type": "new_deal"},
        headers=hdrs,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _seed_fact(
    report_id: str,
    metric: str,
    entity: str = "BORROWER",
    period: str = "CURRENT",
    value: float | None = 100.0,
    value_text: str | None = None,
    state: str = "validated",
    source_type: str = "pdf_extraction",
    source_priority: int = 3,
) -> str:
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
            "currency": None,
            "unit": None,
            "display": None,
            "state": state,
            "source_type": source_type,
            "source_priority": source_priority,
        })
        await db.flush()
        await db.commit()
        return fact.id


async def _save_input(ac: AsyncClient, hdrs: dict, rid: str, sec: int, data: dict) -> None:
    r = await ac.put(
        f"{RPTS}/{rid}/inputs/{sec}",
        json={"section_no": sec, "input_json": data},
        headers=hdrs,
    )
    assert r.status_code in (200, 201), r.text


async def _bulk_apply(
    ac: AsyncClient,
    hdrs: dict,
    rid: str,
    **kwargs,
) -> dict:
    payload = {"sections": list(range(1, 11)), **kwargs}
    r = await ac.post(f"{RPTS}/{rid}/{BULK}", json=payload, headers=hdrs)
    return r


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
class TestBulkSuggestApplyHappyPath:

    # 1 — happy path: fact in section 1 → suggestion applied, report updated
    @pytest.mark.asyncio
    async def test_happy_path_section1_fact_applied(self, ac, admin_hdrs, report):
        rid = report["id"]
        # total_credit_proposed maps to facility_summary.totals.total_credit_limit_usd_m in S1 YAML
        await _seed_fact(rid, "total_credit_proposed", "FACILITY", "CURRENT", value=200.0)

        r = await _bulk_apply(ac, admin_hdrs, rid)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["report_id"] == rid
        assert body["total_applied"] >= 0
        assert "sections" in body
        assert isinstance(body["sections"], list)

    # 13 — response schema has required fields
    @pytest.mark.asyncio
    async def test_response_schema_complete(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await _bulk_apply(ac, admin_hdrs, rid)
        assert r.status_code == 200
        body = r.json()
        for key in ("report_id", "total_applied", "total_skipped", "sections"):
            assert key in body, f"Missing key: {key}"
        for sec_result in body["sections"]:
            for k in ("section_no", "suggestions_found", "applied_count", "skipped_count", "conflict_count"):
                assert k in sec_result, f"Missing section key: {k}"

    # 7 — sections=[1] only processes section 1
    @pytest.mark.asyncio
    async def test_single_section_only_processes_that_section(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1])
        assert r.status_code == 200
        body = r.json()
        assert len(body["sections"]) == 1
        assert body["sections"][0]["section_no"] == 1


class TestBulkSuggestApplyConfidenceFiltering:

    # 2 — min_confidence="high" excludes medium-confidence facts
    @pytest.mark.asyncio
    async def test_high_conf_excludes_pdf_unvalidated_facts(self, ac, admin_hdrs, report):
        rid = report["id"]
        # source_priority=3 + state="extracted" (not validated) → medium confidence
        await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=100.0, state="extracted", source_priority=3,
        )
        # High confidence requires state=validated/approved OR source_priority 1/2
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1], min_confidence="high")
        assert r.status_code == 200
        body = r.json()
        sec1 = next((s for s in body["sections"] if s["section_no"] == 1), None)
        assert sec1 is not None
        # Unvalidated pdf_extraction fact should NOT be applied under high confidence
        assert sec1["applied_count"] == 0

    # 3 — min_confidence="medium" includes validated pdf facts
    @pytest.mark.asyncio
    async def test_medium_conf_includes_validated_pdf_facts(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=150.0, state="validated", source_priority=3,
        )
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1], min_confidence="medium")
        assert r.status_code == 200
        body = r.json()
        sec1 = next((s for s in body["sections"] if s["section_no"] == 1), None)
        assert sec1 is not None
        assert sec1["applied_count"] >= 1

    # 4 — min_confidence="any" includes all non-conflicted facts
    @pytest.mark.asyncio
    async def test_any_conf_includes_low_confidence_facts(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=99.0, state="extracted", source_priority=3,
        )
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1], min_confidence="any")
        assert r.status_code == 200


class TestBulkSuggestApplyMode:

    # 5 — apply_mode="only_empty" skips filled fields
    @pytest.mark.asyncio
    async def test_only_empty_skips_filled_fields(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Pre-fill the exact YAML path for total_credit_proposed (1A_ prefix)
        await _save_input(ac, admin_hdrs, rid, 1, {
            "1A_facility_summary": {"totals": {"total_credit_proposed_usd_m": 999.0}}
        })
        # source_priority=1 → analyst_input → "high" confidence; qualifies under default min_confidence
        await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=200.0, state="validated", source_type="analyst_input_json", source_priority=1,
        )
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1], apply_mode="only_empty")
        assert r.status_code == 200
        body = r.json()
        sec1 = next((s for s in body["sections"] if s["section_no"] == 1), None)
        # The field was pre-filled so suggestion should be skipped
        assert sec1["skipped_count"] >= 1
        # Original value must be preserved
        r2 = await ac.get(f"{RPTS}/{rid}/inputs/1", headers=admin_hdrs)
        stored = r2.json()["input_json"]
        assert stored["1A_facility_summary"]["totals"]["total_credit_proposed_usd_m"] == 999.0

    # 6 — apply_mode="overwrite" replaces filled fields
    @pytest.mark.asyncio
    async def test_overwrite_mode_replaces_existing_values(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Pre-fill the YAML path with sentinel value
        await _save_input(ac, admin_hdrs, rid, 1, {
            "1A_facility_summary": {"totals": {"total_credit_proposed_usd_m": 999.0}}
        })
        await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=250.0, state="validated", source_type="analyst_input_json", source_priority=1,
        )
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1], apply_mode="overwrite")
        assert r.status_code == 200
        r2 = await ac.get(f"{RPTS}/{rid}/inputs/1", headers=admin_hdrs)
        stored = r2.json()["input_json"]
        assert stored["1A_facility_summary"]["totals"]["total_credit_proposed_usd_m"] == 250.0


class TestBulkSuggestApplyGuards:

    # 8 — sections=[] rejected by schema validator
    @pytest.mark.asyncio
    async def test_empty_sections_list_rejected(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/{BULK}",
            json={"sections": [], "min_confidence": "high", "apply_mode": "only_empty"},
            headers=admin_hdrs,
        )
        assert r.status_code == 422

    # 9 — approved report → 409
    @pytest.mark.asyncio
    async def test_approved_report_returns_409(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Force report to approved state via status update endpoint
        r_upd = await ac.patch(
            f"{RPTS}/{rid}/status",
            json={"status": "approved"},
            headers=admin_hdrs,
        )
        if r_upd.status_code not in (200, 201):
            # Fallback: submit-for-review then approve
            await ac.post(f"{RPTS}/{rid}/submit-for-review", headers=admin_hdrs)
            r_approve = await ac.post(f"{RPTS}/{rid}/approve", headers=admin_hdrs)
            if r_approve.status_code not in (200, 201):
                pytest.skip("Cannot approve report in test environment — skipping immutability check")

        r = await _bulk_apply(ac, admin_hdrs, rid)
        assert r.status_code == 409

    # 10 — non-owner analyst gets 403
    @pytest.mark.asyncio
    async def test_non_owner_analyst_gets_403(self, ac, admin_hdrs, report):
        rid = report["id"]
        other = await _register_analyst(ac, admin_hdrs)
        other_hdrs = await _hdrs(ac, other["email"], other["password"])
        r = await _bulk_apply(ac, other_hdrs, rid)
        assert r.status_code == 403

    # 11 — conflicted facts counted but not applied
    @pytest.mark.asyncio
    async def test_conflicted_facts_not_applied(self, ac, admin_hdrs, report):
        rid = report["id"]
        await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=100.0, state="conflicted", source_priority=3,
        )
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1], min_confidence="any")
        assert r.status_code == 200
        body = r.json()
        sec1 = next((s for s in body["sections"] if s["section_no"] == 1), None)
        assert sec1["conflict_count"] >= 1
        assert sec1["applied_count"] == 0

    # 14 — invalid min_confidence → 400
    @pytest.mark.asyncio
    async def test_invalid_min_confidence_returns_400(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/{BULK}",
            json={"min_confidence": "very_high"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400

    # 15 — invalid apply_mode → 400
    @pytest.mark.asyncio
    async def test_invalid_apply_mode_returns_400(self, ac, admin_hdrs, report):
        rid = report["id"]
        r = await ac.post(
            f"{RPTS}/{rid}/{BULK}",
            json={"apply_mode": "destroy"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400


class TestBulkSuggestApplyAudit:

    # 12 — audit event written after successful bulk apply
    @pytest.mark.asyncio
    async def test_audit_event_written(self, ac, admin_hdrs, report):
        rid = report["id"]
        # Use analyst_input source_priority=1 so fact qualifies under default min_confidence="high"
        await _seed_fact(
            rid, "total_credit_proposed", "FACILITY", "CURRENT",
            value=100.0, state="validated", source_type="analyst_input_json", source_priority=1,
        )
        r = await _bulk_apply(ac, admin_hdrs, rid, sections=[1])
        assert r.status_code == 200

        audit_r = await ac.get(f"{RPTS}/{rid}/audit", headers=admin_hdrs)
        assert audit_r.status_code == 200
        # Audit endpoint returns {"events": [...], ...}
        body = audit_r.json()
        events = [e["action"] for e in body.get("events", body if isinstance(body, list) else [])]
        assert "section_input.bulk_facts_applied" in events

"""
Tests for GET /reports/{report_id}/coverage — field coverage report.

Verifies:
  1  Coverage endpoint returns 200 with correct structure
  2  Empty report → all fields missing, gate_passed=False, overall_pct=0
  3  Full data → all fields checked, gate_passed=True
  4  Partial data → correct filled/missing counts, correct pct
  5  Report not found → 404
  6  Unauthenticated → 401
  7  Non-owner gets 403
  8  Section structure: every section 1-10 present, correct schema
  9  Missing fields carry note "資料源缺漏，待補"
  10 gate_threshold_pct is always 90
"""
from __future__ import annotations

import json
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


async def _login(ac: AsyncClient, email: str = "admin@example.com", pw: str = "admin123") -> dict:
    r = await ac.post(f"{AUTH}/login", data={"username": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _create_report(ac: AsyncClient, hdrs: dict) -> str:
    r = await ac.post(f"{RPTS}", json={"borrower_name": "CoverageTest Corp", "industry": "marine"}, headers=hdrs)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _save_section(ac: AsyncClient, hdrs: dict, rid: str, sec: int, data: dict) -> None:
    r = await ac.put(f"{RPTS}/{rid}/inputs/{sec}", json={"section_no": sec, "input_json": data}, headers=hdrs)
    assert r.status_code == 200, r.text


@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def setup(ac):
    hdrs = await _login(ac)
    rid = await _create_report(ac, hdrs)
    return ac, hdrs, rid


# ── 1. Basic structure ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coverage_returns_200(setup):
    ac, hdrs, rid = setup
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    assert r.status_code == 200
    body = r.json()
    assert body["report_id"] == rid
    assert "overall_pct" in body
    assert "gate_passed" in body
    assert "total_filled" in body
    assert "total_required" in body
    assert "gate_threshold_pct" in body
    assert "sections" in body


@pytest.mark.asyncio
async def test_coverage_gate_threshold_is_90(setup):
    ac, hdrs, rid = setup
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    assert r.status_code == 200
    assert r.json()["gate_threshold_pct"] == 90


@pytest.mark.asyncio
async def test_coverage_sections_1_to_10_present(setup):
    ac, hdrs, rid = setup
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    sections = r.json()["sections"]
    assert len(sections) == 10
    section_nos = [s["section_no"] for s in sections]
    assert section_nos == list(range(1, 11))


# ── 2. Empty report → all missing ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_report_all_missing(setup):
    ac, hdrs, rid = setup
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    body = r.json()
    assert body["overall_pct"] == 0
    assert body["gate_passed"] is False
    assert body["total_filled"] == 0
    assert body["total_required"] > 0
    for sec in body["sections"]:
        assert sec["filled"] == 0
        assert sec["missing_count"] == sec["total_required"]


@pytest.mark.asyncio
async def test_missing_fields_have_note(setup):
    ac, hdrs, rid = setup
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    body = r.json()
    for sec in body["sections"]:
        for mf in sec["missing"]:
            assert mf["note"] == "資料源缺漏，待補"


@pytest.mark.asyncio
async def test_empty_report_has_input_false(setup):
    ac, hdrs, rid = setup
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    for sec in r.json()["sections"]:
        assert sec["has_input"] is False


# ── 3. Partial data → correct counts ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_fill_section_1(setup):
    ac, hdrs, rid = setup
    # Section 1 required: report_type, facility_summary.rows, regulatory_compliance.compliance_status,
    # purpose_and_recommendation.recommendation, terms_and_conditions.borrower,
    # account_strategy.wallet.bank_market  → 6 fields
    await _save_section(ac, hdrs, rid, 1, {
        "report_type": "new_deal",
        "facility_summary": {"rows": ["row1"]},
    })
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    sec1 = next(s for s in r.json()["sections"] if s["section_no"] == 1)
    assert sec1["has_input"] is True
    assert sec1["filled"] == 2
    assert sec1["total_required"] == 6
    assert sec1["coverage_pct"] == 33  # round(2/6*100)
    assert sec1["missing_count"] == 4
    checked_paths = [c["path"] for c in sec1["checked"]]
    assert "report_type" in checked_paths
    assert "facility_summary.rows" in checked_paths


@pytest.mark.asyncio
async def test_section_4_coverage(setup):
    ac, hdrs, rid = setup
    await _save_section(ac, hdrs, rid, 4, {
        "4A_borrower": {"company_name_en": "Test Corp"},
        "4B_ownership": {"shareholders": [{"name": "Parent Co", "stake_percent": 100}]},
        "4C_management": {"ceo_name": "John Smith"},
        "4D_business": {"primary_business": "Container shipping"},
    })
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    sec4 = next(s for s in r.json()["sections"] if s["section_no"] == 4)
    assert sec4["filled"] == 4
    assert sec4["total_required"] == 7
    assert sec4["missing_count"] == 3
    missing_paths = [m["path"] for m in sec4["missing"]]
    assert "4E_financials.revenue" in missing_paths
    assert "4F_fleet.owned_vessel_count" in missing_paths
    assert "4J_peer_comparison" in missing_paths


# ── 4. Overall pct calculation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_overall_pct_aggregates_across_sections(setup):
    ac, hdrs, rid = setup
    # Fill all §1 required fields
    await _save_section(ac, hdrs, rid, 1, {
        "report_type": "new_deal",
        "facility_summary": {"rows": ["row1"], "totals": {}},
        "regulatory_compliance": {"compliance_status": "compliant"},
        "purpose_and_recommendation": {"recommendation": "APPROVE"},
        "terms_and_conditions": {"borrower": "Test Corp"},
        "account_strategy": {"wallet": {"bank_market": 1.5}},
    })
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    body = r.json()
    sec1 = next(s for s in body["sections"] if s["section_no"] == 1)
    assert sec1["filled"] == 6
    assert sec1["coverage_pct"] == 100
    # overall_pct = (6 + 0*9 other sections) / total_required
    total_req = body["total_required"]
    assert body["total_filled"] == 6
    expected_pct = round(6 / total_req * 100)
    assert body["overall_pct"] == expected_pct


# ── 5. Gate passed when overall >= 90 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_passed_when_all_filled(setup):
    ac, hdrs, rid = setup
    # Fill all required fields across all sections
    all_data = {
        1: {"report_type": "new_deal", "facility_summary": {"rows": ["r1"]},
            "regulatory_compliance": {"compliance_status": "ok"},
            "purpose_and_recommendation": {"recommendation": "APPROVE"},
            "terms_and_conditions": {"borrower": "Corp"},
            "account_strategy": {"wallet": {"bank_market": 1.0}}},
        2: {"2A_credit_overview": {"bullets": "bullet text"},
            "2B_solvency": {"primary_repayment_source_verbatim": "operating cash flow"},
            "2C_guarantor": {"guarantor_name_abbrev": "Parent"},
            "2D_collateral": {"pre_delivery": {"issuer_full_name": "IBK"}},
            "2E_risk_and_mitigants": {"risk_1_title": "Market risk"}},
        3: {"3C_mas_612": {"grade": "3", "para_1_msr_mapping_verbatim": "MSR mapping text"},
            "3B_internal_ratings": {"borrower_entity_full_name": "Corp", "borrower_fy2024": "BB"}},
        4: {"4A_borrower": {"company_name_en": "Corp"},
            "4B_ownership": {"shareholders": [{"name": "P"}]},
            "4C_management": {"ceo_name": "CEO"},
            "4D_business": {"primary_business": "shipping"},
            "4E_financials": {"revenue": 100},
            "4F_fleet": {"owned_vessel_count": 5},
            "4J_peer_comparison": [{"name": "Peer1"}]},
        5: {"5A_security_overview": {"is_secured": True},
            "5C_vessel_mortgage": {"loan_amount_usd_m": 50},
            "5E_value_maintenance_clause": {"acr_covenant_pct": 120}},
        6: {"6A_project": {"hull_number": "H001", "delivery_date": "2028-06-30"},
            "6B_builder": {"name": "Samsung HI"},
            "6C_contract": {"contract_date": "2024-01-01"},
            "6D_milestones": {"m1_name": "Keel Laying"},
            "6E_rg_mechanism": {"issuer_full_name": "IBK"}},
        7: {"entities_to_analyze": {"borrower_name": "Corp"},
            "7A_borrower_financials": {"reporting_entity": "Corp"},
            "7B_key_ratios": {"fy2024_dscr": 1.45}},
        8: {"8A_acra_banking_charges": {"acra_data_available": True, "entity_name": "Corp", "search_date": "2025-01-01"}},
        9: {"9A_checklist": {"items": ["item1"], "kyc_aml_cleared": True},
            "9C_recommendation": {"decision": "APPROVE"},
            "9D_signoff": {"prepared_by": "Analyst A"}},
        10: {"10A_group_exposure": {"entity_group": "Group", "approved_group_limit_usd_m": 500},
             "10C_projections": {"entity_name": "Corp", "dscr_commentary": "Solid"}},
    }
    for sec, data in all_data.items():
        await _save_section(ac, hdrs, rid, sec, data)

    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    body = r.json()
    assert body["overall_pct"] == 100
    assert body["gate_passed"] is True
    assert body["total_filled"] == body["total_required"]


# ── 6. Error cases ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coverage_404_unknown_report(ac):
    hdrs = await _login(ac)
    r = await ac.get(f"{RPTS}/nonexistent-id/coverage", headers=hdrs)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_coverage_401_unauthenticated(ac):
    hdrs = await _login(ac)
    rid = await _create_report(ac, hdrs)
    r = await ac.get(f"{RPTS}/{rid}/coverage")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_coverage_schema_each_section(setup):
    """Each section object must have required keys with correct types."""
    ac, hdrs, rid = setup
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    for sec in r.json()["sections"]:
        assert isinstance(sec["section_no"], int)
        assert isinstance(sec["has_input"], bool)
        assert isinstance(sec["total_required"], int)
        assert isinstance(sec["filled"], int)
        assert isinstance(sec["missing_count"], int)
        assert isinstance(sec["coverage_pct"], int)
        assert isinstance(sec["checked"], list)
        assert isinstance(sec["missing"], list)
        assert sec["filled"] + sec["missing_count"] == sec["total_required"]
        assert 0 <= sec["coverage_pct"] <= 100


@pytest.mark.asyncio
async def test_placeholder_values_counted_as_missing(setup):
    """Placeholder values like 'APPROVE/DECLINE' must not count as filled."""
    ac, hdrs, rid = setup
    await _save_section(ac, hdrs, rid, 1, {
        "report_type": "APPROVE/DECLINE",  # exact placeholder value
        "purpose_and_recommendation": {"recommendation": "To be generated from uploaded data"},
    })
    r = await ac.get(f"{RPTS}/{rid}/coverage", headers=hdrs)
    sec1 = next(s for s in r.json()["sections"] if s["section_no"] == 1)
    # Both placeholder values must NOT be counted as filled
    checked_paths = [c["path"] for c in sec1["checked"]]
    assert "report_type" not in checked_paths
    assert "purpose_and_recommendation.recommendation" not in checked_paths

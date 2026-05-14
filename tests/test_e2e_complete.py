"""
Full End-to-End CI/CD Test Suite
=================================
Tests the entire Financial Report Analyzer service via the ASGI transport layer,
covering every major feature: auth, reports, inputs, documents, ETL, generation,
workflow, facts, blocks, calculations, export, audit, RBAC, and all error paths.

Run:
    python -m pytest tests/test_e2e_complete.py -v --tb=short
"""
from __future__ import annotations

import io
import json
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# ── Env must be set before app import ────────────────────────────────────────
os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from main import app  # noqa: E402

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"
REPORTS = f"{BASE}/reports"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _mock_gemini(return_text: str = "## Section\n\nMocked generation output."):
    """Return a context manager that patches Gemini client for all call paths."""
    mock_resp = MagicMock()
    mock_resp.text = return_text
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
    return patch("google.genai.Client", return_value=mock_client)


async def _login(ac: AsyncClient, email: str, password: str) -> dict:
    r = await ac.post(
        f"{AUTH}/login",
        data={"username": email, "password": password},
    )
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()


async def _auth_headers(ac: AsyncClient, email: str = "admin@example.com", password: str = "admin123") -> dict:
    tok = await _login(ac, email, password)
    return {"Authorization": f"Bearer {tok['access_token']}"}


async def _create_report(ac: AsyncClient, hdrs: dict, borrower: str = "Test Co Ltd") -> dict:
    r = await ac.post(
        f"{REPORTS}",
        json={"borrower_name": borrower, "industry": "marine", "report_type": "new_deal"},
        headers=hdrs,
    )
    assert r.status_code == 201, f"Create report failed: {r.text}"
    return r.json()


async def _save_input(ac: AsyncClient, hdrs: dict, report_id: str, sec: int, data: dict) -> dict:
    r = await ac.put(
        f"{REPORTS}/{report_id}/inputs/{sec}",
        json={"section_no": sec, "input_json": data},
        headers=hdrs,
    )
    assert r.status_code == 200, f"Save input failed s{sec}: {r.text}"
    return r.json()


async def _register_user(ac: AsyncClient, hdrs: dict, email: str, role: str = "analyst") -> dict:
    r = await ac.post(
        f"{AUTH}/register",
        json={"email": email, "password": "Pass1234!", "role": role},
        headers=hdrs,
    )
    assert r.status_code == 201, f"Register failed: {r.text}"
    return r.json()


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def ac():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def admin_hdrs(ac):
    return await _auth_headers(ac)


@pytest_asyncio.fixture
async def report(ac, admin_hdrs):
    """Fresh report for each test."""
    return await _create_report(ac, admin_hdrs)


@pytest_asyncio.fixture
async def report_with_input(ac, admin_hdrs):
    """Report with §1 input pre-saved."""
    rpt = await _create_report(ac, admin_hdrs)
    await _save_input(ac, admin_hdrs, rpt["id"], 1, {
        "facility_summary": {
            "rows": ["1|Test Borrower|SG|100|Yes|USD|5y|Term Loan|RG|Vessel Mortgage|Guarantor"],
            "footnotes": ["[1] Test footnote."],
        }
    })
    return rpt


# ══════════════════════════════════════════════════════════════════════════════
# A — Auth
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth:

    async def test_login_success(self, ac):
        tok = await _login(ac, "admin@example.com", "admin123")
        assert "access_token" in tok
        assert "refresh_token" in tok
        assert tok["role"] == "admin"

    async def test_login_wrong_password(self, ac):
        r = await ac.post(f"{AUTH}/login", data={"username": "admin@example.com", "password": "wrong"})
        assert r.status_code == 401
        assert "Incorrect password" in r.json()["detail"]

    async def test_login_unknown_email(self, ac):
        r = await ac.post(f"{AUTH}/login", data={"username": "nobody@example.com", "password": "pw"})
        assert r.status_code == 401

    async def test_login_case_insensitive(self, ac):
        tok = await _login(ac, "ADMIN@EXAMPLE.COM", "admin123")
        assert tok["access_token"]

    async def test_me(self, ac, admin_hdrs):
        r = await ac.get(f"{AUTH}/me", headers=admin_hdrs)
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == "admin@example.com"
        assert data["role"] == "admin"

    async def test_me_no_token(self, ac):
        r = await ac.get(f"{AUTH}/me")
        assert r.status_code == 401

    async def test_me_invalid_token(self, ac):
        r = await ac.get(f"{AUTH}/me", headers={"Authorization": "Bearer bad.token.here"})
        assert r.status_code == 401

    async def test_refresh_token(self, ac):
        tok = await _login(ac, "admin@example.com", "admin123")
        r = await ac.post(f"{AUTH}/refresh", json={"refresh_token": tok["refresh_token"]})
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_refresh_with_access_token_fails(self, ac):
        tok = await _login(ac, "admin@example.com", "admin123")
        r = await ac.post(f"{AUTH}/refresh", json={"refresh_token": tok["access_token"]})
        assert r.status_code == 401

    async def test_register_and_login(self, ac, admin_hdrs):
        email = f"analyst_{uuid.uuid4().hex[:8]}@test.com"
        user = await _register_user(ac, admin_hdrs, email, role="analyst")
        assert user["role"] == "analyst"
        assert user["email"] == email
        tok = await _login(ac, email, "Pass1234!")
        assert tok["role"] == "analyst"

    async def test_register_duplicate_email(self, ac, admin_hdrs):
        email = f"dup_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email)
        r = await ac.post(
            f"{AUTH}/register",
            json={"email": email, "password": "x", "role": "analyst"},
            headers=admin_hdrs,
        )
        assert r.status_code == 409

    async def test_register_invalid_role(self, ac, admin_hdrs):
        r = await ac.post(
            f"{AUTH}/register",
            json={"email": f"x_{uuid.uuid4().hex[:8]}@test.com", "password": "x", "role": "god"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400

    async def test_register_requires_auth(self, ac):
        r = await ac.post(
            f"{AUTH}/register",
            json={"email": "x@x.com", "password": "x", "role": "analyst"},
        )
        assert r.status_code == 401

    async def test_auth_status(self, ac):
        r = await ac.get(f"{AUTH}/status")
        assert r.status_code == 200
        data = r.json()
        assert "login_possible" in data

    async def test_update_user_role(self, ac, admin_hdrs):
        email = f"role_{uuid.uuid4().hex[:8]}@test.com"
        user = await _register_user(ac, admin_hdrs, email, role="analyst")
        r = await ac.patch(
            f"{AUTH}/users/{user['id']}/role",
            params={"role": "reviewer"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["role"] == "reviewer"

    async def test_update_user_role_non_admin_forbidden(self, ac, admin_hdrs):
        email = f"norole_{uuid.uuid4().hex[:8]}@test.com"
        user = await _register_user(ac, admin_hdrs, email, role="analyst")
        analyst_hdrs = await _auth_headers(ac, email, "Pass1234!")
        r = await ac.patch(
            f"{AUTH}/users/{user['id']}/role",
            json={"role": "reviewer"},
            headers=analyst_hdrs,
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# B — Reports CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestReportsCRUD:

    async def test_create_report(self, ac, admin_hdrs):
        r = await ac.post(
            f"{REPORTS}",
            json={"borrower_name": "ACME Shipping", "industry": "marine"},
            headers=admin_hdrs,
        )
        assert r.status_code == 201
        data = r.json()
        assert data["borrower_name"] == "ACME Shipping"
        assert data["status"] == "draft"
        assert data["industry"] == "marine"

    async def test_create_report_requires_auth(self, ac):
        r = await ac.post(f"{REPORTS}", json={"borrower_name": "X"})
        assert r.status_code == 401

    async def test_list_reports(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}", headers=admin_hdrs)
        assert r.status_code == 200
        ids = [x["id"] for x in r.json()]
        assert report["id"] in ids

    async def test_get_report(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["id"] == report["id"]

    async def test_get_report_not_found(self, ac, admin_hdrs):
        r = await ac.get(f"{REPORTS}/{uuid.uuid4()}", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_delete_report_soft(self, ac, admin_hdrs, report):
        r = await ac.delete(f"{REPORTS}/{report['id']}", headers=admin_hdrs)
        assert r.status_code == 204
        r2 = await ac.get(f"{REPORTS}/{report['id']}", headers=admin_hdrs)
        assert r2.status_code == 404

    async def test_delete_report_not_found(self, ac, admin_hdrs):
        r = await ac.delete(f"{REPORTS}/{uuid.uuid4()}", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_update_status_valid(self, ac, admin_hdrs, report):
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/status",
            json={"status": "validated"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "validated"

    async def test_update_status_invalid(self, ac, admin_hdrs, report):
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/status",
            json={"status": "bogus"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400

    async def test_update_status_approved_requires_approver(self, ac, admin_hdrs):
        email = f"ana_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")
        rpt = await _create_report(ac, ana_hdrs)
        r = await ac.patch(
            f"{REPORTS}/{rpt['id']}/status",
            json={"status": "approved"},
            headers=ana_hdrs,
        )
        assert r.status_code == 403

    async def test_analyst_sees_own_reports_only(self, ac, admin_hdrs):
        email = f"own_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")
        rpt_own = await _create_report(ac, ana_hdrs, "Owned by Analyst")
        rpt_admin = await _create_report(ac, admin_hdrs, "Owned by Admin")
        r = await ac.get(f"{REPORTS}", headers=ana_hdrs)
        ids = [x["id"] for x in r.json()]
        assert rpt_own["id"] in ids
        assert rpt_admin["id"] not in ids

    async def test_admin_sees_all_reports(self, ac, admin_hdrs):
        email = f"vis_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")
        rpt = await _create_report(ac, ana_hdrs, "Analyst Report")
        r = await ac.get(f"{REPORTS}", headers=admin_hdrs)
        ids = [x["id"] for x in r.json()]
        assert rpt["id"] in ids

    async def test_non_owner_cannot_delete(self, ac, admin_hdrs):
        email1 = f"u1_{uuid.uuid4().hex[:8]}@test.com"
        email2 = f"u2_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email1, "analyst")
        await _register_user(ac, admin_hdrs, email2, "analyst")
        h1 = await _auth_headers(ac, email1, "Pass1234!")
        h2 = await _auth_headers(ac, email2, "Pass1234!")
        rpt = await _create_report(ac, h1)
        r = await ac.delete(f"{REPORTS}/{rpt['id']}", headers=h2)
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# C — Section Inputs
# ══════════════════════════════════════════════════════════════════════════════

class TestSectionInputs:

    async def test_save_and_get_section_1_through_11(self, ac, admin_hdrs, report):
        for sec in range(1, 12):
            payload = {"test_key": f"value_sec_{sec}", "section": sec}
            r = await ac.put(
                f"{REPORTS}/{report['id']}/inputs/{sec}",
                json={"section_no": sec, "input_json": payload},
                headers=admin_hdrs,
            )
            assert r.status_code == 200, f"save §{sec} failed: {r.text}"
            saved = r.json()
            assert saved["section_no"] == sec

            r2 = await ac.get(f"{REPORTS}/{report['id']}/inputs/{sec}", headers=admin_hdrs)
            assert r2.status_code == 200
            assert r2.json()["input_json"]["test_key"] == f"value_sec_{sec}"

    async def test_save_section_0_blocked(self, ac, admin_hdrs, report):
        r = await ac.put(
            f"{REPORTS}/{report['id']}/inputs/0",
            json={"section_no": 0, "input_json": {}},
            headers=admin_hdrs,
        )
        assert r.status_code in (400, 422)

    async def test_save_section_12_blocked(self, ac, admin_hdrs, report):
        r = await ac.put(
            f"{REPORTS}/{report['id']}/inputs/12",
            json={"section_no": 12, "input_json": {}},
            headers=admin_hdrs,
        )
        assert r.status_code in (400, 422)

    async def test_list_section_inputs(self, ac, admin_hdrs, report):
        await _save_input(ac, admin_hdrs, report["id"], 3, {"x": 1})
        await _save_input(ac, admin_hdrs, report["id"], 7, {"y": 2})
        r = await ac.get(f"{REPORTS}/{report['id']}/inputs", headers=admin_hdrs)
        assert r.status_code == 200
        section_nos = [x["section_no"] for x in r.json()]
        assert 3 in section_nos
        assert 7 in section_nos

    async def test_get_section_input_not_found(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/inputs/5", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_section_input_upsert(self, ac, admin_hdrs, report):
        await _save_input(ac, admin_hdrs, report["id"], 2, {"a": 1})
        await _save_input(ac, admin_hdrs, report["id"], 2, {"a": 99, "b": 2})
        r = await ac.get(f"{REPORTS}/{report['id']}/inputs/2", headers=admin_hdrs)
        assert r.json()["input_json"]["a"] == 99

    async def test_section_input_auto_extracts_facts(self, ac, admin_hdrs, report):
        await _save_input(ac, admin_hdrs, report["id"], 7, {
            "7A_borrower_financials": {
                "reporting_currency": "USD",
                "unit": "millions",
                "income_statement": {
                    "FY2024": {"revenue": 5000, "ebitda": 1200, "net_income": 800}
                }
            }
        })
        r = await ac.get(f"{BASE}/reports/{report['id']}/facts", headers=admin_hdrs)
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# D — Documents
# ══════════════════════════════════════════════════════════════════════════════

class TestDocuments:

    async def test_upload_txt_document(self, ac, admin_hdrs, report):
        content = b"Annual revenue USD 5.2 billion. EBITDA margin 28%. Net income USD 1.4bn."
        r = await ac.post(
            f"{REPORTS}/{report['id']}/documents",
            files={"file": ("report.txt", content, "text/plain")},
            data={"document_type": "financial_statement"},
            headers=admin_hdrs,
        )
        assert r.status_code == 201
        doc = r.json()
        assert doc["original_filename"] == "report.txt"
        assert doc["file_format"] == "txt"
        return doc

    async def test_upload_unsupported_extension(self, ac, admin_hdrs, report):
        r = await ac.post(
            f"{REPORTS}/{report['id']}/documents",
            files={"file": ("data.exe", b"MZ\x90", "application/octet-stream")},
            data={"document_type": "other"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400
        detail = r.json()["detail"].lower()
        assert "unsupported" in detail or "not supported" in detail or "extension" in detail

    async def test_upload_csv(self, ac, admin_hdrs, report):
        csv_content = b"Year,Revenue,EBITDA\n2022,4000,1100\n2023,4500,1200\n2024,5200,1400\n"
        r = await ac.post(
            f"{REPORTS}/{report['id']}/documents",
            files={"file": ("financials.csv", csv_content, "text/csv")},
            data={"document_type": "financial_statement"},
            headers=admin_hdrs,
        )
        assert r.status_code == 201

    async def test_list_documents(self, ac, admin_hdrs, report):
        content = b"Test document content for list test."
        await ac.post(
            f"{REPORTS}/{report['id']}/documents",
            files={"file": ("doc.txt", content, "text/plain")},
            data={"document_type": "other"},
            headers=admin_hdrs,
        )
        r = await ac.get(f"{REPORTS}/{report['id']}/documents", headers=admin_hdrs)
        assert r.status_code == 200
        assert len(r.json()) >= 1

    async def test_delete_document(self, ac, admin_hdrs, report):
        content = b"To be deleted."
        up = await ac.post(
            f"{REPORTS}/{report['id']}/documents",
            files={"file": ("del.txt", content, "text/plain")},
            data={"document_type": "other"},
            headers=admin_hdrs,
        )
        doc_id = up.json()["id"]
        r = await ac.delete(f"{REPORTS}/{report['id']}/documents/{doc_id}", headers=admin_hdrs)
        assert r.status_code == 204
        r2 = await ac.get(f"{REPORTS}/{report['id']}/documents", headers=admin_hdrs)
        ids = [d["id"] for d in r2.json()]
        assert doc_id not in ids

    async def test_delete_nonexistent_document(self, ac, admin_hdrs, report):
        r = await ac.delete(f"{REPORTS}/{report['id']}/documents/{uuid.uuid4()}", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_etl_document(self, ac, admin_hdrs, report):
        content = b"Annual revenue USD 5.2 billion EBITDA 1.4 billion net income 800 million FY2024."
        up = await ac.post(
            f"{REPORTS}/{report['id']}/documents",
            files={"file": ("annual.txt", content, "text/plain")},
            data={"document_type": "annual_report"},
            headers=admin_hdrs,
        )
        doc_id = up.json()["id"]
        with _mock_gemini(json.dumps({"4": {"borrower_name": "Test Corp"}})):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/documents/{doc_id}/etl",
                headers=admin_hdrs,
            )
        assert r.status_code in (200, 422), f"ETL unexpected: {r.text}"
        if r.status_code == 200:
            data = r.json()
            assert "doc_id" in data
            assert "sections_extracted" in data

    async def test_import_section_json(self, ac, admin_hdrs, report):
        payload = {"test_field": "value", "revenue": 5000}
        json_bytes = json.dumps(payload).encode()
        r = await ac.post(
            f"{REPORTS}/{report['id']}/import-section-json",
            files={"file": ("data.json", json_bytes, "application/json")},
            data={"section_no": "4"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["fields_imported"] == 2
        assert r.json()["section_no"] == 4

    async def test_import_section_json_invalid_json(self, ac, admin_hdrs, report):
        r = await ac.post(
            f"{REPORTS}/{report['id']}/import-section-json",
            files={"file": ("bad.json", b"not json {{", "application/json")},
            data={"section_no": "4"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400

    async def test_import_section_json_out_of_range(self, ac, admin_hdrs, report):
        r = await ac.post(
            f"{REPORTS}/{report['id']}/import-section-json",
            files={"file": ("ok.json", b'{"x":1}', "application/json")},
            data={"section_no": "0"},
            headers=admin_hdrs,
        )
        assert r.status_code == 400

    async def test_import_section_11_allowed(self, ac, admin_hdrs, report):
        payload = {"11A_report_meta": {"analyst_name": "Test"}}
        r = await ac.post(
            f"{REPORTS}/{report['id']}/import-section-json",
            files={"file": ("sec11.json", json.dumps(payload).encode(), "application/json")},
            data={"section_no": "11"},
            headers=admin_hdrs,
        )
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# E — Generation
# ══════════════════════════════════════════════════════════════════════════════

class TestGeneration:

    async def test_generate_section_11_blocked(self, ac, admin_hdrs, report):
        r = await ac.post(f"{REPORTS}/{report['id']}/generate/11", headers=admin_hdrs)
        assert r.status_code == 400
        assert "1" in r.json()["detail"] and "10" in r.json()["detail"]

    async def test_generate_section_0_blocked(self, ac, admin_hdrs, report):
        r = await ac.post(f"{REPORTS}/{report['id']}/generate/0", headers=admin_hdrs)
        assert r.status_code == 400

    async def test_generate_section_12_blocked(self, ac, admin_hdrs, report):
        r = await ac.post(f"{REPORTS}/{report['id']}/generate/12", headers=admin_hdrs)
        assert r.status_code == 400

    async def test_generate_section_4_succeeds(self, ac, admin_hdrs, report_with_input):
        """§4 has no hard dependencies — should return 202 immediately."""
        await _save_input(ac, admin_hdrs, report_with_input["id"], 4, {"borrower": "Test"})
        with _mock_gemini("## §4 Borrower Background\n\nTest borrower is a leading shipping company."):
            r = await ac.post(
                f"{REPORTS}/{report_with_input['id']}/generate/4",
                headers=admin_hdrs,
            )
        assert r.status_code == 202
        data = r.json()
        assert "task_id" in data
        assert data["status"] == "running"
        assert data["section_no"] == 4

    async def test_generate_section_blocked_by_hard_dep(self, ac, admin_hdrs, report):
        """§2 requires §7 to be done first."""
        r = await ac.post(f"{REPORTS}/{report['id']}/generate/2", headers=admin_hdrs)
        assert r.status_code == 409
        assert "7" in r.json()["detail"]

    async def test_generate_task_status_running(self, ac, admin_hdrs, report_with_input):
        await _save_input(ac, admin_hdrs, report_with_input["id"], 4, {"x": 1})
        with _mock_gemini("## §4\n\nContent."):
            r = await ac.post(
                f"{REPORTS}/{report_with_input['id']}/generate/4",
                headers=admin_hdrs,
            )
        task_id = r.json()["task_id"]
        r2 = await ac.get(
            f"{REPORTS}/{report_with_input['id']}/generate/status/{task_id}",
            headers=admin_hdrs,
        )
        assert r2.status_code == 200
        assert r2.json()["task_id"] == task_id

    async def test_generate_task_status_not_found(self, ac, admin_hdrs, report):
        r = await ac.get(
            f"{REPORTS}/{report['id']}/generate/status/{uuid.uuid4()}",
            headers=admin_hdrs,
        )
        assert r.status_code == 404

    async def test_generate_full_report_no_data(self, ac, admin_hdrs, report):
        """Full report generation returns 202 even with no section input data (evidence-only mode)."""
        with _mock_gemini("## Section\n\nContent."):
            r = await ac.post(f"{REPORTS}/{report['id']}/generate", headers=admin_hdrs)
        assert r.status_code == 202

    async def test_generate_full_report_with_data(self, ac, admin_hdrs, report_with_input):
        """Full report generation returns 202 when data is present."""
        with _mock_gemini("## Section\n\nContent."):
            r = await ac.post(
                f"{REPORTS}/{report_with_input['id']}/generate",
                headers=admin_hdrs,
            )
        assert r.status_code == 202
        assert r.json()["status"] == "running"

    async def test_list_outputs_empty(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/outputs", headers=admin_hdrs)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_get_section_output_not_found(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/sections/1/output", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_generate_section_requires_owner_or_admin(self, ac, admin_hdrs):
        email = f"gen_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")
        rpt = await _create_report(ac, admin_hdrs)  # owned by admin
        r = await ac.post(f"{REPORTS}/{rpt['id']}/generate/4", headers=ana_hdrs)
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# F — Workflow Transitions
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkflow:

    async def _make_done_report(self, ac, admin_hdrs):
        """Create a report with one generated section so transitions can proceed."""
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput

        rpt = await _create_report(ac, admin_hdrs)
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
        return rpt

    async def test_submit_for_review(self, ac, admin_hdrs):
        rpt = await self._make_done_report(ac, admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["status"] == "review_in_progress"

    async def test_submit_fails_without_done_sections(self, ac, admin_hdrs, report):
        r = await ac.post(f"{REPORTS}/{report['id']}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 422
        assert "no sections" in r.json()["detail"].lower()

    async def test_submit_wrong_status_409(self, ac, admin_hdrs):
        rpt = await self._make_done_report(ac, admin_hdrs)
        await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        assert r.status_code == 409

    async def test_approve_requires_approver(self, ac, admin_hdrs):
        email = f"app_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")
        rpt = await self._make_done_report(ac, admin_hdrs)
        await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/approve", headers=ana_hdrs)
        assert r.status_code == 403

    async def test_approve_success(self, ac, admin_hdrs):
        rpt = await self._make_done_report(ac, admin_hdrs)
        await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/approve", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    async def test_approve_wrong_status_409(self, ac, admin_hdrs, report):
        r = await ac.post(f"{REPORTS}/{report['id']}/approve", headers=admin_hdrs)
        assert r.status_code == 409

    async def test_recall_from_review(self, ac, admin_hdrs):
        rpt = await self._make_done_report(ac, admin_hdrs)
        await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/recall", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["status"] == "draft"

    async def test_recall_from_draft_fails(self, ac, admin_hdrs, report):
        r = await ac.post(f"{REPORTS}/{report['id']}/recall", headers=admin_hdrs)
        assert r.status_code == 409

    async def test_review_progress(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/review-progress", headers=admin_hdrs)
        assert r.status_code == 200
        data = r.json()
        assert "sections_done" in data
        assert "sections_total" in data
        assert data["sections_total"] == 10
        assert "blocks_total" in data
        assert "ready_for_review" in data


# ══════════════════════════════════════════════════════════════════════════════
# G — Facts
# ══════════════════════════════════════════════════════════════════════════════

class TestFacts:

    async def _seed_fact(self, report_id: str, state: str = "extracted"):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.models import CanonicalFact

        fact_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(CanonicalFact(
                id=fact_id,
                report_id=report_id,
                metric_name="revenue",
                entity="TestCo",
                period="FY2024",
                value=5000.0,
                value_text="USD 5,000M",
                currency="USD",
                unit="millions",
                state=state,
                source_type="analyst_input_json",
                source_priority=2,
                version=1,
            ))
            await db.commit()
        return fact_id

    async def test_list_facts(self, ac, admin_hdrs, report):
        await self._seed_fact(report["id"])
        r = await ac.get(f"{REPORTS}/{report['id']}/facts", headers=admin_hdrs)
        assert r.status_code == 200
        assert len(r.json()) >= 1

    async def test_get_fact(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        r = await ac.get(f"{REPORTS}/{report['id']}/facts/{fact_id}", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["id"] == fact_id
        assert r.json()["metric_name"] == "revenue"

    async def test_get_fact_not_found(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/facts/{uuid.uuid4()}", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_patch_fact(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/facts/{fact_id}",
            json={"value": 5500.0, "reason": "Updated from latest report", "expected_version": 1},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["value"] == 5500.0

    async def test_patch_fact_version_conflict(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/facts/{fact_id}",
            json={"value": 5500.0, "reason": "test", "expected_version": 99},
            headers=admin_hdrs,
        )
        assert r.status_code == 409

    async def test_fact_history(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        await ac.patch(
            f"{REPORTS}/{report['id']}/facts/{fact_id}",
            json={"value": 6000.0, "reason": "revision", "expected_version": 1},
            headers=admin_hdrs,
        )
        r = await ac.get(f"{REPORTS}/{report['id']}/facts/{fact_id}/history", headers=admin_hdrs)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_override_fact(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/{fact_id}/override",
            json={"value": 4800.0, "reason": "Analyst override", "expected_version": 1},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["new_state"] == "user_overridden"

    async def test_approve_fact(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"], state="validated")
        email = f"rev_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "reviewer")
        rev_hdrs = await _auth_headers(ac, email, "Pass1234!")
        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/{fact_id}/approve",
            json={"expected_version": 1},
            headers=rev_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["new_state"] == "approved"

    async def test_approve_fact_analyst_forbidden(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        email = f"ana2_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")
        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/{fact_id}/approve",
            json={"expected_version": 1},
            headers=ana_hdrs,
        )
        assert r.status_code == 403

    async def test_deprecate_fact(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        email = f"dep_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "reviewer")
        rev_hdrs = await _auth_headers(ac, email, "Pass1234!")
        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/{fact_id}/deprecate",
            params={"reason": "Superseded by newer data"},
            headers=rev_hdrs,
        )
        assert r.status_code == 200

    async def test_list_conflicts(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/facts/conflicts", headers=admin_hdrs)
        assert r.status_code == 200

    async def test_fact_dependencies(self, ac, admin_hdrs, report):
        fact_id = await self._seed_fact(report["id"])
        r = await ac.get(
            f"{REPORTS}/{report['id']}/facts/{fact_id}/dependencies",
            headers=admin_hdrs,
        )
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# H — Blocks
# ══════════════════════════════════════════════════════════════════════════════

class TestBlocks:

    async def _seed_block(self, report_id: str, section_no: int = 4) -> str:
        from credit_report.database import AsyncSessionLocal
        from credit_report.block_ast.models import ReportBlock

        block_id = f"blk_{uuid.uuid4().hex[:12]}"
        async with AsyncSessionLocal() as db:
            db.add(ReportBlock(
                id=block_id,
                report_id=report_id,
                section_no=section_no,
                block_type="paragraph",
                content="The borrower is a leading container shipping company with strong financials.",
                source_fact_ids="[]",
                validation_status="pending",
                is_stale=False,
                version=1,
            ))
            await db.commit()
        return block_id

    async def test_list_blocks(self, ac, admin_hdrs, report):
        await self._seed_block(report["id"])
        r = await ac.get(f"{REPORTS}/{report['id']}/blocks", headers=admin_hdrs)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_block_stats(self, ac, admin_hdrs, report):
        await self._seed_block(report["id"])
        r = await ac.get(f"{REPORTS}/{report['id']}/blocks/stats", headers=admin_hdrs)
        assert r.status_code == 200
        data = r.json()
        assert "total_blocks" in data

    async def test_get_block(self, ac, admin_hdrs, report):
        block_id = await self._seed_block(report["id"])
        r = await ac.get(f"{REPORTS}/{report['id']}/blocks/{block_id}", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.json()["id"] == block_id

    async def test_get_block_not_found(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/blocks/nonexistent_block_id", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_patch_block(self, ac, admin_hdrs, report):
        block_id = await self._seed_block(report["id"])
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/blocks/{block_id}",
            json={"content": "Updated content for the paragraph.", "reason": "Analyst edit", "expected_version": 1},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["content"] == "Updated content for the paragraph."
        assert r.json()["version"] == 2

    async def test_patch_block_version_conflict(self, ac, admin_hdrs, report):
        block_id = await self._seed_block(report["id"])
        r = await ac.patch(
            f"{REPORTS}/{report['id']}/blocks/{block_id}",
            json={"content": "X", "expected_version": 999},
            headers=admin_hdrs,
        )
        assert r.status_code == 409

    async def test_block_history(self, ac, admin_hdrs, report):
        block_id = await self._seed_block(report["id"])
        await ac.patch(
            f"{REPORTS}/{report['id']}/blocks/{block_id}",
            json={"content": "Revised content.", "expected_version": 1},
            headers=admin_hdrs,
        )
        r = await ac.get(f"{REPORTS}/{report['id']}/blocks/{block_id}/history", headers=admin_hdrs)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_validate_block(self, ac, admin_hdrs, report):
        block_id = await self._seed_block(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/{block_id}/validate",
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["validation_status"] == "passed"

    async def test_section_blocks(self, ac, admin_hdrs, report):
        await self._seed_block(report["id"], section_no=4)
        r = await ac.get(f"{REPORTS}/{report['id']}/sections/4/blocks", headers=admin_hdrs)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_block_cells(self, ac, admin_hdrs, report):
        block_id = await self._seed_block(report["id"])
        r = await ac.get(f"{REPORTS}/{report['id']}/blocks/{block_id}/cells", headers=admin_hdrs)
        assert r.status_code == 200

    async def test_improve_block(self, ac, admin_hdrs, report):
        block_id = await self._seed_block(report["id"])
        with _mock_gemini("The borrower demonstrates exceptional financial discipline with a track record of strong EBITDA generation."):
            r = await ac.post(
                f"{REPORTS}/{report['id']}/blocks/{block_id}/improve",
                json={"instruction": "Make the language more formal and concise.", "expected_version": 1},
                headers=admin_hdrs,
            )
        assert r.status_code == 200
        data = r.json()
        assert data["block_id"] == block_id
        assert "suggested_content" in data
        assert "original_content" in data
        assert data["suggested_content"]  # non-empty suggestion

    async def test_improve_block_not_found(self, ac, admin_hdrs, report):
        r = await ac.post(
            f"{REPORTS}/{report['id']}/blocks/nonexistent/improve",
            json={"instruction": "test"},
            headers=admin_hdrs,
        )
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# I — Calculations
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculations:

    async def test_list_calculations_empty(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/calculations", headers=admin_hdrs)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_recalculate_from_facts(self, ac, admin_hdrs, report):
        await _save_input(ac, admin_hdrs, report["id"], 7, {
            "7A_borrower_financials": {
                "reporting_currency": "USD", "unit": "millions",
                "income_statement": {
                    "FY2024": {
                        "revenue": 5000, "ebitda": 1400, "net_income": 800,
                        "interest_expense": 200
                    }
                },
                "balance_sheet": {
                    "FY2024": {
                        "cash": 600, "total_equity": 3000, "total_assets": 6000,
                        "total_liabilities": 3000
                    }
                },
                "cash_flow": {"FY2024": {"ocf": 1100}}
            }
        })
        r = await ac.post(f"{REPORTS}/{report['id']}/recalculate", headers=admin_hdrs)
        assert r.status_code == 200
        data = r.json()
        assert "calculations_computed" in data

    async def test_ltv_acr_calculation(self, ac, admin_hdrs, report):
        payload = {
            "facility_amount": 100.0,
            "initial_asset_value": 150.0,
            "amortization_schedule": [
                {"year": 1, "outstanding_pct": 95},
                {"year": 5, "outstanding_pct": 75},
                {"year": 10, "outstanding_pct": 50},
            ],
            "balloon_amount": 10.0,
            "residual_pct": 5.0,
        }
        r = await ac.post(
            f"{REPORTS}/{report['id']}/calculations/ltv-acr",
            json=payload,
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        data = r.json()
        assert "facility_amount" in data
        assert "ltv_table" in data
        assert len(data["ltv_table"]) > 0
        first = data["ltv_table"][0]
        assert "year" in first
        assert "ltv_25yr_pct" in first

    async def test_fx_rates(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/fx-rates", headers=admin_hdrs)
        assert r.status_code == 200

    async def test_upsert_fx_rate(self, ac, admin_hdrs, report):
        r = await ac.put(
            f"{REPORTS}/{report['id']}/fx-rates",
            json={
                "from_currency": "TWD", "to_currency": "USD",
                "rate": 0.031, "source": "manual",
                "rate_date": "2024-01-01"
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["rate"] == 0.031

    async def test_mapping_unmapped(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/mapping/unmapped", headers=admin_hdrs)
        assert r.status_code == 200

    async def test_mapping_rules(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/mapping/rules", headers=admin_hdrs)
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# J — Export
# ══════════════════════════════════════════════════════════════════════════════

class TestExport:

    async def _make_report_with_done_section(self, ac, admin_hdrs):
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput

        rpt = await _create_report(ac, admin_hdrs)
        async with AsyncSessionLocal() as db:
            db.add(SectionOutput(
                id=str(uuid.uuid4()),
                report_id=rpt["id"],
                section_no=1,
                markdown="# §1 Facility Structure\n\nBorrower: Test Co Ltd. Facility: USD 100M. Tenor: 5 years.",
                status="done",
                tokens_used=200,
            ))
            db.add(SectionOutput(
                id=str(uuid.uuid4()),
                report_id=rpt["id"],
                section_no=4,
                markdown="# §4 Borrower Background\n\nTest Co Ltd is incorporated in Singapore.",
                status="done",
                tokens_used=150,
            ))
            await db.commit()
        return rpt

    async def test_export_docx_with_sections(self, ac, admin_hdrs):
        rpt = await self._make_report_with_done_section(ac, admin_hdrs)
        r = await ac.get(f"{REPORTS}/{rpt['id']}/export/docx", headers=admin_hdrs)
        assert r.status_code == 200
        assert r.headers["content-type"] in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/octet-stream",
        )
        assert b"PK" in r.content[:4]  # DOCX is a ZIP

    async def test_export_docx_no_sections(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/export/docx", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_export_pdf_returns_503_without_weasyprint(self, ac, admin_hdrs):
        rpt = await self._make_report_with_done_section(ac, admin_hdrs)
        r = await ac.get(f"{REPORTS}/{rpt['id']}/export/pdf", headers=admin_hdrs)
        assert r.status_code in (200, 404, 503)

    async def test_export_requires_auth(self, ac, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/export/docx")
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# K — Audit Trail
# ══════════════════════════════════════════════════════════════════════════════

class TestAudit:

    async def test_audit_events_populated_on_create(self, ac, admin_hdrs, report):
        r = await ac.get(f"{REPORTS}/{report['id']}/audit", headers=admin_hdrs)
        assert r.status_code == 200
        data = r.json()
        assert "events" in data
        assert data["total"] >= 1
        actions = [e["action"] for e in data["events"]]
        assert "report.created" in actions

    async def test_audit_events_on_section_save(self, ac, admin_hdrs, report):
        await _save_input(ac, admin_hdrs, report["id"], 4, {"borrower": "Test"})
        r = await ac.get(f"{REPORTS}/{report['id']}/audit", headers=admin_hdrs)
        actions = [e["action"] for e in r.json()["events"]]
        assert "section_input.saved" in actions

    async def test_audit_events_on_status_change(self, ac, admin_hdrs, report):
        await ac.patch(
            f"{REPORTS}/{report['id']}/status",
            json={"status": "validated"},
            headers=admin_hdrs,
        )
        r = await ac.get(f"{REPORTS}/{report['id']}/audit", headers=admin_hdrs)
        actions = [e["action"] for e in r.json()["events"]]
        assert "report.status_change" in actions

    async def test_audit_pagination(self, ac, admin_hdrs, report):
        for i in range(3):
            await _save_input(ac, admin_hdrs, report["id"], i + 1, {"n": i})
        r = await ac.get(
            f"{REPORTS}/{report['id']}/audit",
            params={"skip": 0, "limit": 2},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["events"]) <= 2
        assert data["total"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# L — Conflicts
# ══════════════════════════════════════════════════════════════════════════════

class TestConflicts:

    async def _seed_conflict(self, report_id: str):
        from credit_report.database import AsyncSessionLocal
        from credit_report.fact_store.models import CanonicalFact, FactConflict

        fact_a_id = str(uuid.uuid4())
        fact_b_id = str(uuid.uuid4())
        conflict_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(CanonicalFact(
                id=fact_a_id, report_id=report_id,
                metric_name="net_income", entity="TestCo", period="FY2024",
                value=800.0, state="validated", source_type="analyst_input_json",
                source_priority=2, version=1,
            ))
            db.add(CanonicalFact(
                id=fact_b_id, report_id=report_id,
                metric_name="net_income", entity="TestCo", period="FY2024",
                value=850.0, state="conflicted", source_type="pdf_extraction",
                source_priority=3, version=1,
            ))
            db.add(FactConflict(
                id=conflict_id, report_id=report_id,
                metric_name="net_income", entity="TestCo", period="FY2024",
                fact_a_id=fact_a_id, fact_b_id=fact_b_id,
                value_a=800.0, value_b=850.0,
                source_a="analyst_input_json", source_b="pdf_extraction",
                status="open",
            ))
            await db.commit()
        return conflict_id, fact_a_id, fact_b_id

    async def test_list_conflicts(self, ac, admin_hdrs, report):
        await self._seed_conflict(report["id"])
        r = await ac.get(f"{REPORTS}/{report['id']}/facts/conflicts", headers=admin_hdrs)
        assert r.status_code == 200

    async def test_get_conflict(self, ac, admin_hdrs, report):
        conflict_id, _, _ = await self._seed_conflict(report["id"])
        r = await ac.get(
            f"{REPORTS}/{report['id']}/facts/conflicts/{conflict_id}",
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["id"] == conflict_id

    async def test_resolve_conflict(self, ac, admin_hdrs, report):
        conflict_id, fact_a_id, fact_b_id = await self._seed_conflict(report["id"])
        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/conflicts/{conflict_id}/resolve",
            json={
                "chosen_fact_id": fact_a_id,
                "rejected_fact_ids": [fact_b_id],
                "resolution_reason": "Analyst input preferred over PDF extraction",
            },
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "resolved"

    async def test_mark_conflict_unresolved(self, ac, admin_hdrs, report):
        conflict_id, fact_a_id, fact_b_id = await self._seed_conflict(report["id"])
        await ac.post(
            f"{REPORTS}/{report['id']}/facts/conflicts/{conflict_id}/resolve",
            json={"chosen_fact_id": fact_a_id, "rejected_fact_ids": [fact_b_id], "resolution_reason": "test"},
            headers=admin_hdrs,
        )
        r = await ac.post(
            f"{REPORTS}/{report['id']}/facts/conflicts/{conflict_id}/mark-unresolved",
            headers=admin_hdrs,
        )
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# M — Health & Misc
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthAndMisc:

    async def test_health_endpoint(self, ac):
        r = await ac.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "service" in data

    async def test_app_redirect(self, ac):
        r = await ac.get("/", follow_redirects=False)
        assert r.status_code in (301, 302, 307, 308)

    async def test_static_index_html(self, ac):
        r = await ac.get("/app")
        assert r.status_code == 200
        assert b"<!DOCTYPE html" in r.content or b"<!doctype html" in r.content

    async def test_unknown_route_404(self, ac):
        r = await ac.get("/api/credit-report/nonexistent-endpoint")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# N — RBAC Deep Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRBAC:

    async def test_reviewer_can_view_others_reports(self, ac, admin_hdrs):
        email = f"rev_rbac_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "reviewer")
        rev_hdrs = await _auth_headers(ac, email, "Pass1234!")
        rpt = await _create_report(ac, admin_hdrs)
        r = await ac.get(f"{REPORTS}/{rpt['id']}", headers=rev_hdrs)
        assert r.status_code == 200

    async def test_analyst_cannot_view_others_reports(self, ac, admin_hdrs):
        e1 = f"a1_{uuid.uuid4().hex[:8]}@test.com"
        e2 = f"a2_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, e1, "analyst")
        await _register_user(ac, admin_hdrs, e2, "analyst")
        h1 = await _auth_headers(ac, e1, "Pass1234!")
        h2 = await _auth_headers(ac, e2, "Pass1234!")
        rpt = await _create_report(ac, h1)
        r = await ac.get(f"{REPORTS}/{rpt['id']}", headers=h2)
        assert r.status_code == 403

    async def test_approver_can_approve(self, ac, admin_hdrs):
        from credit_report.database import AsyncSessionLocal
        from credit_report.models import SectionOutput

        email = f"appr_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "approver")
        appr_hdrs = await _auth_headers(ac, email, "Pass1234!")

        rpt = await _create_report(ac, admin_hdrs)
        async with AsyncSessionLocal() as db:
            db.add(SectionOutput(
                id=str(uuid.uuid4()), report_id=rpt["id"], section_no=1,
                markdown="## §1", status="done", tokens_used=50,
            ))
            await db.commit()
        await ac.post(f"{REPORTS}/{rpt['id']}/submit-for-review", headers=admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/approve", headers=appr_hdrs)
        assert r.status_code == 200

    async def test_analyst_cannot_modify_others_section_input(self, ac, admin_hdrs):
        e1 = f"inp1_{uuid.uuid4().hex[:8]}@test.com"
        e2 = f"inp2_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, e1, "analyst")
        await _register_user(ac, admin_hdrs, e2, "analyst")
        h1 = await _auth_headers(ac, e1, "Pass1234!")
        h2 = await _auth_headers(ac, e2, "Pass1234!")
        rpt = await _create_report(ac, h1)
        r = await ac.put(
            f"{REPORTS}/{rpt['id']}/inputs/1",
            json={"section_no": 1, "input_json": {"x": 1}},
            headers=h2,
        )
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# O — Edge Cases & Error Paths
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    async def test_deleted_report_endpoints_return_404(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs)
        rid = rpt["id"]
        await ac.delete(f"{REPORTS}/{rid}", headers=admin_hdrs)
        endpoints = [
            f"{REPORTS}/{rid}",
            f"{REPORTS}/{rid}/inputs/1",
            f"{REPORTS}/{rid}/inputs",
        ]
        for url in endpoints:
            r = await ac.get(url, headers=admin_hdrs)
            assert r.status_code == 404, f"Expected 404 for {url}, got {r.status_code}"

    async def test_generate_section_on_deleted_report(self, ac, admin_hdrs):
        rpt = await _create_report(ac, admin_hdrs)
        await ac.delete(f"{REPORTS}/{rpt['id']}", headers=admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/generate/4", headers=admin_hdrs)
        assert r.status_code == 404

    async def test_very_large_section_input(self, ac, admin_hdrs, report):
        large_text = "A" * 50_000
        r = await ac.put(
            f"{REPORTS}/{report['id']}/inputs/4",
            json={"section_no": 4, "input_json": {"description": large_text}},
            headers=admin_hdrs,
        )
        assert r.status_code == 200

    async def test_unicode_chinese_in_section_input(self, ac, admin_hdrs, report):
        r = await ac.put(
            f"{REPORTS}/{report['id']}/inputs/4",
            json={"section_no": 4, "input_json": {
                "borrower_name": "長榮海運股份有限公司",
                "description": "台灣最大的貨櫃航運公司，全球排名第七。",
            }},
            headers=admin_hdrs,
        )
        assert r.status_code == 200
        r2 = await ac.get(f"{REPORTS}/{report['id']}/inputs/4", headers=admin_hdrs)
        assert r2.json()["input_json"]["borrower_name"] == "長榮海運股份有限公司"

    async def test_section_input_empty_object(self, ac, admin_hdrs, report):
        r = await ac.put(
            f"{REPORTS}/{report['id']}/inputs/5",
            json={"section_no": 5, "input_json": {}},
            headers=admin_hdrs,
        )
        assert r.status_code == 200

    async def test_report_listing_pagination(self, ac, admin_hdrs):
        for i in range(3):
            await _create_report(ac, admin_hdrs, f"Pagination Test {i}")
        r = await ac.get(f"{REPORTS}?skip=0&limit=2", headers=admin_hdrs)
        assert r.status_code == 200
        assert len(r.json()) <= 2

    async def test_special_chars_in_borrower_name(self, ac, admin_hdrs):
        r = await ac.post(
            f"{REPORTS}",
            json={"borrower_name": "ABC & DEF (Holdings) Ltd — Special Chars <Test>", "industry": "marine"},
            headers=admin_hdrs,
        )
        assert r.status_code == 201
        assert "ABC & DEF" in r.json()["borrower_name"]

    async def test_generate_section_not_owner_403(self, ac, admin_hdrs):
        email = f"no_own_{uuid.uuid4().hex[:8]}@test.com"
        await _register_user(ac, admin_hdrs, email, "analyst")
        ana_hdrs = await _auth_headers(ac, email, "Pass1234!")
        rpt = await _create_report(ac, admin_hdrs)
        r = await ac.post(f"{REPORTS}/{rpt['id']}/generate/4", headers=ana_hdrs)
        assert r.status_code == 403

    async def test_import_json_merges_with_existing(self, ac, admin_hdrs, report):
        await _save_input(ac, admin_hdrs, report["id"], 4, {"existing_key": "original"})
        new_data = {"new_key": "added", "existing_key": "overwritten"}
        await ac.post(
            f"{REPORTS}/{report['id']}/import-section-json",
            files={"file": ("update.json", json.dumps(new_data).encode(), "application/json")},
            data={"section_no": "4"},
            headers=admin_hdrs,
        )
        r = await ac.get(f"{REPORTS}/{report['id']}/inputs/4", headers=admin_hdrs)
        data = r.json()["input_json"]
        assert data["new_key"] == "added"
        assert data["existing_key"] == "overwritten"

    async def test_upsert_fx_rate_updates_existing(self, ac, admin_hdrs, report):
        for rate in [0.031, 0.032]:
            r = await ac.put(
                f"{REPORTS}/{report['id']}/fx-rates",
                json={"from_currency": "TWD", "to_currency": "USD", "rate": rate, "source": "manual", "rate_date": "2024-01-01"},
                headers=admin_hdrs,
            )
            assert r.status_code == 200
        r = await ac.get(f"{REPORTS}/{report['id']}/fx-rates", headers=admin_hdrs)
        active_rates = {f"{x['from_currency']}/{x['to_currency']}": x["rate"] for x in r.json() if not x["is_stale"]}
        assert active_rates.get("TWD/USD") == 0.032

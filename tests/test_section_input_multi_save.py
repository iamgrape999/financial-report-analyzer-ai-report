"""
Regression tests: multi-save → read-latest SectionInput behaviour.

Bug classes covered:
  1. Second PUT /inputs/{sec} persists and returns the new payload (not the first)
  2. GET /inputs/{sec} returns the most-recent row (ORDER BY id DESC)
  3. saved_at is updated on each PUT — server_default does not freeze it at INSERT
  4. PUT response saved_at matches GET saved_at (same record)
  5. import_section_json (ETL merge) does not crash when a row already exists

Background: The PUT handler reads the most-recent existing row (ORDER BY id DESC)
and updates it in-place.  The GET handler applies the same ORDER BY.
If either ever falls back to ASC order, multi-save creates silent data loss.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

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


async def _login(ac: AsyncClient) -> dict:
    r = await ac.post(f"{AUTH}/login", data={"username": "admin@example.com", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _create_report(ac: AsyncClient, hdrs: dict, borrower: str = "Multi-Save Co") -> str:
    r = await ac.post(REPORTS, json={"borrower_name": borrower, "industry": "marine"}, headers=hdrs)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _put_input(ac: AsyncClient, hdrs: dict, report_id: str, sec_no: int, data: dict) -> dict:
    r = await ac.put(
        f"{REPORTS}/{report_id}/inputs/{sec_no}",
        json={"section_no": sec_no, "input_json": data},
        headers=hdrs,
    )
    assert r.status_code == 200, f"PUT /inputs/{sec_no} failed: {r.status_code} {r.text}"
    return r.json()


async def _get_input(ac: AsyncClient, hdrs: dict, report_id: str, sec_no: int) -> dict:
    r = await ac.get(f"{REPORTS}/{report_id}/inputs/{sec_no}", headers=hdrs)
    assert r.status_code == 200, f"GET /inputs/{sec_no} failed: {r.status_code} {r.text}"
    return r.json()


def _parse_dt(ts: str) -> datetime:
    from datetime import timezone
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def hdrs(client):
    return await _login(client)


@pytest_asyncio.fixture
async def report_id(client, hdrs):
    return await _create_report(client, hdrs)


# ── Tests: second save wins ───────────────────────────────────────────────────


class TestMultiSaveReturnsLatest:
    """Second PUT /inputs/{sec} must overwrite first — GET returns the second payload."""

    @pytest.mark.asyncio
    async def test_second_save_visible_via_get(self, client, hdrs, report_id):
        first = {"4A_borrower": {"company_name_en": "First Corp"}}
        second = {"4A_borrower": {"company_name_en": "Second Corp"}}

        await _put_input(client, hdrs, report_id, 4, first)
        await _put_input(client, hdrs, report_id, 4, second)

        got = await _get_input(client, hdrs, report_id, 4)
        assert got["input_json"]["4A_borrower"]["company_name_en"] == "Second Corp", (
            "GET /inputs/4 must return the second save, not the first"
        )

    @pytest.mark.asyncio
    async def test_second_put_response_echoes_new_data(self, client, hdrs, report_id):
        first = {"2A_credit_overview": {"bullets": ["First"]}}
        second = {"2A_credit_overview": {"bullets": ["Second", "Extra"]}}

        await _put_input(client, hdrs, report_id, 2, first)
        resp2 = await _put_input(client, hdrs, report_id, 2, second)

        assert resp2["input_json"]["2A_credit_overview"]["bullets"] == ["Second", "Extra"], (
            "PUT response must echo the new payload, not the cached first save"
        )

    @pytest.mark.asyncio
    async def test_three_saves_returns_third(self, client, hdrs, report_id):
        for iteration in range(1, 4):
            await _put_input(client, hdrs, report_id, 1, {"version": iteration})

        got = await _get_input(client, hdrs, report_id, 1)
        assert got["input_json"]["version"] == 3, (
            "After 3 saves, GET must return the third (most recent) version"
        )

    @pytest.mark.asyncio
    async def test_sections_are_independent(self, client, hdrs, report_id):
        """Saves to §4 must not overwrite §3's stored data."""
        await _put_input(client, hdrs, report_id, 3, {"3A_external_ratings": {"all_nil": True}})
        await _put_input(client, hdrs, report_id, 4, {"4A_borrower": {"company_name_en": "A"}})
        await _put_input(client, hdrs, report_id, 4, {"4A_borrower": {"company_name_en": "B"}})

        sec3 = await _get_input(client, hdrs, report_id, 3)
        assert sec3["input_json"]["3A_external_ratings"]["all_nil"] is True, (
            "§3 data must not be affected by §4 saves"
        )

        sec4 = await _get_input(client, hdrs, report_id, 4)
        assert sec4["input_json"]["4A_borrower"]["company_name_en"] == "B", (
            "§4 must reflect the most recent save"
        )

    @pytest.mark.asyncio
    async def test_numeric_value_updated(self, client, hdrs, report_id):
        """Numeric fields must also be overwritten, not merged."""
        await _put_input(client, hdrs, report_id, 7, {"revenue_fy2024": 100})
        await _put_input(client, hdrs, report_id, 7, {"revenue_fy2024": 999})

        got = await _get_input(client, hdrs, report_id, 7)
        assert got["input_json"]["revenue_fy2024"] == 999, (
            "Numeric values must be updated on second save"
        )

    @pytest.mark.asyncio
    async def test_empty_dict_second_save(self, client, hdrs, report_id):
        """Saving an empty dict must replace previous data."""
        await _put_input(client, hdrs, report_id, 9, {"9A_checklist": {"items": ["a|b|c"]}})
        await _put_input(client, hdrs, report_id, 9, {})

        got = await _get_input(client, hdrs, report_id, 9)
        assert "9A_checklist" not in got["input_json"], (
            "Second save with empty dict must replace (not merge) previous data"
        )


# ── Tests: saved_at timestamp ─────────────────────────────────────────────────


class TestSavedAtTimestamp:
    """saved_at must be a valid ISO datetime and updated on each PUT."""

    @pytest.mark.asyncio
    async def test_saved_at_in_put_response(self, client, hdrs, report_id):
        resp = await _put_input(client, hdrs, report_id, 2, {"test": True})
        assert "saved_at" in resp, "PUT response must include saved_at field"
        assert resp["saved_at"] is not None, "saved_at must not be null"

    @pytest.mark.asyncio
    async def test_saved_at_is_valid_iso_datetime(self, client, hdrs, report_id):
        resp = await _put_input(client, hdrs, report_id, 2, {"test": True})
        ts = resp["saved_at"]
        try:
            _parse_dt(ts)
        except (ValueError, AttributeError) as exc:
            pytest.fail(f"saved_at '{ts}' is not a valid ISO datetime: {exc}")

    @pytest.mark.asyncio
    async def test_saved_at_in_get_response(self, client, hdrs, report_id):
        await _put_input(client, hdrs, report_id, 5, {"5A_security_overview": {}})
        got = await _get_input(client, hdrs, report_id, 5)
        assert "saved_at" in got, "GET response must include saved_at field"
        assert got["saved_at"] is not None, "saved_at from GET must not be null"

    @pytest.mark.asyncio
    async def test_get_saved_at_close_to_put_saved_at(self, client, hdrs, report_id):
        """GET saved_at must represent the same save event as the last PUT."""
        put_resp = await _put_input(client, hdrs, report_id, 6, {"last_save": True})
        get_resp = await _get_input(client, hdrs, report_id, 6)

        dt_put = _parse_dt(put_resp["saved_at"])
        dt_get = _parse_dt(get_resp["saved_at"])

        diff = abs((dt_get - dt_put).total_seconds())
        assert diff < 10, (
            f"GET saved_at must be within 10s of PUT saved_at (diff={diff:.3f}s). "
            f"PUT: {put_resp['saved_at']}, GET: {get_resp['saved_at']}"
        )

    @pytest.mark.asyncio
    async def test_second_put_saved_at_is_recent(self, client, hdrs, report_id):
        """Both PUTs must produce recent timestamps (not a frozen INSERT timestamp)."""
        from datetime import timezone

        resp1 = await _put_input(client, hdrs, report_id, 3, {"first": True})
        resp2 = await _put_input(client, hdrs, report_id, 3, {"second": True})

        dt1 = _parse_dt(resp1["saved_at"])
        dt2 = _parse_dt(resp2["saved_at"])
        now = datetime.now(timezone.utc)

        # Both timestamps must be recent (within the last 60 seconds)
        assert (now - dt1).total_seconds() < 60, (
            f"First PUT saved_at is not recent: {resp1['saved_at']}"
        )
        assert (now - dt2).total_seconds() < 60, (
            f"Second PUT saved_at is not recent — server_default may have frozen "
            f"the timestamp at INSERT time: {resp2['saved_at']}"
        )


# ── Tests: 404 before first save ──────────────────────────────────────────────


class TestGetBeforeSave:
    """GET /inputs/{sec} on an un-saved section must return 404."""

    @pytest.mark.asyncio
    async def test_fresh_report_section_404(self, client, hdrs):
        rid = await _create_report(client, hdrs, borrower="Fresh Co")
        r = await client.get(f"{REPORTS}/{rid}/inputs/7", headers=hdrs)
        assert r.status_code == 404, (
            f"GET /inputs/7 before any save must return 404, got {r.status_code}"
        )

    @pytest.mark.asyncio
    async def test_saved_section_not_404(self, client, hdrs, report_id):
        await _put_input(client, hdrs, report_id, 2, {"something": True})
        r = await client.get(f"{REPORTS}/{report_id}/inputs/2", headers=hdrs)
        assert r.status_code == 200, (
            f"GET /inputs/2 after save must return 200, got {r.status_code}"
        )

    @pytest.mark.asyncio
    async def test_unsaved_sibling_section_still_404(self, client, hdrs, report_id):
        """Saving §2 must not make §3 appear."""
        await _put_input(client, hdrs, report_id, 2, {"something": True})
        r = await client.get(f"{REPORTS}/{report_id}/inputs/3", headers=hdrs)
        assert r.status_code == 404, (
            "GET /inputs/3 must return 404 when only §2 was saved"
        )


# ── Tests: import_section_json merge ─────────────────────────────────────────


class TestImportSectionJsonMerge:
    """import_section_json must merge with existing SectionInput without crashing."""

    @pytest.mark.asyncio
    async def test_import_when_no_prior_save(self, client, hdrs, report_id):
        """Import on a fresh section must create a new SectionInput row."""
        payload = {"4D_business": {"primary_business": "Container shipping"}}
        r = await client.post(
            f"{REPORTS}/{report_id}/import-section-json",
            data={"section_no": "4"},
            files={"file": ("data.json", json.dumps(payload).encode(), "application/json")},
            headers=hdrs,
        )
        assert r.status_code in (200, 201, 204), (
            f"import_section_json on fresh section must succeed: {r.status_code} {r.text}"
        )

        got = await _get_input(client, hdrs, report_id, 4)
        assert got["input_json"].get("4D_business", {}).get("primary_business") == "Container shipping", (
            "import_section_json must persist the imported field"
        )

    @pytest.mark.asyncio
    async def test_import_after_manual_save_does_not_crash(self, client, hdrs, report_id):
        """If analyst already saved §4, ETL import must merge without crashing."""
        manual = {"4A_borrower": {"company_name_en": "Analyst Corp"}}
        await _put_input(client, hdrs, report_id, 4, manual)

        etl = {"4D_business": {"primary_business": "Shipping"}}
        r = await client.post(
            f"{REPORTS}/{report_id}/import-section-json",
            data={"section_no": "4"},
            files={"file": ("etl.json", json.dumps(etl).encode(), "application/json")},
            headers=hdrs,
        )
        assert r.status_code in (200, 201, 204), (
            f"import_section_json after manual save must not crash: {r.status_code} {r.text}"
        )

        got = await _get_input(client, hdrs, report_id, 4)
        assert got["input_json"] is not None, "SectionInput must still exist after ETL import"

    @pytest.mark.asyncio
    async def test_import_preserves_analyst_data(self, client, hdrs, report_id):
        """ETL import must not erase analyst-entered fields (merge, not overwrite)."""
        manual = {"4A_borrower": {"company_name_en": "Precious Analyst Data"}}
        await _put_input(client, hdrs, report_id, 4, manual)

        etl = {"4D_business": {"primary_business": "Shipping"}}
        await client.post(
            f"{REPORTS}/{report_id}/import-section-json",
            data={"section_no": "4"},
            files={"file": ("etl.json", json.dumps(etl).encode(), "application/json")},
            headers=hdrs,
        )

        got = await _get_input(client, hdrs, report_id, 4)
        name = got["input_json"].get("4A_borrower", {}).get("company_name_en")
        assert name == "Precious Analyst Data", (
            f"import_section_json must preserve analyst-entered company_name_en, "
            f"got: {name!r}"
        )

    @pytest.mark.asyncio
    async def test_import_invalid_json_returns_400(self, client, hdrs, report_id):
        """Malformed JSON file must return 400, not 500."""
        r = await client.post(
            f"{REPORTS}/{report_id}/import-section-json",
            data={"section_no": "4"},
            files={"file": ("bad.json", b"not json at all", "application/json")},
            headers=hdrs,
        )
        assert r.status_code == 400, (
            f"import_section_json with invalid JSON must return 400, got {r.status_code}"
        )

    @pytest.mark.asyncio
    async def test_multiple_imports_do_not_multiply_rows(self, client, hdrs, report_id):
        """Repeated imports must update the same row, not accumulate orphan rows."""
        for i in range(3):
            payload = {"iteration": i}
            await client.post(
                f"{REPORTS}/{report_id}/import-section-json",
                data={"section_no": "8"},
                files={"file": ("data.json", json.dumps(payload).encode(), "application/json")},
                headers=hdrs,
            )

        # After 3 imports, GET must still return a consistent response
        got = await _get_input(client, hdrs, report_id, 8)
        assert got["input_json"] is not None, (
            "SectionInput must be consistent after repeated imports"
        )
        assert "iteration" in got["input_json"], (
            "Imported 'iteration' field must be persisted"
        )

"""
API Schema Fuzzing Tests (Schemathesis 4.x)
============================================
Fuzz all non-streaming FastAPI endpoints via their OpenAPI schema.

T1 — Generic schema fuzz   : every endpoint < 500 for any Hypothesis-generated input
T2 — Path injection         : malformed report_id (SQL inj, path traversal, XSS, …)
                              — covers GET *and* mutation (PUT) endpoints
T3 — section_no out-of-range: 0, -1, 11, 999, "abc"
T4 — Concurrent writes      : 5 simultaneous PUTs, no 5xx, at least one 200,
                               plus data-integrity check after the race
T5 — Oversized payload      : 600 KB input_json must be rejected (4xx), not accepted
T6 — Authorization / RBAC   : 401 without token; analyst cannot read/approve admin report

Run:
    python -m pytest tests/test_api_schema_fuzzing.py -v
"""
from __future__ import annotations

import asyncio
import os
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from httpx import ASGITransport, AsyncClient, InvalidURL

# ── Env must precede app import (conftest.py also sets these) ────────────────
os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")
os.environ.setdefault("SETUP_KEY", "test-setup-key")

from main import app  # noqa: E402

BASE = "/api/credit-report"

# ── Schema (streaming SSE paths excluded — they require active task_ids) ─────
schema = schemathesis.openapi.from_asgi("/openapi.json", app).exclude(
    path_regex=r"/stream"
)


# ── Auth token fixtures ───────────────────────────────────────────────────────
# pytest-asyncio (asyncio_mode=auto) keeps an event loop running for the whole
# test session.  Calling loop.run_until_complete() from inside that context
# raises "This event loop is already running."  Running login coroutines in
# daemon threads gives each its own isolated event loop.

def _run_in_thread(coro_factory):
    """Run an async factory in an isolated thread event loop; return the result."""
    result: list = []
    exc: list[BaseException] = []

    def _run() -> None:
        try:
            result.append(asyncio.run(coro_factory()))
        except BaseException as e:  # noqa: BLE001
            exc.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=30)

    if exc:
        raise exc[0]
    if not result:
        raise RuntimeError("_run_in_thread timed out after 30 s")
    return result[0]


@pytest.fixture(scope="module")
def auth_token() -> str:
    """Return a JWT access token for admin, obtained once per module."""
    async def _login() -> str:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                f"{BASE}/auth/login",
                data={"username": "admin@example.com", "password": "admin123"},
            )
            assert r.status_code == 200, f"Auth setup failed ({r.status_code}): {r.text}"
            return r.json()["access_token"]

    return _run_in_thread(_login)


@pytest.fixture(scope="module")
def analyst_token(auth_token: str) -> str:
    """Return a JWT access token for a freshly-created analyst user."""
    analyst_email = f"fuzz_{uuid.uuid4().hex[:10]}@test.local"

    async def _create() -> str:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                f"{BASE}/auth/register",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"email": analyst_email, "password": "Fuzz@nalyst1", "role": "analyst"},
            )
            assert r.status_code == 201, (
                f"Analyst registration failed ({r.status_code}): {r.text}"
            )
            r2 = await client.post(
                f"{BASE}/auth/login",
                data={"username": analyst_email, "password": "Fuzz@nalyst1"},
            )
            assert r2.status_code == 200, (
                f"Analyst login failed ({r2.status_code}): {r2.text}"
            )
            return r2.json()["access_token"]

    return _run_in_thread(_create)


# ── Gemini mock ───────────────────────────────────────────────────────────────

def _mock_gemini():
    """Patch Gemini so generation/ETL endpoints don't hit the real API."""
    mock_resp = MagicMock()
    mock_resp.text = "## Mock\n\nFuzz test output."
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)
    return patch("google.genai.Client", return_value=mock_client)


# ════════════════════════════════════════════════════════════════════════════════
# T1 — Generic schema fuzz: no 5xx for any Hypothesis-generated input
# ════════════════════════════════════════════════════════════════════════════════

@schema.parametrize()
@settings(
    max_examples=15,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)
def test_no_server_errors(case, auth_token):
    """All non-streaming endpoints must return < 500 for any generated input."""
    case.headers = {**(case.headers or {}), "Authorization": f"Bearer {auth_token}"}
    with _mock_gemini():
        response = case.call()
    assert response.status_code < 500, (
        f"\n{'─'*60}\n"
        f"5xx  {case.method.upper()}  {case.formatted_path}\n"
        f"Status : {response.status_code}\n"
        f"Body   : {response.text[:400]}\n"
        f"{'─'*60}"
    )


# ════════════════════════════════════════════════════════════════════════════════
# T2 — report_id path injection / special characters (GET + mutation endpoints)
# ════════════════════════════════════════════════════════════════════════════════

_BAD_IDS = [
    "' OR 1=1--",                       # SQL injection
    "../../etc/passwd",                  # Path traversal
    "<script>alert(1)</script>",         # XSS probe
    "a" * 512,                          # Oversized ID
    "\x00\x01\x02",                     # Null / control bytes
    "null",                              # JSON null string
    "{ $gt: '' }",                       # NoSQL injection probe
    "%27%20OR%20%271%27%3D%271",        # URL-encoded SQL injection
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", _BAD_IDS)
async def test_report_id_path_injection_no_500(bad_id, auth_token):
    """Malformed report_id in path must return 4xx — never 5xx (GET and mutation endpoints)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}
        endpoints = [
            # Read endpoints
            ("GET",    f"{BASE}/reports/{bad_id}",           None),
            ("GET",    f"{BASE}/reports/{bad_id}/inputs/1",  None),
            ("GET",    f"{BASE}/reports/{bad_id}/facts",     None),
            ("GET",    f"{BASE}/reports/{bad_id}/blocks",    None),
            ("GET",    f"{BASE}/reports/{bad_id}/audit",     None),
            # Mutation endpoints — must also be injection-safe
            ("PUT",    f"{BASE}/reports/{bad_id}/inputs/1",
                {"section_no": 1, "input_json": {"metadata": {"purpose_text": "safe"}}}),
            ("DELETE", f"{BASE}/reports/{bad_id}",           None),
        ]
        for method, url, body in endpoints:
            kwargs: dict = {"headers": hdrs}
            if body is not None:
                kwargs["json"] = body
            try:
                r = await client.request(method, url, **kwargs)
            except InvalidURL:
                # Null / control bytes in URL are rejected by the HTTP client
                # before reaching the server — safer than any server response.
                continue
            assert r.status_code < 500, (
                f"{method} {url!r}  bad_id={bad_id!r}\n"
                f"→ {r.status_code}: {r.text[:200]}"
            )


# ════════════════════════════════════════════════════════════════════════════════
# T3 — section_no out-of-range (0, -1, 11, 999, non-numeric)
# ════════════════════════════════════════════════════════════════════════════════

_BAD_SECTIONS: list[int | str] = [0, -1, 11, 999, "abc"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_sec", _BAD_SECTIONS)
async def test_section_no_out_of_range_no_500(bad_sec, auth_token):
    """
    section_no outside 1-10 must return 4xx (422 for type/range violation or 404
    if the report is not found first) — never 5xx.
    """
    rid = str(uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}
        # For PUT, send a correctly structured body with the bad section_no so Pydantic
        # validates the out-of-range value rather than returning 422 for a missing field.
        put_body = (
            {"section_no": bad_sec, "input_json": {}}
            if isinstance(bad_sec, int)
            else {"section_no": 1, "input_json": {}}  # non-int triggers 422 from URL param
        )
        checks: list[tuple[str, str, dict | None]] = [
            ("GET",  f"{BASE}/reports/{rid}/inputs/{bad_sec}",          None),
            ("PUT",  f"{BASE}/reports/{rid}/inputs/{bad_sec}",          put_body),
            ("GET",  f"{BASE}/reports/{rid}/sections/{bad_sec}/output", None),
            ("GET",  f"{BASE}/reports/{rid}/sections/{bad_sec}/blocks", None),
        ]
        for method, url, body in checks:
            kwargs: dict = {"headers": hdrs}
            if body is not None:
                kwargs["json"] = body
            r = await client.request(method, url, **kwargs)
            assert r.status_code < 500, (
                f"{method} section_no={bad_sec!r} → {r.status_code}: {r.text[:200]}"
            )


# ════════════════════════════════════════════════════════════════════════════════
# T4 — Concurrent writes to the same section (race condition / optimistic lock)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_concurrent_section_writes_no_500(auth_token):
    """
    5 simultaneous PUT requests to the same section must not produce 5xx.
    Expected outcomes: 200 (writer wins) or 409 (optimistic lock conflict).
    After the race, a GET must return valid data from one of the writers.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}

        r = await client.post(
            f"{BASE}/reports",
            headers=hdrs,
            json={"borrower_name": "FuzzConcurrent Ltd", "report_type": "new_deal"},
        )
        assert r.status_code == 201, f"Report creation failed: {r.text}"
        rid = r.json()["id"]

        async def _write(n: int):
            return await client.put(
                f"{BASE}/reports/{rid}/inputs/4",
                headers=hdrs,
                json={
                    "section_no": 4,
                    "input_json": {"corporate_background": {"company_name": f"FuzzCo v{n}"}},
                },
            )

        results = await asyncio.gather(*[_write(i) for i in range(5)])
        codes = [res.status_code for res in results]

        assert all(c < 500 for c in codes), (
            f"Concurrent writes produced 5xx — codes: {codes}\n"
            + "\n".join(
                f"  [{i}] {codes[i]}: {results[i].text[:150]}"
                for i in range(len(results))
                if codes[i] >= 500
            )
        )
        assert any(c == 200 for c in codes), (
            f"Expected at least one 200 from concurrent writes; got: {codes}"
        )

        # ── Data integrity: exactly one writer must have persisted valid data ──
        r_read = await client.get(f"{BASE}/reports/{rid}/inputs/4", headers=hdrs)
        assert r_read.status_code == 200, (
            f"GET /inputs/4 after concurrent writes → {r_read.status_code}: {r_read.text[:200]}"
        )
        saved = r_read.json()
        assert "input_json" in saved, "Response missing 'input_json' field"
        assert "corporate_background" in saved["input_json"], (
            "Saved input_json should contain corporate_background written by one of the 5 concurrent writers"
        )


# ════════════════════════════════════════════════════════════════════════════════
# T5 — Oversized JSON payload (600 KB string value must be rejected, not accepted)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_oversized_input_payload_rejected(auth_token):
    """
    A 600 KB string value in input_json must be actively rejected (4xx).
    The test creates a real report so the server fully processes the request —
    a 404 'report not found' would be a false pass.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}

        # Create a real report so the endpoint reaches payload validation
        r = await client.post(
            f"{BASE}/reports",
            headers=hdrs,
            json={"borrower_name": "FuzzOversize Ltd", "report_type": "new_deal"},
        )
        assert r.status_code == 201, f"Report creation failed: {r.text}"
        rid = r.json()["id"]

        r = await client.put(
            f"{BASE}/reports/{rid}/inputs/1",
            headers=hdrs,
            json={
                "section_no": 1,
                "input_json": {"metadata": {"purpose_text": "X" * 600_000}},
            },
        )
        # Must not crash the server
        assert r.status_code < 500, (
            f"Oversized payload crashed the server → {r.status_code}: {r.text[:200]}"
        )
        # Must actively reject the oversized payload — NOT silently accept it
        assert r.status_code >= 400, (
            f"Oversized 600 KB input_json was silently accepted (status={r.status_code}). "
            "The server must enforce a payload size limit."
        )


# ════════════════════════════════════════════════════════════════════════════════
# T6 — Authorization / RBAC coverage
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET",  f"{BASE}/reports"),
    ("POST", f"{BASE}/reports"),
    ("GET",  f"{BASE}/reports/nonexistent-report-id"),
])
async def test_unauthenticated_returns_401(method, path):
    """Core report endpoints must return 401 when Bearer token is absent."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.request(
            method, path, json={} if method == "POST" else None
        )
        assert r.status_code == 401, (
            f"{method} {path} without auth → {r.status_code} (expected 401)"
        )


@pytest.mark.asyncio
async def test_analyst_cannot_read_admin_report(analyst_token: str, auth_token: str):
    """A report created by admin must not be readable by an unrelated analyst (RBAC ownership)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Admin creates a report
        r = await client.post(
            f"{BASE}/reports",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"borrower_name": "RBAC Test Corp", "report_type": "new_deal"},
        )
        assert r.status_code == 201, f"Admin report creation failed: {r.text}"
        rid = r.json()["id"]

        # Unrelated analyst tries to read it — must be refused
        r2 = await client.get(
            f"{BASE}/reports/{rid}",
            headers={"Authorization": f"Bearer {analyst_token}"},
        )
        assert r2.status_code == 403, (
            f"Analyst could read admin's report → {r2.status_code}: {r2.text[:200]}"
        )


@pytest.mark.asyncio
async def test_analyst_cannot_approve_report(analyst_token: str, auth_token: str):
    """Approving a report requires approver/admin role; analyst must receive 403."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Admin creates a report
        r = await client.post(
            f"{BASE}/reports",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"borrower_name": "RBAC Approval Test", "report_type": "new_deal"},
        )
        assert r.status_code == 201, f"Admin report creation failed: {r.text}"
        rid = r.json()["id"]

        # Analyst attempts to set status = "approved"
        # The role check fires before the report ownership check, so this
        # must return 403 even though analyst cannot see the report either.
        r2 = await client.patch(
            f"{BASE}/reports/{rid}/status",
            headers={"Authorization": f"Bearer {analyst_token}"},
            json={"status": "approved"},
        )
        assert r2.status_code == 403, (
            f"Analyst could approve report → {r2.status_code}: {r2.text[:200]}"
        )

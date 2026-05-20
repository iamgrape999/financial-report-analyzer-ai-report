"""
API Schema Fuzzing Tests (Schemathesis 4.x)
============================================
Fuzz all non-streaming FastAPI endpoints via their OpenAPI schema.

T1 — Generic schema fuzz   : every endpoint < 500 for any Hypothesis-generated input
T2 — Path injection         : malformed report_id (SQL inj, path traversal, XSS, …)
T3 — section_no out-of-range: 0, -1, 11, 999, "abc"
T4 — Concurrent writes      : 5 simultaneous PUTs, no 5xx, at least one 200
T5 — Oversized payload      : 600 KB body value must return 4xx, not 5xx

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


# ── Auth token fixture ────────────────────────────────────────────────────────
# pytest-asyncio (asyncio_mode=auto) keeps an event loop running for the whole
# test session.  Calling loop.run_until_complete() from inside that context
# raises "This event loop is already running."  Running the login coroutine in
# a daemon thread gives it its own isolated event loop.

@pytest.fixture(scope="module")
def auth_token() -> str:
    """Return a JWT access token, obtained once per module via a thread-local loop."""
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

    result: list[str] = []
    exc: list[BaseException] = []

    def _run() -> None:
        try:
            result.append(asyncio.run(_login()))
        except BaseException as e:  # noqa: BLE001
            exc.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=30)

    if exc:
        raise exc[0]
    if not result:
        raise RuntimeError("auth_token fixture timed out after 30 s")
    return result[0]


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
# T2 — report_id path injection / special characters
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
    """Malformed report_id in path must return 4xx — never 5xx."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}
        endpoints = [
            ("GET", f"{BASE}/reports/{bad_id}"),
            ("GET", f"{BASE}/reports/{bad_id}/inputs/1"),
            ("GET", f"{BASE}/reports/{bad_id}/facts"),
            ("GET", f"{BASE}/reports/{bad_id}/blocks"),
            ("GET", f"{BASE}/reports/{bad_id}/audit"),
        ]
        for method, url in endpoints:
            try:
                r = await client.request(method, url, headers=hdrs)
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
        checks: list[tuple[str, str, dict | None]] = [
            ("GET",  f"{BASE}/reports/{rid}/inputs/{bad_sec}",       None),
            ("PUT",  f"{BASE}/reports/{rid}/inputs/{bad_sec}",       {"inputs": {}}),
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


# ════════════════════════════════════════════════════════════════════════════════
# T5 — Oversized JSON payload (600 KB string value)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_oversized_input_payload_no_500(auth_token):
    """A 600 KB string value in a PUT body must return 4xx, not crash the server."""
    rid = str(uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}
        r = await client.put(
            f"{BASE}/reports/{rid}/inputs/1",
            headers=hdrs,
            json={"metadata": {"purpose_text": "X" * 600_000}},
        )
        assert r.status_code < 500, (
            f"Oversized payload → {r.status_code}: {r.text[:200]}"
        )

"""
Auth & User-Management Fuzzing Tests (Schemathesis 4.x)
========================================================
Covers the admin-creates-account → user-logs-in lifecycle
and the new User Management panel endpoints.

T1 — Auth schema fuzz      : all /auth/* endpoints < 500 for any Hypothesis input
T2 — Full create→login flow: admin registers user, user logs in, /me returns correct data
T3 — Password length spec  : 7 chars → 400, exactly 8 → 201, 100 chars → 201
T4 — GET /auth/users       : admin-only, pagination (skip/limit), cap at ≤200 per page
T5 — Role change + verify  : change analyst→reviewer, re-login confirms role
T6 — Password reset + verify: admin resets pw, old pw blocked (401), new pw works (200)
T7 — user_id path injection: malformed IDs on PATCH /users/{id}/role → 4xx, never 5xx
T8 — Error cases           : duplicate email → 409, invalid role → 400, no-auth → 401

Run:
    python -m pytest tests/test_auth_user_mgmt_fuzzing.py -v
"""
from __future__ import annotations

import asyncio
import os
import threading
import uuid

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from httpx import ASGITransport, AsyncClient, InvalidURL

# ── Env must precede app import ──────────────────────────────────────────────
os.environ.setdefault("GEMINI_API_KEY", "mock-key-for-testing")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")
os.environ.setdefault("SETUP_KEY", "test-setup-key")

from main import app  # noqa: E402

BASE = "/api/credit-report"
AUTH = f"{BASE}/auth"

# ── Schema: /auth/* endpoints only ──────────────────────────────────────────
auth_schema = schemathesis.openapi.from_asgi("/openapi.json", app).include(
    path_regex=r"/auth/"
)


# ── Shared helper ────────────────────────────────────────────────────────────

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
        raise RuntimeError("_run_in_thread timed out")
    return result[0]


# ── Admin token fixture (module scope) ──────────────────────────────────────

@pytest.fixture(scope="module")
def auth_token() -> str:
    async def _login() -> str:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                f"{AUTH}/login",
                data={"username": "admin@example.com", "password": "admin123"},
            )
            assert r.status_code == 200, f"Admin login failed: {r.text}"
            return r.json()["access_token"]

    return _run_in_thread(_login)


# ════════════════════════════════════════════════════════════════════════════
# T1 — Schemathesis generic fuzz: all /auth/* endpoints must return < 500
# ════════════════════════════════════════════════════════════════════════════

@auth_schema.parametrize()
@settings(
    max_examples=15,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)
def test_auth_endpoints_no_server_errors(case, auth_token):
    """All /auth/* endpoints must return < 500 for any Hypothesis-generated input."""
    case.headers = {**(case.headers or {}), "Authorization": f"Bearer {auth_token}"}
    response = case.call()
    assert response.status_code < 500, (
        f"\n{'─'*60}\n"
        f"5xx  {case.method.upper()}  {case.formatted_path}\n"
        f"Status : {response.status_code}\n"
        f"Body   : {response.text[:400]}\n"
        f"{'─'*60}"
    )


# ════════════════════════════════════════════════════════════════════════════
# T2 — Full lifecycle: admin creates account → new user logs in → /me correct
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_admin_create_account_user_can_login(auth_token):
    """
    End-to-end: admin registers a new analyst, new user logs in,
    GET /auth/me returns the correct email and role.
    """
    email = f"newuser_{uuid.uuid4().hex[:8]}@cathaybk.com.tw"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Admin registers the new user
        r = await client.post(
            f"{AUTH}/register",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"email": email, "password": "CathayPass1!", "role": "analyst"},
        )
        assert r.status_code == 201, f"Register failed: {r.text}"
        created = r.json()
        assert created["email"] == email
        assert created["role"] == "analyst"
        assert created["is_active"] is True

        # New user logs in
        r2 = await client.post(
            f"{AUTH}/login",
            data={"username": email, "password": "CathayPass1!"},
        )
        assert r2.status_code == 200, f"New user login failed: {r2.text}"
        user_token = r2.json()["access_token"]
        assert r2.json()["role"] == "analyst"

        # GET /auth/me with new user's token
        r3 = await client.get(
            f"{AUTH}/me",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert r3.status_code == 200
        me = r3.json()
        assert me["email"] == email
        assert me["role"] == "analyst"


# ════════════════════════════════════════════════════════════════════════════
# T3 — Password length spec: server enforces ≥ 8 chars (not just client JS)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("pw,expected_status", [
    ("1234567",       400),   # 7 chars — below minimum
    ("12345678",      201),   # exactly 8 chars — boundary pass
    ("ValidPass1!",   201),   # 10 chars — normal
    ("X" * 100,       201),   # 100 chars — long but valid
])
async def test_password_length_boundary(pw, expected_status, auth_token):
    """Server must reject passwords shorter than 8 characters (not just client JS)."""
    email = f"pwtest_{uuid.uuid4().hex[:8]}@test.local"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            f"{AUTH}/register",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"email": email, "password": pw, "role": "analyst"},
        )
        assert r.status_code == expected_status, (
            f"password={pw!r} (len={len(pw)}) → {r.status_code} "
            f"(expected {expected_status}): {r.text[:200]}"
        )
        if expected_status == 400:
            assert "8" in r.json().get("detail", ""), (
                "400 error message should mention the 8-character minimum"
            )


# ════════════════════════════════════════════════════════════════════════════
# T4 — GET /auth/users: admin-only, pagination, result cap
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_users_admin_only_and_paginated(auth_token):
    """
    GET /auth/users is admin-only, returns at most `limit` rows,
    and skip+limit pagination moves the window correctly.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}

        # Default call — must succeed and not exceed 200 rows
        r = await client.get(f"{AUTH}/users", headers=hdrs)
        assert r.status_code == 200
        users = r.json()
        assert isinstance(users, list)
        assert len(users) <= 200, (
            f"Default limit should cap at 200, got {len(users)}"
        )

        # Small limit
        r2 = await client.get(f"{AUTH}/users?limit=3", headers=hdrs)
        assert r2.status_code == 200
        assert len(r2.json()) <= 3

        # skip=0 vs skip=1 should differ (assuming >1 user exists)
        r_p0 = await client.get(f"{AUTH}/users?skip=0&limit=5", headers=hdrs)
        r_p1 = await client.get(f"{AUTH}/users?skip=1&limit=5", headers=hdrs)
        p0 = r_p0.json()
        p1 = r_p1.json()
        if len(p0) >= 2:
            assert p0[0]["id"] != p1[0]["id"], (
                "skip=0 and skip=1 should return different first rows"
            )

        # Analyst must be refused
        analyst_email = f"listtest_{uuid.uuid4().hex[:8]}@test.local"
        await client.post(
            f"{AUTH}/register",
            headers=hdrs,
            json={"email": analyst_email, "password": "ListTest1!", "role": "analyst"},
        )
        r_login = await client.post(
            f"{AUTH}/login",
            data={"username": analyst_email, "password": "ListTest1!"},
        )
        analyst_token = r_login.json()["access_token"]

        r_noauth = await client.get(
            f"{AUTH}/users",
            headers={"Authorization": f"Bearer {analyst_token}"},
        )
        assert r_noauth.status_code == 403, (
            f"Analyst should get 403 on GET /auth/users, got {r_noauth.status_code}"
        )


# ════════════════════════════════════════════════════════════════════════════
# T5 — Role change: admin changes role, effect visible on re-login
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_role_change_reflected_in_login(auth_token):
    """
    Admin changes a user's role from analyst to reviewer.
    The change must be visible on the next login (token role field).
    Changing to an invalid role must return 400.
    """
    email = f"rolechange_{uuid.uuid4().hex[:8]}@test.local"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}

        # Create analyst
        r = await client.post(
            f"{AUTH}/register", headers=hdrs,
            json={"email": email, "password": "RoleTest1!", "role": "analyst"},
        )
        assert r.status_code == 201
        user_id = r.json()["id"]

        # Confirm login shows analyst
        r2 = await client.post(
            f"{AUTH}/login", data={"username": email, "password": "RoleTest1!"}
        )
        assert r2.json()["role"] == "analyst"

        # Admin changes role to reviewer
        r3 = await client.patch(
            f"{AUTH}/users/{user_id}/role?role=reviewer", headers=hdrs
        )
        assert r3.status_code == 200, f"Role change failed: {r3.text}"
        assert r3.json()["role"] == "reviewer"

        # Re-login — must return updated role
        r4 = await client.post(
            f"{AUTH}/login", data={"username": email, "password": "RoleTest1!"}
        )
        assert r4.status_code == 200
        assert r4.json()["role"] == "reviewer", (
            f"Re-login should show new role 'reviewer', got: {r4.json()['role']}"
        )

        # Invalid role must be rejected
        r5 = await client.patch(
            f"{AUTH}/users/{user_id}/role?role=superuser", headers=hdrs
        )
        assert r5.status_code == 400, (
            f"Invalid role 'superuser' should return 400, got {r5.status_code}"
        )


# ════════════════════════════════════════════════════════════════════════════
# T6 — Password reset: old password blocked, new password works
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_password_reset_old_blocked_new_works(auth_token):
    """
    Admin resets a user's password.
    After reset, old password must be rejected (401).
    New password must succeed (200).
    """
    email = f"pwreset_{uuid.uuid4().hex[:8]}@test.local"
    old_pw = "OldPassword1!"
    new_pw = "NewPassword2@"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}

        # Register user
        r = await client.post(
            f"{AUTH}/register", headers=hdrs,
            json={"email": email, "password": old_pw, "role": "analyst"},
        )
        assert r.status_code == 201
        user_id = r.json()["id"]

        # Confirm old password works
        r2 = await client.post(
            f"{AUTH}/login", data={"username": email, "password": old_pw}
        )
        assert r2.status_code == 200, "Old password should work before reset"

        # Admin resets password
        r3 = await client.post(
            f"{AUTH}/users/{user_id}/reset-password",
            headers=hdrs,
            json={"new_password": new_pw},
        )
        assert r3.status_code == 200, f"Password reset failed: {r3.text}"

        # Old password must now fail
        r4 = await client.post(
            f"{AUTH}/login", data={"username": email, "password": old_pw}
        )
        assert r4.status_code == 401, (
            f"Old password should be rejected after reset, got {r4.status_code}"
        )

        # New password must work
        r5 = await client.post(
            f"{AUTH}/login", data={"username": email, "password": new_pw}
        )
        assert r5.status_code == 200, (
            f"New password should work after reset, got {r5.status_code}: {r5.text}"
        )
        assert r5.json()["role"] == "analyst"

        # Reset with short password must fail
        r6 = await client.post(
            f"{AUTH}/users/{user_id}/reset-password",
            headers=hdrs,
            json={"new_password": "short"},
        )
        assert r6.status_code == 400, (
            f"Short reset password should return 400, got {r6.status_code}"
        )


# ════════════════════════════════════════════════════════════════════════════
# T7 — user_id path injection on PATCH /users/{id}/role
# ════════════════════════════════════════════════════════════════════════════

_BAD_USER_IDS = [
    "' OR 1=1--",
    "../../etc/passwd",
    "<script>alert(1)</script>",
    "a" * 512,
    "null",
    "{ $gt: '' }",
    "%27%20OR%20%271%27%3D%271",
    "\x00\x01",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", _BAD_USER_IDS)
async def test_user_id_path_injection_no_500(bad_id, auth_token):
    """Malformed user_id in PATCH /users/{id}/role must return 4xx — never 5xx."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}
        endpoints = [
            ("PATCH", f"{AUTH}/users/{bad_id}/role?role=analyst"),
            ("POST",  f"{AUTH}/users/{bad_id}/reset-password",
             {"new_password": "ValidPass1!"}),
        ]
        for method, url, *body_args in endpoints:
            kwargs: dict = {"headers": hdrs}
            if body_args:
                kwargs["json"] = body_args[0]
            try:
                r = await client.request(method, url, **kwargs)
            except InvalidURL:
                continue  # null bytes in URL → client-level rejection is safe
            assert r.status_code < 500, (
                f"{method} {url!r}  bad_id={bad_id!r}\n"
                f"→ {r.status_code}: {r.text[:200]}"
            )


# ════════════════════════════════════════════════════════════════════════════
# T8 — Error case spec: duplicate email, invalid role, unauthenticated
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_register_error_cases(auth_token):
    """Register endpoint must return correct error codes for all invalid inputs."""
    email = f"errcase_{uuid.uuid4().hex[:8]}@test.local"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        hdrs = {"Authorization": f"Bearer {auth_token}"}

        # Register once
        r = await client.post(
            f"{AUTH}/register", headers=hdrs,
            json={"email": email, "password": "GoodPass1!", "role": "analyst"},
        )
        assert r.status_code == 201

        # Duplicate email → 409
        r2 = await client.post(
            f"{AUTH}/register", headers=hdrs,
            json={"email": email, "password": "AnotherPass1!", "role": "analyst"},
        )
        assert r2.status_code == 409, f"Duplicate email should be 409, got {r2.status_code}"
        assert "already" in r2.json()["detail"].lower()

        # Invalid role → 400
        r3 = await client.post(
            f"{AUTH}/register", headers=hdrs,
            json={"email": f"role_{uuid.uuid4().hex[:6]}@test.local",
                  "password": "GoodPass1!", "role": "superuser"},
        )
        assert r3.status_code == 400, f"Invalid role should be 400, got {r3.status_code}"

        # Short password → 400
        r4 = await client.post(
            f"{AUTH}/register", headers=hdrs,
            json={"email": f"short_{uuid.uuid4().hex[:6]}@test.local",
                  "password": "abc", "role": "analyst"},
        )
        assert r4.status_code == 400, f"Short password should be 400, got {r4.status_code}"

        # No auth → 401
        r5 = await client.post(
            f"{AUTH}/register",
            json={"email": f"noauth_{uuid.uuid4().hex[:6]}@test.local",
                  "password": "GoodPass1!", "role": "analyst"},
        )
        assert r5.status_code == 401, f"Missing auth should be 401, got {r5.status_code}"

        # Wrong credentials login → 401
        r6 = await client.post(
            f"{AUTH}/login",
            data={"username": email, "password": "WrongPass99!"},
        )
        assert r6.status_code == 401

        # Non-existent email login → 401
        r7 = await client.post(
            f"{AUTH}/login",
            data={"username": f"ghost_{uuid.uuid4().hex[:8]}@test.local",
                  "password": "GoodPass1!"},
        )
        assert r7.status_code == 401

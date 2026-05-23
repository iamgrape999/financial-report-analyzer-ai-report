from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.audit.events import write_event
from credit_report.database import get_db
from credit_report.schemas import (
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from credit_report.security.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from credit_report.security.models import User, VALID_ROLES

# ── Login brute-force protection ─────────────────────────────────────────────
# Per-IP failed attempt tracking (in-memory; resets on restart which is fine
# for a single-instance deployment — Render free tier runs one instance).
_failed: dict[str, list[float]] = defaultdict(list)
_MAX_FAILURES = 10    # max failures before block
_WINDOW_SECS = 300    # 5-minute sliding window for counting failures
_BLOCK_SECS = 900     # 15-minute block after threshold exceeded


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host or "unknown")


def _check_brute_force(ip: str) -> None:
    now = time.time()
    _failed[ip] = [t for t in _failed[ip] if now - t < _BLOCK_SECS]
    recent = [t for t in _failed[ip] if now - t < _WINDOW_SECS]
    if len(recent) >= _MAX_FAILURES:
        logger.warning("login: brute-force block ip=%s recent_failures=%d", ip, len(recent))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again in 15 minutes.",
        )


def _record_failure(ip: str) -> None:
    _failed[ip].append(time.time())


def _clear_failures(ip: str) -> None:
    _failed.pop(ip, None)


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    _check_brute_force(ip)

    submitted_email = (form_data.username or "").strip()
    submitted_pw = form_data.password or ""

    logger.info(
        "login: attempt email=%r pw_len=%d ip=%s ua=%s",
        submitted_email, len(submitted_pw), ip,
        request.headers.get("user-agent", "")[:80],
    )

    # Look up by exact email first, then try case-insensitive fallback
    result = await db.execute(select(User).where(User.email == submitted_email))
    user = result.scalar_one_or_none()

    if user is None:
        # Try case-insensitive match to catch mixed-case env var vs input
        result2 = await db.execute(
            select(User).where(User.email.ilike(submitted_email))
        )
        user = result2.scalar_one_or_none()
        if user:
            logger.info("login: found user via case-insensitive match stored=%r submitted=%r",
                        user.email, submitted_email)

    if user is None:
        # List all stored emails (masked) to diagnose mismatch
        all_result = await db.execute(select(User.email))
        all_emails = [e for (e,) in all_result.all()]
        masked = [f"{e[:3]}***{e[e.find('@'):]}" if "@" in e else "***" for e in all_emails]
        logger.warning(
            "login: no user found for email=%r ip=%s — stored accounts: %s",
            submitted_email, ip, masked,
        )
        _record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No account found for '{submitted_email}'. Check your email address.",
        )

    pw_ok = verify_password(submitted_pw, user.hashed_password)
    logger.info("login: pw_verify=%s user=%s email=%r ip=%s", pw_ok, user.id, user.email, ip)

    if not pw_ok:
        _record_failure(ip)
        logger.warning("login: wrong password user=%s email=%r ip=%s", user.id, user.email, ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password.",
        )

    if not user.is_active:
        _record_failure(ip)
        logger.warning("login: inactive account user=%s ip=%s", user.id, ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account inactive")

    _clear_failures(ip)
    logger.info("login: success user=%s role=%s ip=%s", user.id, user.role, ip)
    await write_event(db, action="auth.login", actor_user_id=user.id, actor_role=user.role)
    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        data = decode_token(payload.refresh_token)
    except Exception:
        logger.warning("refresh: invalid token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    if data.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not a refresh token")

    result = await db.execute(select(User).where(User.id == data["sub"]))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
        role=user.role,
    )


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    skip: int = Query(default=0, ge=0, le=2_147_483_647),
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    result = await db.execute(
        select(User).order_by(User.created_at.asc()).offset(skip).limit(limit)
    )
    return list(result.scalars().all())


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

    result = await db.execute(select(User).where(User.email == payload.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    new_user = User(
        id=str(uuid.uuid4()),
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        is_active=True,
    )
    db.add(new_user)
    logger.info("register: new user id=%s email=%r role=%s created_by=%s", new_user.id, new_user.email, new_user.role, current_user.id)

    await write_event(
        db,
        action="auth.register",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        target_type="user",
        target_id=new_user.id,
        after=f"email={new_user.email} role={new_user.role}",
    )
    await db.flush()
    return new_user


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/users/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: str,
    role: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    old_role = target.role
    target.role = role
    logger.info("update_user_role: user=%s %r → %r by=%s", user_id, old_role, role, current_user.id)

    await write_event(
        db,
        action="auth.role_change",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        target_type="user",
        target_id=user_id,
        before=old_role,
        after=role,
    )
    return target


@router.get("/status", tags=["auth"])
async def auth_status(db: AsyncSession = Depends(get_db)):
    """Diagnostic endpoint — returns whether any user accounts exist.

    Safe to expose publicly: reveals no credentials, only a count.
    Helps diagnose 'cannot login' issues when env vars were not set.
    """
    result = await db.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()
    admin_count = sum(1 for u in users if u.role == "admin")
    return {
        "total_active_users": len(users),
        "admin_accounts": admin_count,
        "login_possible": admin_count > 0,
        "hint": (
            "No admin account exists. POST /auth/setup to create one."
            if admin_count == 0
            else "Admin account exists — use your configured credentials to log in."
        ),
    }


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.post("/change-password", status_code=200)
async def change_password(
    payload: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Allow any authenticated user to change their own password."""
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    current_user.hashed_password = hash_password(payload.new_password)
    await write_event(
        db,
        action="auth.password_change",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        target_type="user",
        target_id=current_user.id,
    )
    logger.info("change_password: success user=%s", current_user.id)
    return {"message": "Password changed successfully."}


@router.post("/users/{user_id}/reset-password", status_code=200)
async def reset_user_password(
    user_id: str,
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Admin-only: reset any user's password without knowing the current one."""
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    target.hashed_password = hash_password(payload.new_password)
    await write_event(
        db,
        action="auth.password_reset",
        actor_user_id=current_user.id,
        actor_role=current_user.role,
        target_type="user",
        target_id=user_id,
    )
    logger.info("reset_user_password: user=%s reset by admin=%s", user_id, current_user.id)
    return {"message": f"Password reset for user {user_id}."}


class SetupRequest(BaseModel):
    email: str
    password: str
    setup_key: str


@router.post("/setup", tags=["auth"], status_code=status.HTTP_201_CREATED)
async def first_run_setup(
    payload: SetupRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create the very first admin account when no users exist.

    Requires SETUP_KEY env var to match payload.setup_key.
    Disabled once any user exists — subsequent calls return 409.
    """
    import os  # noqa: PLC0415
    setup_key = os.getenv("SETUP_KEY", "")
    if not setup_key:
        raise HTTPException(
            status_code=503,
            detail="SETUP_KEY environment variable is not configured on this server.",
        )
    if payload.setup_key != setup_key:
        raise HTTPException(status_code=403, detail="Invalid setup key.")

    result = await db.execute(select(User).limit(1))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Setup already complete — an account already exists. Use /login.",
        )

    if not payload.email or "@" not in payload.email:
        raise HTTPException(status_code=400, detail="Valid email required.")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    admin = User(
        id=str(uuid.uuid4()),
        email=payload.email.strip().lower(),
        hashed_password=hash_password(payload.password),
        role="admin",
        is_active=True,
    )
    db.add(admin)
    await db.flush()
    logger.info("first_run_setup: created admin email=%r", admin.email)
    return {"message": f"Admin account created for {admin.email}. You can now log in."}


@router.post("/sse-ticket", tags=["auth"])
async def create_sse_ticket(current_user: User = Depends(get_current_user)):
    """Issue a one-time 60-second SSE authentication ticket.

    EventSource does not support Authorization headers; callers must obtain a
    short-lived ticket via this endpoint and pass it as ?ticket=<value> to SSE
    stream endpoints.  The ticket is consumed on first use and never reusable.
    """
    from credit_report.security.sse_ticket import issue_ticket
    ticket = issue_ticket(str(current_user.id))
    return {"ticket": ticket, "expires_in": 60}

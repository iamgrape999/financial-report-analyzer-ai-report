from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

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

    # form_data.username holds the email (OAuth2 standard field name)
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        _record_failure(ip)
        logger.warning("login: failed credential check email=%r ip=%s", form_data.username, ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
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

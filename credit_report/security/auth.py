from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    DEFAULT_SECRET_KEY,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from credit_report.database import get_db
from credit_report.security.models import User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/credit-report/auth/login")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _sign(header_b64: str, payload_b64: str) -> str:
    msg = f"{header_b64}.{payload_b64}".encode("utf-8")
    key = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY).encode("utf-8")
    sig = hmac.new(key, msg, hashlib.sha256).digest()
    return _b64url_encode(sig)


def _encode_jwt(payload: dict) -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url_encode(json.dumps(payload).encode())
    sig = _sign(header, body)
    return f"{header}.{body}.{sig}"


def _decode_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("Malformed token")
    header_b64, payload_b64, sig_b64 = parts
    expected_sig = _sign(header_b64, payload_b64)
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise JWTError("Invalid signature")
    payload = json.loads(_b64url_decode(payload_b64))
    if "exp" in payload and payload["exp"] < time.time():
        raise JWTError("Token expired")
    return payload


class JWTError(Exception):
    pass


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, role: str) -> str:
    expire = time.time() + ACCESS_TOKEN_EXPIRE_MINUTES * 60
    return _encode_jwt({"sub": user_id, "role": role, "exp": expire, "type": "access"})


def create_refresh_token(user_id: str) -> str:
    expire = time.time() + REFRESH_TOKEN_EXPIRE_DAYS * 86400
    return _encode_jwt({"sub": user_id, "exp": expire, "type": "refresh"})


def decode_token(token: str) -> dict:
    return _decode_jwt(token)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise credentials_exc
        user_id: Optional[str] = payload.get("sub")
        if user_id is None:
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User inactive or not found",
        )
    return user


def require_role(*roles: str):
    async def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not authorized for this action",
            )
        return user
    return _check


require_analyst = require_role("analyst", "reviewer", "approver", "admin")
require_reviewer = require_role("reviewer", "approver", "admin")
require_approver = require_role("approver", "admin")
require_admin = require_role("admin")

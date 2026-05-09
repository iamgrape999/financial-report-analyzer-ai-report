from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.config import DAILY_TOKEN_LIMIT
from credit_report.generation.models import UserTokenQuota

# Per-role daily token limits — higher-trust roles get more headroom.
# DAILY_TOKEN_LIMIT (env var) sets the analyst baseline; others scale from it.
_ROLE_LIMITS: dict[str, int] = {
    "analyst":  DAILY_TOKEN_LIMIT,       # default 4M  — ~8 full reports/day
    "reviewer": DAILY_TOKEN_LIMIT * 2,   # 8M          — reviews multiple reports
    "approver": DAILY_TOKEN_LIMIT * 2,   # 8M
    "admin":    DAILY_TOKEN_LIMIT * 5,   # 20M         — effectively uncapped
}


def _limit_for_role(role: str) -> int:
    return _ROLE_LIMITS.get(role, DAILY_TOKEN_LIMIT)


async def check_quota(db: AsyncSession, user_id: str, role: str = "analyst") -> None:
    """Raise HTTP 429 if the user has reached their daily token limit."""
    limit = _limit_for_role(role)
    today = datetime.now(timezone.utc).date()
    result = await db.execute(
        select(UserTokenQuota).where(
            UserTokenQuota.user_id == user_id,
            UserTokenQuota.quota_date == today,
        )
    )
    quota = result.scalar_one_or_none()
    used = quota.tokens_used if quota else 0
    if used >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily token limit of {limit:,} tokens reached. "
                "Resets at midnight UTC."
            ),
        )


async def record_tokens(db: AsyncSession, user_id: str, tokens: int) -> None:
    """Add tokens to the user's daily quota record (upsert)."""
    if tokens <= 0:
        return
    today = datetime.now(timezone.utc).date()
    result = await db.execute(
        select(UserTokenQuota).where(
            UserTokenQuota.user_id == user_id,
            UserTokenQuota.quota_date == today,
        )
    )
    quota = result.scalar_one_or_none()
    if quota:
        quota.tokens_used += tokens
    else:
        db.add(UserTokenQuota(
            id=str(uuid.uuid4()),
            user_id=user_id,
            quota_date=today,
            tokens_used=tokens,
        ))
    await db.flush()

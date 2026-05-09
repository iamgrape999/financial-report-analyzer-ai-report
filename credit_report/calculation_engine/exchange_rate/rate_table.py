from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from credit_report.calculation_engine.models import FXRate


STALENESS_HOURS = 24


async def get_rate(
    db: AsyncSession,
    report_id: str,
    from_currency: str,
    to_currency: str,
) -> Optional[FXRate]:
    """Retrieve most recent non-stale rate for a currency pair in this report."""
    result = await db.execute(
        select(FXRate)
        .where(
            FXRate.report_id == report_id,
            FXRate.from_currency == from_currency,
            FXRate.to_currency == to_currency,
            FXRate.is_stale == False,
        )
        .order_by(FXRate.created_at.desc())
    )
    return result.scalars().first()


async def set_rate(
    db: AsyncSession,
    report_id: str,
    from_currency: str,
    to_currency: str,
    rate: float,
    rate_date: str,
    source: str = "internal_bank_rate_table",
) -> FXRate:
    """Store a new FX rate, marking any existing rates for this pair stale."""
    # Mark existing rates stale
    existing_result = await db.execute(
        select(FXRate).where(
            FXRate.report_id == report_id,
            FXRate.from_currency == from_currency,
            FXRate.to_currency == to_currency,
        )
    )
    for old in existing_result.scalars().all():
        old.is_stale = True

    new_rate = FXRate(
        id=str(uuid.uuid4()),
        report_id=report_id,
        from_currency=from_currency,
        to_currency=to_currency,
        rate=rate,
        rate_date=rate_date,
        source=source,
        is_stale=False,
    )
    db.add(new_rate)
    return new_rate


async def check_staleness(db: AsyncSession, report_id: str) -> list[FXRate]:
    """Return FX rates that are more than STALENESS_HOURS old."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALENESS_HOURS)
    result = await db.execute(
        select(FXRate).where(
            FXRate.report_id == report_id,
            FXRate.created_at < cutoff,
            FXRate.is_stale == False,
        )
    )
    return list(result.scalars().all())

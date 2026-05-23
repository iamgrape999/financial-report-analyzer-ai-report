"""Lightweight in-process sliding-window rate limiter (no external dependency).

Usage:
    from credit_report.security.rate_limit import rate_limit_check

    # In an async endpoint:
    rate_limit_check(f"upload:{user_id}", max_requests=10, window_seconds=3600)
"""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException

# {bucket_key: [timestamp, ...]}
_windows: dict[str, list[float]] = defaultdict(list)
# Cap total bucket count to prevent unbounded growth (one entry per user per action)
_MAX_BUCKETS = 50_000


def rate_limit_check(key: str, max_requests: int, window_seconds: int) -> None:
    """Raise HTTP 429 if the caller has exceeded max_requests within window_seconds.

    Thread-safety: asyncio is single-threaded per event loop so no lock needed.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    hits = _windows[key]

    # Evict expired entries (oldest-first list)
    while hits and hits[0] < cutoff:
        hits.pop(0)

    if len(hits) >= max_requests:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Maximum {max_requests} requests per "
                   f"{window_seconds // 60} minute(s). Please try again later.",
        )

    hits.append(now)

    # Hard cap to prevent memory growth if bucket count explodes
    if len(_windows) > _MAX_BUCKETS:
        oldest_key = next(iter(_windows))
        del _windows[oldest_key]


def reset_all() -> None:
    """Clear all rate-limit windows. Use only in tests."""
    _windows.clear()

"""One-time short-lived SSE authentication tickets.

EventSource does not support Authorization headers, so callers first call
POST /auth/sse-ticket to obtain a 60-second one-time ticket, then pass it
as ?ticket=<value> to the SSE endpoint.  The endpoint consumes (deletes)
the ticket on first use, preventing replay even if the URL is captured in
proxy or server logs.
"""
from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from typing import Optional

_TICKET_TTL_SECONDS = 60
_MAX_TICKETS = 10_000

# Ordered so the oldest entries can be evicted when at capacity.
_tickets: OrderedDict[str, tuple[str, float]] = OrderedDict()


def issue_ticket(user_id: str) -> str:
    """Generate and store a one-time SSE ticket bound to user_id."""
    ticket = secrets.token_urlsafe(32)
    _tickets[ticket] = (user_id, time.monotonic() + _TICKET_TTL_SECONDS)
    if len(_tickets) > _MAX_TICKETS:
        _tickets.popitem(last=False)
    return ticket


def consume_ticket(ticket: str) -> Optional[str]:
    """Validate and immediately consume a one-time ticket.

    Returns the bound user_id on success, or None if the ticket is unknown,
    already used, or expired.
    """
    _evict_expired()
    entry = _tickets.pop(ticket, None)
    if entry is None:
        return None
    user_id, expiry = entry
    if time.monotonic() > expiry:
        return None
    return user_id


def _evict_expired() -> None:
    now = time.monotonic()
    expired = [k for k, (_, exp) in _tickets.items() if exp < now]
    for k in expired:
        _tickets.pop(k, None)

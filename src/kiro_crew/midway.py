"""Midway credential status (OSS stub).

Midway is an Amazon-internal authentication system and is not available in the
public KiroCrew distribution.  These functions are retained as no-op stubs so
that callers (dashboard, Slack handlers/events) keep importing and awaiting the
same symbols without behavioral surprises.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def midway_status() -> dict[str, object]:
    """Return midway credential status (not available in OSS)."""
    return {"available": False}


async def midway_status_async() -> dict[str, object]:
    """Non-blocking variant — returns the same stub status."""
    return {"available": False}


async def get_midway_status_line(prefix: str = "*Midway:*") -> str:
    """Format midway status as a display line (empty in OSS — no status shown)."""
    return ""

"""SSO credential status (OSS stub).

Enterprise single-sign-on is not available in the public KiroCrew distribution.
These functions are retained as no-op stubs so that callers (dashboard, Slack
handlers/events) keep importing and awaiting the same symbols without behavioral
surprises.  An enterprise companion package composes a real identity provider
through the platform seam; the public edition ships this inert stub.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def sso_status() -> dict[str, object]:
    """Return SSO credential status (not available in OSS)."""
    return {"available": False}


async def sso_status_async() -> dict[str, object]:
    """Non-blocking variant — returns the same stub status."""
    return {"available": False}


async def get_sso_status_line(prefix: str = "*SSO:*") -> str:
    """Format SSO status as a display line (empty in OSS — no status shown)."""
    return ""

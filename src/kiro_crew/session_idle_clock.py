"""The clock the idle sweep reads for one session.

``session_cleanup`` imports :func:`last_activity` under its old name, so callers
and tests keep using ``kiro_crew.session_cleanup._last_activity``.
"""

from __future__ import annotations

from typing import Any, Protocol


class _IdleClockEntry(Protocol):
    last_used: float
    provider: Any


def last_activity(session: _IdleClockEntry, timeout_secs: int) -> float:
    """Backend frames grant at most one extra idle window.

    Passive traffic is not proof of finite work. Capping its contribution keeps
    a looping backend from retaining an unlocked session indefinitely.
    """
    at = getattr(getattr(session, "provider", None), "session_activity_at", None)
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return max(session.last_used, min(at, session.last_used + timeout_secs))
    return session.last_used

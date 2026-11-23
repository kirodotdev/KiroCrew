"""The clock the idle sweep reads for one session.

``session_cleanup`` imports :func:`last_activity` under its old name, so callers
and tests keep using ``kiro_crew.session_cleanup._last_activity``.
"""

from __future__ import annotations

from typing import Any, Protocol

from kiro_crew.metrics.usage_attrs import TRIGGER_USER, current_trigger


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


def turn_was_automated() -> bool:
    """Whether the turn running in this context declared a starter that is not a person.

    The turn entry points declare their trigger (``metrics.usage_attrs``); a
    person's send is ``user``. A context that declared none is not judged
    automated, so a session it releases keeps the ordinary idle timeout.
    """
    trigger = current_trigger()
    return trigger is not None and trigger != TRIGGER_USER


def idle_limit(session: object, timeout_secs: int, automated_timeout_secs: int) -> int:
    """The idle timeout the sweep applies to *session*.

    ``session.automated_idle_timeout_secs`` applies only to a session whose last
    turn was automated, and only when it is shorter than ``timeout_secs``: it
    never lengthens a hold. 0 turns it off.
    """
    if getattr(session, "last_turn_automated", False) is not True:
        return timeout_secs
    if isinstance(automated_timeout_secs, int) and 0 < automated_timeout_secs < timeout_secs:
        return automated_timeout_secs
    return timeout_secs

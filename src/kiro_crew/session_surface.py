"""Which surfaces a session is currently attached to.

A session key names a *conversation*, not the place it is displayed. A
conversation that started on Slack runs under ``slack:<thread_ts>`` even when
the user has its dashboard tab open, so ``session_key.startswith("dashboard:")``
answers "where did this start?" and not "can the user see a dashboard right
now?". Gates that mean the second question — offer widgets, render a question
card, accept a dashboard-only directive — need observed state instead of a
prefix test.

The dashboard publishes that state here as tabs open and close. This module
deliberately imports nothing from ``kiro_crew`` so the layers that must not
depend on the dashboard (prompt building, audit, the MCP gateway) can read it
without an import cycle.

Reads are lock-free: the registry is a ``frozenset`` replaced wholesale on
every update, so a reader either sees the previous set or the next one and
never a half-built one. Staleness is bounded by how often the dashboard
publishes, and fails toward the prefix test — an empty registry degrades to
exactly the pre-existing behaviour rather than to a wrong answer.
"""

from __future__ import annotations

from typing import Iterable, Mapping

#: Session keys with a live dashboard surface. Replaced, never mutated.
_dashboard_surfaced: frozenset[str] = frozenset()
#: ``(session key, live tab name)`` pairs. Replaced, never mutated; a frozenset,
#: like the keys, so a republish of the same tabs is the same value.
_dashboard_tabs: frozenset[tuple[str, str]] = frozenset()


def set_dashboard_surfaced(
    session_keys: Iterable[str], tabs: Mapping[str, str] | None = None
) -> None:
    """Publish the set of session keys that currently have an open tab.

    Called by the dashboard whenever its slot table changes. Passing an empty
    iterable is meaningful — it says "no tabs open" — and is what a shutting-down
    dashboard leaves behind. ``tabs`` maps a key to the name of the tab showing
    it, for :func:`dashboard_tab_name`; omitted, no names are known.
    """
    global _dashboard_surfaced, _dashboard_tabs
    _dashboard_surfaced = frozenset(session_keys)
    _dashboard_tabs = frozenset((tabs or {}).items())


def has_dashboard_surface(session_key: str) -> bool:
    """True when *session_key* is displayed in an open dashboard tab.

    The prefix test comes first so a genuinely dashboard-born session is
    recognised even before the dashboard has published anything (startup, or a
    process where the dashboard never runs).
    """
    if not session_key:
        return False
    if session_key.startswith("dashboard:") or session_key.startswith("dashboard_"):
        return True
    return session_key in _dashboard_surfaced


def dashboard_tab_name(session_key: str) -> str:
    """The name of the open tab showing *session_key*, or ``""`` if none is known."""
    return next((name for key, name in _dashboard_tabs if key == session_key), "")


def dashboard_surfaced_keys() -> frozenset[str]:
    """The published set, for diagnostics. Callers must not mutate it."""
    return _dashboard_surfaced

"""A real ``DashboardState`` for tests, and readers for what it delivered.

``real_dashboard_state(tmp_path)`` is the production
``kiro_crew.dashboard.state.DashboardState`` built by ``chat_test_helpers._make_state``:
its notification bus, slot directory and SSE fan-out are the real ones, and only
its outer services (the session manager, cron, lessons) are stand-ins. Nothing is
patched on the state itself, so a test can read what the code under test left
behind instead of reading back what a ``MagicMock`` dashboard was sent:

- ``notes(state)``: each notification the state delivered, as
  ``(kind, title, body)``, oldest first; ``note_rows(state)`` gives the whole
  stored rows. Both read the in-memory log the bell feed and
  ``/api/notifications`` read.
- ``open_feed(state)`` / ``drain(feed)``: an SSE client's queue, and the events a
  browser tab on it would have received.
"""

from __future__ import annotations

import asyncio
from typing import Any

from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import DashboardState


def real_dashboard_state(tmp_path: Any, **kwargs: Any) -> DashboardState:
    """A real ``DashboardState`` under *tmp_path*, its own methods unpatched."""
    return _make_state(tmp_path, **kwargs)


def notes(state: DashboardState) -> list[tuple[str, str, str]]:
    """``(kind, title, body)`` of every notification *state* delivered, oldest first."""
    return [
        (n.get("kind", ""), n.get("title", ""), n.get("body", "")) for n in state._notification_log
    ]


def note_rows(state: DashboardState) -> list[dict[str, Any]]:
    """Every delivered notification as stored, oldest first.

    A legacy ``notify(..., meta=...)`` merges the meta keys into the row itself,
    so a caller's ``task_id`` or ``slot`` is read as a top-level key here.
    """
    return [dict(n) for n in state._notification_log]


def open_feed(state: DashboardState) -> asyncio.Queue[dict[str, Any]]:
    """Register an SSE client on *state*; every later broadcast lands in its queue."""
    return state.register_sse()


def drain(feed: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything queued on *feed* so far, oldest first, leaving it empty."""
    events = []
    while not feed.empty():
        events.append(feed.get_nowait())
    return events

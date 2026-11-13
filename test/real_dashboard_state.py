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

A real ``notify`` hands its ``notifications.jsonl`` append to the single-worker
``notif-io`` executor, which resolves the data home when the job RUNS. A test that
returned before that job ran would write into whichever home the next test on the
worker pinned. So every module that builds one imports the autouse
``_settle_real_dashboard_states`` fixture by name; its teardown waits for the
queued writes before the home pin is undone, and ``real_dashboard_state`` refuses
to build a state without it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.state_parts.notification_log import _notification_io_executor

#: How long teardown waits for queued notification writes before failing the test.
SETTLE_TIMEOUT_S = 30.0

_settle = {"active": False, "built": 0}


def settle_notification_writes(timeout: float = SETTLE_TIMEOUT_S) -> None:
    """Return once every notification write queued so far has run.

    The executor has one worker and runs jobs in order, so a no-op submitted now
    finishes only after every earlier append. This works from a sync teardown, after
    the test's event loop has closed, where ``state.last_notification_persist`` (an
    asyncio future of that loop) cannot be awaited, and it holds whether that future
    is None, done or pending. Raises ``concurrent.futures.TimeoutError`` past
    *timeout*.
    """
    _notification_io_executor().submit(lambda: None).result(timeout=timeout)


@contextlib.contextmanager
def settling_notification_writes() -> Iterator[None]:
    """Allow ``real_dashboard_state`` inside; on any exit, wait for the writes it queued.

    The wait runs on an exceptional exit too: a failing test still queued its appends.
    """
    outer = dict(_settle)
    _settle["active"] = True
    _settle["built"] = 0
    try:
        yield
    finally:
        try:
            if _settle["built"]:
                settle_notification_writes()
        finally:
            _settle.update(outer)


@pytest.fixture(autouse=True)
def _settle_real_dashboard_states() -> Iterator[None]:
    """Wait for this test's queued notification writes before its home pin is undone."""
    with settling_notification_writes():
        yield


def real_dashboard_state(tmp_path: Any, **kwargs: Any) -> DashboardState:
    """A real ``DashboardState`` under *tmp_path*, its own methods unpatched."""
    if not _settle["active"]:
        raise RuntimeError(
            "import _settle_real_dashboard_states from real_dashboard_state into this "
            "test module (noqa: F401) so its notification writes settle at teardown"
        )
    _settle["built"] += 1
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

"""The fixtures the DashboardState WebSocket tests share.

``test_dashboard_state_ws.py`` and its ``_status_refresh`` and ``_unregister`` siblings
import what they use from here by name: ``sync_event_loop``, the autouse fixture that
gives every test, sync ones included, an event loop for the fire-and-forget broadcast
sends (imported, it applies to every test of the importing module), and ``state``
(declared ``@pytest.fixture(name="state")`` and imported as ``state_fixture``), a
``DashboardState`` over mocked session, cron and lesson stores whose config directory is
the test's ``tmp_path``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


@pytest.fixture(autouse=True)
def sync_event_loop():
    """Provide an event loop for sync tests calling asyncio.ensure_future.

    Production broadcast methods use ensure_future (fire-and-forget) which
    requires a running event loop.  Under xdist each worker is a separate
    process with no default loop, so we create one here.  autouse=True
    ensures every test gets a loop without opt-in, preventing flakes when
    new broadcast tests are added.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture(name="state")
def state_fixture(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )

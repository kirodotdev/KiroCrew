"""The fixtures the DashboardState WebSocket tests share.

``sync_event_loop`` is autouse once imported. ``state`` is declared
``@pytest.fixture(name="state")`` and imported as ``state_fixture``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


@pytest.fixture(autouse=True)
def sync_event_loop():
    """Provide an event loop for sync tests calling asyncio.ensure_future.

    Broadcasts use ensure_future, and an xdist worker has no default loop.
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

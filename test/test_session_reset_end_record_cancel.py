"""A cancel that lands on ``reset``'s end record still ends the reset's caller.

The end record absorbs a cancellation at its crumb hop so the reset's post-pop
cleanup runs. If ``reset`` then returned normally, a timeout around it would have
fired once and been used up: the caller ran on with no bound. The injection route's
session-not-found reload hit this, and its next prompt hung until the test ceiling.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.metrics import sessions as session_metrics
from kiro_crew.session import SessionManager

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30


def _provider(session_key: Any = None, agent: Any = None, channel_id: Any = None, **kwargs: Any):
    m = AsyncMock()
    m.memory_mode = kwargs.get("memory_mode", "persistent")
    m.is_process_alive = lambda: True
    m.disown_work_dir = MagicMock()
    m.context_usage_pct = lambda: 0.0
    m.context_window_tokens = lambda: 0
    m.has_active_turn = lambda: False
    m.runtime_abort_target = lambda: None
    m.spawn_identity = ""
    m._runtime = None
    m.session_id = "sess-a"
    return m


@pytest.mark.asyncio
async def test_a_reset_cancelled_at_its_end_record_raises_after_its_cleanup(monkeypatch) -> None:
    key = "discord:dm:42"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    released = threading.Event()
    real_unlink = session_metrics._unlink_all

    def held(paths: Any) -> None:
        # Runs on the crumb worker; the reset is parked on this hop until released.
        loop.call_soon_threadsafe(entered.set)
        released.wait(_HANG_CEILING_S)
        real_unlink(paths)

    monkeypatch.setattr(session_metrics, "_unlink_all", held)
    try:
        client, _is_new, _resumed = await sessions.get_or_create(key)
        sessions.release(key)
        task = asyncio.create_task(sessions.reset(key))
        await asyncio.wait_for(entered.wait(), _HANG_CEILING_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _HANG_CEILING_S)
        # The cleanup the cancel was absorbed for still ran before it was raised.
        client.shutdown.assert_awaited_once()
    finally:
        released.set()
        await sessions.close_all()

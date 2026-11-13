"""A slow stage of a slot close logs a warning that names it.

The gateway logs warnings and up, so this line is the only record of where a
minute-long close spent its time. Driven through the real ``close_slot`` with the
history save stubbed, on a manual clock that moves only inside the stub.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.testing.clock import ManualClock


@pytest.mark.asyncio
async def test_a_slow_session_teardown_is_named_in_one_warning(tmp_path, monkeypatch, caplog):
    clock = ManualClock()
    clock.install(monkeypatch, handlers)
    monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock(return_value=True))
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("slow")

    async def _slow_teardown(_key: str) -> None:
        clock.advance(52.0)

    state.sessions.remove = _slow_teardown

    with caplog.at_level(logging.WARNING, logger=handlers.logger.name):
        await handlers.close_slot(state, slot, "slow")

    assert state.get_slot("slow") is None
    stage_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == handlers.logger.name and record.getMessage().startswith("Closing slot ")
    ]
    assert stage_lines == ["Closing slot slow: session teardown took 52.0s"]

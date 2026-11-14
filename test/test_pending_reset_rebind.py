"""A deferred project reset never tears down a session its slot left during the consume."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_runner


def _slot(key: str = "dashboard:test", project: str = "/project/b"):
    return SimpleNamespace(
        key=key,
        project=project,
        _pending_reset_history_key=key,
        _pending_reset_clears_cwd=False,
        forget_session_model_state=MagicMock(),
        messages=[],
    )


def _state(reset):
    state = MagicMock()
    state.sessions.reset = reset
    return state


@pytest.mark.asyncio
async def test_a_rebind_during_the_children_probe_follows_the_slot_and_resets_nothing():
    slot = _slot()
    keys = iter(["dashboard:test", "cron:job-9", "cron:job-9", "cron:job-9"])
    reset = AsyncMock(return_value=True)
    with (
        patch.object(chat_runner, "effective_session_key", lambda _s: next(keys)),
        patch.object(chat_runner, "subagents_attached_async", AsyncMock(return_value=False)),
        patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
    ):
        torn_down = await chat_runner._consume_pending_reset(_state(reset), slot)

    assert torn_down is False
    reset.assert_not_awaited()
    assert slot._pending_reset_history_key == "cron:job-9"
    retry.assert_called_once()


@pytest.mark.parametrize("moved", ["project", "key"])
@pytest.mark.asyncio
async def test_a_change_during_the_teardown_keeps_the_reset_owed(moved):
    slot = _slot()
    key = {"now": "dashboard:test"}

    async def _reset(*_a, **_k):
        if moved == "project":
            slot.project = "/project/c"
        else:
            key["now"] = "cron:job-9"
        return True

    with (
        patch.object(chat_runner, "effective_session_key", lambda _s: key["now"]),
        patch.object(chat_runner, "subagents_attached_async", AsyncMock(return_value=False)),
        patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
    ):
        torn_down = await chat_runner._consume_pending_reset(_state(_reset), slot)

    assert torn_down is True
    assert slot._pending_reset_history_key == key["now"]
    retry.assert_called_once()

"""A deferred project reset lands on the key its slot runs on when the slot moves mid-consume.

``_consume_pending_reset`` awaits twice while it holds the queued key: the children
probe and the teardown. A rebind during either, or another project change during the
teardown, must leave the reset owed on the slot's current key rather than spend it on
the key the slot left.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state

from kiro_crew.dashboard import chat_runner


def _armed_slot():
    slot = _slot()
    slot.linked_session_key = "slack:A.old"
    slot.project = "/project/b"
    slot._pending_reset_history_key = "slack:A.old"
    return slot


@pytest.mark.asyncio
async def test_a_rebind_during_the_children_probe_resets_nothing_and_follows_the_slot(tmp_path):
    state, slot = _state(tmp_path), _armed_slot()
    state.sessions.reset = AsyncMock(return_value=True)

    async def _probe(*_args):
        slot.linked_session_key = "slack:B.new"
        return False

    with (
        patch.object(chat_runner, "subagents_attached_async", _probe),
        patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
    ):
        torn_down = await chat_runner._consume_pending_reset(state, slot)

    assert torn_down is False
    state.sessions.reset.assert_not_awaited()
    assert slot._pending_reset_history_key == "slack:B.new"
    retry.assert_called_once_with(state, slot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moved, owed_on",
    [
        pytest.param("project", "slack:A.old", id="another-project-change"),
        pytest.param("key", "slack:B.new", id="a-rebind"),
    ],
)
async def test_a_change_during_the_teardown_keeps_the_reset_owed(tmp_path, moved, owed_on):
    state, slot = _state(tmp_path), _armed_slot()

    async def _reset(_key, **_kwargs):
        if moved == "project":
            slot.project = "/project/c"
        else:
            slot.linked_session_key = "slack:B.new"
        return True

    state.sessions.reset = AsyncMock(side_effect=_reset)
    with patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry:
        torn_down = await chat_runner._consume_pending_reset(state, slot)

    assert torn_down is True
    state.sessions.reset.assert_awaited_once_with("slack:A.old", skip_if_busy=True)
    assert slot._pending_reset_history_key == owed_on
    retry.assert_called_once_with(state, slot)

"""A deferred reset may not be applied against a key whose arm transfer never settled.

The consume path compares the key it armed against the key the settle reports. An UNSETTLED
settle reports the key it last observed -- which is the armed one when the rebind that stopped
it settling was PARKED rather than applied -- so key equality alone reads as "no rebind". The
reset then tears down the session the slot is leaving while the session it moves to keeps the
project it was supposed to lose.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.chat_runner import _consume_pending_reset
from kiro_crew.dashboard.chat_utils import bind_linked_session_key
from kiro_crew.dashboard.state import _ChatSlot


def _state_for(slot):
    state = MagicMock()
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.supersede_arm_for_new_slot = MagicMock()
    state.sessions.transfer_retire_arm = MagicMock()
    state.conversation_log = MagicMock()
    return state


class TestAnUnsettledTransferDoesNotConsumeTheReset:
    @pytest.mark.asyncio
    async def test_a_parked_rebind_leaves_the_reset_armed_for_the_live_key(self):
        """The reproduction: the settle reports the ARMED key while reporting unsettled."""
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1111.0001"
        # CLEARED, which is the only state whose settle awaits at all: a stated project and an
        # unset one are both answered synchronously, so no rebind can land inside their region.
        slot.project = ""
        slot.project_cleared = True
        slot._pending_reset_history_key = "slack:1111.0001"

        state = _state_for(slot)

        # A rebind reaches the slot inside the settle's own region, so it is PARKED: the key
        # the settle observes never moves, and the settle reports unsettled instead.
        async def resolve(key, cleared):
            bind_linked_session_key(slot, "cron:job-9")
            return "/workspace/new"

        state.sessions.resolve_arm_cwd = resolve

        with patch("kiro_crew.dashboard.chat_runner._arm_pending_reset_retry") as rearm:
            torn_down = await _consume_pending_reset(state, slot, allow_discard=False)

        assert state.sessions.reset.await_count == 0, (
            "the reset was applied against a key whose transfer never settled, tearing down "
            "the session the slot is leaving while its live session keeps the old project; "
            f"reset called with {state.sessions.reset.await_args}"
        )
        assert not torn_down, "a teardown was reported for an unsettled transfer"
        assert rearm.called, (
            "the reset was neither applied nor re-armed, so the project change is lost "
            "entirely rather than deferred"
        )
        assert slot._pending_reset_history_key == "cron:job-9", (
            "the flag was left on the abandoned key, so the retry repeats the same mistake; "
            f"flag={slot._pending_reset_history_key!r}"
        )

    @pytest.mark.asyncio
    async def test_a_settled_transfer_still_applies_the_reset(self):
        """The refusal must be the unsettled transfer, not every deferred reset."""
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1111.0001"
        slot.project = "/workspace/new"
        slot._pending_reset_history_key = "slack:1111.0001"

        state = _state_for(slot)
        state.sessions.resolve_arm_cwd = AsyncMock(return_value="/workspace/new")

        with patch("kiro_crew.dashboard.chat_runner.subagents_attached", return_value=False):
            await _consume_pending_reset(state, slot, allow_discard=False)

        assert (
            state.sessions.reset.await_count == 1
        ), "a settled transfer failed to apply its reset, so no project change ever lands"
        assert state.sessions.reset.await_args[0][0] == "slack:1111.0001"

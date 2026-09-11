"""Cron delivery into a target session whose TAB IS CLOSED.

Closing a dashboard tab archives it: the slot is popped while the transcript stays
on disk and the conversation reopens from that file. Mirroring into the live slot
therefore covers only the window in which the tab happens to be open -- which is
the opposite of what naming a target is usually for, since "remind me in this
thread later" implies the thread is not on screen when the job fires.

Delivery closes that gap by making the conversation LIVE first
(``ensure_target_session_slot``) and letting the ordinary live-slot mirror do the
writing, rather than appending to a transcript this job does not own. These pin
that two-step shape: what it reaches, what it declines to reach, and that the two
steps agree on which targets they act for -- a rehydrate the mirror then skips
would reopen somebody's archived tab for a delivery that never arrives.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from chat_test_helpers import _make_state  # noqa: E402

from kiro_crew.cron import CronJob  # noqa: E402
from kiro_crew.dashboard.cron_inject import (  # noqa: E402
    ensure_target_session_slot,
    inject_cron_result_to_dashboard,
)
from kiro_crew.session_surface import set_dashboard_surfaced  # noqa: E402

_SLOT = "my-chat"
_TARGET = f"dashboard:{_SLOT}"
_TAB = "tab0123456789"
_RESULT = "the queue is empty"


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The bind publishes to the process-global dashboard-surface registry.

    Reset it so keys from these states never leak into other tests -- the same
    fixture `test_cron_first_run_tab.py` and the live-mirror suite carry.
    """
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _job(**overrides):
    kwargs = {
        "id": "j1",
        "name": "nightly",
        "message": "summarize the queue",
        "session_key": _TARGET,
    }
    kwargs.update(overrides)
    return CronJob(**kwargs)


def _closed_tab_state(tmp_path, *, memory_mode=None, deleted=False):
    """A conversation that exists on disk with its tab closed.

    Built the way the product does it: open the tab, log a turn under the tab's
    identity so the transcript carries it, then pop the slot exactly as the close
    handler does -- which also discards the restricted-key entry, leaving the
    transcript header as the only remaining record of the session's memory mode.
    """
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name=_SLOT)
    slot._tab_id = _TAB
    if memory_mode:
        slot.memory_mode = memory_mode
    state.conversation_log.append(_TARGET, "user", "what shipped?", tab_id=_TAB)
    if memory_mode:
        state.conversation_log.update_metadata(_TARGET, {"memory_mode": memory_mode})
    state._slots.pop(_SLOT, None)
    state._restricted_keys.discard(_TARGET)
    if deleted:
        state.conversation_log.delete_session(_TARGET)
    return state


async def _deliver(state, job):
    """Both steps, in the order the executor performs them."""
    await ensure_target_session_slot(state, job)
    inject_cron_result_to_dashboard(state, job, _RESULT, history=[])


def _slot_results(state, slot_name=_SLOT):
    slot = state.get_slot(slot_name)
    if slot is None:
        return []
    return [
        m.get("content", "")
        for m in slot.messages
        if m.get("content", "").startswith("# Cron Job Result:")
    ]


def _disk_rows(state, key=_TARGET):
    return state.conversation_log.read_messages(key)


class TestDeliveryIntoAClosedTab:
    @pytest.mark.asyncio
    async def test_the_result_reaches_the_reopened_conversation(self, tmp_path):
        state = _closed_tab_state(tmp_path)
        assert state.get_slot(_SLOT) is None
        await _deliver(state, _job())
        assert len(_slot_results(state)) == 1
        assert _RESULT in _slot_results(state)[0]

    @pytest.mark.asyncio
    async def test_the_conversations_own_history_comes_back_with_it(self, tmp_path):
        """A rehydrate, not a fresh tab: the result lands BELOW what was there."""
        state = _closed_tab_state(tmp_path)
        await _deliver(state, _job())
        bodies = [m.get("content", "") for m in state.get_slot(_SLOT).messages]
        assert bodies[0] == "what shipped?"
        assert bodies[-1].startswith("# Cron Job Result:")

    @pytest.mark.asyncio
    async def test_the_job_still_gets_its_own_tab(self, tmp_path):
        """A mirror, not a move -- the cron keeps its own conversation."""
        state = _closed_tab_state(tmp_path)
        await _deliver(state, _job())
        assert state.get_slot("cron-j1") is not None

    @pytest.mark.asyncio
    async def test_the_target_tab_is_not_rebound_to_the_cron(self, tmp_path):
        """Rebinding would make the person's next turn run AS the job."""
        state = _closed_tab_state(tmp_path)
        await _deliver(state, _job())
        linked = getattr(state.get_slot(_SLOT), "linked_session_key", "") or ""
        assert linked != "cron:j1"

    @pytest.mark.asyncio
    async def test_a_refire_does_not_stack_a_second_copy(self, tmp_path):
        state = _closed_tab_state(tmp_path)
        await _deliver(state, _job())
        await _deliver(state, _job())
        assert len(_slot_results(state)) == 1


class TestWhatDeliveryDeclinesToReach:
    @pytest.mark.asyncio
    async def test_a_deleted_conversation_is_not_resurrected(self, tmp_path):
        """Reopening it would rebuild a chat holding one orphaned result.

        The guarantee is the rehydrate's, not this path's: ``delete_session``
        leaves no tombstone, so ``_deletion_during_read`` is what declines to
        publish a slot from content the read already held.
        """
        state = _closed_tab_state(tmp_path, deleted=True)
        await _deliver(state, _job())
        assert state.get_slot(_SLOT) is None
        assert _disk_rows(state) == []

    @pytest.mark.asyncio
    async def test_a_target_that_was_never_persisted_is_left_alone(self, tmp_path):
        state = _make_state(tmp_path)
        await _deliver(state, _job())
        assert state.get_slot(_SLOT) is None
        assert _disk_rows(state) == []

    @pytest.mark.asyncio
    async def test_the_job_still_records_its_own_result_when_the_target_is_gone(self, tmp_path):
        """A missing target costs a copy, not the run."""
        state = _closed_tab_state(tmp_path, deleted=True)
        await _deliver(state, _job())
        assert _slot_results(state, "cron-j1")


class TestASessionWithMemoryWritesDisabled:
    """The mode the user chose keeps binding after a close.

    A close discards the session's ``_restricted_keys`` entry, so nothing in live
    state remembers the choice. The restore re-adds it from the transcript header,
    which is what puts the decision back under the platform's own restriction
    rather than under a header comparison made here.
    """

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    @pytest.mark.asyncio
    async def test_the_restriction_is_reinstated(self, tmp_path, mode):
        state = _closed_tab_state(tmp_path, memory_mode=mode)
        await _deliver(state, _job())
        assert _TARGET in state._restricted_keys
        assert state.get_slot(_SLOT).memory_mode == mode

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    @pytest.mark.asyncio
    async def test_no_new_durable_row_is_written(self, tmp_path, mode):
        state = _closed_tab_state(tmp_path, memory_mode=mode)
        before = len(_disk_rows(state))
        await _deliver(state, _job())
        assert len(_disk_rows(state)) == before

    @pytest.mark.asyncio
    async def test_a_persistent_target_is_unrestricted(self, tmp_path):
        state = _closed_tab_state(tmp_path, memory_mode="persistent")
        await _deliver(state, _job())
        assert _TARGET not in state._restricted_keys
        assert len(_slot_results(state)) == 1


class TestTheTwoStepsAgree:
    """Neither step may act for a target the other one skips.

    The rehydrate has a visible side effect -- somebody's archived conversation
    reappears in their tab list -- so a target the mirror would decline must not
    reach it. These pin the exclusions on the rehydrate side; the live-mirror
    suite pins the same ones on the mirror side.
    """

    @pytest.mark.asyncio
    async def test_a_job_targeting_its_own_tab_reopens_nothing(self, tmp_path):
        state = _closed_tab_state(tmp_path)
        await ensure_target_session_slot(state, _job(session_key="dashboard:cron-j1"))
        assert state.get_slot("cron-j1") is None

    @pytest.mark.asyncio
    async def test_a_channel_target_reopens_nothing(self, tmp_path):
        state = _closed_tab_state(tmp_path)
        await ensure_target_session_slot(state, _job(session_key="slack:C123"))
        assert state.get_slot(_SLOT) is None

    @pytest.mark.asyncio
    async def test_a_job_with_no_target_reopens_nothing(self, tmp_path):
        state = _closed_tab_state(tmp_path)
        await ensure_target_session_slot(state, _job(session_key=""))
        assert state.get_slot(_SLOT) is None

    @pytest.mark.asyncio
    async def test_a_corrupt_non_string_target_is_survived(self, tmp_path):
        """The field round trips through `crons.json` without coercion."""
        state = _closed_tab_state(tmp_path)
        job = _job()
        job.session_key = 42  # type: ignore[assignment]
        await ensure_target_session_slot(state, job)
        assert state.get_slot(_SLOT) is None


class TestTheLiveTabIsUnchanged:
    @pytest.mark.asyncio
    async def test_an_open_tab_takes_the_mirror_without_a_rehydrate(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name=_SLOT)
        slot._tab_id = _TAB
        await _deliver(state, _job())
        assert state.get_slot(_SLOT) is slot
        assert len(_slot_results(state)) == 1

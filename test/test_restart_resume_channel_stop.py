"""A Stop from another surface while a resume is pending: the person's intent wins.

Sibling of ``test_restart_resume.py`` (which holds the shared slot/state helpers,
the held seams and the ``quiet_dispatch`` fixture this module imports). A Stop
issued on a linked channel (Slack's ``!stop``, a transport's stop verb) moves the
session manager's stop counter for the session the chat's turns run on and
nothing else: on an idle chat it writes no Stop card, moves no slot epoch and
cancels no task. Both passes therefore read that counter themselves -- when the
retry is armed or the chat becomes a boot candidate, again after the wait, and a
last time under the slot lock before the queue insert (GPT review, rev 47).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from test_cross_surface_stop_gate import LINKED_KEY, _provider_factory
from test_restart_resume import (  # noqa: F401 -- the fixture is requested by name
    _RETRY_INVITED_ROW,
    _cfg,
    _cut_off,
    _HeldPause,
    _HeldSleep,
    _mock_state,
    _slot,
    quiet_dispatch_fixture,
)
from test_restart_resume_budget import _FakeLog

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import restart_resume
from kiro_crew.session import SessionManager


def _state_with_stop_counter(*slots):
    """A state whose session manager answers the stop counter the test sets."""
    state = _mock_state(*slots)
    state.sessions = MagicMock()
    state.sessions.stop_generation = MagicMock(return_value=0)
    return state


def _channel_stop(state) -> None:
    """``SessionManager.stop_turn`` / ``note_stop`` from the channel: the counter alone moves."""
    state.sessions.stop_generation.return_value = 1


def _state_with_real_sessions(*slots):
    """A state whose session manager is the real one, with NO live session for
    the chats: a restored chat before its first turn, or one whose failed turn
    tore its session down. ``note_stop`` records nothing for such a key unless a
    resume is armed for it (GPT review, rev 49)."""
    state = _mock_state(*slots)
    state.sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    return state


def _linked(slot):
    """A channel-born chat: its turns run on the channel's own session."""
    slot.linked_session_key = LINKED_KEY
    return slot


def _slack_stop(state) -> None:
    """What Slack's ``!stop`` does first on an idle thread (``note_user_stop``):
    one ``note_stop`` under the bare thread ts; ``stop_turn`` then finds nothing
    running."""
    state.sessions.note_stop(LINKED_KEY.split(":", 1)[1])


class TestAChannelStopBeforeTheSessionExists:
    """GPT review, rev 49: the counter the passes compare is moved by ``note_stop``
    only for a key with a live session or an open replay gap. A restored chat
    has neither until its first turn, so without the passes arming the key the
    Slack Stop was dropped ("Nothing running.") and the resume ran anyway."""

    @pytest.mark.asyncio
    async def test_a_slack_stop_during_the_boot_pause_skips_the_restored_chat(
        self, monkeypatch, quiet_dispatch
    ):
        slot = _linked(_cut_off(_slot("restored")))
        state = _state_with_real_sessions(slot)
        held = _HeldPause()
        monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.pause_before", held)

        task = asyncio.ensure_future(restart_resume.resume_after_restart(state, _cfg()))
        await asyncio.wait_for(held.entered.wait(), 30)
        _slack_stop(state)
        held.release.set()

        assert await asyncio.wait_for(task, 30) == 0
        assert quiet_dispatch == []
        assert slot._restart_cut_turn is False
        # The pass is over: the key is disarmed and an idle Stop records nothing again.
        assert state.sessions.note_stop(LINKED_KEY) is False
        await state.sessions.close_all()

    @pytest.mark.asyncio
    async def test_a_slack_stop_during_the_notice_pass_skips_the_restored_chat(
        self, monkeypatch, quiet_dispatch
    ):
        # The boot task runs the approval-mode notice pass FIRST, and that pass
        # awaits the deferred transcript of every chat that was on a grant. A
        # Stop landing there is before the resume pass could arm anything, so
        # the arming is synchronous in start_boot_pass, before the task exists
        # (GPT review, rev 50).
        slot = _linked(_cut_off(_slot("restored")))
        state = _state_with_real_sessions(slot)
        monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.pause_before", _HeldPause())
        held = _HeldPause()

        async def notice_pass(st, cfg):
            await held("notice")
            return 0

        monkeypatch.setattr(restart_resume, "notice_dropped_approval_modes", notice_pass)
        task = restart_resume.start_boot_pass(state, _cfg())
        assert state.sessions.note_stop(LINKED_KEY) is True  # armed before the first await
        await asyncio.wait_for(held.entered.wait(), 30)
        held.release.set()

        assert await asyncio.wait_for(task, 30) == 0
        assert quiet_dispatch == []
        assert slot._restart_cut_turn is False
        assert state.sessions.note_stop(LINKED_KEY) is False
        await state.sessions.close_all()

    def test_arming_the_cut_chats_is_synchronous_and_once_per_chat(self):
        cut, other, owned = _linked(_cut_off(_slot("cut"))), _slot("other"), _cut_off(_slot("x"))
        owned._app = "an-app"  # the app's to continue: never armed
        state = _state_with_real_sessions(cut, other, owned)

        assert restart_resume.arm_cut_chats(state) == 1
        assert restart_resume.arm_cut_chats(state) == 0  # already armed: not again
        assert cut._restart_stop_count == 0
        assert other._restart_stop_count is None and owned._restart_stop_count is None
        assert state.sessions.note_stop(LINKED_KEY) is True
        # One arm, so one disarm releases the key.
        restart_resume.disarm_cut_chats(state)
        assert cut._restart_stop_count is None
        assert state.sessions.note_stop(LINKED_KEY) is False

    @pytest.mark.asyncio
    async def test_a_boot_task_cancelled_before_it_ran_disarms_the_keys(self):
        slot = _linked(_cut_off(_slot("restored")))
        state = _state_with_real_sessions(slot)

        task = restart_resume.start_boot_pass(state, _cfg())
        assert state.sessions.note_stop(LINKED_KEY) is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert state.sessions.note_stop(LINKED_KEY) is False
        await state.sessions.close_all()

    @pytest.mark.asyncio
    async def test_a_slack_stop_during_the_backoff_drops_the_retry(
        self, monkeypatch, quiet_dispatch
    ):
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        slot = _linked(_cut_off(_slot(), kind=_RETRY_INVITED_ROW))
        state = _state_with_real_sessions(slot)

        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(held.entered.wait(), 30)
        _slack_stop(state)
        held.release.set()
        await asyncio.wait_for(slot._auto_resume_task, 30)

        assert quiet_dispatch == []
        assert slot._auto_resume_stamps == []
        assert state.sessions.note_stop(LINKED_KEY) is False
        await state.sessions.close_all()

    @pytest.mark.asyncio
    async def test_a_retry_cancelled_before_it_ran_disarms_the_key(self, monkeypatch):
        # cancel_armed_retry can cancel the task before its first step, so the
        # body's own cleanup never runs: the disarm must not depend on it.
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        slot = _linked(_cut_off(_slot(), kind=_RETRY_INVITED_ROW))
        state = _state_with_real_sessions(slot)

        assert restart_resume.schedule_after_error(state, slot) is True
        task = slot._auto_resume_task
        assert state.sessions.note_stop(LINKED_KEY) is True  # armed: recordable
        restart_resume.cancel_armed_retry(slot)
        with pytest.raises(asyncio.CancelledError):
            await task

        assert state.sessions.note_stop(LINKED_KEY) is False
        await state.sessions.close_all()


class TestAChannelStopDuringTheErrorPass:
    @pytest.mark.asyncio
    async def test_a_stop_during_the_backoff_drops_the_retry(self, monkeypatch, quiet_dispatch):
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _state_with_stop_counter(slot)

        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(held.entered.wait(), 30)
        _channel_stop(state)  # "!stop" in the linked thread while the retry waits
        held.release.set()
        await asyncio.wait_for(slot._auto_resume_task, 30)

        assert quiet_dispatch == []
        assert slot._auto_resume_stamps == []

    @pytest.mark.asyncio
    async def test_a_stop_after_the_backoff_refuses_the_insert_under_the_lock(self, monkeypatch):
        # The retry woke and reached continue_slot_turn; the Stop lands on the
        # channel before the queue insert. The hook runs under the slot lock,
        # refuses, and the stamp spent for this attempt is taken back.
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: None)
        monkeypatch.setattr(restart_resume, "_audit", lambda *a, **k: None)
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _state_with_stop_counter(slot)
        state.conversation_log = _FakeLog(True)
        inserted: list[str] = []

        async def continue_with_hook(state, slot, **kwargs):
            _channel_stop(state)
            kwargs["before_dispatch"]()
            inserted.append(slot.key)
            return "resumed"

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.continue_slot_turn", continue_with_hook
        )
        assert restart_resume.schedule_after_error(state, slot) is True
        task = slot._auto_resume_task
        await asyncio.wait_for(held.entered.wait(), 30)
        held.release.set()
        await asyncio.wait_for(task, 30)

        assert inserted == []
        assert slot._auto_resume_stamps == []


class TestAChannelStopDuringTheBootPass:
    @pytest.mark.asyncio
    async def test_a_stop_during_the_window_load_skips_the_chat(self, monkeypatch, quiet_dispatch):
        # The window load awaits a worker thread; a Stop on the channel during
        # that await moves the counter before the pass could otherwise read it.
        slot = _cut_off(_slot("stopped"))
        state = _state_with_stop_counter(slot)

        async def load_then_stop(s):
            _channel_stop(state)

        monkeypatch.setattr(restart_resume, "load_window_off_loop", load_then_stop)

        assert await restart_resume.resume_after_restart(state, _cfg()) == 0
        assert quiet_dispatch == []
        assert slot._auto_resume_stamps == []
        assert slot._restart_cut_turn is False

    @pytest.mark.asyncio
    async def test_a_stop_during_the_settle_skips_the_chat(self, monkeypatch, quiet_dispatch):
        stopped, other = _cut_off(_slot("stopped")), _cut_off(_slot("other"))
        state = _state_with_stop_counter(stopped, other)
        # One counter per session: only the stopped chat's session moves.
        state.sessions.stop_generation = MagicMock(return_value=0)
        held = _HeldPause()
        monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.pause_before", held)

        task = asyncio.ensure_future(restart_resume.resume_after_restart(state, _cfg()))
        await asyncio.wait_for(held.entered.wait(), 30)
        state.sessions.stop_generation = MagicMock(
            side_effect=lambda key: 1 if key.endswith("stopped") else 0
        )
        held.release.set()

        assert await asyncio.wait_for(task, 30) == 1
        assert [c["slot"] for c in quiet_dispatch] == ["other"]
        assert stopped._auto_resume_stamps == []
        # This boot's reading is spent for the stopped chat too: a later pass in
        # this process must not resume what the person stopped.
        assert stopped._restart_cut_turn is False

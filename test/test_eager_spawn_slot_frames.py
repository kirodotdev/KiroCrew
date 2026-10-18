"""The slot signals that start or relay speculation: the loop-safe resumable-hint probe, the
slot-focused intent frame, and the cross-window slot-read relay.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _cfg,
    _isolate_armed_registry,
    _mock_state,
    _native_resume_cfg,
    _pin_prewarm_allowance,
    _prime,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


class TestResumableHint:
    """SessionMap.has_hint: the loop-safe membership probe."""

    @pytest.fixture
    def smap(self, tmp_path, monkeypatch):
        from kiro_crew.session_map import SessionMap

        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        return SessionMap(), sessions_dir

    def test_hint_true_for_entry_even_without_files(self, smap):
        """The hint is membership only — a stale entry (files gone) still
        hints True. Callers tolerate the false positive: the pruning get()
        inside the resume path is the authority."""
        m, _ = smap
        m.set("dashboard:a", "sid-a")
        assert m.has_hint("dashboard:a") is True

    def test_hint_false_for_absent_key(self, smap):
        m, _ = smap
        assert m.has_hint("dashboard:missing") is False

    def test_hint_never_mutates_the_map(self, smap):
        """Unlike get(), has_hint must not prune or save — it runs on the
        event loop, and SessionMap is unlocked and loop-owned."""
        m, _ = smap
        m.set("dashboard:stale", "gone-sid")  # no session files exist
        assert m.has_hint("dashboard:stale") is True
        assert m.has_hint("dashboard:stale") is True  # still there — no prune
        assert m.get("dashboard:stale") is None  # get() DOES prune it
        assert m.has_hint("dashboard:stale") is False


class TestSlotFocusedFrame:
    """ws._handle_slot_focused: the slot-focused intent signal."""

    def _state(self, slot, *, has_session=False, resumable="prior-sid"):
        state = MagicMock(spec=DashboardState)
        state.get_slot = MagicMock(return_value=slot)
        state.sessions = MagicMock()
        state.sessions.has_session = MagicMock(return_value=has_session)
        state.sessions.resumable_hint = MagicMock(return_value=bool(resumable))
        return state

    @pytest.mark.asyncio
    async def test_resumable_focus_schedules_resume_prefetch(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        _prime(True)
        task = _handle_slot_focused(state, "t1", None, owner=True)
        assert task is not None
        assert slot._eager_spawn_task is task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_focus_change_cancels_previous_prefetch(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        _prime(True)
        first = _handle_slot_focused(state, "t1", None, owner=True)
        second = _handle_slot_focused(state, "t1", first, owner=True)
        assert first is not None and second is not None
        with pytest.raises(asyncio.CancelledError):
            await first
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    @pytest.mark.asyncio
    async def test_blur_cancels_and_schedules_nothing(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        _prime(True)
        pending = _handle_slot_focused(state, "t1", None, owner=True)
        result = _handle_slot_focused(state, None, pending, owner=True)
        assert result is None
        assert pending is not None
        with pytest.raises(asyncio.CancelledError):
            await pending

    @pytest.mark.asyncio
    async def test_live_session_schedules_nothing(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot, has_session=True)
        assert _handle_slot_focused(state, "t1", None, owner=True) is None

    @pytest.mark.asyncio
    async def test_non_resumable_slot_spawns_nothing(self, monkeypatch):
        """A non-resumable key never creates a session from the focus path.
        The probe is the loop-safe in-memory HINT (no disk, no pruning — the
        pruning ``resumable_sid`` lookup must not run off-loop against the
        unlocked, loop-owned SessionMap); the spawn task re-checks it after
        the debounce, and fresh eager spawn stays owned by the
        create/project/agent signals."""
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.resumable_hint = MagicMock(return_value=False)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_resumable_focus_preserves_pending_fresh_spawn(self):
        """Creating a slot focuses it, so the focus frame lands right behind
        the create signal. schedule_eager_spawn keeps ONE task per slot —
        routing a non-resumable focus through it would cancel the
        create-armed FRESH spawn and then no-op, silently gutting fresh eager
        spawn for every new slot. The handler must check the hint itself and
        leave the pending task untouched."""
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot, resumable=None)
        fresh = asyncio.get_running_loop().create_future()
        pending = asyncio.ensure_future(fresh)
        slot._eager_spawn_task = pending
        try:
            result = _handle_slot_focused(state, "t1", None, owner=True)
            assert result is None
            assert slot._eager_spawn_task is pending
            assert not pending.cancelled()
        finally:
            fresh.set_result(None)
            # flake-ok: pre-existing task await with no timeout, moved verbatim; the set_result() just before it releases it
            await pending

    @pytest.mark.asyncio
    async def test_running_turn_schedules_nothing(self):
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        # slot.running derives from slot.task being a live task.
        _turn = asyncio.get_running_loop().create_future()
        _task = asyncio.ensure_future(_turn)
        slot.task = _task
        state = self._state(slot)
        try:
            assert _handle_slot_focused(state, "t1", None, owner=True) is None
        finally:
            _turn.set_result(None)
            # flake-ok: pre-existing task await with no timeout, moved verbatim; the set_result() just before it releases it
            await _task

    @pytest.mark.asyncio
    async def test_non_owner_socket_schedules_nothing_and_cancels_nothing(self):
        """An app-scoped socket must not start owner-session processes or
        cancel another arm's prefetch — the frame is ignored entirely."""
        from kiro_crew.dashboard.ws import _handle_slot_focused

        slot = _ChatSlot("t1")
        state = self._state(slot)
        _prime(True)
        pending = _handle_slot_focused(state, "t1", None, owner=True)
        result = _handle_slot_focused(state, "t1", pending, owner=False)
        assert result is pending  # passed through untouched
        assert pending is not None and not pending.cancelled()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    @pytest.mark.asyncio
    async def test_failed_speculative_load_leaves_nothing_behind(self):
        """A speculative resume whose load fell back is rejected BEFORE
        registration (SpeculativeResumeRefused): no claimable fallback session
        ever exists, so there is nothing to remove, no TTL to arm, and no
        semaphore to release — the first real message creates and maps the
        fallback itself with the normal F2 recovery."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(
            side_effect=chat_runner.SpeculativeResumeRefused("dashboard:t1")
        )
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        state.sessions.remove_if_unclaimed.assert_not_awaited()
        state.sessions.remove.assert_not_awaited()
        state.sessions.release.assert_not_called()
        assert getattr(slot, "_prefetch_ttl_task", None) is None


class TestSlotReadFrame:
    """ws._handle_slot_read: the cross-window unread-badge read relay.

    A window that read a slot tells the gateway; the gateway rebroadcasts to
    every owner window so their bubbles retire too. Pure relay — no server
    read-state — so the contract under test is small: owner-gated, validated
    slot key, one owner-scoped broadcast.
    """

    def _state(self):
        return MagicMock(spec=DashboardState)

    def test_owner_read_broadcasts_to_owner_clients(self):
        from kiro_crew.dashboard.ws import _handle_slot_read

        state = self._state()
        assert _handle_slot_read(state, "t1", owner=True) is True
        state.broadcast_ws_owners.assert_called_once_with("slot_read", {"slot": "t1"})

    def test_read_watermark_is_relayed_opaquely(self):
        from kiro_crew.dashboard.ws import _handle_slot_read

        state = self._state()
        assert _handle_slot_read(state, "t1", "2026-09-10T00:00:00Z", owner=True) is True
        state.broadcast_ws_owners.assert_called_once_with(
            "slot_read", {"slot": "t1", "read_ts": "2026-09-10T00:00:00Z"}
        )

    def test_junk_read_watermark_is_dropped_but_frame_relays(self):
        """A bad watermark degrades to a watermark-less relay (receivers apply
        their conservative default) rather than dropping the read gesture."""
        from kiro_crew.dashboard.ws import _handle_slot_read

        for junk in (42, "", "x" * 65, {"ts": "y"}):
            state = self._state()
            assert _handle_slot_read(state, "t1", junk, owner=True) is True
            state.broadcast_ws_owners.assert_called_once_with("slot_read", {"slot": "t1"})

    def test_non_owner_frame_is_ignored(self):
        """An app-scoped socket must not clear the user's badges."""
        from kiro_crew.dashboard.ws import _handle_slot_read

        state = self._state()
        assert _handle_slot_read(state, "t1", owner=False) is False
        state.broadcast_ws_owners.assert_not_called()

    def test_junk_slot_keys_are_ignored(self):
        from kiro_crew.dashboard.ws import _handle_slot_read

        state = self._state()
        for junk in (None, "", 42, {"slot": "x"}, "k" * 513):
            assert _handle_slot_read(state, junk, owner=True) is False
        state.broadcast_ws_owners.assert_not_called()

    def test_deleted_slot_key_still_relays(self):
        """No liveness check on purpose: a read of a just-deleted slot must
        still clear stale badges in other windows (their unread drain only
        prunes keys missing from a later slots snapshot)."""
        from kiro_crew.dashboard.ws import _handle_slot_read

        state = self._state()
        state.get_slot = MagicMock(return_value=None)
        assert _handle_slot_read(state, "gone", owner=True) is True
        state.broadcast_ws_owners.assert_called_once_with("slot_read", {"slot": "gone"})

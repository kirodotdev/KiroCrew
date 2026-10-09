"""The live-slot count stays under ``MAX_LIVE_SLOTS`` without a person's click.

Two halves: the open-tab restore stops building ordinary tabs at
``RESTORE_SLOT_BUDGET`` (newest first; pinned and loop-driven tabs always come
back), and the idle sweep archives idle tabs through ``close_slot`` once the
count reaches ``SWEEP_HIGH_WATER``.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import slot_retention
from kiro_crew.dashboard.chat_persistence import (
    ENV_AUTHORITY_RESTORED,
    restore_open_slots,
    restore_open_slots_async,
)
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS

# ── Restore budget ──

_SEEDED = MAX_LIVE_SLOTS + 1
_PINNED = "chat-0-pinned"
_LOOPED = "chat-0-looped"


def _seed_open_tabs(tmp_path, monkeypatch):
    """Seed more open tabs than the cap; the pinned and looped ones are the OLDEST."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    keys = [f"chat-{i}-tab" for i in range(1, _SEEDED + 1)] + [_PINNED, _LOOPED]
    base = 1_700_000_000
    for i, key in enumerate(keys):
        history_key = _history_key_for(key)
        log.append(history_key, "user", "hello")
        # chat-1 is the newest; the two exempt tabs are older than every other one.
        mtime = base + (_SEEDED - i if key not in (_PINNED, _LOOPED) else -i)
        os.utime(log._path(history_key), (mtime, mtime))
    log.update_metadata(_history_key_for(_PINNED), {"pinned": True})
    os.utime(log._path(_history_key_for(_PINNED)), (base - 10, base - 10))
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))
    (tmp_path / "autonudge.json").write_text(
        json.dumps({"version": 1, "loops": [{"slot_key": _LOOPED, "active": True}]})
    )
    return _make_state(tmp_path / "sessions"), log


def _assert_budget_kept(state, log, restored: int) -> None:
    budget = slot_retention.RESTORE_SLOT_BUDGET
    assert restored == budget + 2
    assert len(state._slots) == budget + 2 <= MAX_LIVE_SLOTS
    assert _PINNED in state._slots
    assert _LOOPED in state._slots
    # The newest ordinary tabs came back, the oldest did not.
    assert all(f"chat-{i}-tab" in state._slots for i in range(1, budget + 1))
    left = [f"chat-{i}-tab" for i in range(budget + 1, _SEEDED + 1)]
    assert left and not any(k in state._slots for k in left)
    # Left in history, not deleted and not carried as unreadable.
    assert all(log.has_log(_history_key_for(k)) for k in left)
    assert not set(left) & set(state.unrestored_slot_keys)


def test_sync_restore_stops_at_the_budget_and_keeps_pinned_and_looped(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
    state, log = _seed_open_tabs(tmp_path, monkeypatch)
    # Suspended as at startup, so the 400 builds broadcast once rather than each.
    with state.suspend_slots_push():
        restored = restore_open_slots(state)
    _assert_budget_kept(state, log, restored)


def test_async_restore_stops_at_the_budget_and_keeps_pinned_and_looped(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
    state, log = _seed_open_tabs(tmp_path, monkeypatch)
    with state.suspend_slots_push():
        restored = asyncio.run(restore_open_slots_async(state))
    _assert_budget_kept(state, log, restored)


def test_unreadable_loop_store_applies_no_budget(tmp_path, monkeypatch):
    """Which tabs a loop drives is unknown, so no tab is left unbuilt."""
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
    monkeypatch.setattr(slot_retention, "RESTORE_SLOT_BUDGET", 2)
    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.RESTORE_SLOT_BUDGET", 2)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-tab" for i in range(1, 5)]
    for key in keys:
        seed.conversation_log.append(_history_key_for(key), "user", "hello")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))
    (tmp_path / "autonudge.json").write_text("{not json")
    state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state) == 4


# ── Idle sweep ──

_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _FakeNudge:
    def __init__(self, looped: str) -> None:
        self._loops = [MagicMock(active=True, slot_key=looped)]
        self.remove_by_slot = AsyncMock(return_value=None)

    def arm(self, slot_key: str) -> None:
        self._loops.append(MagicMock(active=True, slot_key=slot_key))

    def list_all(self):
        return list(self._loops)


def _sweep_state(tmp_path, monkeypatch, *, fill_to: int):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    nudge = _FakeNudge("looped")
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: nudge)
    state = _make_state(tmp_path)
    old = (_NOW - timedelta(days=30)).isoformat()
    fresh = (_NOW - timedelta(hours=1)).isoformat()
    for name in ("idle", "pinned", "looped", "running", "fresh", "app"):
        slot = state.get_or_create_slot(name)
        slot.append("user", "msg", ts=fresh if name == "fresh" else old)
        slot.drain()
    state._slots["pinned"].pinned = True
    state._slots["app"]._app = "issue-radar"
    running = state._slots["running"]
    never_done = asyncio.get_running_loop().create_future()
    running.task = never_done
    for i in range(fill_to - len(state._slots)):
        filler = state.get_or_create_slot(f"filler-{i}")
        filler.append("user", "msg", ts=fresh)
        filler.drain()
    return state, never_done


@pytest.mark.asyncio
async def test_sweep_archives_only_the_idle_slot_above_high_water(tmp_path, monkeypatch):
    state, never_done = _sweep_state(tmp_path, monkeypatch, fill_to=slot_retention.SWEEP_HIGH_WATER)
    archived = await slot_retention.sweep_idle_slots(state, 7, now=_NOW.timestamp())
    never_done.cancel()
    assert archived == ["idle"]
    assert "idle" not in state._slots
    for kept in ("pinned", "looped", "running", "fresh", "app"):
        assert kept in state._slots
    meta = state.conversation_log.get_metadata(_history_key_for("idle"))
    assert meta.get("closed") is True


@pytest.mark.asyncio
async def test_sweep_does_nothing_below_high_water(tmp_path, monkeypatch):
    state, never_done = _sweep_state(
        tmp_path, monkeypatch, fill_to=slot_retention.SWEEP_HIGH_WATER - 1
    )
    archived = await slot_retention.sweep_idle_slots(state, 7, now=_NOW.timestamp())
    never_done.cancel()
    assert archived == []
    assert "idle" in state._slots


@pytest.mark.asyncio
async def test_sweep_does_nothing_without_the_nudge_service(tmp_path, monkeypatch):
    state, never_done = _sweep_state(tmp_path, monkeypatch, fill_to=slot_retention.SWEEP_HIGH_WATER)
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
    archived = await slot_retention.sweep_idle_slots(state, 7, now=_NOW.timestamp())
    never_done.cancel()
    assert archived == []
    assert "idle" in state._slots


@pytest.mark.asyncio
async def test_sweep_keeps_a_slot_that_turns_active_before_the_pop(tmp_path, monkeypatch):
    """The pre-pop re-check aborts the close when the slot got pinned mid-close."""
    state, never_done = _sweep_state(tmp_path, monkeypatch, fill_to=slot_retention.SWEEP_HIGH_WATER)
    from kiro_crew.dashboard import chat_handlers

    real_close = chat_handlers.close_slot

    async def _pin_then_close(state_, slot, name, *, pre_pop_check=None):
        slot.pinned = True
        await real_close(state_, slot, name, pre_pop_check=pre_pop_check)

    monkeypatch.setattr(chat_handlers, "close_slot", _pin_then_close)
    archived = await slot_retention.sweep_idle_slots(state, 7, now=_NOW.timestamp())
    never_done.cancel()
    assert archived == []
    assert "idle" in state._slots
    assert not state._slots["idle"].is_closing


@pytest.mark.asyncio
async def test_sweep_keeps_a_slot_whose_loop_was_armed_after_selection(tmp_path, monkeypatch):
    """A loop armed between the selection and the close is seen before close_slot retires it."""
    import kiro_crew.autonudge as autonudge

    state, never_done = _sweep_state(tmp_path, monkeypatch, fill_to=slot_retention.SWEEP_HIGH_WATER)
    nudge = autonudge.get_instance()
    real_select = slot_retention.select_idle_slot_keys

    def _select_then_arm(*args, **kwargs):
        picked = real_select(*args, **kwargs)
        nudge.arm("idle")
        return picked

    monkeypatch.setattr(slot_retention, "select_idle_slot_keys", _select_then_arm)
    archived = await slot_retention.sweep_idle_slots(state, 7, now=_NOW.timestamp())
    never_done.cancel()
    assert archived == []
    assert "idle" in state._slots
    nudge.remove_by_slot.assert_not_awaited()


def test_unreadable_pin_read_past_the_budget_keeps_the_reopen_seed(tmp_path, monkeypatch):
    """Past the budget an unreadable tab is carried as unrestored, not dropped as unpinned."""
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.RESTORE_SLOT_BUDGET", 1)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = _make_state(tmp_path / "sessions")
    log = seed.conversation_log
    for i, key in enumerate(("chat-1-new", "chat-2-old")):
        log.append(_history_key_for(key), "user", "hello")
        mtime = 1_700_000_000 - i
        os.utime(log._path(_history_key_for(key)), (mtime, mtime))
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-new", "chat-2-old"], "ts": 0.0})
    )
    state = _make_state(tmp_path / "sessions")
    real_status = state.conversation_log.get_metadata_status

    def _status(history_key):
        if history_key == _history_key_for("chat-2-old"):
            return {}, False
        return real_status(history_key)

    monkeypatch.setattr(state.conversation_log, "get_metadata_status", _status)
    assert restore_open_slots(state) == 1
    assert "chat-1-new" in state._slots
    assert "chat-2-old" in state.unrestored_slot_keys


def test_sweep_help_text_names_the_high_water_mark():
    from kiro_crew.config.sections import DashboardConfig

    field = DashboardConfig.__dataclass_fields__["idle_slot_sweep_days"]
    help_text = str(field.metadata)
    assert f"{slot_retention.SWEEP_HIGH_WATER} or more sessions" in help_text


@pytest.mark.asyncio
async def test_sweep_keeps_a_slot_whose_loop_is_paused(tmp_path, monkeypatch):
    """A paused loop exempts its tab: a resume could land while close_slot removes it."""
    import kiro_crew.autonudge as autonudge

    state, never_done = _sweep_state(tmp_path, monkeypatch, fill_to=slot_retention.SWEEP_HIGH_WATER)
    nudge = autonudge.get_instance()
    nudge._loops.append(MagicMock(active=False, slot_key="idle"))
    archived = await slot_retention.sweep_idle_slots(state, 7, now=_NOW.timestamp())
    never_done.cancel()
    assert archived == []
    assert "idle" in state._slots
    nudge.remove_by_slot.assert_not_awaited()


@pytest.mark.parametrize("driver", ["sync", "async"])
def test_remote_only_tab_past_the_budget_keeps_the_reopen_seed(tmp_path, monkeypatch, driver):
    """After a remote authority restore a tab with no local transcript stays in the seed."""
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: None)
    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.RESTORE_SLOT_BUDGET", 1)
    monkeypatch.setenv(ENV_AUTHORITY_RESTORED, "1")
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = _make_state(tmp_path / "sessions")
    seed.conversation_log.append(_history_key_for("chat-1-local"), "user", "hello")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-local", "chat-2-remote"], "ts": 0.0})
    )
    state = _make_state(tmp_path / "sessions")
    if driver == "sync":
        restored = restore_open_slots(state)
    else:
        restored = asyncio.run(restore_open_slots_async(state))
    assert restored == 1
    assert "chat-1-local" in state._slots
    assert "chat-2-remote" in state.unrestored_slot_keys

"""``dashboard.warm_task_brief_away_minutes``: how long away before a return brief.

The focus-frame tests write the key to this test's own data home, load it with
the real loader and prime the live watcher with the result, as gateway boot
does, so they also prove the key round-trips through ``config.json``. The
autouse ``_drop_live_config_snapshot`` fixture drops the primed snapshot.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.dashboard import warm_task_brief


class _State:
    def __init__(self) -> None:
        self._foreground_slots: dict[int, str] = {}
        self._warm_task_brief_last_seen: dict[str, float] = {}
        self._warm_task_brief_not_before: dict[str, float] = {}
        self._warm_task_brief_tasks: dict[str, asyncio.Task] = {}
        self._warm_task_brief_opened: set[str] = set()
        self.slots: dict[str, object] = {}

    def get_slot(self, key: str):
        return self.slots.get(key)


class _Slot:
    key = "thread-x"
    mode = "member"
    running = False


def _thread_left(minutes_ago: float) -> tuple[_State, _Slot]:
    """A crewmate thread the owner last saw on screen *minutes_ago*."""
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._warm_task_brief_last_seen[slot.key] = time.time() - minutes_ago * 60
    return state, slot


def _write(section: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dashboard": section}), encoding="utf-8")


def _boot_with(section: dict) -> None:
    """Write *section* and adopt the loaded config on the live watcher, as boot does."""
    _write(section)
    live.watch().prime(KiroCrewConfig.load())


@pytest.fixture
def prepared(monkeypatch) -> list[str]:
    """Record each brief that starts instead of preparing it."""
    started: list[str] = []

    async def fake_prepare(state, slot, seen_at):
        started.append(slot.key)

    monkeypatch.setattr(warm_task_brief, "_prepare_warm_task_brief", fake_prepare)
    return started


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    """The wall clock ``warm_task_brief`` reads, moved by the test."""
    now = [time.time()]
    fake = SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic)
    monkeypatch.setattr(warm_task_brief, "time", fake)
    return now


@pytest.fixture
def skips(monkeypatch) -> list[str]:
    reasons: list[str] = []
    monkeypatch.setattr(warm_task_brief, "_skip", lambda key, reason: reasons.append(reason))
    return reasons


async def _short_return(monkeypatch, state: _State, slot: _Slot, anchor: float) -> None:
    """Run the durable away check once for a return whose away anchor is *anchor*."""
    snapshot = SimpleNamespace(
        previous_claim="old", fingerprint="new", away_anchor=lambda seen_at: anchor
    )

    async def fake_snapshot(state, slot, why=None):
        return snapshot

    monkeypatch.setattr(warm_task_brief, "_snapshot", fake_snapshot)
    state._foreground_slots[9] = slot.key
    await warm_task_brief._run_warm_task_brief(state, slot, ExitStack())
    del state._foreground_slots[9]


def test_the_key_defaults_to_thirty_minutes():
    assert KiroCrewConfig().dashboard.warm_task_brief_away_minutes == 30


@pytest.mark.parametrize("stored,loaded", [(10, 30), (45, 45), (99999, 1440)])
def test_a_stored_value_clamps_to_the_rfc_floor_and_the_ceiling(stored, loaded):
    _write({"warm_task_brief_away_minutes": stored})
    assert KiroCrewConfig.load().dashboard.warm_task_brief_away_minutes == loaded


def test_default_still_skips_a_twenty_minute_absence(prepared):
    _boot_with({})
    state, slot = _thread_left(20)

    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert state._warm_task_brief_tasks == {}
    assert prepared == []


def test_a_value_below_the_floor_still_skips_a_twenty_minute_absence(skips, prepared):
    _boot_with({"warm_task_brief_away_minutes": 10})
    snapshot = live.snapshot()
    assert snapshot is not None
    assert snapshot.dashboard.warm_task_brief_away_minutes == 30
    state, slot = _thread_left(20)

    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert skips == ["short_away"]
    assert state._warm_task_brief_tasks == {}
    assert prepared == []


def test_a_higher_away_time_skips_a_forty_minute_absence(prepared):
    _boot_with({"warm_task_brief_away_minutes": 60})
    state, slot = _thread_left(40)

    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert state._warm_task_brief_tasks == {}
    assert prepared == []


@pytest.mark.asyncio
async def test_a_higher_away_time_briefs_a_longer_absence(prepared):
    _boot_with({"warm_task_brief_away_minutes": 60})
    state, slot = _thread_left(90)

    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    await asyncio.wait_for(state._warm_task_brief_tasks[slot.key], timeout=30)

    assert prepared == [slot.key]


@pytest.mark.asyncio
async def test_a_lowered_away_time_applies_past_a_cached_short_return(
    monkeypatch, clock, skips, prepared
):
    # Under 60 minutes, a return 40 minutes after the anchor is short and cached.
    _boot_with({"warm_task_brief_away_minutes": 60})
    state, slot = _thread_left(0)
    anchor = clock[0] - 40 * 60
    await _short_return(monkeypatch, state, slot, anchor)
    assert skips == ["short_away"]
    assert slot.key in state._warm_task_brief_not_before

    # Lowered to 30, the next return must not wait out the old 60.
    _boot_with({"warm_task_brief_away_minutes": 30})
    state._warm_task_brief_last_seen[slot.key] = anchor
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    await asyncio.wait_for(state._warm_task_brief_tasks[slot.key], timeout=30)

    assert prepared == [slot.key]


@pytest.mark.asyncio
async def test_a_raised_away_time_applies_past_a_cached_short_return(
    monkeypatch, clock, skips, prepared
):
    # Under 30 minutes, a return 20 minutes after the anchor is short and cached.
    _boot_with({"warm_task_brief_away_minutes": 30})
    state, slot = _thread_left(0)
    anchor = clock[0] - 20 * 60
    await _short_return(monkeypatch, state, slot, anchor)
    assert skips == ["short_away"]

    # Raised to 60, a return 40 minutes after the anchor is still short. With no
    # on-screen record (as after a restart), only the cached anchor can skip it.
    _boot_with({"warm_task_brief_away_minutes": 60})
    clock[0] += 20 * 60
    state._warm_task_brief_last_seen.pop(slot.key, None)
    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert skips == ["short_away", "short_away"]
    assert state._warm_task_brief_tasks == {}
    assert prepared == []

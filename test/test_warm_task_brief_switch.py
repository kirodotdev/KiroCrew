"""``dashboard.warm_task_brief``: the owner's off switch for the return brief.

The focus-frame tests write the key to this test's own data home, load it with
the real loader and prime the live watcher with the result, as gateway boot
does, so they also prove the key round-trips through ``config.json``. The
autouse ``_drop_live_config_snapshot`` fixture drops the primed snapshot.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard.handlers import api_kirocrew_config_patch
from kiro_crew.testing.wait import await_until


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


def _away_thread() -> tuple[_State, _Slot]:
    """A crewmate thread the owner last saw on screen two hours ago."""
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._warm_task_brief_last_seen[slot.key] = time.time() - 2 * 3600
    return state, slot


def _write_dashboard(section: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dashboard": section}), encoding="utf-8")


def _boot_with(section: dict) -> None:
    """Write *section* and adopt the loaded config on the live watcher, as boot does."""
    _write_dashboard(section)
    live.watch().prime(KiroCrewConfig.load())


@pytest.fixture
def prepared(monkeypatch) -> list[str]:
    """Record each brief that starts instead of preparing it."""
    started: list[str] = []

    async def fake_prepare(state, slot, seen_at):
        started.append(slot.key)

    monkeypatch.setattr(warm_task_brief, "_prepare_warm_task_brief", fake_prepare)
    return started


def test_the_key_defaults_on_and_reads_off():
    assert KiroCrewConfig().dashboard.warm_task_brief is True
    _write_dashboard({"warm_task_brief": False})
    assert KiroCrewConfig.load().dashboard.warm_task_brief is False


@pytest.mark.asyncio
async def test_turned_off_a_qualifying_return_schedules_no_brief(prepared):
    _boot_with({"warm_task_brief": False})
    state, slot = _away_thread()
    left_at = state._warm_task_brief_last_seen[slot.key]

    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert state._warm_task_brief_tasks == {}
    assert prepared == []
    # The on-screen bookkeeping still runs: the thread is stamped as seen again.
    assert state._warm_task_brief_last_seen[slot.key] > left_at


@pytest.mark.asyncio
async def test_turned_off_leaving_a_thread_still_cancels_its_unshown_brief(prepared):
    _boot_with({"warm_task_brief": False})
    state, slot = _away_thread()
    state._foreground_slots[1] = slot.key
    pending = asyncio.get_running_loop().create_future()
    state._warm_task_brief_tasks[slot.key] = pending  # type: ignore[assignment]

    warm_task_brief.handle_focus_frame(state, 1, "other-thread")

    assert pending.cancelled()


@pytest.mark.asyncio
async def test_with_the_key_absent_a_qualifying_return_still_schedules(prepared):
    _boot_with({})
    state, slot = _away_thread()

    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    await await_until(state._warm_task_brief_tasks[slot.key], what="the scheduled brief")

    assert prepared == [slot.key]


@pytest.mark.asyncio
async def test_with_no_live_snapshot_a_qualifying_return_still_schedules(prepared):
    _write_dashboard({"warm_task_brief": False})
    state, slot = _away_thread()

    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    await await_until(state._warm_task_brief_tasks[slot.key], what="the scheduled brief")

    assert prepared == [slot.key]


@web.middleware
async def _owner(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


@pytest.mark.asyncio
async def test_the_settings_patch_stores_the_key_and_refuses_a_non_bool():
    _write_dashboard({})
    app = web.Application(middlewares=[_owner])
    app["state"] = MagicMock(owner_id="")
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    body = {"path": "dashboard.warm_task_brief", "value": False}
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/config/kirocrew", json=body)
        assert resp.status == 200, await resp.text()
        refused = await client.patch("/api/config/kirocrew", json={**body, "value": "no"})
        assert refused.status == 400
    stored = json.loads(config_path().read_text(encoding="utf-8"))
    assert stored["dashboard"]["warm_task_brief"] is False
    assert KiroCrewConfig.load().dashboard.warm_task_brief is False

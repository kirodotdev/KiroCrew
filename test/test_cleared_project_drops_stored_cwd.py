"""Clearing a chat's project must stop its next resume binding the directory the user removed.

A resume that states no cwd restores the one stored in the session map, so the clear drops it
there. Driven through the real ``SessionManager`` allocator, with only the provider faked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.session import SessionManager
from kiro_crew.session_map import SessionMap
from kiro_crew.testing.wait import await_until

KEY = "dashboard:test"


def _provider_factory(tmp_path, bound: list):
    from kiro_crew.acp.types import ACP_BACKEND_KIRO
    from kiro_crew.providers.acp import AcpProvider

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = object.__new__(AcpProvider)
        provider._client = MagicMock()
        provider._client._session_id = "old-sid"
        provider._client._work_dir = kwargs.get("cwd") or str(tmp_path / "session-default")
        provider._client.cwd = provider._client._work_dir
        bound.append(provider._client.cwd)
        provider._client._pid = None
        provider._client.backend = ACP_BACKEND_KIRO
        provider._client.resumed = True
        provider._client.set_resume_session_id = MagicMock()
        provider._history_replay_needed = False
        provider._defer_replay_sid_promotion = False
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        return provider

    return factory


@pytest.fixture
def resumable(tmp_path, monkeypatch):
    """A session map entry for ``KEY`` that resumes ``old-sid`` in ``old-project``."""
    native = tmp_path / "native-sessions"
    native.mkdir()
    (native / "old-sid.json").write_text("{}", encoding="utf-8")
    (native / "old-sid.jsonl").write_text('{"role":"user","content":"x"}\n', encoding="utf-8")
    monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: native)
    old = tmp_path / "old-project"
    old.mkdir()
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    bound: list = []
    factory = _provider_factory(tmp_path, bound)
    mgr = SessionManager(cfg, provider_factory=factory)
    mgr._session_map.set(KEY, "old-sid", cwd=str(old))
    return mgr, str(old), bound, lambda: SessionManager(cfg, provider_factory=factory)


def _dashboard_state(mgr, slot):
    from kiro_crew.dashboard.state import DashboardState

    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.sessions = mgr
    state.file_indexes = MagicMock()
    state.file_indexes.acquire = AsyncMock()
    state.file_indexes.release = AsyncMock()
    return state


async def _clear_over_http(state, expect_status=200):
    from aiohttp import web

    from kiro_crew.dashboard.chat import api_chat_slot_project

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/project", json={"project": ""})
        assert resp.status == expect_status
        return await resp.json()


async def _clear_by_directive(state, slot):
    from kiro_crew.dashboard.session_directive_apply import _set_project

    return await _set_project(state, slot, {"clear": True})


def test_clear_cwd_keeps_the_resume_sid_and_saves_only_on_a_drop():
    smap = SessionMap.__new__(SessionMap)
    SessionMap.__init__(smap)
    smap._save = MagicMock()
    smap._data = {KEY: {"sid": "old-sid", "cwd": "/old"}}

    smap.clear_cwd(KEY)
    smap.clear_cwd(KEY)
    smap.clear_cwd("dashboard:absent")

    assert smap._data[KEY] == {"sid": "old-sid"}
    assert smap._save.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", ["http", "directive"])
async def test_a_cleared_chat_does_not_resume_in_the_removed_directory(resumable, clear):
    mgr, old, bound, _ = resumable
    slot = _ChatSlot("test")
    slot.project = old
    state = _dashboard_state(mgr, slot)

    if clear == "http":
        await _clear_over_http(state)
    else:
        await _clear_by_directive(state, slot)
    await mgr.get_or_create(KEY, cwd=slot.project or None)

    assert bound[-1] != old
    assert mgr._session_map.get(KEY) == "old-sid", "the clear must keep the conversation"
    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", ["http", "directive"])
async def test_the_clear_is_on_disk_when_it_is_acknowledged(resumable, clear, monkeypatch):
    """The debounced write is held off, so only a write the clear itself awaited can land."""
    mgr, old, _, _ = resumable
    await mgr.aflush()
    monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
    slot = _ChatSlot("test")
    slot.project = old
    state = _dashboard_state(mgr, slot)

    if clear == "http":
        await _clear_over_http(state)
    else:
        assert not (await _clear_by_directive(state, slot)).startswith("Error")

    assert SessionMap().get_cwd(KEY) == ""
    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", ["http", "directive"])
async def test_a_clear_that_cannot_be_saved_is_not_acknowledged(resumable, clear):
    mgr, old, _, _ = resumable
    mgr.aflush = AsyncMock(side_effect=OSError("disk full"))
    slot = _ChatSlot("test")
    slot.project = old
    state = _dashboard_state(mgr, slot)

    if clear == "http":
        body = await _clear_over_http(state, expect_status=500)
        assert body["code"] == "project_clear_not_saved"
    else:
        assert (await _clear_by_directive(state, slot)).startswith("Error")
    del mgr.aflush
    await mgr.close_all()


async def _clear(state, slot, clear, expect_ok):
    if clear == "http":
        body = await _clear_over_http(state, expect_status=200 if expect_ok else 500)
        return body.get("ok") is True
    return not (await _clear_by_directive(state, slot)).startswith("Error")


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", ["http", "directive"])
async def test_a_retried_clear_is_not_acknowledged_until_it_is_saved(resumable, clear, monkeypatch):
    """The retry finds the project already empty, so it must still wait for the owed write."""
    mgr, old, _, _ = resumable
    monkeypatch.setattr("kiro_crew.session_map._FLUSH_DEBOUNCE_SECS", 60.0)
    smap = mgr._session_map
    real_write = smap._write_payload
    smap._write_payload = MagicMock(side_effect=OSError("disk full"))
    slot = _ChatSlot("test")
    slot.project = old
    state = _dashboard_state(mgr, slot)

    assert not await _clear(state, slot, clear, expect_ok=False)
    assert not await _clear(state, slot, clear, expect_ok=False)
    assert SessionMap().get_cwd(KEY) == old

    smap._write_payload = real_write
    assert await _clear(state, slot, clear, expect_ok=True)
    assert SessionMap().get_cwd(KEY) == ""
    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", ["http", "directive"])
async def test_a_clear_after_a_failed_map_load_drops_the_cwd_from_the_file(resumable, clear):
    """The unread file still holds the cwd; a clear that skipped it would save it back."""
    _, old, _, fresh = resumable
    mgr = fresh()
    mgr._session_map._data = {}
    mgr._session_map._load_failed = True
    slot = _ChatSlot("test")
    slot.project = old
    state = _dashboard_state(mgr, slot)

    assert await _clear(state, slot, clear, expect_ok=True)

    assert SessionMap().get_cwd(KEY) == ""
    assert SessionMap().get(KEY) == "old-sid"
    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear", ["http", "directive"])
async def test_a_clear_is_refused_while_the_map_stays_unreadable(resumable, clear, monkeypatch):
    _, old, _, fresh = resumable
    mgr = fresh()
    mgr._session_map._data = {}
    mgr._session_map._load_failed = True
    monkeypatch.setattr(
        "kiro_crew.session_map_store._read_map_file", MagicMock(side_effect=OSError("io"))
    )
    slot = _ChatSlot("test")
    slot.project = old
    state = _dashboard_state(mgr, slot)

    assert not await _clear(state, slot, clear, expect_ok=False)

    assert slot.project == old
    assert SessionMap().get_cwd(KEY) == old


@pytest.mark.asyncio
async def test_a_clear_for_a_slot_replaced_while_the_map_was_prepared_is_not_found(resumable):
    """The chat closed and resumed under the same name during the await; the clear is not its."""
    mgr, old, _, _ = resumable
    slot = _ChatSlot("test")
    slot.project = old
    state = _dashboard_state(mgr, slot)
    replacement = _ChatSlot("test")
    replacement.project = old
    real_prepare = mgr.aprepare_cwd_clear

    async def prepare_while_resumed():
        await real_prepare()
        state._slots["test"] = replacement

    mgr.aprepare_cwd_clear = prepare_while_resumed

    await _clear_over_http(state, expect_status=404)

    assert replacement.project == old
    assert slot.project == old
    assert mgr._session_map.get_cwd(KEY) == old
    del mgr.aprepare_cwd_clear
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_reset_successor_of_a_cleared_session_does_not_inherit_the_removed_directory(
    resumable,
):
    """Reset and compaction successors rebuild from ``allocation_identity`` of the old session."""
    from kiro_crew.session_lifecycle_parts.parked_queue import allocation_identity

    mgr, old, bound, _ = resumable
    await mgr.get_or_create(KEY, cwd=old)
    predecessor = mgr._sessions[KEY]
    mgr.retract_cwd(KEY)
    identity = allocation_identity(mgr, KEY, predecessor)
    await mgr.reset(KEY)

    await mgr.get_or_create(KEY, **identity)

    assert bound[-1] != old
    assert mgr._session_map.get_cwd(KEY) != old
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_claude_sid_backfill_does_not_store_a_cleared_sessions_directory(
    resumable, monkeypatch
):
    mgr, old, _, _ = resumable
    monkeypatch.setattr("kiro_crew.session._is_claude_provider", lambda provider: True)
    await mgr.get_or_create(KEY, cwd=old)
    mgr.release(KEY)
    mgr.retract_cwd(KEY)
    mgr._session_map._data.pop(KEY)

    await mgr.get_or_create(KEY)
    mgr.release(KEY)

    assert mgr._session_map.get(KEY) == "old-sid", "the backfill must have run"
    assert mgr._session_map.get_cwd(KEY) == ""
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_directive_clear_reads_the_project_after_preparing_the_map(resumable):
    """A project set while the clear waits on the map is the one the clear must drop."""
    mgr, old, _, _ = resumable
    slot = _ChatSlot("test")
    slot.project = ""
    state = _dashboard_state(mgr, slot)
    real_prepare = mgr.aprepare_cwd_clear

    async def prepare_while_a_project_is_set():
        await real_prepare()
        slot.project = old

    mgr.aprepare_cwd_clear = prepare_while_a_project_is_set

    assert not (await _clear_by_directive(state, slot)).startswith("Error")

    assert slot.project == ""
    assert mgr._session_map.get_cwd(KEY) == ""
    del mgr.aprepare_cwd_clear
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_cleared_chats_reply_thread_never_resumes_a_stored_cwd(resumable):
    """A thread key is stateless, so the ``cwd`` a stale map row carries for it is never read."""
    from kiro_crew.dashboard.chat_threads import thread_session_key

    mgr, old, bound, _ = resumable
    slot = _ChatSlot("test")
    slot.project = old
    thread_key = thread_session_key(slot.key, "m1")
    mgr._session_map._data[thread_key] = {"sid": "old-sid", "cwd": old}
    await _clear_by_directive(_dashboard_state(mgr, slot), slot)

    await mgr.get_or_create(thread_key, cwd=slot.project or None)

    assert bound[-1] != old
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_replay_commit_after_the_clear_does_not_store_the_removed_directory(resumable):
    mgr, old, bound, _ = resumable
    await mgr.get_or_create(KEY, cwd=old)
    mgr.release(KEY)
    mgr._sessions[KEY].provider_switch_replay = True
    mgr.retract_cwd(KEY)

    assert mgr.commit_provider_switch_replay_sid(KEY)

    assert mgr._session_map.get_cwd(KEY) == ""
    assert mgr._session_map.get(KEY) == "old-sid"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_clear_survives_a_restart(resumable):
    mgr, old, bound, restart = resumable
    slot = _ChatSlot("test")
    slot.project = old
    await _clear_by_directive(_dashboard_state(mgr, slot), slot)
    await mgr.close_all()

    mgr = restart()
    await mgr.get_or_create(KEY, cwd=None)

    assert bound[-1] != old
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_clear_survives_a_restart_while_the_old_session_is_still_live(resumable):
    """The deferred reset has not landed, so shutdown would write the live session's cwd back."""
    mgr, old, bound, restart = resumable
    slot = _ChatSlot("test")
    slot.project = old
    await mgr.get_or_create(KEY, cwd=old)
    mgr.release(KEY)
    await _clear_by_directive(_dashboard_state(mgr, slot), slot)
    await mgr.close_all()

    mgr = restart()
    await mgr.get_or_create(KEY, cwd=None)

    assert bound[-1] != old
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_cold_start_paused_across_the_clear_does_not_survive_a_restart(resumable):
    """The start read the stored cwd before the clear and registers after it."""
    import asyncio

    mgr, old, bound, restart = resumable
    entered, resume = asyncio.Event(), asyncio.Event()

    async def paused_start():
        entered.set()
        await resume.wait()

    factory = mgr._provider_factory

    def pausing_factory(*args, **kwargs):
        provider = factory(*args, **kwargs)
        provider.start = paused_start
        return provider

    mgr._provider_factory = pausing_factory
    slot = _ChatSlot("test")
    slot.project = old
    start = asyncio.create_task(mgr.get_or_create(KEY, cwd=None))
    await await_until(entered, what="the cold start to reach provider start")
    assert bound[-1] == old

    await _clear_by_directive(_dashboard_state(mgr, slot), slot)
    resume.set()
    await await_until(start, what="the paused cold start to register")
    mgr.release(KEY)

    assert mgr._session_map.get_cwd(KEY) == ""
    assert not mgr._allocation_boundary().state.cwd_retracted_reservations
    await mgr.close_all()
    mgr = restart()
    await mgr.get_or_create(KEY, cwd=None)

    assert bound[-1] != old
    assert mgr._session_map.get(KEY) == "old-sid"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_reservation_taken_after_the_clear_keeps_its_own_cwd(resumable, tmp_path):
    mgr, old, bound, _ = resumable
    mgr.retract_cwd(KEY)
    chosen = tmp_path / "chosen"
    chosen.mkdir()

    await mgr.get_or_create(KEY, cwd=str(chosen))
    mgr.release(KEY)

    assert mgr._session_map.get_cwd(KEY) == str(chosen)
    assert not mgr._sessions[KEY].cwd_retracted
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_pending_reset_drops_a_cwd_stored_after_the_clear(tmp_path):
    """A cold start already in flight at the clear registers with the old directory."""
    from chat_runner_coverage_helpers import _slot, _state

    from kiro_crew.dashboard import chat_runner

    state, slot = _state(tmp_path), _slot("test")
    state._slots[slot.key] = slot
    state.sessions.reset = AsyncMock(return_value=True)
    slot._pending_reset_history_key = KEY
    slot._pending_reset_clears_cwd = True

    await chat_runner._consume_pending_reset(state, slot)

    state.sessions.retract_cwd.assert_called_once_with(KEY)
    state.sessions.reset.assert_awaited_once()
    assert slot._pending_reset_clears_cwd is False


@pytest.mark.asyncio
async def test_a_model_only_reset_on_a_chat_with_no_project_keeps_the_stored_cwd(tmp_path):
    """A model switch arms the same pending reset, but no project was cleared."""
    from chat_runner_coverage_helpers import _slot, _state

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.session_control_parts.model_switch import arm_model_switch_reset

    state, slot = _state(tmp_path), _slot("test")
    state._slots[slot.key] = slot
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.get_provider = MagicMock(return_value=object())
    arm_model_switch_reset(state, slot)
    assert slot._pending_reset_history_key

    await chat_runner._consume_pending_reset(state, slot)

    state.sessions.retract_cwd.assert_not_called()
    state.sessions.reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_pending_reset_keeps_the_cwd_of_a_chosen_project(resumable, tmp_path):
    from chat_runner_coverage_helpers import _slot, _state

    from kiro_crew.dashboard import chat_runner

    mgr, old, bound, _ = resumable
    await mgr.get_or_create(KEY, cwd=None)
    mgr.release(KEY)
    assert bound == [old]
    state, slot = _state(tmp_path), _slot("test")
    state.sessions = mgr
    state._slots[slot.key] = slot
    slot.project = old
    slot._pending_reset_history_key = KEY
    slot._pending_reset_clears_cwd = True

    try:
        assert await chat_runner._consume_pending_reset(state, slot) is True

        assert not mgr.has_session(KEY)
        assert mgr._session_map.get_cwd(KEY) == old
    finally:
        await mgr.close_all()

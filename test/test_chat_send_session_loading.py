"""``POST /api/chat`` during the startup restore: a not-yet-built tab is refused.

A send naming an open tab the restore has not built must not mint a fresh slot under
its key (it would lose the saved agent). A genuinely new chat is still admitted.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_app_with_agent_routes, _make_state


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()

    async def run_chat(_st, slot, _message, **_kwargs):
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat)
    return st


async def _post(st, slot: str):
    async with TestClient(TestServer(_make_app(st))) as client:
        resp = await client.post("/api/chat?ws=1", json={"slot": slot, "message": "hi"})
        body = await resp.json()
        live = st.get_slot(slot)
        if resp.status == 200 and live is not None and live.task is not None:
            await live.task
        return resp.status, body


@pytest.mark.asyncio
async def test_unbuilt_open_tab_is_refused_while_restoring(state):
    state.restoring_open_slots = True
    state.restoring_open_slot_keys = frozenset({"chat-1-100"})

    status, body = await _post(state, "chat-1-100")

    assert (status, body["code"]) == (409, "session_loading")
    assert "chat-1-100" not in state._slots


@pytest.mark.asyncio
async def test_new_chat_is_admitted_while_restoring(state):
    state.restoring_open_slots = True
    state.restoring_open_slot_keys = frozenset({"chat-1-100"})

    status, _ = await _post(state, "brand-new-chat")

    assert status == 200


@pytest.mark.asyncio
async def test_built_tab_is_admitted_while_restoring(state):
    state.get_or_create_slot("chat-1-100")
    state.restoring_open_slots = True
    state.restoring_open_slot_keys = frozenset({"chat-1-100"})

    status, _ = await _post(state, "chat-1-100")

    assert status == 200


@pytest.mark.asyncio
async def test_open_tab_is_admitted_once_restore_ends(state):
    state.restoring_open_slot_keys = frozenset()

    status, _ = await _post(state, "chat-1-100")

    assert status == 200


async def _waiting(st, key):
    """Start the guard for *key* and return it once it is parked on the event."""
    task = asyncio.ensure_future(st.session_still_loading(key))
    for _ in range(50):
        await asyncio.sleep(0)
    assert not task.done()
    return task


def _publish(st, keys):
    st.restoring_open_slot_keys = frozenset(keys)
    st.owed_slot_keys_known.set()


@pytest.mark.asyncio
async def test_a_named_key_before_the_snapshot_waits_then_is_refused_when_owed(state):
    state.begin_slot_list_rebuild()
    task = await _waiting(state, "chat-1-100")

    _publish(state, {"chat-1-100"})

    assert await task is True


@pytest.mark.asyncio
async def test_a_named_key_before_the_snapshot_waits_then_is_admitted_when_not_owed(state):
    state.begin_slot_list_rebuild()
    task = await _waiting(state, "chat-9-900")

    _publish(state, {"chat-1-100"})

    assert await task is False


@pytest.mark.asyncio
async def test_a_named_send_after_the_wait_gets_session_loading_for_an_owed_key(state):
    state.begin_slot_list_rebuild()
    _publish(state, {"chat-1-100"})

    status, body = await _post(state, "chat-1-100")

    assert (status, body["code"]) == (409, "session_loading")
    assert "chat-1-100" not in state._slots


@pytest.mark.asyncio
async def test_a_new_chat_before_the_snapshot_is_admitted_without_waiting(state, monkeypatch):
    state.begin_slot_list_rebuild()
    waited = AsyncMock(side_effect=AssertionError("a new chat must not wait"))
    monkeypatch.setattr(state.owed_slot_keys_known, "wait", waited)
    async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
        resp = await client.post("/api/chat/slots", json={})

    assert resp.status == 200
    assert waited.await_count == 0


@pytest.mark.asyncio
async def test_a_named_send_is_refused_when_the_wait_runs_out(state, monkeypatch):
    # The bound running out, without a real sleep: the bounded wait raises its timeout.
    # The plan is still unknown then, so the name may be a saved tab: refused, no mint.
    state.begin_slot_list_rebuild()
    expired = AsyncMock(side_effect=asyncio.TimeoutError)
    monkeypatch.setattr(state.owed_slot_keys_known, "wait", expired)

    status, body = await _post(state, "chat-1-100")

    assert (status, body["code"]) == (409, "session_loading")
    assert expired.await_count == 1
    assert "chat-1-100" not in state._slots


def test_the_owed_keys_wait_ends_before_the_dashboard_aborts_the_send():
    import re
    from pathlib import Path

    from kiro_crew.dashboard.state_parts.slot_directory import OWED_KEYS_WAIT_SECONDS

    ts = Path(__file__).resolve().parents[1] / "website/src/chat-core/transport/sendTurn.ts"
    match = re.search(r"export const SEND_ABORT_MS = ([0-9_]+)", ts.read_text(encoding="utf-8"))
    assert match, "SEND_ABORT_MS not found in sendTurn.ts"
    send_abort_ms = int(match.group(1).replace("_", ""))

    # Well below, so the 409 or the admit reaches the client before it gives up.
    assert OWED_KEYS_WAIT_SECONDS * 1000 <= send_abort_ms / 2


@pytest.mark.asyncio
async def test_an_app_caller_naming_an_owed_key_on_openai_compat_gets_no_session_loading(state):
    from aiohttp import web

    from kiro_crew.dashboard.openai_compat import api_completions

    state.restoring_open_slot_keys = frozenset({"chat-1-100"})

    @web.middleware
    async def as_app(request, handler):
        request["app"] = "some-app"
        return await handler(request)

    app = web.Application(middlewares=[as_app])
    app["state"] = state
    app.router.add_post("/v1/chat/completions", api_completions)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "id": "chat-1-100",
                "model": "default",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        body = await resp.json()

    assert resp.status in (403, 404), body
    assert body.get("code") != "session_loading"
    assert "chat-1-100" not in state._slots


@pytest.mark.asyncio
async def test_an_app_that_may_not_claim_an_owed_tab_gets_the_unknown_key_answer(
    state, monkeypatch
):
    from kiro_crew.dashboard.chat_api import app_admission

    state.restoring_open_slot_keys = frozenset({"chat-1-100"})
    refused = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._app_claim_refused", refused)
    request = MagicMock()
    request.get = lambda key, default=None: "some-app" if key == "app" else default

    answer = await app_admission._loading_refusal(request, state, "chat-1-100")

    assert answer.status == 404
    assert refused.await_count == 1


@pytest.mark.asyncio
async def test_restore_publishes_its_keys_before_the_plan_is_built(state, monkeypatch):
    import kiro_crew.dashboard.chat_persistence as rd

    seen = []

    def plan(_log, keys):
        seen.append(state.restoring_open_slot_keys)
        return [], None

    monkeypatch.setattr(rd, "_read_open_slots_snapshot", lambda: (["chat-1-100"], True))
    monkeypatch.setattr(rd, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(rd, "_open_slot_restore_plan", plan)
    state.conversation_log = MagicMock()

    await rd.restore_open_slots_async(state)

    assert seen == [frozenset({"chat-1-100"})]
    assert state.owed_slot_keys_known.is_set()
    # Kept: the recent-sessions pass can still rebuild an owed key (session_restore clears it).
    assert state.restoring_open_slot_keys == frozenset({"chat-1-100"})


@pytest.mark.asyncio
async def test_a_new_chat_under_construction_is_not_refused(state):
    state.restoring_open_slots = True
    state.restoring_open_slot_keys = frozenset({"chat-1-100"})
    state._slots_under_construction.add("brand-new-chat")

    assert await state.session_still_loading("brand-new-chat") is False
    assert await state.session_still_loading("chat-1-100") is True


@pytest.mark.asyncio
async def test_an_owed_key_under_construction_is_refused(state):
    state.get_or_create_slot("chat-1-100")
    state.restoring_open_slot_keys = frozenset({"chat-1-100"})
    state._slots_under_construction.add("chat-1-100")

    assert await state.session_still_loading("chat-1-100") is True


@pytest.mark.asyncio
async def test_slot_create_refuses_an_owed_key(state):
    state.restoring_open_slot_keys = frozenset({"chat-1-100"})
    async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
        resp = await client.post("/api/chat/slots", json={"name": "chat-1-100"})
        body = await resp.json()

    assert (resp.status, body.get("code")) == (409, "session_loading"), body
    assert "chat-1-100" not in state._slots


@pytest.mark.asyncio
async def test_openai_compat_refuses_an_owed_key(state):
    from aiohttp import web

    from kiro_crew.dashboard.openai_compat import api_completions

    state.restoring_open_slot_keys = frozenset({"chat-1-100"})
    app = web.Application()
    app["state"] = state
    app.router.add_post("/v1/chat/completions", api_completions)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "id": "chat-1-100",
                "model": "default",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        body = await resp.json()

    assert (resp.status, body.get("code")) == (409, "session_loading"), body
    assert "chat-1-100" not in state._slots

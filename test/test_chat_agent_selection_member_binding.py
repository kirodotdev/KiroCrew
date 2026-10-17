"""The memory a crewmate pick binds on a new, pre-warmed or restricted chat.

A crewmate with private memory picked on a new chat binds its private store over a
pre-warm, a V1 pick keeps its pre-warmed session, and a pre-warm is released only
from a chat that is provably empty. An incognito or temporary member chat keeps its
transcript but writes no owner record, and survives a restart. A record-less
``template`` stamp never binds a same-named crewmate's private store or model pin.
The shared turn state and waits live in ``chat_agent_selection_helpers.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_agent_selection_helpers import (
    _finished,
    _turn_state,
)
from chat_test_helpers import _make_app_with_agent_routes, _make_state, drain_background_tasks
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.execution_context import read_session_execution
from kiro_crew.member_memory_auth import read_private_session_store
from kiro_crew.memory_stores import persist_member_config, provision_member_memory
from kiro_crew.session_agent_selection import resolve_session_agent_bindings


def _prewarmed_member_state(tmp_path, monkeypatch, *, sid: str | None):
    """An empty new chat whose pre-warm left ``sid`` behind, plus a member.

    The eager pre-warm allocates while the chat is still on the default agent,
    and the switch handler's own reset preserves the persistence entry, so what
    survives into the pin is a resume pointer with no live provider.
    """
    cfg = KiroCrewConfig.load()
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, "reviewer")
    persist_member_config(cfg, "reviewer", create=True)
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    holder = {"sid": sid}
    state.sessions.resumable_sid = MagicMock(side_effect=lambda _key: holder["sid"])

    def _forget(_key):
        dropped, holder["sid"] = holder["sid"], None
        return dropped

    state.sessions.forget_conversation = MagicMock(side_effect=_forget)
    return state, store


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [None, "acp-prewarm-sid"])
async def test_new_chat_member_pick_binds_private_memory_over_a_prewarm(tmp_path, monkeypatch, sid):
    """ "+ New conversation" then picking a member must bind its private store.

    Regression test. A pre-warm's surviving resume pointer is not V1 context:
    the chat has never been sent a message, so the pick is granted and the turn
    runs on the member's private store instead of reporting no verified private
    assignment while the member's name is displayed. Both parametrizations
    assert the same outcome, so the pre-warm is what varies and the binding is
    what does not.
    """
    state, store = await asyncio.to_thread(_prewarmed_member_state, tmp_path, monkeypatch, sid=sid)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post("/api/chat/slots", json={"name": "new"}), "the request's response"
        )
        assert response.status == 200, await response.text()
        slot = state._slots["new"]
        key = f"dashboard:{slot.key}"
        assert await asyncio.to_thread(read_private_session_store, key) is None
        response = await _finished(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer"}),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    assert slot.agent == "reviewer"
    assert slot.memory_store == store
    assert await asyncio.to_thread(read_private_session_store, key) == store
    # Nothing can resume the default agent's pre-warmed process into the
    # member's private store: the pointer is dropped, not merely ignored.
    assert state.sessions.resumable_sid(key) is None
    assert state.sessions.forget_conversation.called is bool(sid)


@pytest.mark.asyncio
async def test_new_chat_v1_pick_keeps_its_prewarmed_session(tmp_path, monkeypatch):
    """A V1 pick has nothing to grant and must keep its resumable session."""
    state, _ = await asyncio.to_thread(
        _prewarmed_member_state, tmp_path, monkeypatch, sid="acp-prewarm-sid"
    )
    cfg = KiroCrewConfig.load()
    cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post("/api/chat/slots", json={"name": "v1"}), "the request's response"
        )
        assert response.status == 200, await response.text()
        slot = state._slots["v1"]
        response = await _finished(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": "plain"}),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    state.sessions.forget_conversation.assert_not_called()
    assert state.sessions.resumable_sid(f"dashboard:{slot.key}") == "acp-prewarm-sid"


@pytest.mark.asyncio
@pytest.mark.parametrize("obstacle", ["transcript_rows", "unreadable_transcript", "live_provider"])
async def test_prewarmed_session_is_kept_when_the_chat_is_not_provably_empty(
    tmp_path, monkeypatch, obstacle
):
    """Only a provably empty chat may lose its pre-warm; each check fails closed."""
    from kiro_crew.dashboard.chat_persistence import release_prewarmed_session

    state, _ = await asyncio.to_thread(
        _prewarmed_member_state, tmp_path, monkeypatch, sid="acp-prewarm-sid"
    )
    key = "dashboard:not-empty"
    if obstacle == "transcript_rows":
        await asyncio.to_thread(state.conversation_log.append, key, "user", "earlier V1 turn")
    elif obstacle == "unreadable_transcript":
        state.conversation_log.has_messages = MagicMock(side_effect=OSError("unreadable"))
    else:
        state.sessions.get_provider = MagicMock(return_value=MagicMock())
    released = await _finished(
        release_prewarmed_session(state, key, "reviewer", KiroCrewConfig.load()),
        "the pre-warm release",
    )
    assert released is False
    state.sessions.forget_conversation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_member_session_persists_transcript_but_no_owner_record(
    tmp_path, monkeypatch, mode
):
    """A restricted member chat keeps its transcript and survives a restart.

    The line records the restricted mode and the member's name, and NO memory
    store: with no execution carrier written for a restricted session, a store
    name would read back as a legacy owner claim and the restart would refuse
    the chat as a member record with no identity. Left out, the restart reads
    the session as unbound and the next turn re-selects the member from
    ``agent`` under the retained mode -- the same live-only carrier the session
    ran under before the restart.
    """
    from kiro_crew.dashboard.chat_persistence import restore_recent_sessions
    from kiro_crew.execution_context import _LIVE_EXECUTIONS, _live_key
    from kiro_crew.history import ConversationLog

    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    member_store = provision_member_memory(cfg, "writer")
    cfg.save()
    state = _turn_state(tmp_path, monkeypatch)
    # One log for the slot save and the execution reads, as in the gateway.
    state.conversation_log = ConversationLog()
    slot = state.get_or_create_slot("restricted", agent="default", memory_mode=mode)
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        response = await client.post("/api/chat/slots/restricted/agent", json={"agent": "writer"})
        assert response.status == 200, await response.text()
    key = "dashboard:restricted"
    captured = read_session_execution(key)
    assert captured.memory_mode == mode
    assert captured.store.store_id == member_store
    slot.append("user", "restricted body sentinel")
    await _finished(chat_runner._run_chat(state, slot, "restricted body sentinel"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")
    state.sessions.get_or_create.assert_awaited_once()
    if mode == "temporary":
        state.context_builder.ensure_store.assert_not_awaited()

    body = state.conversation_log._path(key).read_text(encoding="utf-8")
    assert "restricted body sentinel" in body
    meta = state.conversation_log.get_metadata(key)
    assert meta.get("memory_mode") == mode
    assert meta.get("agent") == "writer"
    assert meta.get("agent_kind") == "member"
    assert "memory_store" not in meta
    assert "execution_context" not in meta

    # Restart: the live carrier is gone and only the record remains.
    _LIVE_EXECUTIONS.pop(_live_key(key), None)
    assert read_session_execution(key) is None
    restarted = _turn_state(tmp_path, monkeypatch)
    restarted.conversation_log = ConversationLog()
    assert restore_recent_sessions(restarted, window_minutes=0) >= 1
    restored = restarted._slots["restricted"]
    assert restored.memory_mode == mode
    assert key in restarted._restricted_keys
    assert [m["content"] for m in restored.messages][:1] == ["restricted body sentinel"]
    restarted.sessions.reset = AsyncMock(return_value=True)
    await _finished(chat_runner._run_chat(restarted, restored, "second sentinel"), "the chat turn")
    await _finished(drain_background_tasks(restarted), "the background tasks draining")
    rebound = read_session_execution(key)
    assert rebound is not None
    assert rebound.memory_mode == mode
    assert rebound.store.store_id == member_store
    assert "memory_store" not in restarted.conversation_log.get_metadata(key)


def test_a_record_less_template_stamp_never_resolves_a_same_named_crewmate(monkeypatch):
    """A slot stamped ``template`` whose name a crewmate also uses, read before
    any execution record exists (a non-owner create leaves the first send to
    resolve from the stamp alone): the stamp decides the namespace, so the
    crewmate's private store and pin are never bound to a chat that did not
    pick it. A ``member`` stamp picks the crewmate; no stamp keeps the
    name-first order the record-less read always had."""
    from kiro_crew import session_agent_selection
    from kiro_crew.config.loader import resolve_agent_bindings
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig()
    cfg.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "atlas": KiroCrewAgentConfig(kiro_agent="kirocrew", model="pinned"),
    }
    cfg.default_agent = "default"
    private = provision_member_memory(cfg, "atlas")
    monkeypatch.setattr(session_agent_selection, "read_session_execution", lambda _: None)
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name == "atlas" else "",
    )

    stamped = resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, "dashboard:app-chat", "atlas", agent_kind="template"
    )
    assert stamped.selection_kind == "template"
    assert stamped.resolved_alias == ""
    assert stamped.memory_store_name == "default"
    assert stamped.model == ""
    assert stamped.kiro_agent == "atlas"

    member = resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, "dashboard:app-chat", "atlas", agent_kind="member"
    )
    assert member.resolved_alias == "atlas"
    assert member.memory_store_name == private
    assert member.model == "pinned"

    bare = resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, "dashboard:app-chat", "atlas"
    )
    assert bare.resolved_alias == "atlas"

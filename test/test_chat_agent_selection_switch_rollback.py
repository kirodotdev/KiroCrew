"""Agent switches that fail, are cancelled or rebound, or come from a non-owner.

A failed history write or a failed or cancelled agent lookup leaves the slot, its
recorded selection and its history as they were. A cancelled switch drains its
running write and every rollback write before its cancellation completes: the chat
reopens on its template, a later owner pick is never overwritten, and a failed
rollback still leaves an empty chat resumable. A non-owner may pick a template, a V1
crewmate or the default, but a private member is refused before anything changes,
and a failed first private grant restores the protected selection. The shared
template chat, turn state and waits live in ``chat_agent_selection_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_agent_selection_helpers import (
    TEMPLATE,
    _finished,
    _signalled,
    _template_chat,
    _turn_state,
)
from chat_test_helpers import _make_app_with_agent_routes, drain_background_tasks
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.chat_persistence import (
    _apply_recent_session,
    _rehydrate_slot_from_history,
    rehydrate_slot_from_history_async,
)
from kiro_crew.execution_context import read_session_execution
from kiro_crew.member_memory_auth import read_private_session_store
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.session_agent_selection import record_agent_selection, session_agent_selection_kind
from kiro_crew.testing.wait import await_until, default_timeout


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["open", "recent", "resume"])
async def test_failed_agent_history_write_keeps_prior_selection(tmp_path, monkeypatch, restore):
    """A history lock failure cannot publish an agent that restart cannot use."""
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    protected = read_session_execution(key).to_record()
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    publish = MagicMock(wraps=record_agent_selection)
    monkeypatch.setattr(chat_handlers, "record_agent_selection", publish)
    monkeypatch.setattr(
        state.conversation_log, "_locked", MagicMock(side_effect=TimeoutError("history lock"))
    )
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post("/api/chat/slots/template-chat/agent", json={"agent": later_agent}),
            "the request's response",
        )
        assert response.status == 503, await response.text()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert slot._dirty
    publish.assert_not_called()
    assert read_session_execution(key).to_record() == protected

    # A fresh dashboard and ConversationLog read the durable state, not the
    # rolled-back slot or its metadata cache.
    restored = _turn_state(tmp_path, monkeypatch)
    if restore == "open":
        slot = _rehydrate_slot_from_history(restored, "template-chat")
    elif restore == "recent":
        _apply_recent_session(
            restored,
            key,
            "template-chat",
            {},
            restored.conversation_log.get_metadata(key),
            restored.conversation_log._read_messages(key),
            conv_log=restored.conversation_log,
            kiro_model_map={},
            restore_cfg=KiroCrewConfig.load(),
        )
        slot = restored._slots["template-chat"]
    else:
        async with TestClient(
            TestServer(as_owner(_make_app_with_agent_routes(restored)))
        ) as client:
            response = await _finished(
                client.post("/api/chat/slots/template-chat/resume", json={"key": key}),
                "the request's response",
            )
            assert response.status == 200, await response.text()
        slot = restored._slots["template-chat"]
    assert slot is not None
    assert slot.agent == TEMPLATE
    await _finished(chat_runner._run_chat(restored, slot, "Continue the task."), "the chat turn")
    await _finished(drain_background_tasks(restored), "the background tasks draining")
    restored.sessions.record_failure.assert_not_awaited()
    restored.sessions.get_or_create.assert_awaited_once()
    assert not any(row["role"] == "error" for row in slot.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lookup_error", "unresolved"])
async def test_failed_agent_lookup_preserves_existing_selection(tmp_path, monkeypatch, failure):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    with monkeypatch.context() as patch:
        if failure == "lookup_error":
            patch.setattr(
                chat_handlers,
                "resolve_agent_bindings",
                MagicMock(side_effect=OSError("agent lookup unavailable")),
            )
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await _finished(
                client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": "missing-template"}
                ),
                "the request's response",
            )
            assert response.status == 503, await response.text()
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata
    state.sessions.get_or_create.reset_mock()
    await _finished(chat_runner._run_chat(state, slot, "Continue."), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_agent_lookup_preserves_existing_selection(tmp_path, monkeypatch):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    body = json.dumps({"agent": "kirocrew-worker"}).encode()
    payload = MagicMock()

    async def chunks(_size):
        yield body

    payload.iter_chunked = chunks
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot.key}/agent",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        match_info={"slot": slot.key},
        app=_make_app_with_agent_routes(state),
        payload=payload,
    )
    request["user"] = "local-app"
    request["app"] = ""
    with monkeypatch.context() as patch:
        patch.setattr(
            chat_handlers, "warm_project_agent_names", AsyncMock(side_effect=asyncio.CancelledError)
        )
        with pytest.raises(asyncio.CancelledError):
            await _finished(chat_handlers.api_chat_slot_agent(request), "the agent switch handler")
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata
    state.sessions.get_or_create.reset_mock()
    await _finished(chat_runner._run_chat(state, slot, "Continue."), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_agent_switch_replaces_protected_selection(tmp_path, monkeypatch):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": ""}),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    state.sessions.get_or_create.reset_mock()
    await _finished(chat_runner._run_chat(state, slot, "Continue."), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()
    restored = _turn_state(tmp_path, monkeypatch)
    restored_slot = _rehydrate_slot_from_history(restored, slot.key)
    assert restored_slot is not None
    await _finished(chat_runner._run_chat(restored, restored_slot, "Continue."), "the chat turn")
    await _finished(drain_background_tasks(restored), "the background tasks draining")
    restored.sessions.record_failure.assert_not_awaited()
    restored.sessions.get_or_create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["template", "legacy_member", "default"])
async def test_non_owner_switch_keeps_durable_selection(tmp_path, monkeypatch, selection):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    cfg = KiroCrewConfig.load()
    default_agent = cfg.default_agent
    cfg.agents["legacy-worker"] = replace(cfg.agents[default_agent])
    cfg.save()
    later_agent = {
        "template": "kirocrew-worker",
        "legacy_member": "legacy-worker",
        "default": "",
    }[selection]
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post(
                f"/api/chat/slots/{slot.key}/agent",
                json={"agent": later_agent},
                headers={"X-Test-User": "allowed-chat-user"},
            ),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    state.sessions.get_or_create.reset_mock()
    await _finished(chat_runner._run_chat(state, slot, "Continue."), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()
    restored = _turn_state(tmp_path, monkeypatch)
    restored_slot = _rehydrate_slot_from_history(restored, slot.key)
    # An empty switch is the default TEMPLATE, stamped like an agent-less
    # create -- never the default crewmate alias.
    assert restored_slot is not None and restored_slot.agent == (later_agent or "kirocrew")
    await _finished(chat_runner._run_chat(restored, restored_slot, "Continue."), "the chat turn")
    await _finished(drain_background_tasks(restored), "the background tasks draining")
    restored.sessions.record_failure.assert_not_awaited()
    restored.sessions.get_or_create.assert_awaited_once()
    assert not any(row["role"] == "error" for row in restored_slot.messages)
    if selection == "legacy_member":
        # A permitted V1 choice is not authority to admit private memory after
        # that member is upgraded. Exercise the live slot, not only a restore.
        cfg = KiroCrewConfig.load()
        provision_member_memory(cfg, later_agent)
        cfg.save()
        state.sessions.get_or_create.reset_mock()
        await _finished(chat_runner._run_chat(state, slot, "Continue."), "the chat turn")
        await _finished(drain_background_tasks(state), "the background tasks draining")
        state.sessions.get_or_create.assert_awaited_once()
        state.sessions.record_failure.assert_not_awaited()
        assert read_session_execution("dashboard:template-chat").store.store_id == "default"
        assert read_private_session_store("dashboard:template-chat") is None


@pytest.mark.asyncio
async def test_non_owner_member_upgraded_during_lookup_is_refused(tmp_path, monkeypatch):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    cfg = KiroCrewConfig.load()
    cfg.agents["legacy-worker"] = replace(cfg.agents[cfg.default_agent])
    cfg.save()
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    calls = 0

    def resolve_then_upgrade(config, name, *args, **kwargs):
        nonlocal calls
        result = resolve_agent_bindings(config, name, *args, **kwargs)
        calls += 1
        if calls == 1:
            upgraded = KiroCrewConfig.load()
            provision_member_memory(upgraded, "legacy-worker")
            upgraded.save()
        return result

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve_then_upgrade)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post(
                f"/api/chat/slots/{slot.key}/agent",
                json={"agent": "legacy-worker"},
                headers={"X-Test-User": "allowed-chat-user"},
            ),
            "the request's response",
        )
        assert response.status == 403, await response.text()
    assert calls == 2
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata


@pytest.mark.asyncio
async def test_non_owner_member_switch_is_refused_before_mutation(tmp_path, monkeypatch):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post(
                f"/api/chat/slots/{slot.key}/agent",
                json={"agent": TEMPLATE},
                headers={"X-Test-User": "allowed-chat-user"},
            ),
            "the request's response",
        )
        assert response.status == 403, await response.text()
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("phase", ["write", "rebound_restore"])
async def test_cancelled_agent_history_write_drains_before_restore(
    tmp_path, monkeypatch, cancel_count, phase
):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch), "the template chat setup"
    )
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    protected = read_session_execution(key).to_record()
    started, release, finished = (threading.Event() for _ in range(3))
    restore_started, release_restore, restore_finished = (threading.Event() for _ in range(3))
    update_metadata = state.conversation_log.update_metadata
    writes = []

    def gated_write(history_key, fields):
        if fields.get("agent") == later_agent:
            try:
                started.set()
                assert release.wait(default_timeout()), "Agent metadata writer was not released"
                update_metadata(history_key, fields)
                writes.append(later_agent)
            finally:
                finished.set()
        else:
            try:
                if phase == "rebound_restore":
                    restore_started.set()
                    assert release_restore.wait(
                        default_timeout()
                    ), "Agent metadata rollback was not released"
                update_metadata(history_key, fields)
                writes.append(fields.get("agent"))
            finally:
                restore_finished.set()

    monkeypatch.setattr(state.conversation_log, "update_metadata", gated_write)
    body = json.dumps({"agent": later_agent}).encode()
    payload = MagicMock()

    async def chunks(_size):
        yield body

    payload.iter_chunked = chunks
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot.key}/agent",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        match_info={"slot": slot.key},
        app=_make_app_with_agent_routes(state),
        payload=payload,
    )
    request["user"] = "local-app"
    request["app"] = ""
    task = asyncio.create_task(chat_handlers.api_chat_slot_agent(request))
    try:
        await _signalled(started, "the gated writer starting")
        if phase == "rebound_restore":
            slot.linked_session_key = "task:rebound"
            release.set()
            await _signalled(restore_started, "the restore starting")
        for _ in range(cancel_count):
            assert task.cancel()
            # Cross the scheduled cancellation boundary with the writer held.
            delivered = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(delivered.set_result, None)
            await await_until(delivered, what="the loop turn")
            assert not task.done()
            assert slot._lock.locked()
        release.set()
        release_restore.set()
        result = await await_until(
            asyncio.gather(task, return_exceptions=True), what="the tasks settling"
        )
        assert isinstance(result[0], asyncio.CancelledError)
        assert finished.is_set()
        assert restore_finished.is_set()
        assert writes == [later_agent, TEMPLATE]
        assert slot.agent == TEMPLATE
        assert read_session_execution(key).to_record() == protected
        restored = _turn_state(tmp_path, monkeypatch)
        restored_slot = _rehydrate_slot_from_history(restored, "template-chat")
        assert restored_slot is not None and restored_slot.agent == TEMPLATE
        await _finished(
            chat_runner._run_chat(restored, restored_slot, "Continue."), "the chat turn"
        )
        await _finished(drain_background_tasks(restored), "the background tasks draining")
        restored.sessions.record_failure.assert_not_awaited()
        restored.sessions.get_or_create.assert_awaited_once()
    finally:
        release.set()
        release_restore.set()
        if not task.done():
            task.cancel()
        await await_until(asyncio.gather(task, return_exceptions=True), what="the tasks settling")
        if started.is_set():
            await _signalled(finished, "the gated writer finishing")
        if restore_started.is_set():
            await _signalled(restore_finished, "the restore finishing")
        await _finished(drain_background_tasks(state), "the background tasks draining")


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["open", "recent", "resume", "async"])
async def test_cancelled_agent_switch_with_failed_history_restore(tmp_path, monkeypatch, restore):
    """A failed rollback must not leave an empty conversation unresumable."""
    from kiro_crew.history import HistoryLockTimeout

    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch, first_turn=False), "the template chat setup"
    )
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    started, release, finished = (threading.Event() for _ in range(3))
    update_metadata = state.conversation_log.update_metadata

    def fail_restore(history_key, fields):
        if fields.get("agent") != later_agent:
            raise HistoryLockTimeout("history rollback lock")
        try:
            started.set()
            assert release.wait(default_timeout()), "Agent metadata writer was not released"
            update_metadata(history_key, fields)
        finally:
            finished.set()

    monkeypatch.setattr(state.conversation_log, "update_metadata", fail_restore)
    body = json.dumps({"agent": later_agent}).encode()
    payload = MagicMock()

    async def chunks(_size):
        yield body

    payload.iter_chunked = chunks
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot.key}/agent",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        match_info={"slot": slot.key},
        app=_make_app_with_agent_routes(state),
        payload=payload,
    )
    request["user"] = "local-app"
    request["app"] = ""
    task = asyncio.create_task(chat_handlers.api_chat_slot_agent(request))
    try:
        await _signalled(started, "the gated writer starting")
        assert task.cancel()
        delivered = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(delivered.set_result, None)
        await await_until(delivered, what="the loop turn")
        release.set()
        result = await await_until(
            asyncio.gather(task, return_exceptions=True), what="the tasks settling"
        )
        assert isinstance(result[0], asyncio.CancelledError)
        assert finished.is_set()
        assert not slot.messages
        await _finished(drain_background_tasks(state), "the background tasks draining")

        restored = _turn_state(tmp_path, monkeypatch)
        if restore == "open":
            restored_slot = _rehydrate_slot_from_history(restored, slot.key)
        elif restore == "async":
            restored_slot = await _finished(
                rehydrate_slot_from_history_async(restored, slot.key), "the slot rehydrating"
            )
        elif restore == "recent":
            _apply_recent_session(
                restored,
                key,
                slot.key,
                {},
                restored.conversation_log.get_metadata(key),
                restored.conversation_log._read_messages(key),
                conv_log=restored.conversation_log,
                kiro_model_map={},
                restore_cfg=KiroCrewConfig.load(),
            )
            restored_slot = restored._slots[slot.key]
        else:
            async with TestClient(
                TestServer(as_owner(_make_app_with_agent_routes(restored)))
            ) as client:
                response = await _finished(
                    client.post(f"/api/chat/slots/{slot.key}/resume", json={"key": key}),
                    "the request's response",
                )
                assert response.status == 200, await response.text()
            restored_slot = restored._slots[slot.key]
        assert restored_slot is not None
        assert restored_slot.agent == TEMPLATE
        assert read_session_execution(key).to_record() == protected
        await _finished(
            chat_runner._run_chat(restored, restored_slot, "Continue."), "the chat turn"
        )
        await _finished(drain_background_tasks(restored), "the background tasks draining")
        restored.sessions.record_failure.assert_not_awaited()
        restored.sessions.get_or_create.assert_awaited_once()
        assert not any(row["role"] == "error" for row in restored_slot.messages)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await await_until(asyncio.gather(task, return_exceptions=True), what="the tasks settling")
        if started.is_set():
            await _signalled(finished, "the gated writer finishing")
        await _finished(drain_background_tasks(state), "the background tasks draining")


@pytest.mark.asyncio
@pytest.mark.parametrize("interleaving", ["rebound", "write_error", "later_pick"])
async def test_rebound_owner_switch_restores_selection_provenance(
    tmp_path, monkeypatch, interleaving
):
    # flake-ok: the setup coroutine finishes on its own; pytest-timeout bounds the test
    state, slot, _ = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    key = "dashboard:template-chat"
    state.sessions.reset = AsyncMock(return_value=True)
    to_thread = asyncio.to_thread

    async def rebind_after_selection(func, *args, **kwargs):
        if (
            func is record_agent_selection
            and kwargs.get("replace")
            and interleaving == "write_error"
        ):
            raise OSError("selection write unavailable")
        result = await to_thread(func, *args, **kwargs)
        if func is record_agent_selection and kwargs.get("replace"):
            if interleaving == "later_pick":
                await to_thread(func, *args, **kwargs)
            slot.linked_session_key = "task:rebound"
        return result

    # flake-ok: the wrapper hands every call to the saved real to_thread, and monkeypatch restores it at teardown
    monkeypatch.setattr(chat_handlers.asyncio, "to_thread", rebind_after_selection)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        response = await client.post(
            "/api/chat/slots/template-chat/agent", json={"agent": TEMPLATE}
        )
        assert response.status == (
            503 if interleaving == "write_error" else 409
        ), await response.text()
    assert session_agent_selection_kind(key, TEMPLATE) == (
        "member" if interleaving == "later_pick" else "template"
    )
    assert (read_private_session_store(key) is not None) is (interleaving == "later_pick")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2], ids=["one_cancel", "two_cancels"])
@pytest.mark.parametrize("later_choice", ["same_name_template", "different_name_template"])
async def test_cancelled_owner_writer_cannot_overwrite_later_choice(
    tmp_path, monkeypatch, later_choice, cancel_count
):
    """A cancelled owner still owns its running publication until it settles."""
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch, first_turn=False), "the template chat setup"
    )
    key = "dashboard:template-chat"
    later_agent = TEMPLATE if later_choice == "same_name_template" else "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    monkeypatch.setattr("kiro_crew.llm_helpers._slot_switch_session_locks", {})
    state.sessions.reset = AsyncMock(return_value=True)
    app = _make_app_with_agent_routes(state)
    loop_thread = threading.get_ident()
    writer_started, release_writer, writer_finished = (threading.Event() for _ in range(3))
    later_lock_attempted = asyncio.Event()
    publications = {}
    writer_threads = {}
    retired_after_drain = []
    later_task = None

    class ObservedSlotLock(asyncio.Lock):
        async def acquire(self):
            if asyncio.current_task() is later_task:
                # Observe the actual lock attempt, without requiring acquisition:
                # a corrected handler may still hold this lock while draining.
                later_lock_attempted.set()
            return await super().acquire()

    monkeypatch.setattr(slot, "_lock", ObservedSlotLock())

    def gated_publication(*args, **kwargs):
        assert kwargs.get("replace") is True
        owner = "later" if writer_started.is_set() else "cancelled"
        writer_threads[owner] = threading.get_ident()
        try:
            if owner == "cancelled":
                # The real to_thread worker has started, so cancelling its
                # asyncio waiter cannot stop this publication. Pause before the
                # real writer acquires its file lock, not inside the file lock.
                writer_started.set()
                assert release_writer.wait(
                    default_timeout()
                ), "Cancelled owner writer was not released"
            change = record_agent_selection(*args, **kwargs)
            assert change is not None
            publications[owner] = change[1]
            return change
        finally:
            if owner == "cancelled":
                writer_finished.set()

    monkeypatch.setattr(chat_handlers, "record_agent_selection", gated_publication)

    def owner_request(agent):
        body = json.dumps({"agent": agent}).encode()
        payload = MagicMock()

        async def chunks(_size):
            yield body

        payload.iter_chunked = chunks
        request = make_mocked_request(
            "POST",
            f"/api/chat/slots/{slot.key}/agent",
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            match_info={"slot": slot.key},
            app=app,
            payload=payload,
        )
        # The same authenticated bootstrap owner used by as_owner; retain the
        # real handler's authorization, JSON decoding, resolution and writes.
        request["user"] = "local-app"
        request["app"] = ""
        return request

    async def first_owner():
        try:
            return await chat_handlers.api_chat_slot_agent(owner_request(TEMPLATE))
        finally:
            retired_after_drain.append(writer_finished.is_set())

    old_task = asyncio.create_task(first_owner())

    async def cancel_blocked_owner():
        assert not release_writer.is_set()
        assert not writer_finished.is_set()
        assert old_task.cancel()
        # cancel() schedules the handler's wakeup before this callback. Cross
        # that loop boundary with the worker still gated, so the second cancel
        # is a separate delivery during the drain, not two coalesced requests.
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await await_until(checkpoint, what="the loop turn")
        assert not writer_finished.is_set()

    try:
        await _signalled(writer_started, "the gated writer starting")
        assert not writer_finished.is_set()
        assert not old_task.done()

        if later_choice == "same_name_template":
            # A already resolved the discovered member. Removing that member
            # makes B's explicit same-name choice resolve the available template.
            def remove_member():
                cfg = KiroCrewConfig.load()
                del cfg.agents[TEMPLATE]
                cfg.save()

            await _finished(asyncio.to_thread(remove_member), "remove_member off the loop")

        await cancel_blocked_owner()
        later_task = asyncio.create_task(
            chat_handlers.api_chat_slot_agent(owner_request(later_agent))
        )
        await await_until(later_lock_attempted, what="the later request's lock attempt")
        if cancel_count == 2:
            await cancel_blocked_owner()
        if old_task.done():
            # The broken handler abandoned its worker and released serialization.
            # Make the later real request commit before releasing that worker.
            # A handler retaining its lock while draining takes the other path;
            # it need not finish B while A's writer is deliberately paused.
            await await_until(later_task, what="the later request")
        release_writer.set()
        results = await await_until(
            asyncio.gather(old_task, later_task, return_exceptions=True), what="the tasks settling"
        )
        await _signalled(writer_finished, "the gated writer finishing")
        assert old_task.cancelled(), "The real owner handler must propagate cancellation"
        assert isinstance(results[0], asyncio.CancelledError)
        assert not isinstance(results[1], BaseException), results[1]
        assert results[1].status == 200, results[1].text
        assert set(writer_threads) == {"cancelled", "later"}
        assert all(thread != loop_thread for thread in writer_threads.values())
        assert publications["cancelled"]["selection_kind"] == "member"
        assert publications["later"]["selection_kind"] == "template"

        def final_identity():
            selection = read_session_execution(key).to_record()
            metadata = state.conversation_log.get_metadata(key)
            return {
                "protected_agent": selection["selection_name"],
                "protected_namespace": session_agent_selection_kind(key, later_agent),
                "protected_revision": selection["selection_revision"],
                "slot_agent": str(slot.agent),
                "history_agent": metadata["agent"],
            }

        observed = await _finished(asyncio.to_thread(final_identity), "final_identity off the loop")
        observed["cancelled_handler_retired_after_writer"] = retired_after_drain
        assert observed == {
            "protected_agent": later_agent,
            "protected_namespace": "template",
            "protected_revision": publications["later"]["selection_revision"],
            "slot_agent": later_agent,
            "history_agent": later_agent,
            "cancelled_handler_retired_after_writer": [True],
        }
    finally:
        release_writer.set()
        tasks = [task for task in (old_task, later_task) if task is not None]
        try:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await await_until(
                asyncio.gather(*tasks, return_exceptions=True), what="the tasks settling"
            )
        finally:
            if writer_started.is_set():
                await _signalled(writer_finished, "the gated writer finishing")
            await _finished(drain_background_tasks(state), "the background tasks draining")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2], ids=["one_cancel", "two_cancels"])
@pytest.mark.parametrize("cleanup_phase", ["selection", "history"])
async def test_cancelled_rebound_cleanup_finishes_before_later_owner(
    tmp_path, monkeypatch, cleanup_phase, cancel_count
):
    """Neither rollback write may outlive the handler or skip the other write."""
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch, first_turn=False), "the template chat setup"
    )
    history_key = "dashboard:template-chat"
    rebound_key = "task:rebound-cleanup"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    monkeypatch.setattr("kiro_crew.llm_helpers._slot_switch_session_locks", {})
    state.sessions.reset = AsyncMock(return_value=True)
    app = _make_app_with_agent_routes(state)
    loop_thread = threading.get_ident()
    rebound = threading.Event()
    cleanup_started, release_cleanup = threading.Event(), threading.Event()
    selection_finished, history_finished = threading.Event(), threading.Event()
    later_lock_attempted = asyncio.Event()
    cleanup_threads = {}
    retired_after_cleanup = []
    cancel_results = []
    later_task = None
    record_owner = chat_handlers._record_explicit_agent_selection
    restore_selection = chat_handlers.restore_agent_selection
    update_metadata = state.conversation_log.update_metadata

    class ObservedSlotLock(asyncio.Lock):
        async def acquire(self):
            if asyncio.current_task() is later_task:
                later_lock_attempted.set()
            return await super().acquire()

    monkeypatch.setattr(slot, "_lock", ObservedSlotLock())

    async def rebind_after_publication(*args, **kwargs):
        change = await record_owner(*args, **kwargs)
        if not rebound.is_set():
            # Exercise the real post-publication rebound branch. Its history
            # rollback still names the dashboard transcript, while B selects
            # an agent for the newly linked session.
            slot.linked_session_key = rebound_key
            rebound.set()
        return change

    def pause_cleanup(phase):
        cleanup_threads[phase] = threading.get_ident()
        if cleanup_phase == phase:
            cleanup_started.set()
            assert release_cleanup.wait(default_timeout()), "Rollback worker was not released"

    def restore_protected(key, change):
        if key != history_key or change is None:
            return restore_selection(key, change)
        try:
            pause_cleanup("selection")
            return restore_selection(key, change)
        finally:
            selection_finished.set()

    def restore_history(key, metadata):
        if key != history_key or not rebound.is_set() or metadata.get("agent") != TEMPLATE:
            return update_metadata(key, metadata)
        try:
            pause_cleanup("history")
            return update_metadata(key, metadata)
        finally:
            history_finished.set()

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", rebind_after_publication)
    monkeypatch.setattr(chat_handlers, "restore_agent_selection", restore_protected)
    monkeypatch.setattr(state.conversation_log, "update_metadata", restore_history)

    def owner_request(agent):
        body = json.dumps({"agent": agent}).encode()
        payload = MagicMock()

        async def chunks(_size):
            yield body

        payload.iter_chunked = chunks
        request = make_mocked_request(
            "POST",
            f"/api/chat/slots/{slot.key}/agent",
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            match_info={"slot": slot.key},
            app=app,
            payload=payload,
        )
        request["user"] = "local-app"
        request["app"] = ""
        return request

    async def first_owner():
        try:
            return await chat_handlers.api_chat_slot_agent(owner_request(TEMPLATE))
        finally:
            retired_after_cleanup.append(selection_finished.is_set() and history_finished.is_set())

    old_task = asyncio.create_task(first_owner())
    try:
        await _signalled(cleanup_started, "the rollback cleanup starting")
        assert rebound.is_set()
        assert not old_task.done()
        for index in range(cancel_count):
            assert not release_cleanup.is_set()
            cancel_results.append(old_task.cancel())
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await await_until(checkpoint, what="the loop turn")
            if index == 0:
                later_task = asyncio.create_task(
                    chat_handlers.api_chat_slot_agent(owner_request(later_agent))
                )
                await await_until(later_lock_attempted, what="the later request's lock attempt")
        assert cancel_results[0]
        if old_task.done():
            # The unfixed path releases the locks with cleanup still blocked.
            # Force the later metadata to land before that abandoned write.
            # A correct drain retains the locks; never wait for B here then.
            await await_until(later_task, what="the later request")
        release_cleanup.set()
        results = await await_until(
            asyncio.gather(old_task, later_task, return_exceptions=True), what="the tasks settling"
        )
        gated_finished = selection_finished if cleanup_phase == "selection" else history_finished
        await _signalled(gated_finished, "the gated rollback finishing")
        assert old_task.cancelled()
        assert isinstance(results[0], asyncio.CancelledError)
        assert not isinstance(results[1], BaseException), results[1]
        assert results[1].status == 200, results[1].text
        assert all(thread != loop_thread for thread in cleanup_threads.values())

        def final_identity():
            metadata = state.conversation_log.get_metadata(history_key)
            return {
                "original_namespace": session_agent_selection_kind(history_key, TEMPLATE),
                "later_namespace": session_agent_selection_kind(rebound_key, later_agent),
                "slot_agent": str(slot.agent),
                "history_agent": metadata["agent"],
            }

        observed = await _finished(asyncio.to_thread(final_identity), "final_identity off the loop")
        observed.update(
            protected_rollback_finished=selection_finished.is_set(),
            history_rollback_finished=history_finished.is_set(),
            handler_retired_after_full_cleanup=retired_after_cleanup,
            cancellations_accepted=cancel_results,
        )
        assert observed == {
            "original_namespace": "template",
            "later_namespace": "template",
            "slot_agent": later_agent,
            "history_agent": later_agent,
            "protected_rollback_finished": True,
            "history_rollback_finished": True,
            "handler_retired_after_full_cleanup": [True],
            "cancellations_accepted": [True] * cancel_count,
        }
    finally:
        release_cleanup.set()
        tasks = [task for task in (old_task, later_task) if task is not None]
        try:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await await_until(
                asyncio.gather(*tasks, return_exceptions=True), what="the tasks settling"
            )
        finally:
            if cleanup_started.is_set():
                gated_finished = (
                    selection_finished if cleanup_phase == "selection" else history_finished
                )
                await _signalled(gated_finished, "the gated rollback finishing")
            await _finished(drain_background_tasks(state), "the background tasks draining")


@pytest.mark.asyncio
async def test_failed_empty_chat_private_grant_restores_protected_selection(tmp_path, monkeypatch):
    """A failed first private grant must not commit the same-name member choice."""
    state, slot, private_store = await _finished(
        _template_chat(tmp_path, monkeypatch, first_turn=False), "the template chat setup"
    )
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    prior = (slot.agent, slot.memory_store, slot._memory_assignment_from_history)
    state.sessions.reset = AsyncMock(return_value=True)

    async def fail_grant(_state, session_key, agent_name, cfg):
        assert session_key == key
        assert agent_name == TEMPLATE
        assert cfg.agents[agent_name].memory_store == private_store
        assert session_agent_selection_kind(key, TEMPLATE) == "member"
        raise OSError("private grant unavailable")

    pin = AsyncMock(side_effect=fail_grant)
    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": TEMPLATE}),
            "the request's response",
        )
        assert response.status == 503, await response.text()

    pin.assert_awaited_once()
    assert (slot.agent, slot.memory_store, slot._memory_assignment_from_history) == prior
    assert read_session_execution(key).to_record() == protected
    assert session_agent_selection_kind(key, TEMPLATE) == "template"
    assert state.conversation_log.get_metadata(key)["agent"] == TEMPLATE
    assert read_private_session_store(key) is None

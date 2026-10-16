"""The template chat, turn state and bounded waits the chat agent-selection tests share.

``test_chat_agent_selection.py`` and its ``_switch_rollback`` and ``_member_binding``
siblings import these by name: ``TEMPLATE``, the discovered template a chat opens on;
``_turn_state``, a dashboard state whose turn ends after one stubbed reply;
``_template_chat``, a ``template-chat`` slot created on ``TEMPLATE`` through the real slot
route, its template then synced into a same-named crewmate with private memory; and
``_finished`` and ``_signalled``, which wait for a coroutine or a worker thread's
``threading.Event`` under the lost-run bound and fail by name. Nothing here is a fixture.
``test_chat_agent_selection.py`` re-exports ``TEMPLATE``, ``_template_chat`` and
``_turn_state`` for ``test_agent_slash_command.py``, ``test_app_command_agent_guard.py``,
``test_chat_agent_kind.py`` and ``test_pruned_crewmate_records.py``.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_state, drain_background_tasks
from dashboard_owner_helpers import as_owner

from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.handlers import agents
from kiro_crew.member_memory_auth import read_private_session_store
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

TEMPLATE = "kirocrew-conductor"


async def _finished(coro, what: str, *, timeout: float | None = None):
    """Run ``coro`` to completion under the lost-run bound, failing by name (D8).

    ``await_until`` takes a future the test already holds; a coroutine the test
    itself calls (a chat turn, a request, the background drain) is wrapped in a
    task here so a hang is a named failure instead of the runner's kill. The task
    is cancelled on every early exit -- the bound expiring, or an enclosing wait
    cancelling this one -- as ``wait_for`` did, so a hung step never outlives its
    test. A wait nested inside another ``_finished`` passes a smaller ``timeout``
    so the inner step is the one that fails by name.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await await_until(task, timeout=timeout, what=what)
    finally:
        if not task.done():
            task.cancel()


async def _signalled(event: threading.Event, what: str) -> None:
    """Wait for a ``threading.Event`` a worker thread sets, under the lost-run bound.

    ``await_until`` reads asyncio signals only; a flag a thread sets is read on
    the loop, where ``is_set`` is a plain, thread-safe read. Raises by name when
    the bound expires, so no caller needs to assert on the result.
    """
    await async_wait_until(event.is_set, describe=lambda: f"{what}: still clear")


def _turn_state(tmp_path, monkeypatch):
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.context_builder.build_message = MagicMock(return_value=("task", None))
    state.context_builder.ensure_store = AsyncMock(return_value=object())
    provider = MagicMock()

    async def stream(*args, **kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="The task is complete.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = stream
    state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    return state


async def _template_chat(tmp_path, monkeypatch, *, first_turn=True):
    cfg = KiroCrewConfig.load()
    assert TEMPLATE not in cfg.agents
    cfg.save()
    # Keep discovery and the actual sync/config/private-store publication real;
    # the installed template catalog and provider are the external boundaries.
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: TEMPLATE if name == TEMPLATE else "",
    )
    monkeypatch.setattr(
        agents,
        "list_agents",
        lambda: [
            AgentInfo(
                name=TEMPLATE,
                filename=f"{TEMPLATE}.json",
                description="",
                model="auto",
                source="builtin",
            )
        ],
    )
    state = _turn_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    app.router.add_post("/api/agents/sync", agents.api_kirocrew_agents_sync)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "template-chat", "agent": TEMPLATE}
        )
        assert response.status == 200, await response.text()
        slot = state._slots["template-chat"]
        if first_turn:
            slot.append("user", "Complete the assigned task.")
            # Half the caller's bound: this setup runs under its own _finished,
            # and the step that hung must be the one the failure names.
            inner = default_timeout() / 2
            await _finished(
                chat_runner._run_chat(state, slot, "Complete the task."),
                "the chat turn",
                timeout=inner,
            )
            await _finished(
                drain_background_tasks(state), "the background tasks draining", timeout=inner
            )
            state.sessions.record_failure.assert_not_awaited()
            assert state.sessions.get_or_create.await_count == 1

        response = await client.post("/api/agents/sync", json={})
        assert response.status == 200, await response.text()
        assert TEMPLATE in (await response.json())["synced"]
    synced = KiroCrewConfig.load()
    assert synced.agents[TEMPLATE].memory_store == "default"
    # The namespace collision requires an owner-created private member.
    private_store = provision_member_memory(synced, TEMPLATE)
    synced.save()
    assert synced.memory_stores[private_store].memory_version == 2
    assert read_private_session_store("dashboard:template-chat") is None
    return state, slot, private_store

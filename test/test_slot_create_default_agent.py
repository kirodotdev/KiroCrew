"""POST /api/chat/slots must stamp the resolved default agent on agent-less creates.

``api_chat_slot_create`` stores ``body["agent"]`` verbatim, so a create that
names no agent persisted ``""`` — dispatch still resolves the config default,
but the slot's metadata disagrees with what actually answers, and the
dashboard footer chip renders its literal ``'default'`` fallback. The
dashboard's auto-create races the agents fetch, so agent-less creates are a
common path, not an edge.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.slot_create_transaction import SlotCreateTransaction


def _stub_config(default_agent: str) -> KiroCrewConfig:
    """A real config object (all sections present) with the default pinned."""
    cfg = KiroCrewConfig()
    cfg.default_agent = default_agent
    return cfg


@pytest.fixture
def dashboard_state(tmp_path: Any) -> Any:
    return _make_state(tmp_path)


async def _create_slot(state: Any, payload: dict[str, Any]) -> None:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        assert resp.status < 300, await resp.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["default", "worker"])
@pytest.mark.parametrize("recovery_error", ["", "restore failed"])
async def test_owner_create_waits_for_memory_recovery_before_allocating(
    dashboard_state, monkeypatch, agent, recovery_error
):
    from kiro_crew import memory_startup
    from kiro_crew.session_agent_selection import session_agent_selection_name

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    startup = memory_startup.MemoryStartup.begin()
    preparation = asyncio.get_running_loop().create_future()
    dashboard_state.memory_startup_task = preparation
    entered = asyncio.Event()

    async def observe_wait(task):
        entered.set()
        await memory_startup.wait_for_memory_preparation(task)

    monkeypatch.setattr(chat_handlers, "wait_for_memory_preparation", observe_wait, raising=False)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    try:
        async with TestClient(TestServer(as_owner(app))) as client:
            request = asyncio.create_task(
                client.post("/api/chat/slots", json={"name": "startup-chat", "agent": agent})
            )
            waiting = asyncio.create_task(entered.wait())
            try:
                finished, _ = await asyncio.wait(
                    {request, waiting}, timeout=10, return_when=asyncio.FIRST_COMPLETED
                )
                assert (
                    waiting in finished
                ), "conversation creation refused before waiting for recovery"
                assert not request.done()
                assert "startup-chat" not in dashboard_state._slots
                assert session_agent_selection_name("dashboard:startup-chat") is None
                if recovery_error:
                    startup.fail(RuntimeError(recovery_error))
                else:
                    assert startup.complete()
                preparation.set_result(None)
                response = await asyncio.wait_for(request, 10)
                data = await response.json()
                if recovery_error:
                    assert response.status == 503
                    assert data["code"] == "store_unavailable"
                    assert recovery_error in data["error"]
                    assert "startup-chat" not in dashboard_state._slots
                    assert session_agent_selection_name("dashboard:startup-chat") is None
                else:
                    assert response.status == 200, data
                    slot = dashboard_state._slots["startup-chat"]
                    assert slot.agent == agent
                    assert session_agent_selection_name("dashboard:startup-chat") == agent
            finally:
                if not preparation.done():
                    preparation.set_result(None)
                request.cancel()
                waiting.cancel()
                await asyncio.gather(request, waiting, return_exceptions=True)
    finally:
        startup.stop()
        startup.release()


@pytest.mark.asyncio
async def test_agentless_create_stamps_the_resolved_default(
    dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat_handlers,
        "KiroCrewConfig",
        SimpleNamespace(load=lambda: _stub_config("sales-agent")),
    )
    await _create_slot(dashboard_state, {"name": "agentless"})
    assert (
        dashboard_state._slots["agentless"].agent == "sales-agent"
    ), "an agent-less create must record the resolved default, not ''"


@pytest.mark.asyncio
async def test_explicit_agent_is_stored_verbatim(
    dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp must not touch a caller-named agent (the verbatim-intent rule)."""
    monkeypatch.setattr(
        chat_handlers,
        "KiroCrewConfig",
        SimpleNamespace(load=lambda: _stub_config("sales-agent")),
    )
    await _create_slot(dashboard_state, {"name": "explicit", "agent": "custom-x"})
    assert dashboard_state._slots["explicit"].agent == "custom-x"


@pytest.mark.asyncio
async def test_unloadable_config_still_creates_with_empty_agent(
    dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config-load failure keeps the fail-open path: slot created, agent ''."""

    def _boom() -> Any:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=_boom))
    await _create_slot(dashboard_state, {"name": "no-config"})
    assert dashboard_state._slots["no-config"].agent == ""


# ── The same-binding relaxation on /api/chat's 409 guard ──
#
# Stamping the resolved default alias at creation means a programmatic first
# send naming the underlying kiro agent (or a sibling alias) now arrives at an
# already-bound slot. The guard allows it ONLY when every dispatch-relevant
# binding field matches; these tests pin the identity to all of them and the
# auditability of every outcome.


def _alias_config(**aliases: Any) -> KiroCrewConfig:
    """A real config with each named member bound to its own private store."""
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig()
    cfg.agents = {name: KiroCrewAgentConfig(**fields) for name, fields in aliases.items()}
    cfg.default_agent = next(iter(cfg.agents))
    for name in cfg.agents:
        if name != "default":
            provision_member_memory(cfg, name)
    return cfg


async def _post_chat(state: Any, payload: dict[str, Any]) -> Any:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(app)) as client:
        return await client.post("/api/chat?ws=1", json=payload), None


class TestSameBindingGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("change_project", [False, True])
    async def test_comparison_runs_off_loop_and_refuses_changed_selection(
        self, dashboard_state, monkeypatch, change_project
    ):
        import kiro_crew.config.loader as loader_mod

        cfg = _alias_config(default={"kiro_agent": "kirocrew"})
        monkeypatch.setattr(loader_mod, "_MATERIALIZED_AGENTS_READY", True)
        monkeypatch.setattr(loader_mod, "_MATERIALIZED_AGENTS", {"kirocrew"})
        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        slot = dashboard_state.get_or_create_slot("comparison", agent="default")
        loop = asyncio.get_running_loop()
        original = chat_handlers.resolve_agent_bindings
        calls = []

        def resolve(config, agent, project=None, **kwargs):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            calls.append((agent, project))
            result = original(config, agent, project, **kwargs)
            if change_project and len(calls) == 1:
                loop.call_soon_threadsafe(setattr, slot, "project", "/changed-project")
            return result

        monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
        app = web.Application()
        app["state"] = dashboard_state
        app.router.add_post("/api/chat", chat_handlers.api_chat)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat?ws=1", json={"message": "", "slot": slot.key, "agent": "kirocrew"}
            )
            data = await response.json()
        assert [call[0] for call in calls] == ["default", "kirocrew"]
        assert calls[0][1] == calls[1][1]
        assert response.status == (409 if change_project else 400)
        assert data["code"] == ("session_rebound" if change_project else "message_required")
        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_different_memory_store_still_409s(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Aliases sharing kiro agent + workspace but NOT memory store are
        different bindings: allowing the send would read and write the other
        alias's memory store."""
        cfg = _alias_config(
            **{
                "alias-a": {"kiro_agent": "kirocrew"},
                "alias-b": {"kiro_agent": "kirocrew"},
            }
        )
        assert cfg.agents["alias-a"].memory_store != cfg.agents["alias-b"].memory_store
        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        slot = dashboard_state.get_or_create_slot("pinned")
        slot.agent = "alias-a"
        events: list[Any] = []
        monkeypatch.setattr(
            chat_handlers,
            "_emit_agent_assignment",
            lambda key, agent, outcome="applied": events.append(outcome),
        )
        resp, _ = await _post_chat(
            dashboard_state, {"message": "hi", "slot": "pinned", "agent": "alias-b"}
        )
        assert resp.status == 409
        assert events == ["denied_mismatch"]

    @pytest.mark.asyncio
    async def test_identical_bindings_allowed_and_audited(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two names resolving to the same binding pass the guard, and the
        bypass of the 409 boundary emits its own SEL outcome."""
        import kiro_crew.config.loader as loader_mod

        cfg = _alias_config(default={"kiro_agent": "kirocrew"})
        monkeypatch.setattr(loader_mod, "_MATERIALIZED_AGENTS_READY", True)
        monkeypatch.setattr(loader_mod, "_MATERIALIZED_AGENTS", {"kirocrew"})
        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        slot = dashboard_state.get_or_create_slot("pinned2")
        slot.agent = "default"
        events: list[str] = []
        monkeypatch.setattr(
            chat_handlers,
            "_emit_agent_assignment",
            lambda key, agent, outcome="applied": events.append(outcome),
        )
        resp, _ = await _post_chat(
            dashboard_state, {"message": "hi", "slot": "pinned2", "agent": "kirocrew"}
        )
        assert resp.status == 200
        assert "allowed_same_binding" in events

    @pytest.mark.asyncio
    async def test_project_agent_slot_still_409s_on_default_alias(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slot bound to a PROJECT-scoped agent must not falsely match a
        request naming the default alias: without the slot's project scope both
        names resolve to default bindings and the guard would wave the request
        through while dispatch runs the project agent."""
        import kiro_crew.config.loader as loader_mod

        cfg = _alias_config(default={"kiro_agent": "kirocrew"})
        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        # The project declares "proj-agent"; resolution must see it ONLY when
        # the guard passes the slot's project scope through.
        monkeypatch.setattr(
            loader_mod,
            "_project_declares_agent",
            lambda name, project: name == "proj-agent" and project == "/proj",
        )

        async def _noop_warm(project: Any, **kw: Any) -> None:
            # **kw: the warm takes keyword-only SEL attribution labels
            # that this guard test does not care about.
            return None

        monkeypatch.setattr(chat_handlers, "warm_project_agent_names", _noop_warm)
        slot = dashboard_state.get_or_create_slot("proj-slot")
        slot.agent = "proj-agent"
        slot.project = "/proj"
        events: list[str] = []
        monkeypatch.setattr(
            chat_handlers,
            "_emit_agent_assignment",
            lambda key, agent, outcome="applied": events.append(outcome),
        )
        resp, _ = await _post_chat(
            dashboard_state, {"message": "hi", "slot": "proj-slot", "agent": "default"}
        )
        assert resp.status == 409
        assert events == ["denied_mismatch"]

    @pytest.mark.asyncio
    async def test_resolution_failure_fails_closed_with_distinct_outcome(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config-load failure keeps the deny (fail closed) but reports it as
        a resolution failure, not an agent mismatch, so operators triage the
        config problem instead of agent naming."""

        def _boom() -> KiroCrewConfig:
            raise OSError("config unreadable")

        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=_boom))
        slot = dashboard_state.get_or_create_slot("pinned3")
        slot.agent = "alias-a"
        events: list[str] = []
        monkeypatch.setattr(
            chat_handlers,
            "_emit_agent_assignment",
            lambda key, agent, outcome="applied": events.append(outcome),
        )
        resp, _ = await _post_chat(
            dashboard_state, {"message": "hi", "slot": "pinned3", "agent": "alias-b"}
        )
        assert resp.status == 409
        assert events == ["denied_resolution_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_slot", [False, True])
async def test_create_resolves_off_loop_without_adopting_a_concurrent_slot(
    dashboard_state, monkeypatch, replace_slot
):
    from kiro_crew.dashboard.state import _ChatSlot

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    loop = asyncio.get_running_loop()
    original = chat_handlers.resolve_agent_bindings
    calls = []
    replacement = _ChatSlot("offloop-create", agent="another-owner")

    def resolve(config, agent, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(agent)
        result = original(config, agent, **kwargs)
        if replace_slot:
            loop.call_soon_threadsafe(
                dashboard_state._slots.__setitem__, replacement.key, replacement
            )
        return result

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": replacement.key, "agent": "worker"}
        )
        data = await response.json()
    assert calls == ["worker"]
    if replace_slot:
        assert response.status == 409
        assert data["code"] == "session_rebound"
        assert dashboard_state._slots[replacement.key] is replacement
        assert replacement.agent == "another-owner"
        assert replacement.memory_store == ""
    else:
        assert response.status == 200
        assert dashboard_state._slots[replacement.key].agent == "worker"


@pytest.mark.asyncio
async def test_a_replaced_newborns_undo_keeps_the_replacements_transcript(
    dashboard_state, monkeypatch
):
    """A create whose newborn is replaced while its assignment runs undoes the
    assignment and hands the key over. The key had no transcript when the
    assignment started, but the replacement has since written its own
    metadata-only line there (a title, no messages yet), so the undo must not
    remove it as this create's stub."""
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.history import ConversationLog

    _member_create_setup(dashboard_state, monkeypatch)
    replacement = _ChatSlot("replaced-stub", agent="another-owner")
    history_key = f"dashboard:{replacement.key}"
    assert not ConversationLog().has_log(history_key)
    real_record = chat_handlers._record_explicit_agent_selection

    async def record_then_replaced(*args: Any, **kwargs: Any) -> Any:
        change = await real_record(*args, **kwargs)
        # Close-and-recreate on the same key while the assignment ran: the
        # replacement's title lands on the key's transcript.
        dashboard_state._slots[replacement.key] = replacement
        await asyncio.to_thread(
            ConversationLog().update_metadata, history_key, {"title": "Acknowledged"}
        )
        return change

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", record_then_replaced)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": replacement.key, "agent": "worker"}
        )
        data = await response.json()
    assert response.status == 409, data
    assert data["code"] == "session_rebound"
    assert dashboard_state._slots[replacement.key] is replacement
    assert ConversationLog().has_log(history_key), "the replacement's transcript was deleted"
    assert ConversationLog().get_metadata(history_key)["title"] == "Acknowledged"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "replacement", "session"])
async def test_switch_resolves_off_loop_and_refuses_rebound_slot_before_reset(
    dashboard_state, monkeypatch, change
):
    from kiro_crew.dashboard.state import _ChatSlot

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    await asyncio.to_thread(cfg.save)
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    slot = dashboard_state.get_or_create_slot("offloop-switch", agent="default")
    replacement = _ChatSlot(slot.key, agent="another-owner")
    dashboard_state.sessions.reset = AsyncMock(return_value=True)
    dashboard_state.sessions.get_provider.return_value = None
    loop = asyncio.get_running_loop()
    original = chat_handlers.resolve_agent_bindings
    calls = []

    def resolve(config, agent, project=None, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append((agent, project))
        result = original(config, agent, project, **kwargs)
        if change == "replacement":
            loop.call_soon_threadsafe(dashboard_state._slots.__setitem__, slot.key, replacement)
        elif change == "session":
            loop.call_soon_threadsafe(setattr, slot, "linked_session_key", "slack:other-session")
        return result

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots/{slot}/agent", chat_handlers.api_chat_slot_agent)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": "worker"})
        data = await response.json()
    assert len(calls) == 1
    assert calls[0][0] == "worker"
    if change == "none":
        assert response.status == 200
        assert slot.agent == "worker"
        assert slot.memory_store == cfg.agents["worker"].memory_store
        dashboard_state.sessions.reset.assert_awaited_once()
    else:
        assert response.status == 409
        assert data["code"] == "session_rebound"
        dashboard_state.sessions.reset.assert_not_awaited()
        assert slot.agent == "default"
        assert slot.memory_store == ""
        if change == "replacement":
            assert dashboard_state._slots[slot.key] is replacement
            assert replacement.agent == "another-owner"


@pytest.mark.asyncio
async def test_create_refused_by_member_identity_leaves_no_slot(dashboard_state, monkeypatch):
    """An owner create whose name resolves to neither a configured member nor
    a template visible from the slot's project is refused fail-closed by
    member identity resolution. That refusal must not leave the half-created slot
    registered, nor publish it in any broadcast.
    """
    from kiro_crew.session_agent_selection import session_agent_selection_name

    cfg = _alias_config(default={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    broadcast_keys: list[set[str]] = []
    original_push = dashboard_state.push_slots_update

    def record_push(*args: Any, **kwargs: Any) -> None:
        if not dashboard_state._slots_push_suspend:
            broadcast_keys.append(set(dashboard_state._slots))
        original_push(*args, **kwargs)

    monkeypatch.setattr(dashboard_state, "push_slots_update", record_push)
    before = set(dashboard_state._slots)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "ghost", "agent": "project-only-agent"}
        )
        data = await response.json()
    assert response.status == 503, data
    assert data["code"] == "store_unavailable"
    assert "member identity is missing or ambiguous" in data["error"]
    assert set(dashboard_state._slots) == before
    assert all("ghost" not in keys for keys in broadcast_keys), broadcast_keys
    assert session_agent_selection_name("dashboard:ghost") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "refused"), [("project-only-agent", True), ("default", False)])
async def test_owner_create_unhides_its_folder_only_once_assigned(
    dashboard_state, monkeypatch, agent, refused
):
    """Filing a newborn into a hidden folder un-hides that folder, a durable
    write. A create refused by its member assignment registers no slot, so it
    must leave the folder hidden; a create that lands still un-hides it.
    """
    cfg = _alias_config(default={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    dashboard_state._folders.append(
        {"id": "f-hidden", "name": "Parked", "order": 0, "hidden": True}
    )
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "filed", "agent": agent, "folder_id": "f-hidden"}
        )
        data = await response.json()
    hidden = await dashboard_state.read_folders(
        lambda folders: next(f for f in folders if f["id"] == "f-hidden").get("hidden")
    )
    if refused:
        assert response.status == 503, data
        assert "filed" not in dashboard_state._slots
        assert hidden is True
    else:
        assert response.status == 200, data
        assert dashboard_state._slots["filed"].folder_id == "f-hidden"
        assert hidden is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "counted"), [("project-only-agent", 0), ("default", 1)])
async def test_owner_create_counts_a_user_session_only_once_assigned(
    dashboard_state, monkeypatch, agent, counted
):
    """The user-session count has no decrement, so a create refused by its
    member assignment, whose newborn is retracted, must not count; a create
    that lands counts exactly once.
    """
    import kiro_crew.dashboard.state as state_mod

    cfg = _alias_config(default={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    increments: list[str] = []
    for module in (chat_handlers, state_mod):
        monkeypatch.setattr(
            module,
            "increment_user_session_count_off_loop",
            lambda module=module: increments.append(module.__name__),
        )
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        # Nameless, like the new-chat tab: the only create the mint counts.
        response = await client.post("/api/chat/slots", json={"agent": agent})
        data = await response.json()
    assert response.status == (200 if counted else 503), data
    assert len(increments) == counted, increments


@pytest.mark.asyncio
async def test_deferred_folder_unhide_failure_does_not_fail_a_committed_create(
    dashboard_state, monkeypatch
):
    """The deferred un-hide runs after the assignment has landed, so the create
    is committed: a folder-store write failure there is logged, not returned
    as an error for a session that exists.
    """
    cfg = _alias_config(default={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    dashboard_state._folders.append(
        {"id": "f-hidden", "name": "Parked", "order": 0, "hidden": True}
    )

    async def failing_unhide(state: Any, folder_id: str) -> bool:
        raise OSError("folders.json write failed")

    monkeypatch.setattr(chat_handlers, "_unhide_folder", failing_unhide)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "filed", "agent": "default", "folder_id": "f-hidden"}
        )
        data = await response.json()
    assert response.status == 200, data
    assert dashboard_state._slots["filed"].folder_id == "f-hidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "turn", "messages", "replaced"])
async def test_retraction_spares_a_newborn_that_is_no_longer_idle_and_its_own(
    dashboard_state, change
):
    """The refused-create retraction removes only an idle, empty newborn that is
    still the registered slot for its key. One on which a turn started, one that
    already holds messages, or one a concurrent create replaced is left alone.
    """
    slot = dashboard_state.get_or_create_slot("newborn")
    pending = asyncio.get_running_loop().create_future()
    if change == "turn":
        slot.task = pending
    elif change == "messages":
        slot.messages.append({"role": "user", "content": "hello"})
    elif change == "replaced":
        dashboard_state._slots.pop(slot.key)
        replacement = dashboard_state.get_or_create_slot("newborn")
        assert replacement is not slot
    try:
        async with SlotCreateTransaction("test") as txn:
            chat_handlers._record_newborn_reservation(txn, dashboard_state, slot)
    finally:
        pending.cancel()
    if change == "none":
        assert "newborn" not in dashboard_state._slots
    elif change == "replaced":
        assert dashboard_state._slots["newborn"] is replacement
    else:
        assert dashboard_state._slots["newborn"] is slot


@pytest.mark.asyncio
async def test_refused_create_rolls_back_the_member_pin_it_published(dashboard_state, monkeypatch):
    """A private-member pin publishes the session's execution binding before
    the rest of the assignment runs. When a later step refuses the create, the
    retraction must take that binding back too, or a metadata-only session
    survives the refused create in history.
    """
    from kiro_crew.history import ConversationLog
    from kiro_crew.memory_stores import UnknownMemoryStore
    from kiro_crew.session_agent_selection import session_agent_selection_name

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    pinned: list[str] = []
    real_pin = chat_handlers.pin_private_agent_store

    async def observed_pin(*args: Any, **kwargs: Any) -> str:
        store = await real_pin(*args, **kwargs)
        pinned.append(store)
        return store

    def refuse_bindings(*args: Any, **kwargs: Any) -> Any:
        raise UnknownMemoryStore("bindings unavailable")

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", observed_pin)
    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", refuse_bindings)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "pinned", "agent": "worker"})
        data = await response.json()
    assert response.status == 503, data
    assert pinned and pinned[0], "the member pin must have published for this test to mean anything"
    assert "pinned" not in dashboard_state._slots
    assert session_agent_selection_name("dashboard:pinned") is None
    assert not ConversationLog().has_log("dashboard:pinned")


@pytest.mark.asyncio
async def test_a_pin_refused_by_a_corrupt_record_leaves_no_slot(dashboard_state, monkeypatch):
    """Re-opening a transcript whose metadata line is corrupt: the member pin
    raises while reading it, before writing anything. The rollback must still
    retract the newborn, not take the unreadable line as a failed undo and
    reinstate a slot the caller was told was not created."""
    from kiro_crew.history import ConversationLog

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    key = "dashboard:corrupt-reopen"
    log = ConversationLog()
    log.update_metadata(key, {"title": "Kept"})
    log.append(key, "user", "hello")
    path = log._path(key)
    rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
    rows[0] = "{not json\n"
    path.write_text("".join(rows), encoding="utf-8")
    log._invalidate_cache(key)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "corrupt-reopen", "agent": "worker"}
        )
        data = await response.json()
    assert response.status == 503, data
    assert "corrupt-reopen" not in dashboard_state._slots
    assert log.has_messages(key), "the session's own transcript is untouched"


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [True, False])
async def test_a_same_name_create_waits_for_the_minting_assignment_to_settle(
    dashboard_state, monkeypatch, refused
):
    """A same-name create that arrives while the first create's assignment is
    still awaiting must not answer until that assignment settles. If it is
    refused, the newborn is retracted and the second create answers 409
    instead of 200 for a slot that is gone; if it lands, both share the slot.
    """
    from kiro_crew.memory_stores import UnknownMemoryStore

    cfg = _alias_config(default={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    entered = asyncio.Event()
    release = asyncio.Event()
    real_record = chat_handlers._record_explicit_agent_selection

    async def blocked_record(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        if refused:
            raise UnknownMemoryStore("assignment refused")
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", blocked_record)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        first = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "shared", "agent": "default"})
        )
        second: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            second = asyncio.create_task(
                client.post("/api/chat/slots", json={"name": "shared", "agent": "default"})
            )
            await asyncio.sleep(0.2)
            assert not second.done(), "the same-name create answered before the birth settled"
            release.set()
            first_response = await asyncio.wait_for(first, 10)
            first_data = await first_response.json()
            second_response = await asyncio.wait_for(second, 10)
            second_data = await second_response.json()
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (first, second) if t is not None), return_exceptions=True
            )
    if refused:
        assert first_response.status == 503, first_data
        assert second_response.status == 409, second_data
        assert second_data["code"] == "slot_create_refused"
        assert "shared" not in dashboard_state._slots
    else:
        assert first_response.status == 200, first_data
        assert second_response.status == 200, second_data
        assert second_data["key"] == first_data["key"] == "shared"
        assert "shared" in dashboard_state._slots


@pytest.mark.asyncio
async def test_a_refused_create_whose_newborn_survives_keeps_its_binding(
    dashboard_state, monkeypatch
):
    """The rollback of a refused assignment runs only for a retracted newborn.
    One that survives the refusal (here it gained a message while the
    assignment awaited) keeps the member binding it now runs under.
    """
    from kiro_crew.memory_stores import UnknownMemoryStore
    from kiro_crew.session_agent_selection import session_agent_selection_name

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""

    def refuse_after_a_message(*args: Any, **kwargs: Any) -> Any:
        dashboard_state._slots["kept"].messages.append({"role": "user", "content": "hi"})
        raise UnknownMemoryStore("bindings unavailable")

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", refuse_after_a_message)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "kept", "agent": "worker"})
        data = await response.json()
    assert response.status == 503, data
    assert "kept" in dashboard_state._slots
    assert session_agent_selection_name("dashboard:kept") == "worker"


@pytest.mark.asyncio
async def test_a_refused_same_name_acquirer_does_not_keep_the_newborn_alive(
    dashboard_state, monkeypatch
):
    """An app-token create on the same key waits for the minting create's
    assignment like any other same-name create, then answers the uniform 404,
    and the minting create's refusal still removes the newborn.
    """
    from kiro_crew.memory_stores import UnknownMemoryStore

    cfg = _alias_config(default={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_refusal(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        raise UnknownMemoryStore("assignment refused")

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", blocked_refusal)
    audit = MagicMock()
    monkeypatch.setattr(chat_handlers, "sel", lambda: audit)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        first = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "ghosted", "agent": "default"})
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            denied_task = asyncio.create_task(
                client.post(
                    "/api/chat/slots",
                    json={"name": "ghosted", "agent": "default"},
                    headers={"X-Test-App": "other-app"},
                )
            )
            await asyncio.sleep(0.1)
            release.set()
            refused = await asyncio.wait_for(first, 10)
            refused_data = await refused.json()
            denied = await asyncio.wait_for(denied_task, 10)
            assert denied.status == 404, await denied.text()
            # Audited like every sibling app-isolation denial in this handler.
            assert any(
                call.kwargs.get("caller") == "other-app"
                and call.kwargs.get("outcome") == "denied"
                and call.kwargs.get("resources") == "slot=ghosted"
                for call in audit.log_api_access.call_args_list
            ), audit.log_api_access.call_args_list
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    assert refused.status == 503, refused_data
    assert "ghosted" not in dashboard_state._slots


@pytest.mark.asyncio
async def test_a_same_name_create_waits_for_an_app_mint_that_rolls_back(
    dashboard_state, monkeypatch
):
    """An app-token create runs no member assignment, yet its newborn still
    holds the slot lock until its transaction settles. A same-name create from
    the same app that arrives while the first one persists waits for it, and
    when that persist fails it answers for no slot, never 200 for a slot the
    rollback then removes."""
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def blocked_failing_save(*args: Any, **kwargs: Any) -> None:
        calls.append("save")
        if len(calls) == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("persist failed")

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", blocked_failing_save)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    body = {"name": "app-ghost", "agent": "default", "title": "T"}
    headers = {"X-Test-App": "my-app"}
    async with TestClient(TestServer(as_owner(app))) as client:
        first = asyncio.create_task(client.post("/api/chat/slots", json=body, headers=headers))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            second = asyncio.create_task(client.post("/api/chat/slots", json=body, headers=headers))
            await asyncio.sleep(0.2)
            assert not second.done(), "the same-name create must wait for the mint to settle"
            release.set()
            failed = await asyncio.wait_for(first, 10)
            queued = await asyncio.wait_for(second, 10)
            assert failed.status == 500, await failed.text()
            assert queued.status == 404, await queued.text()
        finally:
            release.set()
            pending = [task for task in (first, second) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    assert "app-ghost" not in dashboard_state._slots


@pytest.mark.asyncio
async def test_refused_create_restores_a_legacy_binding_exactly(dashboard_state, monkeypatch):
    """A transcript that predates ``execution_context`` carries its binding in
    ``memory_store``/``memory_mode``. Re-opening it by name with a member pin
    that is later refused must leave those fields as they were, not remove
    them along with the binding the pin published."""
    from kiro_crew.history import ConversationLog
    from kiro_crew.memory_stores import UnknownMemoryStore

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    log = ConversationLog()
    log.update_metadata(
        "dashboard:legacy",
        {"title": "Old", "memory_store": "default", "memory_mode": "persistent"},
    )
    before = log.get_metadata("dashboard:legacy")
    pinned: list[str] = []
    real_pin = chat_handlers.pin_private_agent_store

    async def observed_pin(*args: Any, **kwargs: Any) -> str:
        store = await real_pin(*args, **kwargs)
        pinned.append(store)
        return store

    def refuse_bindings(*args: Any, **kwargs: Any) -> Any:
        raise UnknownMemoryStore("bindings unavailable")

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", observed_pin)
    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", refuse_bindings)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "legacy", "agent": "worker"})
        data = await response.json()
    assert response.status == 503, data
    assert pinned and pinned[0], "the member pin must have published for this test to mean anything"
    assert "legacy" not in dashboard_state._slots
    assert log.get_metadata("dashboard:legacy") == before


@pytest.mark.asyncio
async def test_a_create_that_raises_after_the_mint_leaves_no_slot(dashboard_state, monkeypatch):
    """Not only a refusal response: an exception anywhere between the mint and
    the commit rolls the create back, so no ghost slot is registered, counted
    or broadcast."""
    import kiro_crew.dashboard.state as state_mod

    cfg = _alias_config(default={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    increments: list[str] = []
    for module in (chat_handlers, state_mod):
        monkeypatch.setattr(
            module, "increment_user_session_count_off_loop", lambda: increments.append("count")
        )
    broadcast_keys: list[set[str]] = []
    original_push = dashboard_state.push_slots_update

    def record_push(*args: Any, **kwargs: Any) -> None:
        if not dashboard_state._slots_push_suspend:
            broadcast_keys.append(set(dashboard_state._slots))
        original_push(*args, **kwargs)

    async def failing_save(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("persist failed")

    monkeypatch.setattr(dashboard_state, "push_slots_update", record_push)
    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", failing_save)
    before = set(dashboard_state._slots)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        # Nameless with a title: a counted create whose persist step runs.
        response = await client.post("/api/chat/slots", json={"agent": "default", "title": "T"})
    assert response.status == 500
    assert set(dashboard_state._slots) == before
    assert all(keys <= before for keys in broadcast_keys), broadcast_keys
    assert increments == []


@pytest.mark.asyncio
async def test_a_failed_binding_rollback_keeps_the_slot_it_belongs_to(dashboard_state, monkeypatch):
    """When the assignment's undo itself fails, the rollback stops there: the
    newborn stays registered with the binding it carries, rather than being
    retracted and leaving that binding behind with no slot."""
    from kiro_crew.memory_stores import UnknownMemoryStore

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""

    def refuse_bindings(*args: Any, **kwargs: Any) -> Any:
        raise UnknownMemoryStore("bindings unavailable")

    def failing_restore(snapshot: Any) -> None:
        raise OSError("transcript write failed")

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", refuse_bindings)
    monkeypatch.setattr(chat_handlers, "restore_session_binding", failing_restore)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "bound", "agent": "worker"})
        data = await response.json()
    assert response.status == 503, data
    assert "bound" in dashboard_state._slots


@pytest.mark.asyncio
async def test_no_send_can_find_the_newborn_while_its_binding_restores(
    dashboard_state, monkeypatch
):
    """The rollback takes the newborn out of the registry before the binding
    restore suspends, so a send cannot start a turn on it inside that window
    and leave it running without the binding it was admitted under."""
    from kiro_crew.memory_stores import UnknownMemoryStore

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    seen: list[bool] = []

    def refuse_bindings(*args: Any, **kwargs: Any) -> Any:
        raise UnknownMemoryStore("bindings unavailable")

    def observing_restore(snapshot: Any) -> None:
        seen.append("fenced" in dashboard_state._slots)

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", refuse_bindings)
    monkeypatch.setattr(chat_handlers, "restore_session_binding", observing_restore)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "fenced", "agent": "worker"})
        data = await response.json()
    assert response.status == 503, data
    assert seen == [False], "the newborn was still registered while its binding restored"
    assert "fenced" not in dashboard_state._slots


def _member_create_setup(dashboard_state: Any, monkeypatch: Any) -> None:
    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""


@pytest.mark.asyncio
async def test_a_same_name_create_during_the_stub_cleanup_is_refused(dashboard_state, monkeypatch):
    """While a rollback has the newborn detached and is removing the transcript
    stub its pin created, a same-name create must not mint a replacement on
    that key: it would write its title into the stub the cleanup then deletes.
    The detached key refuses mints until the rollback settles, and is free
    again afterwards."""
    from kiro_crew.history import ConversationLog
    from kiro_crew.memory_stores import UnknownMemoryStore

    _member_create_setup(dashboard_state, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_restore = chat_handlers.restore_session_binding
    real_bindings = chat_handlers.resolve_agent_bindings
    loop = asyncio.get_running_loop()

    def refuse_bindings(*args: Any, **kwargs: Any) -> Any:
        raise UnknownMemoryStore("bindings unavailable")

    def blocked_restore(snapshot: Any) -> None:
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
        real_restore(snapshot)

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", refuse_bindings)
    monkeypatch.setattr(chat_handlers, "restore_session_binding", blocked_restore)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    body = {"name": "cleanup", "agent": "worker"}
    async with TestClient(TestServer(as_owner(app))) as client:
        first = asyncio.create_task(client.post("/api/chat/slots", json=body))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            replacement = await asyncio.wait_for(
                client.post("/api/chat/slots", json={"name": "cleanup", "title": "Replacement"}),
                10,
            )
            assert replacement.status == 409, await replacement.text()
            release.set()
            refused = await asyncio.wait_for(first, 10)
            assert refused.status == 503, await refused.text()
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        assert "cleanup" not in dashboard_state._slots
        assert not ConversationLog().has_log("dashboard:cleanup")
        monkeypatch.setattr(chat_handlers, "restore_session_binding", real_restore)
        monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", real_bindings)
        retry = await client.post("/api/chat/slots", json={"name": "cleanup"})
        assert retry.status == 200, await retry.text()


@pytest.mark.asyncio
async def test_a_re_open_waits_for_the_create_outside_the_push_suspension(
    dashboard_state, monkeypatch
):
    """A same-name re-open waits for the minting create BEFORE it enters the
    state-wide slot-push suspension, so a stalled create holds back that one
    request and not every slot broadcast for the length of the wait."""
    _member_create_setup(dashboard_state, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_record = chat_handlers._record_explicit_agent_selection
    suspensions: list[str] = []
    real_suspend = dashboard_state.suspend_slots_push

    def counted_suspend(*args: Any, **kwargs: Any) -> Any:
        suspensions.append("enter")
        return real_suspend(*args, **kwargs)

    async def blocked_record(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(dashboard_state, "suspend_slots_push", counted_suspend)
    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", blocked_record)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        first = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "held", "agent": "worker"})
        )
        second: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            assert suspensions == ["enter"]
            second = asyncio.create_task(
                client.post("/api/chat/slots", json={"name": "held", "agent": "worker"})
            )
            await asyncio.sleep(0.3)
            assert not second.done(), "the re-open did not wait for the create"
            assert suspensions == ["enter"], "the waiting re-open entered the push suspension"
            release.set()
            first_response = await asyncio.wait_for(first, 10)
            first_text = await first_response.text()
            second_response = await asyncio.wait_for(second, 10)
            second_data = await second_response.json()
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (first, second) if t is not None), return_exceptions=True
            )
    assert first_response.status == 200, first_text
    assert second_response.status == 200, second_data
    assert second_data["key"] == "held"


@pytest.mark.asyncio
async def test_a_re_open_does_not_wait_on_a_held_switch_lock(dashboard_state, monkeypatch):
    """A same-name re-open waits only for the create that minted the slot, not
    for whatever else holds ``slot._lock`` (agent, effort and workspace
    switches, regenerate). It runs inside the state-wide push suspension, so
    waiting on that lock would hold every dashboard's broadcasts."""
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        created = await client.post("/api/chat/slots", json={"name": "switching"})
        assert created.status == 200, await created.text()
        slot = dashboard_state._slots["switching"]
        async with slot._lock:  # a switch in progress on this slot
            reopened = await asyncio.wait_for(
                client.post("/api/chat/slots", json={"name": "switching"}), 5
            )
        assert reopened.status == 200, await reopened.text()


@pytest.mark.asyncio
async def test_a_cancelled_create_rolls_back_only_after_the_pin_worker_finished(
    dashboard_state, monkeypatch
):
    """Shutdown can cancel a create while the member pin's worker thread runs.
    The create drains that worker before the rollback starts, so the binding
    restore never runs while the pin can still publish behind it."""
    import threading

    _member_create_setup(dashboard_state, monkeypatch)
    order: list[str] = []
    worker_started = threading.Event()
    go = threading.Event()
    real_restore = chat_handlers.restore_session_binding
    handler_tasks: list[asyncio.Task[Any]] = []

    async def slow_pin(*args: Any, **kwargs: Any) -> str:
        def worker() -> str:
            worker_started.set()
            go.wait(10)
            order.append("pin finished")
            return ""

        return await asyncio.to_thread(worker)

    def observed_restore(snapshot: Any) -> None:
        order.append("restore")
        real_restore(snapshot)

    async def tracked(request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        assert task is not None
        handler_tasks.append(task)
        return await chat_handlers.api_chat_slot_create(request)

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", slow_pin)
    monkeypatch.setattr(chat_handlers, "restore_session_binding", observed_restore)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", tracked)
    async with TestClient(TestServer(as_owner(app))) as client:
        request = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "cancelled", "agent": "worker"})
        )
        try:
            assert await asyncio.to_thread(worker_started.wait, 10)
            handler_tasks[0].cancel()
            await asyncio.sleep(0.2)
            go.set()
            await asyncio.wait({handler_tasks[0]}, timeout=10)
        finally:
            go.set()
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert order == ["pin finished", "restore"]
    assert "cancelled" not in dashboard_state._slots


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [True, False])
async def test_a_persons_create_claims_the_folder_before_its_assignment(
    dashboard_state, monkeypatch, refused
):
    """A person filing a new chat into an agent-created folder claims it in the
    same locked step that confirms it exists, before the member assignment, so
    the creating agent's ``chat_folder_delete`` is refused for the whole create.
    The claim is for good: a refused create keeps it, as a committed one does."""
    from kiro_crew.dashboard.chat_folders import CREATED_BY_SESSION
    from kiro_crew.memory_stores import UnknownMemoryStore

    _member_create_setup(dashboard_state, monkeypatch)
    dashboard_state._folders.append(
        {"id": "f-agent", "name": "Agent's", "order": 0, CREATED_BY_SESSION: "dashboard:maker"}
    )
    seen: list[Any] = []
    real_record = chat_handlers._record_explicit_agent_selection

    def mark() -> Any:
        return next(f for f in dashboard_state._folders if f["id"] == "f-agent").get(
            CREATED_BY_SESSION
        )

    async def observed_record(*args: Any, **kwargs: Any) -> Any:
        seen.append(mark())
        if refused:
            raise UnknownMemoryStore("assignment refused")
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", observed_record)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "claimed", "agent": "worker", "folder_id": "f-agent"}
        )
        data = await response.json()
    assert seen == [None], "the folder was still the agent's while the assignment ran"
    if refused:
        assert response.status == 503, data
        assert "claimed" not in dashboard_state._slots
        assert mark() is None
    else:
        assert response.status == 200, data
        assert mark() is None


@pytest.mark.asyncio
async def test_a_failed_folder_claim_refuses_the_create(dashboard_state, monkeypatch):
    """The person's claim is part of the create, not a best-effort publish: a
    folder-store write failure there fails the request and leaves no slot."""
    from kiro_crew.dashboard.chat_folders import CREATED_BY_SESSION

    _member_create_setup(dashboard_state, monkeypatch)
    dashboard_state._folders.append(
        {"id": "f-agent", "name": "Agent's", "order": 0, CREATED_BY_SESSION: "dashboard:maker"}
    )

    async def failing_mutate(*args: Any, **kwargs: Any) -> Any:
        raise OSError("folders.json write failed")

    monkeypatch.setattr(dashboard_state, "mutate_folders", failing_mutate)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "unclaimed", "agent": "worker", "folder_id": "f-agent"}
        )
    assert response.status == 500, await response.text()
    assert "unclaimed" not in dashboard_state._slots


@pytest.mark.asyncio
async def test_a_cancelled_create_rolls_back_only_after_its_persist_worker_finished(
    dashboard_state, monkeypatch
):
    """Cancelled while the metadata save's worker runs, the create drains that
    worker before rolling back, so the binding restore is never overwritten by
    a save that commits after it."""
    import threading

    _member_create_setup(dashboard_state, monkeypatch)
    order: list[str] = []
    worker_started = threading.Event()
    go = threading.Event()
    real_restore = chat_handlers.restore_session_binding
    handler_tasks: list[asyncio.Task[Any]] = []

    async def slow_save(*args: Any, **kwargs: Any) -> bool:
        def worker() -> bool:
            worker_started.set()
            go.wait(10)
            order.append("save finished")
            return True

        return await asyncio.to_thread(worker)

    def observed_restore(snapshot: Any) -> None:
        order.append("restore")
        real_restore(snapshot)

    async def tracked(request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        assert task is not None
        handler_tasks.append(task)
        return await chat_handlers.api_chat_slot_create(request)

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", slow_save)
    monkeypatch.setattr(chat_handlers, "restore_session_binding", observed_restore)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", tracked)
    async with TestClient(TestServer(as_owner(app))) as client:
        request = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "saving", "agent": "worker", "title": "T"})
        )
        try:
            assert await asyncio.to_thread(worker_started.wait, 10)
            handler_tasks[0].cancel()
            await asyncio.sleep(0.2)
            go.set()
            await asyncio.wait({handler_tasks[0]}, timeout=10)
        finally:
            go.set()
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert order == ["save finished", "restore"]
    assert "saving" not in dashboard_state._slots


@pytest.mark.asyncio
async def test_a_folder_filing_waits_for_the_newborns_create_to_settle(
    dashboard_state, monkeypatch
):
    """A folder PATCH that names a newborn while its create is still running
    waits for that create. When the create rolls back, the PATCH is refused
    with 409 ``session_gone`` instead of acknowledging a placement the rollback
    then deletes with the slot and its transcript stub."""
    from kiro_crew.dashboard.chat_folders import api_chat_slot_folder
    from kiro_crew.memory_stores import UnknownMemoryStore

    _member_create_setup(dashboard_state, monkeypatch)
    dashboard_state._folders.append({"id": "f-mine", "name": "Mine", "order": 0})
    clients: list[Any] = []
    filing: list[asyncio.Task[Any]] = []

    async def filed_then_refused(*args: Any, **kwargs: Any) -> Any:
        filing.append(
            asyncio.create_task(
                clients[0].patch("/api/chat/slots/inflight/folder", json={"folder_id": "f-mine"})
            )
        )
        await asyncio.sleep(0.3)
        assert not filing[0].done(), "the filing did not wait for the create"
        raise UnknownMemoryStore("assignment refused")

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", filed_then_refused)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
    async with TestClient(TestServer(as_owner(app))) as client:
        clients.append(client)
        response = await client.post(
            "/api/chat/slots", json={"name": "inflight", "agent": "worker"}
        )
        assert response.status == 503, await response.text()
        filed = await asyncio.wait_for(filing[0], 10)
        assert filed.status == 409, await filed.text()
        assert (await filed.json())["code"] == "session_gone"
    assert "inflight" not in dashboard_state._slots


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["title", "tags", "color"])
async def test_a_title_or_tag_edit_waits_for_the_newborns_create_to_settle(
    dashboard_state, monkeypatch, edit
):
    """A rename, a tag replace or a colour change on a newborn waits for its
    create like the folder PATCH does: when the create rolls back, the edit is
    refused with 409 ``session_gone`` instead of acknowledged and deleted with
    the slot."""
    from kiro_crew.dashboard.chat_tags import api_chat_slot_tags
    from kiro_crew.dashboard.chat_title import api_chat_slot_rename
    from kiro_crew.memory_stores import UnknownMemoryStore

    _member_create_setup(dashboard_state, monkeypatch)
    clients: list[Any] = []
    editing: list[asyncio.Task[Any]] = []

    def send() -> Any:
        if edit == "title":
            return clients[0].patch("/api/chat/slots/edited/title", json={"title": "Mine"})
        if edit == "color":
            return clients[0].patch("/api/chat/slots/edited/color", json={"color_index": 1})
        return clients[0].put("/api/chat/slots/edited/tags", json={"tags": []})

    async def edited_then_refused(*args: Any, **kwargs: Any) -> Any:
        editing.append(asyncio.create_task(send()))
        await asyncio.sleep(0.3)
        assert not editing[0].done(), "the edit did not wait for the create"
        raise UnknownMemoryStore("assignment refused")

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", edited_then_refused)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_patch("/api/chat/slots/{slot}/title", api_chat_slot_rename)
    app.router.add_put("/api/chat/slots/{slot}/tags", api_chat_slot_tags)
    app.router.add_patch("/api/chat/slots/{slot}/color", chat_handlers.api_chat_slot_color)
    async with TestClient(TestServer(as_owner(app))) as client:
        clients.append(client)
        response = await client.post("/api/chat/slots", json={"name": "edited", "agent": "worker"})
        assert response.status == 503, await response.text()
        answered = await asyncio.wait_for(editing[0], 10)
        assert answered.status == 409, await answered.text()
        assert (await answered.json())["code"] == "session_gone"
    assert "edited" not in dashboard_state._slots


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["autocompact", "project"])
async def test_a_threshold_or_project_post_waits_for_the_newborns_create_to_settle(
    dashboard_state, monkeypatch, edit
):
    """The metadata POSTs settle like the PATCHes do. A compaction threshold or a
    project set on a newborn whose create rolls back is refused with 409
    ``session_gone``: the project route queues on the slot lock the create
    holds, so it re-checks after taking it that the slot it looked up is still
    the one registered, instead of writing to the rolled-back slot and saving
    it."""
    from kiro_crew.memory_stores import UnknownMemoryStore

    _member_create_setup(dashboard_state, monkeypatch)
    clients: list[Any] = []
    editing: list[asyncio.Task[Any]] = []

    def send() -> Any:
        if edit == "autocompact":
            return clients[0].post("/api/chat/slots/posted/autocompact", json={"pct": None})
        return clients[0].post("/api/chat/slots/posted/project", json={"project": ""})

    async def posted_then_refused(*args: Any, **kwargs: Any) -> Any:
        editing.append(asyncio.create_task(send()))
        await asyncio.sleep(0.3)
        assert not editing[0].done(), "the POST did not wait for the create"
        raise UnknownMemoryStore("assignment refused")

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", posted_then_refused)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_post(
        "/api/chat/slots/{slot}/autocompact", chat_handlers.api_chat_slot_autocompact
    )
    app.router.add_post("/api/chat/slots/{slot}/project", chat_handlers.api_chat_slot_project)
    async with TestClient(TestServer(as_owner(app))) as client:
        clients.append(client)
        response = await client.post("/api/chat/slots", json={"name": "posted", "agent": "worker"})
        assert response.status == 503, await response.text()
        answered = await asyncio.wait_for(editing[0], 10)
        assert answered.status == 409, await answered.text()
        assert (await answered.json())["code"] == "session_gone"
    assert "posted" not in dashboard_state._slots


#: Slot PATCH/PUT routes that write nothing a newborn could hold: a newborn has
#: no checklist row to tick and no queued turn (main or side) to edit or
#: reorder, so those handlers answer 404 for one before any wait would matter.
_SETTLE_EXEMPT_SLOT_WRITES = frozenset(
    {
        "/api/chat/slots/{slot}/todo",
        "/api/chat/slots/{slot}/queue/{queue_id}",
        "/api/chat/slots/{slot}/queue/order",
        "/api/chat/slots/{slot}/side/queue/{queue_id}",
    }
)


#: Slot POST routes that write metadata a newborn could hold: its compaction
#: threshold, its project directory, and its Slack or channel mirror link. Every
#: other slot POST starts or steers a turn, which a newborn has not taken yet.
_SLOT_METADATA_POSTS = frozenset(
    {
        "/api/chat/slots/{slot}/autocompact",
        "/api/chat/slots/{slot}/project",
        "/api/chat/slots/{slot}/slack-link",
        "/api/chat/slots/{slot}/mirror-link",
    }
)


def _slot_metadata_write_routes() -> list[tuple[str, Any]]:
    """Every PATCH/PUT route under ``/api/chat/slots/{slot}/``, and every POST in
    ``_SLOT_METADATA_POSTS``, with its handler."""
    import ast
    import inspect as _inspect

    import kiro_crew.dashboard.routes.chat as chat_routes
    import kiro_crew.dashboard.routes.sessions as session_routes

    found: list[tuple[str, Any]] = []
    for module in (chat_routes, session_routes):
        for node in ast.walk(ast.parse(_inspect.getsource(module))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"add_patch", "add_put", "add_post"}
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Attribute)
                and isinstance(node.args[1].value, ast.Name)
            ):
                continue
            path = node.args[0].value
            if (
                isinstance(path, str)
                and path.startswith("/api/chat/slots/{slot}/")
                and (node.func.attr != "add_post" or path in _SLOT_METADATA_POSTS)
            ):
                owner = getattr(module, node.args[1].value.id)
                found.append((path, getattr(owner, node.args[1].attr)))
    return found


def test_every_slot_metadata_write_takes_the_one_settle_step():
    """Each slot PATCH/PUT handler, and each slot POST that writes metadata, goes
    through ``refuse_write_to_unsettled_create``, so a write route added later
    cannot accept an edit on a newborn whose create then rolls back and deletes
    it."""
    import inspect as _inspect

    routes = _slot_metadata_write_routes()
    assert {path for path, _ in routes} >= {
        "/api/chat/slots/{slot}/folder",
        "/api/chat/slots/{slot}/pin",
        "/api/chat/slots/{slot}/mode",
        "/api/chat/slots/{slot}/title",
        "/api/chat/slots/{slot}/tags",
        "/api/chat/slots/{slot}/color",
        *_SLOT_METADATA_POSTS,
    }, "the route sweep no longer sees the slot write routes"
    missing = [
        path
        for path, handler in routes
        if path not in _SETTLE_EXEMPT_SLOT_WRITES
        and "refuse_write_to_unsettled_create(" not in _inspect.getsource(handler)
    ]
    assert missing == [], f"slot writes that skip the create settle step: {missing}"


@pytest.mark.asyncio
async def test_a_stalled_create_holds_a_waiting_edit_only_until_the_bound(
    dashboard_state, monkeypatch
):
    """The wait on a newborn's create is bounded: an edit queued behind a create
    that does not settle is refused with 409 ``slot_create_pending`` once
    ``SLOT_CREATE_SETTLE_TIMEOUT_SECS`` passes, rather than waiting on it."""
    import kiro_crew.dashboard.state as state_mod
    from kiro_crew.dashboard.chat_folders import api_chat_slot_folder

    _member_create_setup(dashboard_state, monkeypatch)
    monkeypatch.setattr(state_mod, "SLOT_CREATE_SETTLE_TIMEOUT_SECS", 0.2)
    dashboard_state._folders.append({"id": "f-mine", "name": "Mine", "order": 0})
    release = asyncio.Event()
    clients: list[Any] = []
    answers: list[Any] = []
    real_record = chat_handlers._record_explicit_agent_selection

    async def stalled(*args: Any, **kwargs: Any) -> Any:
        filed = await clients[0].patch(
            "/api/chat/slots/stalled/folder", json={"folder_id": "f-mine"}
        )
        answers.append((filed.status, (await filed.json()).get("code")))
        release.set()
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", stalled)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_patch("/api/chat/slots/{slot}/folder", api_chat_slot_folder)
    async with TestClient(TestServer(as_owner(app))) as client:
        clients.append(client)
        response = await client.post("/api/chat/slots", json={"name": "stalled", "agent": "worker"})
        assert response.status == 200, await response.text()
    assert release.is_set()
    assert answers == [(409, "slot_create_pending")]


@pytest.mark.asyncio
async def test_a_create_cancelled_during_its_folder_claim_drains_and_keeps_it(
    dashboard_state, monkeypatch
):
    """Cancelled while the claim's folder write runs, the create waits for that
    write (the store lock is not released under a running writer) before it
    rolls back. The claim is for good: the rollback writes no folder row and
    the person's folder stays theirs."""
    import threading

    from kiro_crew.dashboard.chat_folders import CREATED_BY_SESSION

    _member_create_setup(dashboard_state, monkeypatch)
    dashboard_state._folders.append(
        {"id": "f-agent", "name": "Agent's", "order": 0, CREATED_BY_SESSION: "dashboard:maker"}
    )
    real_write = dashboard_state._write_folders_confirmed
    writes: list[str] = []
    write_started = threading.Event()
    go = threading.Event()
    handler_tasks: list[asyncio.Task[Any]] = []

    def slow_write(path: Any, snapshot: Any) -> None:
        if not writes:
            write_started.set()
            go.wait(10)
        writes.append(
            next(f for f in snapshot if f["id"] == "f-agent").get(CREATED_BY_SESSION) or "claimed"
        )
        real_write(path, snapshot)

    async def tracked(request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        assert task is not None
        handler_tasks.append(task)
        return await chat_handlers.api_chat_slot_create(request)

    monkeypatch.setattr(dashboard_state, "_write_folders_confirmed", slow_write)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots", tracked)
    async with TestClient(TestServer(as_owner(app))) as client:
        request = asyncio.create_task(
            client.post(
                "/api/chat/slots",
                json={"name": "midclaim", "agent": "worker", "folder_id": "f-agent"},
            )
        )
        try:
            assert await asyncio.to_thread(write_started.wait, 10)
            handler_tasks[0].cancel()
            await asyncio.sleep(0.2)
            go.set()
            await asyncio.wait({handler_tasks[0]}, timeout=10)
        finally:
            go.set()
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert writes == ["claimed"], writes
    assert "midclaim" not in dashboard_state._slots
    folder = next(f for f in dashboard_state._folders if f["id"] == "f-agent")
    assert CREATED_BY_SESSION not in folder

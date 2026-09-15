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
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers


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

        def resolve(config, agent, project=None):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            calls.append((agent, project))
            result = original(config, agent, project)
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

    def resolve(config, agent):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(agent)
        result = original(config, agent)
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
@pytest.mark.parametrize("change", ["none", "replacement", "session"])
async def test_switch_resolves_off_loop_and_refuses_rebound_slot_before_reset(
    dashboard_state, monkeypatch, change
):
    from kiro_crew.dashboard.state import _ChatSlot

    cfg = _alias_config(default={"kiro_agent": "kirocrew"}, worker={"kiro_agent": "kirocrew"})
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    slot = dashboard_state.get_or_create_slot("offloop-switch", agent="default")
    replacement = _ChatSlot(slot.key, agent="another-owner")
    dashboard_state.sessions.reset = AsyncMock(return_value=True)
    dashboard_state.sessions.get_provider.return_value = None
    loop = asyncio.get_running_loop()
    original = chat_handlers.resolve_agent_bindings
    calls = []

    def resolve(config, agent, project=None):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append((agent, project))
        result = original(config, agent, project)
        if change == "replacement":
            loop.call_soon_threadsafe(dashboard_state._slots.__setitem__, slot.key, replacement)
        elif change == "session":
            loop.call_soon_threadsafe(setattr, slot, "linked_session_key", "slack:other-session")
        return result

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
    app = web.Application()
    app["state"] = dashboard_state
    app.router.add_post("/api/chat/slots/{slot}/agent", chat_handlers.api_chat_slot_agent)
    async with TestClient(TestServer(app)) as client:
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

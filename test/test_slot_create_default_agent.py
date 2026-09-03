"""POST /api/chat/slots must stamp the resolved default agent on agent-less creates.

``api_chat_slot_create`` stores ``body["agent"]`` verbatim, so a create that
names no agent persisted ``""`` — dispatch still resolves the config default,
but the slot's metadata disagrees with what actually answers, and the
dashboard footer chip renders its literal ``'default'`` fallback. The
dashboard's auto-create races the agents fetch, so agent-less creates are a
common path, not an edge. Same defect class as #2891, which records the
resolved agent on channel-transport writes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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
    """A real config whose ``agents`` map holds the given alias entries.

    Any alias's ``memory_store`` is also registered in ``memory_stores`` —
    resolution silently falls back to the default store for unknown names,
    which would collapse the distinct-store case this helper exists to build.
    """
    from kiro_crew.config.loader import KiroCrewAgentConfig, MemoryStoreConfig

    cfg = KiroCrewConfig()
    cfg.agents = {name: KiroCrewAgentConfig(**fields) for name, fields in aliases.items()}
    cfg.default_agent = next(iter(cfg.agents))
    for entry in cfg.agents.values():
        if entry.memory_store not in cfg.memory_stores:
            cfg.memory_stores[entry.memory_store] = MemoryStoreConfig()
    return cfg


async def _post_chat(state: Any, payload: dict[str, Any]) -> Any:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(app)) as client:
        return await client.post("/api/chat?ws=1", json=payload), None


class TestSameBindingGuard:
    @pytest.mark.asyncio
    async def test_different_memory_store_still_409s(
        self, dashboard_state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Aliases sharing kiro agent + workspace but NOT memory store are
        different bindings: allowing the send would read and write the other
        alias's memory store."""
        cfg = _alias_config(
            **{
                "alias-a": {"kiro_agent": "kirocrew", "memory_store": "store-a"},
                "alias-b": {"kiro_agent": "kirocrew", "memory_store": "store-b"},
            }
        )
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
        cfg = _alias_config(
            **{
                "alias-a": {"kiro_agent": "kirocrew"},
                "alias-b": {"kiro_agent": "kirocrew"},
            }
        )
        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        slot = dashboard_state.get_or_create_slot("pinned2")
        slot.agent = "alias-a"
        events: list[str] = []
        monkeypatch.setattr(
            chat_handlers,
            "_emit_agent_assignment",
            lambda key, agent, outcome="applied": events.append(outcome),
        )
        resp, _ = await _post_chat(
            dashboard_state, {"message": "hi", "slot": "pinned2", "agent": "alias-b"}
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

        cfg = _alias_config(**{"kirocrew": {"kiro_agent": "kirocrew"}})
        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        # The project declares "proj-agent"; resolution must see it ONLY when
        # the guard passes the slot's project scope through.
        monkeypatch.setattr(
            loader_mod,
            "_project_declares_agent",
            lambda name, project: name == "proj-agent" and project == "/proj",
        )

        async def _noop_warm(project: Any, **kw: Any) -> None:
            # **kw: the warm takes keyword-only SEL attribution labels (#6764)
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
            dashboard_state, {"message": "hi", "slot": "proj-slot", "agent": "kirocrew"}
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

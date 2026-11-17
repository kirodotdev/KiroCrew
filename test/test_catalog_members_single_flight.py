"""``GET /api/agents/catalog`` and ``GET /api/members``: a burst costs one build, a read stays fresh.

The dashboard re-reads both on every refresh frame, from every open tab, and each read is a
multi-second build on a busy gateway. Concurrent reads share one build that started
after each of them arrived (the crew-board rule, see ``crew_board_flight``), and the answer
carries an ``ETag`` so an unchanged body is a 304. Nothing is kept between builds: a read
after a change sees the change.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers import agent_catalog
from kiro_crew.dashboard.handlers import members as members_handlers
from kiro_crew.dashboard.handlers import members_roster

#: Longest any wait in these tests may take before the test fails instead of hanging.
_BOUND_S = 30.0
_OWNER = "flight-owner"


def _template(name: str) -> AgentInfo:
    return AgentInfo(name=name, filename=f"{name}.json", description="Test helper", model="")


@pytest.fixture
def catalog_app(monkeypatch, tmp_path):
    """The real catalog handler over a config the test can change between reads."""
    config = KiroCrewConfig()
    config.agents = {"reviewer": KiroCrewAgentConfig(kiro_agent="reviewer")}
    config.default_agent = "reviewer"
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(agent_catalog, "list_agents", Mock(return_value=[_template("reviewer")]))
    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", Mock(return_value=True))
    monkeypatch.setattr(agent_catalog, "reset_dangling_default_agent", Mock(return_value=False))
    monkeypatch.setattr(agent_catalog, "materialized_snapshot_complete", lambda: True)
    monkeypatch.setattr(agent_catalog.agent_state, "all_fork_info", lambda: {})
    state = SimpleNamespace(
        owner_id=_OWNER,
        _slots={
            "chat-one": SimpleNamespace(project=str(tmp_path), _app=""),
            "chat-two": SimpleNamespace(project="", _app=""),
        },
    )

    @web.middleware
    async def identity(request, handler):
        request["user"] = _OWNER
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = state
    app.router.add_get("/api/agents/catalog", agent_catalog.api_agent_catalog)
    return SimpleNamespace(app=app, config=config)


def _count_builds(monkeypatch, module, name: str, gate: asyncio.Event | None = None) -> list[int]:
    """Wrap ``module.name`` so each call is counted; hold every call until *gate* is set."""
    real = getattr(module, name)
    calls: list[int] = []

    async def counted(*args, **kwargs):
        calls.append(1)
        if gate is not None:
            await asyncio.wait_for(gate.wait(), _BOUND_S)
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def _count_arrivals(monkeypatch, module, expected: int) -> asyncio.Event:
    """An event set once *expected* reads have reached ``module.shared_board``."""
    real = module.shared_board
    arrived = asyncio.Event()
    seen: list[int] = []

    async def counted(*args, **kwargs):
        seen.append(1)
        if len(seen) >= expected:
            arrived.set()
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, "shared_board", counted)
    return arrived


@pytest.mark.asyncio
async def test_a_burst_of_catalog_reads_shares_builds(catalog_app, monkeypatch):
    gate = asyncio.Event()
    calls = _count_builds(monkeypatch, agent_catalog, "_build_catalog", gate)
    arrived = _count_arrivals(monkeypatch, agent_catalog, 8)
    async with TestClient(TestServer(catalog_app.app)) as client:
        reads = [
            asyncio.ensure_future(
                client.get("/api/agents/catalog", headers={"X-Session-Key": "chat-one"})
            )
            for _ in range(8)
        ]
        await asyncio.wait_for(arrived.wait(), _BOUND_S)
        gate.set()
        responses = await asyncio.wait_for(asyncio.gather(*reads), _BOUND_S)
        bodies = [await r.json() for r in responses]
    assert [r.status for r in responses] == [200] * 8
    # All eight reads had arrived before the first build finished: at most one build
    # was running and one waiting, whatever the scheduling.
    assert len(calls) <= 2
    assert all(body == bodies[0] for body in bodies)


@pytest.mark.asyncio
async def test_catalog_reads_for_different_chats_do_not_share_a_build(catalog_app, monkeypatch):
    # The answer names this chat's harness and project agents, so two chats cannot share.
    calls = _count_builds(monkeypatch, agent_catalog, "_build_catalog")
    async with TestClient(TestServer(catalog_app.app)) as client:
        await asyncio.gather(
            client.get("/api/agents/catalog", headers={"X-Session-Key": "chat-one"}),
            client.get("/api/agents/catalog", headers={"X-Session-Key": "chat-two"}),
        )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_catalog_read_after_a_change_sees_it(catalog_app):
    async with TestClient(TestServer(catalog_app.app)) as client:
        first = await (await client.get("/api/agents/catalog")).json()
        catalog_app.config.agents["added"] = KiroCrewAgentConfig(kiro_agent="reviewer")
        second = await (await client.get("/api/agents/catalog")).json()
    assert [r["name"] for r in first["agents"] if r["selection_kind"] == "member"] == ["reviewer"]
    assert [r["name"] for r in second["agents"] if r["selection_kind"] == "member"] == [
        "reviewer",
        "added",
    ]


@pytest.mark.asyncio
async def test_an_unchanged_catalog_is_a_304_and_a_changed_one_is_not(catalog_app):
    async with TestClient(TestServer(catalog_app.app)) as client:
        first = await client.get("/api/agents/catalog")
        etag = first.headers["ETag"]
        again = await client.get("/api/agents/catalog", headers={"If-None-Match": etag})
        assert again.status == 304
        assert again.headers["ETag"] == etag
        catalog_app.config.agents["added"] = KiroCrewAgentConfig(kiro_agent="reviewer")
        changed = await client.get("/api/agents/catalog", headers={"If-None-Match": etag})
    assert first.status == 200
    assert changed.status == 200
    assert changed.headers["ETag"] != etag


@pytest.mark.asyncio
async def test_a_failed_catalog_build_answers_503_to_every_reader(catalog_app, monkeypatch):
    monkeypatch.setattr(agent_catalog, "list_agents", Mock(side_effect=RuntimeError("scan failed")))
    async with TestClient(TestServer(catalog_app.app)) as client:
        responses = await asyncio.gather(*(client.get("/api/agents/catalog") for _ in range(3)))
        codes = [(await r.json())["code"] for r in responses]
    assert [r.status for r in responses] == [503] * 3
    assert codes == ["agent_catalog_unavailable"] * 3


def _members_request(state, *, headers=None):
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", "/api/members", app=app, headers=headers or {})
    req["app"] = ""
    req["user"] = _OWNER
    return req


def _roster_state(tmp_path):
    state = _make_state(tmp_path)
    state.owner_id = _OWNER
    return state


def _roster_config():
    return SimpleNamespace(
        agents={"code-reviewer": KiroCrewAgentConfig(kiro_agent="kirocrew", description="d")},
        default_agent="code-reviewer",
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


@pytest.mark.asyncio
async def test_a_burst_of_members_reads_shares_builds(tmp_path, monkeypatch):
    gate = asyncio.Event()
    calls = _count_builds(monkeypatch, members_roster, "_build_roster", gate)
    arrived = _count_arrivals(monkeypatch, members_roster, 8)
    state = _roster_state(tmp_path)
    with patch.object(members_handlers.KiroCrewConfig, "load", return_value=_roster_config()):
        reads = [
            asyncio.ensure_future(members_handlers.api_members(_members_request(state)))
            for _ in range(8)
        ]
        await asyncio.wait_for(arrived.wait(), _BOUND_S)
        gate.set()
        responses = await asyncio.wait_for(asyncio.gather(*reads), _BOUND_S)
    assert [r.status for r in responses] == [200] * 8
    assert len(calls) <= 2
    assert len({bytes(r.body) for r in responses}) == 1


@pytest.mark.asyncio
async def test_an_unchanged_roster_is_a_304(tmp_path):
    state = _roster_state(tmp_path)
    with patch.object(members_handlers.KiroCrewConfig, "load", return_value=_roster_config()):
        first = await members_handlers.api_members(_members_request(state))
        etag = first.headers["ETag"]
        again = await members_handlers.api_members(
            _members_request(state, headers={"If-None-Match": etag})
        )
    assert first.status == 200
    assert json.loads(first.body)["members"]
    assert again.status == 304
    assert again.headers["ETag"] == etag


@pytest.mark.asyncio
async def test_a_refused_roster_reader_never_reaches_a_build(tmp_path, monkeypatch):
    calls = _count_builds(monkeypatch, members_roster, "_build_roster")
    req = _members_request(_roster_state(tmp_path))
    req["user"] = "123456789"
    resp = await members_handlers.api_members(req)
    assert resp.status == 403
    assert calls == []

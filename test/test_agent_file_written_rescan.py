"""A template chosen by name finds a spec file written after the snapshot warmed.

The dispatch snapshot (``_materialized_kiro_agent``) is warmed once and refreshed
only by the registered write paths, so a spec written by hand or by kiro-cli was
refused with 409 ``agent_choice_unavailable`` until something else rescanned.
Chat create and the agent switch now rescan once on a template miss and resolve
again; the fork and publish handlers publish the name they write. A name that is
still missing after the rescan keeps its 409.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import _turn_state

from kiro_crew.config import loader
from kiro_crew.config.loader import refresh_materialized_agents
from kiro_crew.config.loader_snapshots import (
    _materialized_kiro_agent,
    materialized_snapshot_complete,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers import agent_catalog


@pytest.fixture
def agents_home(tmp_path, monkeypatch):
    """kiro-cli's home under the test's tmp dir, with a fresh catalog gate."""
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
    monkeypatch.setattr(agent_catalog, "_REFRESH_GATE", agent_catalog._RefreshGate())
    directory = kiro_agents_dir()
    directory.mkdir(parents=True)
    assert directory.is_relative_to(tmp_path)
    return directory


def _write_spec(directory: Path, name: str) -> None:
    spec = {"name": name, "description": "", "prompt": ""}
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _warm_without(directory: Path, name: str) -> None:
    """A warm, complete snapshot that predates *name*'s file, as after boot."""
    _write_spec(directory, "existing")
    await asyncio.to_thread(refresh_materialized_agents)
    assert materialized_snapshot_complete()
    assert _materialized_kiro_agent("existing") == "existing"
    _write_spec(directory, name)
    # The file is on disk, and the warm snapshot does not know it yet.
    assert _materialized_kiro_agent(name) == ""


@pytest.mark.asyncio
async def test_create_finds_a_spec_written_after_the_snapshot_warmed(
    tmp_path, monkeypatch, agents_home
):
    state = _turn_state(tmp_path, monkeypatch)
    await _warm_without(agents_home, "fresh")
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={"name": "fresh-chat", "agent": "fresh", "agent_kind": "template"},
        )
        assert response.status == 200, await response.text()
        body = await response.json()
    assert body["agent"] == "fresh"
    assert body["agent_kind"] == "template"
    assert state._slots["fresh-chat"].agent == "fresh"


@pytest.mark.asyncio
async def test_switch_finds_a_spec_written_after_the_snapshot_warmed(
    tmp_path, monkeypatch, agents_home
):
    state = _turn_state(tmp_path, monkeypatch)
    await _warm_without(agents_home, "fresh")
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        created = await client.post("/api/chat/slots", json={"name": "switch-chat"})
        assert created.status == 200, await created.text()
        response = await client.post(
            "/api/chat/slots/switch-chat/agent",
            json={"agent": "fresh", "agent_kind": "template"},
        )
        assert response.status == 200, await response.text()
    assert state._slots["switch-chat"].agent == "fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "switch"])
async def test_a_missing_spec_is_still_refused_after_the_rescan(
    tmp_path, monkeypatch, agents_home, route
):
    state = _turn_state(tmp_path, monkeypatch)
    await _warm_without(agents_home, "fresh")
    scans = []
    real_refresh = agent_catalog.refresh_materialized_agents

    def counted(**kwargs):
        scans.append(kwargs)
        return real_refresh(**kwargs)

    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", counted)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        if route == "switch":
            created = await client.post("/api/chat/slots", json={"name": "missing-chat"})
            assert created.status == 200, await created.text()
            prior = state._slots["missing-chat"].agent
        for _ in range(2):
            if route == "create":
                response = await client.post(
                    "/api/chat/slots",
                    json={"name": "missing-chat", "agent": "absent", "agent_kind": "template"},
                )
            else:
                response = await client.post(
                    "/api/chat/slots/missing-chat/agent",
                    json={"agent": "absent", "agent_kind": "template"},
                )
            assert response.status == 409, await response.text()
            assert (await response.json())["code"] == "agent_choice_unavailable"
    if route == "create":
        assert "missing-chat" not in state._slots
    else:
        assert state._slots["missing-chat"].agent == prior
    # The miss did rescan, and the rescan really ran (it found the other file).
    assert scans
    assert _materialized_kiro_agent("fresh") == "fresh"


@pytest.mark.asyncio
async def test_a_member_miss_does_not_rescan(tmp_path, monkeypatch, agents_home):
    """Members come from the config the handler loaded; a scan cannot add one."""
    state = _turn_state(tmp_path, monkeypatch)
    calls = []

    async def recorded():
        calls.append(True)

    monkeypatch.setattr(agent_catalog, "rescan_agents_after_miss", recorded)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={"name": "member-chat", "agent": "absent", "agent_kind": "member"},
        )
        assert response.status == 409, await response.text()
    assert calls == []


def _fork_request(name, body):
    from unittest.mock import MagicMock

    from aiohttp import web

    request = MagicMock(spec=web.Request)
    request.method = "POST"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}

    async def _json():
        return body

    request.json = _json
    return request


def _seed_config(crew: str, kiro_agent: str) -> None:
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agents = {crew: KiroCrewAgentConfig(kiro_agent=kiro_agent)}
    cfg.default_agent = crew
    cfg.save()


@pytest.fixture
def owner_writer(tmp_path, monkeypatch):
    """Past the owner gate, with the off-loop rescan recorded rather than run."""
    from unittest.mock import patch

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    scheduled = []
    monkeypatch.setattr(
        loader, "schedule_materialized_agents_refresh", lambda: scheduled.append(True)
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        yield agents_dir, scheduled


async def _warm_empty() -> None:
    await asyncio.to_thread(refresh_materialized_agents)
    assert materialized_snapshot_complete()


@pytest.mark.asyncio
async def test_fork_publishes_the_copy_to_the_dispatch_snapshot(owner_writer):
    from kiro_crew.dashboard.handlers.agents import api_agent_fork

    agents_dir, scheduled = owner_writer
    _write_spec(agents_dir, "mytemplate")
    _seed_config("design-crew", "mytemplate")
    await _warm_empty()
    assert loader._materialized_kiro_agent("design-crew") == ""

    resp = await api_agent_fork(_fork_request("mytemplate", {"crew": "design-crew"}))

    assert resp.status == 200, resp.text
    assert loader._materialized_kiro_agent("design-crew") == "design-crew"
    assert scheduled == [True]


@pytest.mark.asyncio
async def test_publish_publishes_the_template_to_the_dispatch_snapshot(owner_writer):
    from kiro_crew import agent_state
    from kiro_crew.dashboard.handlers.agents import api_agent_publish

    agents_dir, scheduled = owner_writer
    _write_spec(agents_dir, "design-crew")
    agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")
    _seed_config("design-crew", "design-crew")
    await _warm_empty()
    assert loader._materialized_kiro_agent("reviewer-v2") == ""

    resp = await api_agent_publish(
        _fork_request("design-crew", {"crew": "design-crew", "name": "reviewer-v2"})
    )

    assert resp.status == 200, resp.text
    assert loader._materialized_kiro_agent("reviewer-v2") == "reviewer-v2"
    assert scheduled == [True]

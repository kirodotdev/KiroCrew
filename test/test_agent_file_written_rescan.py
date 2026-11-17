"""A template chosen by name finds a spec file written after the snapshot warmed.

The dispatch snapshot learns of a spec from the in-process writers and from a
rescan, so a spec written by hand or by kiro-cli after it warmed was refused
(409 ``agent_choice_unavailable`` on chat create and switch, ``agent_unresolved``
from ``session_create``) until something else rescanned. Each of the three now
refreshes the snapshot once when a template or name-first pick misses, and
resolves again; a name that still has no file keeps its refusal. kiro-cli's own
agent switch and the Crew Mode migration's target check do the same.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_state
from dashboard_owner_helpers import as_owner
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)
from test_chat_agent_selection import _record_store, _turn_state

from kiro_crew import session_agent_selection
from kiro_crew.config import KiroCrewConfig, loader
from kiro_crew.config.loader_snapshots import (
    _materialized_kiro_agent,
    materialized_snapshot_complete,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard import crew_mode_migration
from kiro_crew.dashboard import session_control as sc
from kiro_crew.memory_stores import UnknownMemoryStore


@pytest.fixture
def agents_home(tmp_path, monkeypatch):
    """kiro-cli's home under the test's tmp dir."""
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
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
    await asyncio.to_thread(loader.refresh_materialized_agents)
    assert materialized_snapshot_complete()
    assert _materialized_kiro_agent("existing") == "existing"
    _write_spec(directory, name)
    # The file is on disk, and the warm snapshot does not know it yet.
    assert _materialized_kiro_agent(name) == ""


def _count_refreshes(monkeypatch) -> list[None]:
    """Record each snapshot refresh while still running the real one."""
    refreshes: list[None] = []
    real = loader.refresh_materialized_agents

    def counted(**kwargs):
        refreshes.append(None)
        return real(**kwargs)

    monkeypatch.setattr(loader, "refresh_materialized_agents", counted)
    return refreshes


async def _choose(client, route: str, agent: str, kind: str = "template"):
    pick = {"agent": agent, **({"agent_kind": kind} if kind else {})}
    if route == "create":
        return await client.post("/api/chat/slots", json={"name": "picked-chat", **pick})
    return await client.post("/api/chat/slots/picked-chat/agent", json=pick)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["template", ""])
@pytest.mark.parametrize("route", ["create", "switch"])
async def test_a_spec_written_after_the_snapshot_warmed_is_chosen_first_time(
    tmp_path, monkeypatch, agents_home, route, kind
):
    state = _turn_state(tmp_path, monkeypatch)
    await _warm_without(agents_home, "fresh")
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        if route == "switch":
            created = await client.post("/api/chat/slots", json={"name": "picked-chat"})
            assert created.status == 200, await created.text()
        response = await _choose(client, route, "fresh", kind)
        assert response.status == 200, await response.text()
    assert state._slots["picked-chat"].agent == "fresh"
    # The pick read the new file, so the chat's turns dispatch it as well.
    assert _materialized_kiro_agent("fresh") == "fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "switch"])
async def test_a_missing_spec_is_still_refused_after_one_rescan(
    tmp_path, monkeypatch, agents_home, route
):
    state = _turn_state(tmp_path, monkeypatch)
    await _warm_without(agents_home, "fresh")
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        if route == "switch":
            created = await client.post("/api/chat/slots", json={"name": "picked-chat"})
            assert created.status == 200, await created.text()
            prior = state._slots["picked-chat"].agent
        refreshes = _count_refreshes(monkeypatch)
        response = await _choose(client, route, "absent")
        assert response.status == 409, await response.text()
        assert (await response.json())["code"] == "agent_choice_unavailable"
    assert len(refreshes) == 1
    if route == "create":
        assert "picked-chat" not in state._slots
    else:
        assert state._slots["picked-chat"].agent == prior
    # That refresh read the disk: it picked up the other new file on the way.
    assert _materialized_kiro_agent("fresh") == "fresh"


@pytest.mark.parametrize("kind", ["template", ""])
def test_session_create_finds_a_spec_written_after_the_snapshot_warmed(
    tmp_path, monkeypatch, agents_home, kind
):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    asyncio.run(_warm_without(agents_home, "fresh"))

    asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), agent="fresh", agent_kind=kind)
    )

    assert [slot.agent for slot in state._slots.values() if slot is not caller] == ["fresh"]


@pytest.mark.parametrize("kind", ["template", ""])
def test_session_create_still_refuses_a_name_with_no_file(tmp_path, monkeypatch, agents_home, kind):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    asyncio.run(_warm_without(agents_home, "fresh"))
    before = set(state._slots)
    refreshes = _count_refreshes(monkeypatch)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(
            sc.create_session(
                state, caller_session_key=_key(caller), agent="absent", agent_kind=kind
            )
        )

    assert err.value.code == "agent_unresolved"
    assert len(refreshes) == 1
    assert set(state._slots) == before


def test_a_provider_switch_to_a_spec_written_after_the_snapshot_warmed_is_recorded(
    monkeypatch, agents_home
):
    asyncio.run(_warm_without(agents_home, "fresh"))
    cell = _record_store(monkeypatch, None)

    session_agent_selection.record_provider_agent_switch(
        KiroCrewConfig(), "dashboard:switched", None, "fresh", None
    )

    assert cell["record"].template_id == "fresh"
    with pytest.raises(UnknownMemoryStore):
        session_agent_selection.record_provider_agent_switch(
            KiroCrewConfig(), "dashboard:switched", None, "absent", None
        )


def test_a_crew_mode_target_written_after_the_snapshot_warmed_is_usable(agents_home):
    asyncio.run(_warm_without(agents_home, "fresh"))

    assert crew_mode_migration._usable("fresh") == ""
    assert crew_mode_migration._usable("absent") == "agent_not_found"

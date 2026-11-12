"""A template whose spec the boot scan could not read resolves once the file reads.

The first scan in a process has no earlier snapshot to keep a skipped spec's name
from, so a template whose file was held open during that scan is missing while the
file is on disk. A template chat create and a template switch rescan once before
refusing, then resolve again; a name with no file is still refused after that one
rescan.
"""

from __future__ import annotations

import asyncio
import json
import unittest.mock
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import _turn_state

import kiro_crew.hooks as hooks_mod
from kiro_crew.config import loader
from kiro_crew.config.loader_snapshots import (
    _materialized_kiro_agent,
    materialized_snapshot_complete,
)
from kiro_crew.config.paths import kiro_agents_dir


@pytest.fixture
def agents_home(tmp_path, monkeypatch):
    """kiro-cli's home under the test's tmp dir."""
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
    directory = kiro_agents_dir()
    directory.mkdir(parents=True)
    assert directory.is_relative_to(tmp_path)
    return directory


async def _boot_while_held(directory: Path, name: str) -> None:
    """The process's first scan runs while *name*'s spec cannot be read."""
    for stem in ("alpha", name):
        spec = {"name": stem, "description": "", "prompt": ""}
        (directory / f"{stem}.json").write_text(json.dumps(spec), encoding="utf-8")
    real = hooks_mod.safe_read_file

    def held(path: str, **kwargs) -> str:
        if path.endswith(f"{name}.json"):
            raise PermissionError("held open by another process")
        return real(path, **kwargs)

    with unittest.mock.patch.object(hooks_mod, "safe_read_file", held):
        await asyncio.to_thread(loader.refresh_materialized_agents)
    assert not materialized_snapshot_complete()
    assert _materialized_kiro_agent("alpha") == "alpha"
    assert _materialized_kiro_agent(name) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "switch"])
async def test_a_template_unreadable_at_boot_is_chosen_once_it_reads(
    tmp_path, monkeypatch, agents_home, route
):
    state = _turn_state(tmp_path, monkeypatch)
    await _boot_while_held(agents_home, "bravo")
    app = _make_app_with_agent_routes(state)

    async def choose(client, agent):
        if route == "create":
            return await client.post(
                "/api/chat/slots",
                json={"name": f"{agent}-chat", "agent": agent, "agent_kind": "template"},
            )
        created = await client.post("/api/chat/slots", json={"name": f"{agent}-chat"})
        assert created.status == 200, await created.text()
        return await client.post(
            f"/api/chat/slots/{agent}-chat/agent",
            json={"agent": agent, "agent_kind": "template"},
        )

    async with TestClient(TestServer(as_owner(app))) as client:
        response = await choose(client, "bravo")
        assert response.status == 200, await response.text()
        assert state._slots["bravo-chat"].agent == "bravo"
        # That rescan read every spec, so the snapshot is complete again: a name
        # with no file is refused as before, after one refresh of its own.
        assert materialized_snapshot_complete()
        scans: list[dict] = []
        monkeypatch.setattr(loader, "refresh_materialized_agents", lambda **kw: scans.append(kw))
        response = await choose(client, "absent")
        assert response.status == 409, await response.text()
        assert (await response.json())["code"] == "agent_choice_unavailable"
    assert scans == [{}]

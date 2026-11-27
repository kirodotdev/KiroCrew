"""The memory page's Graph tab draws the store its picker names."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from member_memory_helpers import env  # noqa: F401  (the fixture)
from member_memory_helpers import document_store, request

from kiro_crew.dashboard.handlers import memory

pytestmark = pytest.mark.xdist_group("member_memory_api")


@pytest.mark.asyncio
async def test_memory_graph_draws_the_selected_bot_store_only(env):  # noqa: F811
    # The memory page's Graph tab follows its store picker: ?store= names a bot.
    await document_store(env, "")
    env.state.lessons = SimpleNamespace(load_all=lambda: [])
    env.tiers["member-alice"].set_semantic("pref.editor", "alice uses vim", 0.9, "user_explicit")
    env.tiers[""].set_semantic("pref.shell", "global uses zsh", 0.9, "user_explicit")

    alice = await memory.api_memory_graph(
        request(env, query={"store": "member-alice"}, owner=True, session="dashboard:ui")
    )
    shared = await memory.api_memory_graph(request(env, owner=True, session="dashboard:ui"))

    assert alice.status == 200 and shared.status == 200
    alice_titles = " ".join(node["title"] for node in json.loads(alice.text)["nodes"])
    shared_titles = " ".join(node["title"] for node in json.loads(shared.text)["nodes"])
    assert "alice uses vim" in alice_titles and "global uses zsh" not in alice_titles
    assert "global uses zsh" in shared_titles and "alice uses vim" not in shared_titles


@pytest.mark.asyncio
async def test_memory_graph_reads_a_v1_store_vector_tier(env, monkeypatch):  # noqa: F811
    # A named V1 store's markdown facade carries no vector tier; its facts still draw.
    from kiro_crew.memory import MemoryStore

    facade = MemoryStore(workspace=env.home / "v1-workspace")
    facade.init()

    async def no_vector_facade(state, store):
        return facade

    monkeypatch.setattr(memory, "markdown_memory_for_store", no_vector_facade)
    env.tiers["member-alice"].set_semantic("pref.editor", "alice uses vim", 0.9, "user_explicit")
    response = await memory.api_memory_graph(
        request(env, query={"store": "member-alice"}, owner=True, session="dashboard:ui")
    )
    assert response.status == 200
    assert "alice uses vim" in " ".join(n["title"] for n in json.loads(response.text)["nodes"])

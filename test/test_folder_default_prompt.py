"""A chat folder's ``default_prompt``: the text a new chat in it starts with.

The dashboard puts it in the new chat's composer and sends nothing; these tests
cover the folder record that stores it.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew.dashboard.chat_folders_records import MAX_FOLDER_DEFAULT_PROMPT_CHARS

_PROMPT = "You are helping the {{customer}} team.\nStart by reading the open tickets."


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


@pytest.mark.asyncio
async def test_create_stores_it_as_typed(state) -> None:
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders", json={"name": "Acme", "default_prompt": _PROMPT}
        )
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["default_prompt"] == _PROMPT
    assert state._folders[0]["default_prompt"] == _PROMPT


@pytest.mark.asyncio
async def test_a_prompt_at_the_cap_is_kept(state) -> None:
    at_cap = "x" * MAX_FOLDER_DEFAULT_PROMPT_CHARS
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders", json={"name": "Acme", "default_prompt": at_cap}
        )
        assert resp.status == 201, await resp.text()
    assert state._folders[0]["default_prompt"] == at_cap


@pytest.mark.asyncio
async def test_a_blank_prompt_is_not_stored(state) -> None:
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders", json={"name": "Acme", "default_prompt": " \n"}
        )
        assert resp.status == 201, await resp.text()
    assert "default_prompt" not in state._folders[0]


@pytest.mark.asyncio
async def test_update_sets_keeps_on_rename_and_clears_it(state) -> None:
    state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.patch("/api/chat/folders/f1", json={"default_prompt": _PROMPT})
        assert resp.status == 200, await resp.text()
        assert state._folders[0]["default_prompt"] == _PROMPT
        resp = await client.patch("/api/chat/folders/f1", json={"name": "Renamed"})
        assert resp.status == 200, await resp.text()
        assert state._folders[0]["default_prompt"] == _PROMPT
        resp = await client.patch("/api/chat/folders/f1", json={"default_prompt": ""})
        assert resp.status == 200, await resp.text()
    assert "default_prompt" not in state._folders[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [7, ["text"], "x" * (MAX_FOLDER_DEFAULT_PROMPT_CHARS + 1)],
    ids=["number", "list", "too-long"],
)
async def test_an_unusable_prompt_is_refused_on_create_and_update(state, bad) -> None:
    state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await client.post("/api/chat/folders", json={"name": "New", "default_prompt": bad})
        assert resp.status == 400
        assert (await resp.json())["code"] == "default_prompt_invalid"
        resp = await client.patch("/api/chat/folders/f1", json={"default_prompt": bad})
        assert resp.status == 400
        assert (await resp.json())["code"] == "default_prompt_invalid"
    assert state._folders == [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]


def test_a_folder_carrying_it_is_kept_by_cleanup() -> None:
    from kiro_crew.dashboard.chat_folder_cleanup import removable_folder_ids

    folders = [
        {"id": "top", "name": "top", "order": 0, "parent_id": ""},
        {"id": "leaf", "name": "leaf", "order": 0, "parent_id": "top", "default_prompt": _PROMPT},
    ]
    assert removable_folder_ids(folders, set(), include_top_level=True) == []

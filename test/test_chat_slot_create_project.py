"""A create that carries its ``project`` binds it before the chat exists, or makes no chat.

The Sessions New row an app contributes sends the directory the app prepared on the
create itself, so a directory the project route would refuse never leaves a session
behind, not even for the instant a separate bind would take.
"""

from __future__ import annotations

import os

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import _turn_state

from kiro_crew.dashboard import chat_handlers


async def _post(state, body):
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await client.post("/api/chat/slots", json=body)
        return response.status, await response.json()


@pytest.mark.asyncio
async def test_a_usable_project_is_the_new_chats_project(tmp_path, monkeypatch):
    project = tmp_path / "mount"
    project.mkdir()
    state = _turn_state(tmp_path, monkeypatch)
    status, body = await _post(state, {"project": str(project), "title": "Prepared"})
    assert status == 200, body
    slot = state._slots[body["key"]]
    assert slot.project == os.path.realpath(project)
    assert slot._project_picked is True


@pytest.mark.parametrize(
    ("raw", "code"),
    [("missing", "not_a_directory"), ("", "not_a_directory"), (3, "not_a_directory")],
)
@pytest.mark.asyncio
async def test_an_unusable_project_makes_no_chat(tmp_path, monkeypatch, raw, code):
    state = _turn_state(tmp_path, monkeypatch)
    before = set(state._slots)
    value = str(tmp_path / raw) if raw == "missing" else raw
    status, body = await _post(state, {"project": value})
    assert (status, body.get("code")) == (400, code)
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_nul_byte_makes_no_chat(tmp_path, monkeypatch):
    state = _turn_state(tmp_path, monkeypatch)
    before = set(state._slots)
    status, body = await _post(state, {"project": f"{tmp_path}\x00x"})
    assert (status, body.get("code")) == (400, "not_a_directory")
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_sensitive_project_is_refused_like_a_pick(tmp_path, monkeypatch):
    project = tmp_path / "secret"
    project.mkdir()
    state = _turn_state(tmp_path, monkeypatch)
    before = set(state._slots)
    monkeypatch.setattr(chat_handlers, "is_sensitive_canonical_path", lambda path: True)
    status, _body = await _post(state, {"project": str(project)})
    assert status == 403
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_project_over_the_data_home_is_refused(tmp_path, monkeypatch):
    project = tmp_path / "home"
    project.mkdir()
    state = _turn_state(tmp_path, monkeypatch)
    before = set(state._slots)
    monkeypatch.setattr(chat_handlers, "voice_runtime_workspace_conflict", lambda path: "overlap")
    status, body = await _post(state, {"project": str(project)})
    assert (status, body.get("code")) == (400, "workspace_overlaps_data_home")
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_windows_share_is_refused_before_any_probe(tmp_path, monkeypatch):
    state = _turn_state(tmp_path, monkeypatch)
    before = set(state._slots)

    def no_probe(raw):
        raise AssertionError(f"probed {raw!r}")

    monkeypatch.setattr(chat_handlers, "probe_project_dir", no_probe)
    with monkeypatch.context() as windows:
        # Only the share screen reads it; the rest of the request runs as on this host.
        windows.setattr(os, "name", "nt")
        status, body = await _post(state, {"project": "//untrusted.example/share/ws"})
    assert (status, body.get("code")) == (400, "not_a_directory")
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_named_create_ignores_a_project_as_before(tmp_path, monkeypatch):
    state = _turn_state(tmp_path, monkeypatch)

    def no_probe(raw):
        raise AssertionError(f"probed {raw!r}")

    monkeypatch.setattr(chat_handlers, "probe_project_dir", no_probe)
    status, body = await _post(state, {"name": "named", "project": str(tmp_path / "missing")})
    assert status == 200, body
    assert state._slots["named"].project != str(tmp_path / "missing")

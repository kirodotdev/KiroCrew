"""Project-scope rules for the dashboard agent-switch handler.

``api_chat_slot_agent`` re-derives ``slot.project`` from the newly selected
agent's bindings, and that value becomes the next turn's subprocess cwd. Two
carve-outs keep the derivation from discarding a directory the user deliberately
chose:

* a slot filed into a project-linked sidebar folder keeps that folder's
  directory, so file search and tools stay in the folder's repo;
* an agent that resolves to the DEFAULT workspace (bound to none, or naming one
  absent from the config) leaves ``slot.project`` alone, so such a pick does not
  silently move the chat to the default workspace root.

These tests pin both carve-outs, plus the cases that must keep resetting.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_state

MOD = "kiro_crew.dashboard.chat_handlers"

_WORKSPACE_DEFAULT_DIR = "/workspace/default"
_WORKSPACE_DEV_DIR = "/workspace/dev"


def _stub_agent_resolution(
    monkeypatch, *, ws_name: str, workspace_dir: str, default_workspace: str = "default"
) -> None:
    """Make the switch resolve ``dev`` as a config alias bound to *ws_name*.

    *default_workspace* is set explicitly rather than left to the MagicMock: the
    handler compares ``ws_name`` against it, and an auto-attribute is never equal
    to anything, so a test would pass whether or not that comparison exists.
    """
    mock_cfg = MagicMock()
    # A config alias, so the project-scope carve-out above does not apply.
    mock_cfg.agents = {"dev": MagicMock(workspace=ws_name)}
    mock_cfg.default_workspace = default_workspace

    mock_bindings = MagicMock()
    mock_bindings.workspace_dir = Path(workspace_dir)
    mock_bindings.requested_resolved = True

    monkeypatch.setattr(f"{MOD}.KiroCrewConfig.load", lambda: mock_cfg)
    monkeypatch.setattr(
        f"{MOD}.resolve_agent_bindings", lambda cfg, name, project_dir=None: mock_bindings
    )
    monkeypatch.setattr(f"{MOD}._workspace_name_for_dir", lambda cfg, ws_dir: ws_name)
    monkeypatch.setattr(f"{MOD}.warm_project_agent_names", AsyncMock())
    monkeypatch.setattr(f"{MOD}.cached_project_agent_names", lambda project_dir: frozenset())
    monkeypatch.setattr(f"{MOD}.default_project_dir", lambda ws: workspace_dir)


class TestChatSlotAgentProjectScope:
    @pytest.mark.asyncio
    async def test_agent_switch_preserves_folder_project_dir(self, tmp_path, monkeypatch):
        """A slot in a project-linked folder keeps that folder's directory.

        Without this the agent pick retargets the slot at the new agent's
        workspace default, and the folder's repo silently stops being the cwd.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        folder_dir = tmp_path / "folder-repo"
        folder_dir.mkdir()
        state = _make_state(tmp_path)
        state._folders = [{"id": "f1", "name": "Repo", "project_dir": str(folder_dir)}]
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f1"
        slot.project = str(folder_dir)
        state.sessions.reset = AsyncMock()
        _stub_agent_resolution(monkeypatch, ws_name="dev-ws", workspace_dir=_WORKSPACE_DEV_DIR)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == str(
                folder_dir
            ), f"agent switch clobbered the folder project: {slot.project!r}"

    @pytest.mark.asyncio
    async def test_agent_switch_inherits_ancestor_folder_project_dir(self, tmp_path, monkeypatch):
        """The folder project is inherited from the nearest configured ancestor."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        folder_dir = tmp_path / "parent-repo"
        folder_dir.mkdir()
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "parent", "name": "Parent", "project_dir": str(folder_dir)},
            {"id": "child", "name": "Child", "parent_id": "parent"},
        ]
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "child"
        slot.project = str(folder_dir)
        state.sessions.reset = AsyncMock()
        _stub_agent_resolution(monkeypatch, ws_name="dev-ws", workspace_dir=_WORKSPACE_DEV_DIR)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == str(folder_dir)

    @pytest.mark.asyncio
    async def test_agent_switch_preserves_project_when_agent_resolves_to_default(
        self, tmp_path, monkeypatch
    ):
        """An agent resolving to the DEFAULT workspace leaves the project alone.

        ``_workspace_name_for_dir`` answers the literal "default" for an agent
        bound to no workspace and for one naming a workspace absent from the
        config, so without the gate every such pick discards the user's choice.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        picked = tmp_path / "picked"
        picked.mkdir()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = str(picked)
        state.sessions.reset = AsyncMock()
        _stub_agent_resolution(monkeypatch, ws_name="default", workspace_dir=_WORKSPACE_DEFAULT_DIR)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == str(
                picked
            ), f"default-resolving agent clobbered the project: {slot.project!r}"

    @pytest.mark.asyncio
    async def test_agent_switch_preserves_project_on_a_RENAMED_default_workspace(
        self, tmp_path, monkeypatch
    ):
        """The same fallback, spelled differently.

        On an install that renames its default workspace, the resolver falls back
        to that NAME rather than the literal "default", so a literal-only gate lets
        the fallback through and the project is clobbered on exactly the installs
        that configured a default.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        picked = tmp_path / "picked"
        picked.mkdir()
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = str(picked)
        state.sessions.reset = AsyncMock()
        _stub_agent_resolution(
            monkeypatch,
            ws_name="house-default",
            workspace_dir=_WORKSPACE_DEFAULT_DIR,
            default_workspace="house-default",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == str(
                picked
            ), f"renamed-default agent clobbered the project: {slot.project!r}"

    @pytest.mark.asyncio
    async def test_agent_switch_retargets_project_for_named_workspace(self, tmp_path, monkeypatch):
        """A folder-less slot still follows a NAMED workspace (control).

        The reset is deliberate for this case — file search must not stay scoped
        to the previous workspace — so the two carve-outs above must not widen
        into an unconditional preserve.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = "/old/project"
        state.sessions.reset = AsyncMock()
        _stub_agent_resolution(monkeypatch, ws_name="dev-ws", workspace_dir=_WORKSPACE_DEV_DIR)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == _WORKSPACE_DEV_DIR

    @pytest.mark.asyncio
    async def test_agent_switch_survives_unusable_folder_project(self, tmp_path, monkeypatch):
        """A folder whose project directory is gone must not block the switch.

        The create path answers 400 for an invalid folder project; doing that
        here would make the agent permanently unswitchable for any folder whose
        directory was moved or deleted, so the switch falls through instead.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._folders = [
            {"id": "f1", "name": "Gone", "project_dir": str(tmp_path / "deleted-repo")}
        ]
        slot = state.get_or_create_slot("s1")
        slot.folder_id = "f1"
        slot.project = "/old/project"
        state.sessions.reset = AsyncMock()
        _stub_agent_resolution(monkeypatch, ws_name="dev-ws", workspace_dir=_WORKSPACE_DEV_DIR)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == _WORKSPACE_DEV_DIR

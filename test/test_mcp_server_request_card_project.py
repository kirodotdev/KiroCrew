"""An ``mcp.server`` card with ``target`` ``project``: the chat folder's own MCP file.

The folder and the file come from the asking chat's live session, never from the
agent; Apply writes that one file through ``POST /api/mcp/project-file``. The
fixtures and request helpers are the main card suite's
(``test_mcp_server_request_card.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_mcp_server_request_card import (  # noqa: F401 -- ``env_fixture`` is ``env``
    ADD,
    ENTRY,
    _apply,
    _ask,
    _run,
    _stored,
    env_fixture,
)

from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE

_OPTED_IN = {"includeMcpJson": True}


def _live_chat(c, monkeypatch, folder: Path, backend: str, spec: object = _OPTED_IN) -> None:
    """The asking chat's live session: its folder, its harness and its agent's spec."""
    from kiro_crew.acp import session_mcp
    from kiro_crew.agent_sdk import capabilities

    provider = SimpleNamespace(cwd=str(folder), client=SimpleNamespace(_agent="crew"))
    c.server.app["state"].sessions = SimpleNamespace(get_provider=lambda sk: provider)
    monkeypatch.setattr(capabilities, "capabilities_of", lambda p: SimpleNamespace(backend=backend))
    from kiro_crew.acp.harness import kas

    def resolved(agent, work_dir):
        if (agent, str(work_dir)) != ("crew", str(folder)) or spec is None:
            raise OSError("no such agent")
        return spec

    monkeypatch.setattr(
        session_mcp, "agent_spec_snapshot", lambda agent, work_dir=None: resolved(agent, work_dir)
    )
    # KAS reads the spec its own projection hands the engine.
    monkeypatch.setattr(
        kas, "resolve_projected_spec", lambda d, agent, work_dir: (resolved(agent, work_dir), d)
    )


@pytest.mark.parametrize("backend", ["", "kas"])
def test_a_project_add_writes_only_the_chat_folders_own_file(env, tmp_path, monkeypatch, backend):
    file = KIRO_PROJECT_MCP_FILE
    store = CardStore(tmp_path / "c.json")
    folder = tmp_path / "project"
    folder.mkdir()

    async def go(c):
        _live_chat(c, monkeypatch, folder, backend)
        status, card = await _ask(c, {**ADD, "target": "project"})
        assert status == 200, card
        return card, await _apply(c, card)

    card, statuses = _run(store, env, go)
    rows = {r["label"]: r for r in card["changes"]}
    assert (rows["Folder"]["after"], rows["File"]["after"]) == (str(folder), file)
    assert card["params"]["project_folder"] == str(folder)
    assert all(s == 200 for s in statuses), statuses
    assert json.loads((folder / file).read_text(encoding="utf-8")) == {
        "mcpServers": {"weather": ENTRY}
    }
    # Nothing reaches the MCP page or kiro-cli's own list.
    assert not env["path"].exists() or "weather" not in _stored(env)
    assert not (tmp_path / "kiro.mcp.json").exists()


def test_a_project_add_for_a_name_the_file_has_raises_no_card(env, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / ".kiro" / "settings").mkdir(parents=True)
    (folder / ".kiro" / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"weather": {}}}), "utf-8"
    )

    async def go(c):
        _live_chat(c, monkeypatch, folder, "kas")
        return await _ask(c, {**ADD, "target": "project"})

    status, out = _run(store, env, go)
    assert status == 400 and out["code"] == "already_exists", out
    assert store.pending(None) == []


def test_a_project_add_with_no_live_session_raises_no_card(env, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        c.server.app["state"].sessions = SimpleNamespace(get_provider=lambda sk: None)
        return await _ask(c, {**ADD, "target": "project"})

    status, out = _run(store, env, go)
    assert status == 409 and out["code"] == "no_chat_folder", out
    assert store.pending(None) == []


def test_an_agent_cannot_name_the_project_folder_itself(env, tmp_path, monkeypatch):
    # The folder comes from the live session; a folder in the agent's body is dropped.
    store = CardStore(tmp_path / "c.json")
    folder = tmp_path / "project"
    folder.mkdir()

    async def go(c):
        _live_chat(c, monkeypatch, folder, "kas")
        body = {**ADD, "target": "project", "project_folder": str(tmp_path / "other")}
        return await _ask(c, body)

    status, out = _run(store, env, go)
    assert status == 400, out
    assert store.pending(None) == []


@pytest.mark.parametrize("backend", ["", "kas"])
@pytest.mark.parametrize("spec", [{}, {"includeMcpJson": False}, None])
def test_a_kiro_chat_whose_agent_skips_project_files_raises_no_card(
    env, tmp_path, monkeypatch, backend, spec
):
    # The default agent leaves includeMcpJson off, and an unreadable spec says nothing:
    # a server written to .kiro/settings/mcp.json would never start in either chat.
    store = CardStore(tmp_path / "c.json")
    folder = tmp_path / "project"
    folder.mkdir()

    async def go(c):
        _live_chat(c, monkeypatch, folder, backend, spec=spec)
        return await _ask(c, {**ADD, "target": "project"})

    status, out = _run(store, env, go)
    assert status == 409 and out["code"] == "spec_skips_project_file", out
    assert "use target 'crew'" in out["error"]
    assert store.pending(None) == []
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode", "copilot"])
def test_a_harness_crew_cannot_confirm_loads_the_file_raises_no_card(
    env, tmp_path, monkeypatch, backend
):
    # Picked by positive identity: only the Kiro family gets a card. Claude Code
    # skips .mcp.json under strictMcpConfig (a Bot's worker) or without the project
    # setting source; Copilot refuses to start in a folder holding .mcp.json.
    store = CardStore(tmp_path / "c.json")
    folder = tmp_path / "project"
    folder.mkdir()

    async def go(c):
        _live_chat(c, monkeypatch, folder, backend)
        return await _ask(c, {**ADD, "target": "project"})

    status, out = _run(store, env, go)
    assert status == 409 and out["code"] == "harness_not_supported", out
    assert store.pending(None) == []
    assert list(folder.iterdir()) == []

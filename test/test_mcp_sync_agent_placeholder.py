"""The MCP page's enable paths never grant a server only a placeholder names.

Turning one tool off on the MCP page writes ``{"disabledTools": [...]}`` into
``~/.kiro/settings/mcp.json`` for a server another file supplies. Enabling that
entry (one server, or Enable all) mounts a server the spec already holds and grants
it nothing; a server the Kiro Crew store installs under the same name is granted as
before, and a grant the spec already holds is left as it is.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers.mcp as mcp_mod

_GRANTED = {"command": "slack-mcp"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def placeholder_env(tmp_path: Path):
    """A global holding the placeholder for ``notes-mcp`` beside an installed ``slack-mcp``."""
    agent_cfg = tmp_path / "kirocrew.json"
    mcp_json = tmp_path / "mcp.json"
    agent_cfg.write_text(json.dumps({"mcpServers": {}, "tools": [], "allowedTools": []}))
    mcp_json.write_text(
        json.dumps(
            {"mcpServers": {"notes-mcp": {"disabledTools": ["write_note"]}, "slack-mcp": _GRANTED}}
        )
    )
    with (
        patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json),
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg
        ),
    ):
        yield agent_cfg


def _mount_notes(agent_cfg: Path, *, granted: bool = False) -> None:
    """The spec already holds ``notes-mcp``, as it does an edition server."""
    cfg = _load(agent_cfg)
    cfg["mcpServers"]["notes-mcp"] = {"command": "notes-mcp"}
    if granted:
        cfg["allowedTools"].append("@notes-mcp")
    agent_cfg.write_text(json.dumps(cfg))


@pytest.mark.parametrize("mounted", [False, True], ids=["unmounted", "mounted"])
def test_enabling_one_server_never_grants_a_placeholder(placeholder_env, mounted):
    """No entry is copied from the placeholder, and a held server is mounted only."""
    if mounted:
        _mount_notes(placeholder_env)

    mcp_mod._sync_mcp_to_agent("notes-mcp", enabled=True)
    cfg = _load(placeholder_env)
    assert ("notes-mcp" in cfg["mcpServers"]) is mounted
    assert ("@notes-mcp" in cfg["tools"]) is mounted
    assert "@notes-mcp" not in cfg["allowedTools"]


@pytest.mark.parametrize("mounted", [False, True], ids=["unmounted", "mounted"])
def test_enable_all_never_grants_a_placeholder_and_grants_an_installed_server(
    placeholder_env, mounted
):
    if mounted:
        _mount_notes(placeholder_env)

    mcp_mod._sync_mcp_to_agent_batch(["notes-mcp", "slack-mcp"], enabled=True)
    cfg = _load(placeholder_env)
    assert ("@notes-mcp" in cfg["tools"]) is mounted
    assert "@notes-mcp" not in cfg["allowedTools"]
    assert cfg["mcpServers"]["slack-mcp"] == _GRANTED
    assert "@slack-mcp" in cfg["allowedTools"]


def test_enabling_grants_a_server_the_store_installs_beside_a_placeholder(
    placeholder_env, tmp_path, monkeypatch
):
    _mount_notes(placeholder_env)
    store = tmp_path / "crew" / "mcp.json"
    store.parent.mkdir()
    store.write_text(json.dumps({"mcpServers": {"notes-mcp": {"command": "notes-mcp"}}}))
    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", store)

    mcp_mod._sync_mcp_to_agent("notes-mcp", enabled=True)
    assert "@notes-mcp" in _load(placeholder_env)["allowedTools"]


@pytest.mark.parametrize("batch", [False, True], ids=["one", "all"])
def test_enabling_keeps_a_grant_the_spec_already_holds(placeholder_env, batch):
    """The grant may be the user's own or another tool's; enabling never withdraws it."""
    _mount_notes(placeholder_env, granted=True)

    if batch:
        mcp_mod._sync_mcp_to_agent_batch(["notes-mcp"], enabled=True)
    else:
        mcp_mod._sync_mcp_to_agent("notes-mcp", enabled=True)
    assert _load(placeholder_env)["allowedTools"] == ["@notes-mcp"]


@pytest.mark.parametrize("batch", [False, True], ids=["one", "all"])
def test_a_slash_named_placeholder_is_read_under_its_listed_alias(tmp_path, batch):
    """The page lists ``ns/notes`` as ``ns-notes`` and enables it by that name; the
    placeholder is found through the one key the row stands for, so no grant."""
    agent_cfg = tmp_path / "kirocrew.json"
    mcp_json = tmp_path / "mcp.json"
    agent_cfg.write_text(
        json.dumps(
            {"mcpServers": {"ns-notes": {"command": "notes"}}, "tools": [], "allowedTools": []}
        )
    )
    mcp_json.write_text(json.dumps({"mcpServers": {"ns/notes": {"disabledTools": ["write_note"]}}}))
    with (
        patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json),
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg
        ),
    ):
        if batch:
            mcp_mod._sync_mcp_to_agent_batch(["ns-notes"], enabled=True)
        else:
            mcp_mod._sync_mcp_to_agent("ns-notes", enabled=True)
    cfg = _load(agent_cfg)
    assert "@ns-notes" in cfg["tools"]
    assert "@ns-notes" not in cfg["allowedTools"]

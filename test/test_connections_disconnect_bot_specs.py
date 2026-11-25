"""A Bot's own MCP entry keeps the sign-in a Disconnect elsewhere would delete.

A Bot's file (``~/.kiro/mates/agents/<id>.json``) adds MCP servers to its
workers, which run on KAS and read a remote server's sign-in from Crew's
gateway-wide grant vault by url. Disconnecting the same provider on the
Connections page must therefore count that file as a sharer, exactly as it
counts a hand-written agent spec, or the Bot's workers lose a sign-in nobody
asked to remove.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import types

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import mcp_discovery, mcp_grant, mcp_grant_kas
from kiro_crew.acp import kas_secret_storage as kss
from kiro_crew.config import paths as connections_paths
from kiro_crew.config.paths import data_home
from kiro_crew.connections import mint, ownership, warm
from kiro_crew.connections.registry import get_provider
from kiro_crew.dashboard.handlers import mcp as mcp_handlers
from kiro_crew.secrets import SecretVault


def _notion_url() -> str:
    provider = get_provider("notion")
    assert provider is not None, "registry no longer ships the fixture provider"
    return str(provider["mcp_url"])


def _sources(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Point every census source at ``tmp_path``; return the Bot folder."""
    agents = tmp_path / "agents"
    agents.mkdir()
    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _notion_url()}}}), encoding="utf-8"
    )
    monkeypatch.setattr(mcp_discovery, "_MCP_SOURCES", ((tmp_path / "mcp.json", "kirocrew"),))
    monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", list)
    monkeypatch.setattr(connections_paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(connections_paths, "_mate_agents_dir_override", lambda: mates)
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", list)
    return mates


def _write_bot(mates: pathlib.Path, url: str) -> None:
    (mates / "research-bot.json").write_text(
        json.dumps({"name": "research-bot", "mcpServers": {"notion-research": {"url": url}}}),
        encoding="utf-8",
    )


def test_the_census_reads_a_bots_own_file_as_a_sharer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    mates = _sources(monkeypatch, tmp_path)
    _write_bot(mates, _notion_url())

    specs, unreadable = ownership.spec_census()

    assert unreadable == ()
    [label] = [label for label in specs if label.endswith("research-bot.json")]
    assert label.startswith("agent:"), "a Bot's file is the user's, never a mirror or scope"
    assert specs[label] == {"notion-research": {"url": _notion_url()}}


def test_an_unreadable_bot_folder_makes_the_census_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    mates = _sources(monkeypatch, tmp_path)
    real_listdir = ownership.os.listdir

    def _deny(path: object) -> list[str]:
        if pathlib.Path(str(path)) == mates:
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(ownership.os, "listdir", _deny)

    _specs, unreadable = ownership.spec_census()

    assert unreadable, "an unlistable Bot folder read as one with no Bots"


@contextlib.asynccontextmanager
async def _no_lock():
    yield


@pytest.mark.asyncio
async def test_disconnect_keeps_the_sign_in_a_bots_workers_still_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    url = _notion_url()
    mates = _sources(monkeypatch, tmp_path)
    _write_bot(mates, url)
    store = kss.McpGrantStore(data_home())
    for name in mcp_grant_kas.kas_saved_sign_in_names(url):
        await store.store(name, "{}")
    revoked: list[str] = []

    async def _no_mint(_slug: str, _token: object) -> bool:
        return False

    monkeypatch.setattr(mint, "cancel_mint", _no_mint)
    monkeypatch.setattr(
        mcp_discovery, "list_servers", lambda: [types.SimpleNamespace(name="notion", url=url)]
    )
    monkeypatch.setattr(mcp_handlers, "_get_mcp_lock", _no_lock)
    monkeypatch.setattr(mcp_handlers, "_purge_server_config", lambda _n, *, scopes=None: {})
    monkeypatch.setattr(
        agent_mod, "rebuild_agent_config_reporting", lambda: (pathlib.Path("/dev/null"), True)
    )
    monkeypatch.setattr(warm, "rearm_invalidated_provider", lambda _slug: None)
    monkeypatch.setattr(mcp_grant, "revoke_local_grant", lambda u: revoked.append(u) or [])

    scope = await ownership.remove_provider_entry("notion", url, ())

    root = mcp_grant_kas.grant_store_root(data_home())
    names = await asyncio.to_thread(lambda: set(SecretVault(root).list_names()))
    assert set(mcp_grant_kas.kas_saved_sign_in_names(url)) <= names, scope
    assert scope.kas_sign_in_urls == (), scope
    assert revoked == [], "revoked kiro-cli's pair a Bot's entry still names"
    assert "notion-research" in scope.grant_shared_with, scope
    assert scope.entry_removed is True

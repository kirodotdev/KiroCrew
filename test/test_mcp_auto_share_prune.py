"""A server deleted from an MCP file by hand leaves the rendered agent spec.

The agent spec rebuild merges onto the existing ``kirocrew.json``, so without this
a hand delete leaves every chat starting a server no file declares any more. The
watch holds what the files declared in memory, never in a file an agent can edit.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.mcp_gateway import auto_share

#: Opts this module out of the conftest guard that turns the pass off.
AUTO_SHARE = True


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An agents dir and the two MCP files, all under *tmp_path*."""
    import kiro_crew.agent as agent_facade
    import kiro_crew.config.paths as paths

    crew = tmp_path / "crew"
    agents = tmp_path / "kiro" / "agents"
    settings = tmp_path / "kiro" / "settings" / "mcp.json"
    for folder in (crew, agents, settings.parent):
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_facade, "user_mcp_files", lambda: [crew / "mcp.json", settings])
    return {"crew": crew, "agents": agents, "settings": settings}


def _servers(path, names):
    path.write_text(
        json.dumps({"mcpServers": {n: {"command": "uvx", "args": [n]} for n in names}}),
        encoding="utf-8",
    )


def _spec(home) -> set[str]:
    raw = json.loads((home["agents"] / "kirocrew.json").read_text(encoding="utf-8"))
    return set(raw["mcpServers"])


def test_a_server_deleted_from_mcp_json_leaves_the_spec(home):
    _servers(home["settings"], ["time", "memory", "fetch"])
    _servers(home["agents"] / "kirocrew.json", ["time", "memory", "fetch", "own-entry"])
    seen = auto_share.source_server_names()

    _servers(home["settings"], ["time"])  # The person deletes two by hand.

    assert auto_share.prune_removed_sources(seen) == (frozenset({"time"}), frozenset())
    # An entry no MCP file declared is not the prune's to remove.
    assert _spec(home) == {"time", "own-entry"}


def test_a_raw_slash_key_is_removed_by_the_alias_it_mounts_as(home):
    _servers(home["settings"], ["npm:@playwright/mcp"])
    _servers(home["agents"] / "kirocrew.json", ["playwright-mcp"])
    seen = auto_share.source_server_names()

    _servers(home["settings"], [])
    auto_share.prune_removed_sources(seen)

    assert _spec(home) == set()


def test_a_server_still_declared_in_the_other_file_stays(home):
    _servers(home["settings"], ["time"])
    _servers(home["crew"] / "mcp.json", ["time"])
    _servers(home["agents"] / "kirocrew.json", ["time"])
    seen = auto_share.source_server_names()

    _servers(home["settings"], [])

    assert auto_share.prune_removed_sources(seen)[1] == frozenset()
    assert _spec(home) == {"time"}


def test_a_spec_that_cannot_be_read_keeps_the_deletion_pending(home, monkeypatch):
    _servers(home["settings"], ["time", "memory"])
    _servers(home["agents"] / "kirocrew.json", ["time", "memory"])
    seen = auto_share.source_server_names()
    _servers(home["settings"], ["time"])
    spec = home["agents"] / "kirocrew.json"
    real_read = type(spec).read_text
    held = {"once": True}

    def _held(self, *args, **kwargs):
        if self == spec and held.pop("once", False):
            raise PermissionError(13, "held by another writer")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(type(spec), "read_text", _held)
    now, pending = auto_share.prune_removed_sources(seen)
    assert pending == frozenset({"memory"})

    # The next look finishes it with no further edit to the MCP files.
    assert auto_share.prune_removed_sources(now, pending) == (frozenset({"time"}), frozenset())
    assert _spec(home) == {"time"}


def test_an_unreadable_mcp_file_removes_nothing(home):
    _servers(home["settings"], ["time", "memory"])
    _servers(home["agents"] / "kirocrew.json", ["time", "memory"])
    seen = auto_share.source_server_names()

    home["settings"].write_text("{not json", encoding="utf-8")

    assert auto_share.prune_removed_sources(seen) == (seen, frozenset())
    assert _spec(home) == {"time", "memory"}

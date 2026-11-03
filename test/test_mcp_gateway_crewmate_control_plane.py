"""A crewmate agent's spec declares Crew's control plane, and the overlay never sees it.

The MCP-gateway rewriter walks only the user-level agents folder (``~/.kiro/agents``), so
a crewmate's custom agent in ``<kiro home>/mates/agents/`` has no overlay of its own. Its
spec still declares ``kirocrew-core`` and ``kirocrew-cron``; with no stub for them the
mirror mounts the raw ``kirocrew mcp-core`` child inside the harness. On an enforced
harness whose credential mask hides ``<crew>/trust`` and ``<crew>/run`` (deepseek, pi,
codex, goose, opencode, copilot) that child can read neither the SEL HMAC key nor the
gateway secret, so ``tools/list`` fails and the crewmate's session never starts.

The fix serves a crewmate agent's control plane from the canonical ``kirocrew`` overlay's
broker stubs. These tests pin what it may and may not borrow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER
from kiro_crew.mcp_gateway.session_servers import (
    injection_server_names,
    pooled_session_servers,
)

MATE = "kirocrew-mate-conductor"


def _stub(server: str) -> dict:
    return {
        _WRAPPER_MARKER: True,
        "command": "/venv/bin/python",
        "args": ["-m", "kiro_crew.mcp_gateway.stub", f"--server={server}"],
        "env": {},
    }


def _write_spec(folder: Path, agent: str, servers: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{agent}.json"
    path.write_text(json.dumps({"name": agent, "mcpServers": servers}), encoding="utf-8")
    return path


@pytest.fixture
def mates_dir(tmp_path, monkeypatch):
    import kiro_crew.config.paths as paths

    d = tmp_path / "mates" / "agents"
    d.mkdir(parents=True)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: d)
    return d


@pytest.fixture
def overlay(tmp_path):
    """The canonical overlay: both control-plane servers stubbed, plus a pooled vendor."""
    d = tmp_path / "overlay" / "agents"
    _write_spec(
        d,
        "kirocrew",
        {
            "kirocrew-core": _stub("kirocrew-core"),
            "kirocrew-cron": _stub("kirocrew-cron"),
            "builder-mcp": _stub("builder-mcp"),
        },
    )
    return d


def _declare(mates_dir: Path, *servers: str, agent: str = MATE) -> None:
    _write_spec(
        mates_dir, agent, {s: {"command": "/opt/kirocrew", "args": ["mcp"]} for s in servers}
    )


def test_crewmate_gets_the_canonical_control_plane_stubs(mates_dir, overlay):
    _declare(mates_dir, "kirocrew-core", "kirocrew-cron")
    names = [e["name"] for e in pooled_session_servers(overlay, MATE)]
    assert names == ["kirocrew-core", "kirocrew-cron"]
    assert injection_server_names(overlay, MATE) == frozenset({"kirocrew-core", "kirocrew-cron"})


def test_both_entry_points_agree(mates_dir, overlay):
    """The array and the withhold set read one overlay, so a stub is never half-applied."""
    _declare(mates_dir, "kirocrew-core")
    pooled = {e["name"] for e in pooled_session_servers(overlay, MATE)}
    assert pooled == set(injection_server_names(overlay, MATE)) == {"kirocrew-core"}


def test_only_servers_the_crewmate_declares_are_served(mates_dir, overlay):
    _declare(mates_dir, "kirocrew-cron")
    assert [e["name"] for e in pooled_session_servers(overlay, MATE)] == ["kirocrew-cron"]


def test_a_non_control_plane_stub_is_never_borrowed(mates_dir, overlay):
    """A vendor's broker stub belongs to the agent that declared it, never a crewmate."""
    _declare(mates_dir, "kirocrew-core", "builder-mcp")
    assert "builder-mcp" not in {e["name"] for e in pooled_session_servers(overlay, MATE)}
    assert "builder-mcp" not in injection_server_names(overlay, MATE)


def test_an_unwrapped_canonical_entry_is_not_served(mates_dir, tmp_path):
    """Without the server in ``mcp_gateway.stub_servers`` there is no stub to borrow."""
    d = tmp_path / "overlay" / "agents"
    _write_spec(
        d, "kirocrew", {"kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]}}
    )
    _declare(mates_dir, "kirocrew-core")
    assert pooled_session_servers(d, MATE) == []
    assert injection_server_names(d, MATE) == frozenset()


def test_an_agent_outside_the_mates_folder_is_unchanged(mates_dir, overlay):
    assert pooled_session_servers(overlay, "some-other-agent") == []
    assert injection_server_names(overlay, "some-other-agent") == frozenset()


def test_a_crewmate_with_its_own_overlay_uses_it(mates_dir, overlay):
    _declare(mates_dir, "kirocrew-core", "kirocrew-cron")
    _write_spec(overlay, MATE, {"kirocrew-core": _stub("kirocrew-core")})
    assert [e["name"] for e in pooled_session_servers(overlay, MATE)] == ["kirocrew-core"]


def test_the_canonical_agent_is_never_its_own_fallback(mates_dir, tmp_path):
    empty = tmp_path / "overlay" / "agents"
    empty.mkdir(parents=True)
    _declare(mates_dir, "kirocrew-core", agent="kirocrew")
    assert pooled_session_servers(empty, "kirocrew") == []


def test_a_project_shadowed_crewmate_gets_nothing(mates_dir, overlay, tmp_path):
    """The project-scope guard ``_load_overlay_for_agent`` applies holds for the fallback."""
    _declare(mates_dir, "kirocrew-core")
    work = tmp_path / "work"
    _write_spec(work / ".kiro" / "agents", MATE, {"kirocrew-core": {"command": "x"}})
    assert pooled_session_servers(overlay, MATE, work_dir=work) == []
    assert injection_server_names(overlay, MATE, work_dir=work) == frozenset()


@pytest.mark.parametrize("body", ["{not json", "[]", '{"mcpServers": []}'])
def test_a_malformed_crewmate_spec_degrades_to_unpooled(mates_dir, overlay, body):
    (mates_dir / f"{MATE}.json").write_text(body, encoding="utf-8")
    assert pooled_session_servers(overlay, MATE) == []
    assert injection_server_names(overlay, MATE) == frozenset()

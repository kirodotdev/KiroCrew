"""The ``mcp.json`` under ``KIRO_HOME`` is named for exclusion on a KAS agent block.

KAS reads its global ``mcp.json`` under ``KIRO_HOME``. Kiro Crew's own global
path (``agent._KIRO_MCP_JSON``) does not follow that variable, so where the two
differ a not-opted-in agent must still be told to leave the engine's servers out.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.kas_settings_mcp import finish_settings_mcp, stamp_settings_mcp
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.config import paths as paths_mod
from kiro_crew.mcp_gateway import session_servers as session_servers_mod


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def crew_global(tmp_path, monkeypatch):
    """Kiro Crew's own global file, as a default install resolves it."""
    path = tmp_path / "home" / ".kiro" / "settings" / "mcp.json"
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", path)
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    return path


@pytest.fixture
def kiro_home(tmp_path, monkeypatch):
    """A ``KIRO_HOME`` that is another folder than Kiro Crew's global one."""
    home = tmp_path / "kiro-home"
    home.mkdir()
    monkeypatch.setenv("KIRO_HOME", str(home))
    return home


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def _finish(work, **stamp):
    agent = {"id": "a", "tools": ["*"], "mcpServers": {"own": {"command": "x"}}}
    (stamped,) = stamp_settings_mcp([agent], "a", work, **stamp)
    return finish_settings_mcp(stamped)


def test_a_server_only_in_the_kiro_home_file_is_excluded(crew_global, kiro_home, work):
    _write(crew_global, {"mcpServers": {"extra": {}}})
    _write(kiro_home / "settings" / "mcp.json", {"mcpServers": {"homeonly": {"command": "h"}}})
    assert _finish(work)["excludedTools"] == ["@extra", "@homeonly"]


def test_an_opted_in_agent_keeps_it_and_gets_its_glob_disabled_tools(crew_global, kiro_home, work):
    _write(
        kiro_home / "settings" / "mcp.json", {"mcpServers": {"homeonly": {"disabledTools": ["w*"]}}}
    )
    assert _finish(work, opted_in=True)["excludedTools"] == ["@homeonly/w*"]


def test_a_server_in_both_global_files_keeps_both_files_globs(crew_global, kiro_home, work):
    """Crew's global file holds the dashboard's tool switches; neither file lifts the other's."""
    _write(crew_global, {"mcpServers": {"s": {"disabledTools": ["a*"]}, "t": {}}})
    _write(
        kiro_home / "settings" / "mcp.json",
        {"mcpServers": {"s": {"disabledTools": ["b*", "a*"]}, "t": {"disabledTools": ["c*"]}}},
    )
    _write(
        work / ".kiro" / "settings" / "mcp.json", {"mcpServers": {"t": {"disabledTools": ["d*"]}}}
    )
    # ``t``: the workspace entry replaces a global one, as it does without ``KIRO_HOME``.
    assert _finish(work, opted_in=True)["excludedTools"] == ["@s/a*", "@s/b*", "@t/d*"]


@pytest.mark.parametrize(
    ("crew_entry", "home_entry", "expected"),
    [
        ({"disabledTools": ["a*"]}, {"command": "h"}, ["@s/a*"]),
        ({"command": "g"}, {"disabledTools": ["b*"]}, ["@s/b*"]),
        ({"disabledTools": ["a*"]}, {"disabled": True, "disabledTools": ["b*"]}, []),
        ({"disabled": True, "disabledTools": ["a*"]}, {"disabledTools": ["b*"]}, ["@s/b*"]),
        ({"disabledTools": ["a*"]}, "not an entry", ["@s/a*"]),
    ],
    ids=["only-crews", "only-kiro-homes", "kiro-home-muted", "crews-muted", "kiro-home-malformed"],
)
def test_a_shared_servers_globs_from_one_file_survive_the_other(
    crew_global, kiro_home, work, crew_entry, home_entry, expected
):
    """A muted entry adds no globs of its own, as on a default install, and removes none.

    A ``KIRO_HOME`` entry that is muted mutes the server: KAS reads that file, so
    it does not start the server and there are no tools to exclude.
    """
    _write(crew_global, {"mcpServers": {"s": crew_entry}})
    _write(kiro_home / "settings" / "mcp.json", {"mcpServers": {"s": home_entry}})
    assert _finish(work, opted_in=True).get("excludedTools", []) == expected


def test_a_shared_server_muted_only_in_the_kiro_home_file_stays_muted(crew_global, kiro_home, work):
    """The stamp follows the file KAS reads: no globs, and still excluded whole."""
    _write(crew_global, {"mcpServers": {"s": {"disabledTools": ["a*"]}}})
    _write(kiro_home / "settings" / "mcp.json", {"mcpServers": {"s": {"disabled": True}}})
    agent = {"id": "a", "tools": ["*"], "mcpServers": {"own": {"command": "x"}}}
    (stamped,) = stamp_settings_mcp([agent], "a", work)
    assert stamped["_crewSettingsMcp"]["fileServers"] == {"s": []}
    assert finish_settings_mcp(stamped)["excludedTools"] == ["@s"]


def test_a_mandatory_shared_server_keeps_crews_globs(crew_global, kiro_home, work, monkeypatch):
    """A not-opted-in agent cannot exclude a mandatory server, so its globs must travel."""
    monkeypatch.setenv("ASBX_KIRO_MANDATORY_MCPS", "s")
    _write(crew_global, {"mcpServers": {"s": {"disabledTools": ["a*"]}}})
    _write(kiro_home / "settings" / "mcp.json", {"mcpServers": {"s": {"command": "h"}}})
    assert _finish(work)["excludedTools"] == ["@s/a*"]


def test_without_kiro_home_only_crews_global_file_is_read(crew_global, tmp_path, work, monkeypatch):
    monkeypatch.delenv("KIRO_HOME", raising=False)
    _write(crew_global, {"mcpServers": {"extra": {}}})
    _write(tmp_path / "kiro-home" / "settings" / "mcp.json", {"mcpServers": {"homeonly": {}}})
    assert _finish(work)["excludedTools"] == ["@extra"]


def test_a_kiro_home_that_is_crews_own_folder_is_read_once(crew_global, work, monkeypatch):
    monkeypatch.setenv("KIRO_HOME", str(crew_global.parent.parent))
    _write(crew_global, {"mcpServers": {"extra": {}}})
    notices: list[str] = []
    assert _finish(work, notices=notices)["excludedTools"] == ["@extra"]
    assert notices == []


def test_a_looping_crew_global_file_keeps_the_kiro_home_exclusions(crew_global, kiro_home, work):
    crew_global.parent.mkdir(parents=True)
    crew_global.symlink_to(crew_global.name)
    _write(kiro_home / "settings" / "mcp.json", {"mcpServers": {"homeonly": {}}})
    notices: list[str] = []

    assert _finish(work, notices=notices)["excludedTools"] == ["@homeonly"]
    (line,) = notices
    assert line.startswith("⚠ Kiro Crew could not read the global MCP settings (")
    assert str(crew_global) in line


def test_an_unreadable_kiro_home_file_is_named_and_the_session_still_starts(
    crew_global, kiro_home, work
):
    _write(crew_global, {"mcpServers": {"extra": {}}})
    (kiro_home / "settings").mkdir()
    (kiro_home / "settings" / "mcp.json").write_bytes(
        b"\xff\xfe" + json.dumps({"mcpServers": {"homeonly": {}}}).encode("utf-16-le")
    )
    notices: list[str] = []
    assert _finish(work, notices=notices)["excludedTools"] == ["@extra"]
    (line,) = notices
    assert line.startswith("⚠ Kiro Crew could not read the Kiro home's MCP settings (")
    assert "saved as UTF-16; re-save it as UTF-8" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("with_kiro_home", [True, False])
async def test_a_folder_agents_kas_session_carries_the_exclusion(
    monkeypatch, tmp_path, with_kiro_home
):
    """What ``session/new`` sends for a checkout's own agent, which never opts in."""
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent: frozenset()
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-global.json")
    monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", lambda *_a, **_kw: [{"id": "b"}])
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    home = tmp_path / "kiro-home"
    _write(home / "settings" / "mcp.json", {"mcpServers": {"homeonly": {"command": "h"}}})
    if with_kiro_home:
        monkeypatch.setenv("KIRO_HOME", str(home))
    else:
        monkeypatch.delenv("KIRO_HOME", raising=False)
    checkout = tmp_path / "checkout"
    spec = {"name": "b", "prompt": "p", "tools": ["*"]}
    _write(checkout / ".kiro" / "agents" / "b.json", spec)

    extras = await harness_for(ACP_BACKEND_KAS).session_extras("b", work_dir=str(checkout))
    params: dict = {"cwd": str(checkout), "mcpServers": []}
    attach_kas_custom_agents(params, extras.custom_agents)

    (sent,) = params["_meta"]["kiro"]["customAgents"]
    assert sent.get("excludedTools", []) == (["@homeonly"] if with_kiro_home else [])

"""What tells every open agent picker to re-read the catalog, now that the end of a turn does not.

Two writers nobody announced: a spec file written on disk (a turn's shell, an editor),
which the end of each turn checks for, and a config write to the keys the catalog
reads, which the config watcher sees from every writer. Both push ``agents``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from aiohttp import web

from kiro_crew import agent_discovery, members
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import agent_folders
from kiro_crew.dashboard.state import DashboardState


def test_a_spec_file_written_since_the_last_turn_end_is_announced_once(tmp_path, monkeypatch):
    user, mate, project = tmp_path / "agents", tmp_path / "mates", tmp_path / "proj"
    for folder in (user, mate, project / ".kiro" / "agents"):
        folder.mkdir(parents=True)
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mate)
    state = DashboardState.__new__(DashboardState)
    state._slots = {"c1": SimpleNamespace(project=str(project))}
    pushed: list[tuple[str, ...]] = []
    state.push_refresh = lambda *kinds: pushed.append(kinds)  # type: ignore[method-assign]

    def turn_end() -> list[tuple[str, ...]]:
        pushed.clear()
        asyncio.run(agent_folders.announce_moved(state))
        return list(pushed)

    assert turn_end() == [("agents",)], "folders seen for the first time count as moved"
    assert turn_end() == []
    (user / "new-agent.json").write_text("{}")
    assert turn_end() == [("agents",)]
    assert turn_end() == []
    (project / ".kiro" / "agents" / "project-agent.json").write_text("{}")
    assert turn_end() == [("agents",)]
    (user / "new-agent.json").unlink()
    assert turn_end() == [("agents",)]
    assert turn_end() == []


def test_a_crewmate_rewriting_its_own_notes_file_is_announced(tmp_path, monkeypatch):
    user, mate = tmp_path / "agents", tmp_path / "mates"
    for folder in (user, mate):
        folder.mkdir()
    briefing = tmp_path / "ada-briefing.md"
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mate)
    monkeypatch.setattr(members, "member_briefing_path", lambda slug: briefing)
    state = DashboardState.__new__(DashboardState)
    state._slots = {"member-ada": SimpleNamespace(project=None)}
    pushed: list[tuple[str, ...]] = []
    state.push_refresh = lambda *kinds: pushed.append(kinds)  # type: ignore[method-assign]

    def turn_end() -> list[tuple[str, ...]]:
        pushed.clear()
        asyncio.run(agent_folders.announce_moved(state))
        return list(pushed)

    assert turn_end() == [("agents",)], "the first sight counts as moved"
    assert turn_end() == []
    briefing.write_text("Notes the crewmate wrote with its own file tool.")
    assert turn_end() == [("agents",)]
    assert turn_end() == []


def test_a_config_write_to_a_key_the_catalog_reads_is_announced():
    from kiro_crew.dashboard.server import _register_config_watch

    state = SimpleNamespace(
        workflow_service=None,
        channel_manager=None,
        push_slots_update=MagicMock(),
        push_refresh=MagicMock(),
        set_dynamic_cards_enabled=MagicMock(),
    )
    app = web.Application()
    _register_config_watch(app, state, None)
    subs = [s for s in app["config_watch_subscriptions"] if "agent.default_agent" in s.prefixes]
    assert subs, "no applier announces a config write the agent catalog reads"
    (sub,) = subs
    assert set(sub.prefixes) == {
        "agents",
        "agent.acp_backend",
        "agent.member_acp_backend",
        "agent.default_agent",
    }
    cfg = KiroCrewConfig()
    sub.callback()(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agents.ada.kiro_agent"})))
    state.push_refresh.assert_called_once_with("agents")

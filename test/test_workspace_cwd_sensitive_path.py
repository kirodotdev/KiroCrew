"""A cron fire and a Slack channel turn do not run in a sensitive or missing workspace folder.

Chat refuses a workspace folder that is missing or on the sensitive-path list
(``default_project_dir``). A scheduled run for a crew alias and a channel bound to a crew
record read the same folder from the workspace entry, so they hold it to the same check
and fall back to the base workspace folder, logging one line.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from kiro_crew.config import live, loader
from kiro_crew.config.loader import KiroCrewConfig, workspace_dir_from_entry
from kiro_crew.config.sections import KiroCrewAgentConfig, WorkspaceConfig


@pytest.fixture()
def crew_cfg(monkeypatch, tmp_path):
    """A config with one crew record on a workspace folder the sensitive-path list flags."""
    loader._invalidate_config_cache()
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = KiroCrewConfig.load()
    bad = tmp_path / "secrets"
    bad.mkdir()
    cfg.workspaces["bad"] = WorkspaceConfig(dir=str(bad))
    cfg.agents["bad-crew"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="bad")
    gone = tmp_path / "gone"
    cfg.workspaces["gone"] = WorkspaceConfig(dir=str(gone))
    cfg.agents["gone-crew"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="gone")
    ghost = tmp_path / "ghost" / "secrets"
    cfg.workspaces["ghost"] = WorkspaceConfig(dir=str(ghost))
    cfg.agents["ghost-crew"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="ghost")
    ok = tmp_path / "fine"
    ok.mkdir()
    cfg.workspaces["fine"] = WorkspaceConfig(dir=str(ok))
    cfg.agents["fine-crew"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="fine")
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr(
        "kiro_crew.security.is_sensitive_canonical_path",
        lambda resolved: resolved.endswith("secrets"),
    )
    return cfg, {"bad": bad, "ok": ok, "gone": gone}


def _cron_cwd(cfg, alias):
    from kiro_crew.slack import gateway  # wires the cron module's shared names

    gw = SimpleNamespace(_cfg=cfg)
    return gateway._cron_fire._resolve_cron_agent(gw, SimpleNamespace(), alias)[1]


def _base() -> str:
    return str(workspace_dir_from_entry(None))


def test_cron_fire_falls_back_when_the_alias_workspace_is_sensitive(crew_cfg, caplog):
    cfg, _ = crew_cfg
    with caplog.at_level(logging.WARNING):
        assert _cron_cwd(cfg, "bad-crew") == _base()
    assert len([r for r in caplog.records if "workspace" in r.getMessage()]) == 1


def test_cron_fire_keeps_a_missing_alias_workspace(crew_cfg):
    """A folder that does not exist yet is not refused at fire time; it is created on use."""
    cfg, dirs = crew_cfg
    assert _cron_cwd(cfg, "gone-crew") == str(dirs["gone"])


def test_cron_fire_refuses_a_sensitive_alias_workspace_even_when_it_is_missing(crew_cfg):
    cfg, _ = crew_cfg
    assert _cron_cwd(cfg, "ghost-crew") == _base()


def test_cron_fire_keeps_an_existing_ordinary_workspace(crew_cfg):
    cfg, dirs = crew_cfg
    assert _cron_cwd(cfg, "fine-crew") == str(dirs["ok"])


def test_a_channel_bound_crew_falls_back_when_its_workspace_is_sensitive(crew_cfg, caplog):
    from kiro_crew.slack.channel_crew import resolve_channel_crew

    with caplog.at_level(logging.WARNING):
        crew = resolve_channel_crew("bad-crew")
    assert crew is not None and crew.cwd == _base()
    assert len([r for r in caplog.records if "workspace" in r.getMessage()]) == 1


def test_a_channel_bound_crew_falls_back_when_its_workspace_is_missing(crew_cfg):
    from kiro_crew.slack.channel_crew import resolve_channel_crew

    crew = resolve_channel_crew("gone-crew")
    assert crew is not None and crew.cwd == _base()


def test_a_channel_bound_crew_keeps_an_existing_ordinary_workspace(crew_cfg):
    from kiro_crew.slack.channel_crew import resolve_channel_crew

    cfg, dirs = crew_cfg
    crew = resolve_channel_crew("fine-crew")
    assert crew is not None and crew.cwd == str(dirs["ok"])

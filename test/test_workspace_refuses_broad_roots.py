"""A workspace folder that is a broad root is not used at cron fire or in a channel turn.

``usable_workspace_dir_from_entry`` refuses, by exact match on the real path, the
filesystem root, the owner's home folder and the data-home root, and falls back to the
base workspace folder. A folder below them stays usable. An archive config whose
workspace folder is the data-home root is refused too. The sensitive-path check is
stubbed off so only the broad-root rule can decide.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

from kiro_crew.config import live, loader
from kiro_crew.config.loader import KiroCrewConfig, workspace_dir_from_entry
from kiro_crew.config.paths import config_dir
from kiro_crew.config.sections import KiroCrewAgentConfig, WorkspaceConfig


@pytest.fixture()
def crews(monkeypatch, tmp_path):
    """One crew per workspace folder: the root, the home, the data home, and a home child."""
    loader._invalidate_config_cache()
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    child = owner_home / "proj"
    child.mkdir()
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setenv("USERPROFILE", str(owner_home))
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    cfg = KiroCrewConfig.load()
    dirs = {
        "root": os.path.abspath(os.sep),
        "home": str(owner_home),
        "data": str(config_dir()),
        "child": str(child),
    }
    for key, folder in dirs.items():
        cfg.workspaces[key] = WorkspaceConfig(dir=folder)
        cfg.agents[f"{key}-crew"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace=key)
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr("kiro_crew.security.is_sensitive_canonical_path", lambda resolved: False)
    return cfg, dirs


def _cron_cwd(cfg, alias):
    from kiro_crew.slack import gateway  # wires the cron module's shared names

    gw = SimpleNamespace(_cfg=cfg)
    return gateway._cron_fire._resolve_cron_agent(gw, SimpleNamespace(), alias)[1]


def _channel_cwd(alias):
    from kiro_crew.slack.channel_crew import resolve_channel_crew

    crew = resolve_channel_crew(alias)
    assert crew is not None
    return crew.cwd


def _base() -> str:
    return str(workspace_dir_from_entry(None))


@pytest.mark.parametrize("key", ["root", "home", "data"])
def test_cron_fire_falls_back_when_the_workspace_is_a_broad_root(crews, caplog, key):
    cfg, _ = crews
    with caplog.at_level(logging.WARNING):
        assert _cron_cwd(cfg, f"{key}-crew") == _base()
    assert len([r for r in caplog.records if "workspace" in r.getMessage()]) == 1


@pytest.mark.parametrize("key", ["root", "home", "data"])
def test_a_channel_bound_crew_falls_back_when_the_workspace_is_a_broad_root(crews, caplog, key):
    with caplog.at_level(logging.WARNING):
        assert _channel_cwd(f"{key}-crew") == _base()
    assert len([r for r in caplog.records if "workspace" in r.getMessage()]) == 1


def test_a_child_folder_of_the_home_is_still_used(crews):
    cfg, dirs = crews
    assert _cron_cwd(cfg, "child-crew") == dirs["child"]
    assert _channel_cwd("child-crew") == dirs["child"]


def test_an_archive_config_with_the_data_home_as_workspace_folder_is_refused(crews):
    from kiro_crew.archive_config_vetting import config_document_refusal

    _, dirs = crews
    refusal = config_document_refusal({"workspaces": {"evil": {"dir": dirs["data"]}}})
    assert refusal is not None and "evil" in refusal
    assert dirs["data"] not in refusal


def test_an_archive_config_with_an_unresolvable_workspace_folder_does_not_raise(crews):
    from kiro_crew.archive_config_vetting import config_document_refusal

    # main's root check refuses an embedded NUL first; the point is a refusal, not a raise.
    assert config_document_refusal({"workspaces": {"bad": {"dir": "workspace\0"}}}) is not None


def test_an_archive_config_with_a_folder_below_the_data_home_is_kept(crews):
    from kiro_crew.archive_config_vetting import config_document_refusal

    assert config_document_refusal({"workspaces": {"ok": {"dir": "workspace"}}}) is None


@pytest.mark.parametrize("folder", [".", "./", "workspace/..", "workspace/../", "a/../b/.."])
def test_an_archive_config_with_a_relative_data_home_folder_is_refused(crews, folder):
    from kiro_crew.archive_config_vetting import config_document_refusal

    # A relative folder is anchored at the data home, so these name the data home itself.
    refusal = config_document_refusal({"workspaces": {"rel": {"dir": folder}}})
    assert refusal is not None and "rel" in refusal and "data home" in refusal


@pytest.mark.parametrize("folder", ["proj", "workspace/proj", "./proj", "workspace/a/../b"])
def test_an_archive_config_with_a_relative_sub_folder_is_kept(crews, folder):
    from kiro_crew.archive_config_vetting import config_document_refusal

    assert config_document_refusal({"workspaces": {"ok": {"dir": folder}}}) is None

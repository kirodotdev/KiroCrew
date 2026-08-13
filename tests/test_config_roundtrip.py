"""Tests that KiroCrewConfig.save() preserves all dataclass fields.

Regression test for the bug where to_dict() omitted secretary,
taskrunner, orchestrator, skills, and tunnel — causing save() to
silently drop them from config.json.
"""

from __future__ import annotations

import json
from dataclasses import fields
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    """Redirect config_path() to a temp file for isolation."""
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def test_to_dict_includes_all_dataclass_fields():
    """Every field on KiroCrewConfig must appear in to_dict() output."""
    cfg = KiroCrewConfig()
    d = cfg.to_dict()
    # Fields that are serialized under a different key or merged into slack
    SPECIAL = {"slack_channels", "slack_dm_activation", "observe_max_messages", "observe_ttl_hours", "_extra_sections"}
    for f in fields(KiroCrewConfig):
        if f.name in SPECIAL:
            continue
        assert f.name in d, f"to_dict() missing field: {f.name}"


def test_save_load_roundtrip_secretary(cfg_file):
    """Secretary config must survive a save/load cycle."""
    cfg = KiroCrewConfig()
    cfg.secretary.enabled = True
    cfg.secretary.poll_interval_seconds = 30
    cfg.secretary.alert_keywords = ["sev", "outage"]
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["secretary"]["enabled"] is True
    assert raw["secretary"]["poll_interval_seconds"] == 30
    assert raw["secretary"]["alert_keywords"] == ["sev", "outage"]


def test_save_load_roundtrip_secretary_retention(cfg_file):
    """Secretary retention fields must survive a save/LOAD cycle.

    Regression for the bug where the UI saved dm_retention_days /
    channel_retention_days / auto_cleanup_enabled to config.json, but
    KiroCrewConfig.load() never read them back into SecretaryConfig —
    so the settings appeared to "not save" and cleanup used stale defaults.
    """
    cfg = KiroCrewConfig()
    cfg.secretary.enabled = True
    cfg.secretary.auto_cleanup_enabled = False
    cfg.secretary.dm_retention_days = 30
    cfg.secretary.channel_retention_days = 90
    cfg.save()

    # On-disk write half
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["secretary"]["auto_cleanup_enabled"] is False
    assert raw["secretary"]["dm_retention_days"] == 30
    assert raw["secretary"]["channel_retention_days"] == 90

    # Read-back (load) half — the part that was broken
    loaded = KiroCrewConfig.load()
    assert loaded.secretary.auto_cleanup_enabled is False
    assert loaded.secretary.dm_retention_days == 30
    assert loaded.secretary.channel_retention_days == 90


def test_save_load_roundtrip_secretary_auto_dismiss(cfg_file):
    """secretary.auto_dismiss_on_reply must survive a save/LOAD cycle.

    Opt-in default is False; flipping it on must persist to config.json AND
    read back through the SecretaryConfig loader constructor (same drop-on-read
    class of bug as the retention fields).
    """
    cfg = KiroCrewConfig()
    assert cfg.secretary.auto_dismiss_on_reply is False  # opt-in default
    cfg.secretary.auto_dismiss_on_reply = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["secretary"]["auto_dismiss_on_reply"] is True

    loaded = KiroCrewConfig.load()
    assert loaded.secretary.auto_dismiss_on_reply is True


def test_save_load_roundtrip_taskrunner(cfg_file):
    """TaskRunner config must survive a save/load cycle."""
    cfg = KiroCrewConfig()
    cfg.taskrunner.max_parallel_steps = 5
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["taskrunner"]["max_parallel_steps"] == 5


def test_save_load_roundtrip_orchestrator(cfg_file):
    """Orchestrator config must survive a save/load cycle."""
    cfg = KiroCrewConfig()
    cfg.orchestrator.stage_timeout_seconds = 900
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["orchestrator"]["stage_timeout_seconds"] == 900


def test_save_load_roundtrip_skills(cfg_file):
    """Skills config must survive a save/load cycle."""
    cfg = KiroCrewConfig()
    cfg.skills.max_triggered = 5
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["skills"]["max_triggered"] == 5


def test_save_load_roundtrip_tunnel(cfg_file):
    """Tunnel config must survive a save/load cycle."""
    cfg = KiroCrewConfig()
    cfg.tunnel.enabled = True
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["tunnel"]["enabled"] is True

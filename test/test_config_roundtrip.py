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
    """Every public field on KiroCrewConfig must appear in to_dict() output."""
    cfg = KiroCrewConfig()
    d = cfg.to_dict()
    # Fields that are serialized under a different key or merged into slack
    SPECIAL = {"slack_channels", "slack_dm_activation", "observe_max_messages", "observe_ttl_hours"}
    for f in fields(KiroCrewConfig):
        if f.name in SPECIAL:
            continue
        if f.name.startswith("_"):
            # Private load-status fields (_extra_sections, _degraded_sections)
            # are deliberately not round-tripped: they describe THIS load, not
            # user configuration.
            continue
        assert f.name in d, f"to_dict() missing field: {f.name}"


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

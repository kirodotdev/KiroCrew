"""Tests for the ``dashboard.auto_open_conductor_board`` setting.

The setting decides whether a conductor mode chat's first entry opens the chat's
right side panel on its Board tab. It is on by default, so an install that never
sets it keeps that behavior. The boolean-field tables in
``test_dashboard_files_coverage.py`` cover the PUT/GET wiring. This file covers
the default, the load path, and the generated schema that the Settings page reads.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig

FIELD = "auto_open_conductor_board"


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def test_default_on():
    assert getattr(KiroCrewConfig().dashboard, FIELD) is True


def test_a_config_that_never_sets_it_loads_on(cfg_file):
    assert getattr(KiroCrewConfig.load().dashboard, FIELD) is True


def test_off_round_trips_through_save_and_load(cfg_file):
    cfg = KiroCrewConfig()
    setattr(cfg.dashboard, FIELD, False)
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"][FIELD] is False
    assert getattr(KiroCrewConfig.load().dashboard, FIELD) is False


def test_non_bool_falls_back_to_on(cfg_file):
    # Only a real false turns the setting off: a hand-edited "no" reads as on.
    cfg_file.write_text(json.dumps({"dashboard": {FIELD: "no"}}), encoding="utf-8")
    assert getattr(KiroCrewConfig.load().dashboard, FIELD) is True


def test_in_generated_schema():
    from kiro_crew.config.schema import JSON_SCHEMA

    node = JSON_SCHEMA["properties"]["dashboard"]["properties"][FIELD]
    assert node["type"] == "boolean"
    assert node["default"] is True
    assert node["x-meta"]["label"]
    assert node["x-meta"]["help"]

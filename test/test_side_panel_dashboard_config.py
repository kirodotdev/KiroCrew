"""Tests for the two side panel dashboard settings.

``dashboard.side_panel_always_open`` opens the chat's right side panel in every
chat; ``dashboard.side_panel_pin_summary`` pins the Summary tab first in it. Both
are opt-in: with either off the panel behaves exactly as it does without the
setting. The PUT/GET wiring is covered by the boolean-field tables in
``test_dashboard_files_coverage.py``; this file covers the defaults, the load
path and the generated schema the Settings page reads.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig

FIELDS = ("side_panel_always_open", "side_panel_pin_summary")


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.mark.parametrize("field", FIELDS)
def test_defaults_off(field):
    assert getattr(KiroCrewConfig().dashboard, field) is False


@pytest.mark.parametrize("field", FIELDS)
def test_save_load_round_trip(cfg_file, field):
    cfg = KiroCrewConfig()
    setattr(cfg.dashboard, field, True)
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"][field] is True
    assert getattr(KiroCrewConfig.load().dashboard, field) is True


@pytest.mark.parametrize("field", FIELDS)
def test_non_bool_falls_back_to_off(cfg_file, field):
    # A hand-edited truthy string must not turn the behaviour on.
    cfg_file.write_text(json.dumps({"dashboard": {field: "yes"}}), encoding="utf-8")
    assert getattr(KiroCrewConfig.load().dashboard, field) is False


@pytest.mark.parametrize("field", FIELDS)
def test_in_generated_schema(field):
    from kiro_crew.config.schema import JSON_SCHEMA

    node = JSON_SCHEMA["properties"]["dashboard"]["properties"][field]
    assert node["type"] == "boolean"
    assert node["default"] is False
    assert node["x-meta"]["label"]
    assert node["x-meta"]["help"]

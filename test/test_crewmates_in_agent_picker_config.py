"""``dashboard.crewmates_in_agent_picker``: whether the chat agent picker lists crewmates."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig


def test_default_is_off():
    # Off keeps the templates-only picker every install has today.
    assert KiroCrewConfig().dashboard.crewmates_in_agent_picker is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), (True, True), (False, False), ("yes", False)],
)
def test_flag_is_read_from_the_config_file(tmp_path, raw, expected):
    # Hand-edited config is the only door to this flag, so a typo ("yes") must
    # read as off rather than silently switching the picker.
    dashboard = {} if raw is None else {"crewmates_in_agent_picker": raw}
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"dashboard": dashboard}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        assert KiroCrewConfig.load().dashboard.crewmates_in_agent_picker is expected


def test_flag_reaches_the_config_api_payload():
    # The SPA reads the flag from GET /api/config/kirocrew, which serves
    # ``to_dict()`` -- so the key must survive serialization.
    cfg = KiroCrewConfig()
    cfg.dashboard.crewmates_in_agent_picker = True
    assert cfg.to_dict()["dashboard"]["crewmates_in_agent_picker"] is True

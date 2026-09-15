"""The workflow run ceiling is bounded at the real configuration load path."""

import json
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig


def _loaded(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load()


@pytest.mark.parametrize("data", [{}, {"agent": {}}])
def test_workflow_run_timeout_secs_absent_uses_default(tmp_path, data):
    assert _loaded(tmp_path, data).agent.workflow_run_timeout_secs == 3600


@pytest.mark.parametrize(
    "value,expected",
    [
        (60, 60),
        (3600, 3600),
        (21600, 21600),
        (1, 60),
        (0, 60),
        (-1000, 60),
        (21612345, 21600),
        ("120", 120),
        (120.0, 120),
        ("1", 60),
        (21612345.0, 21600),
    ],
)
def test_workflow_run_timeout_secs_clamp_is_enforced_at_the_loader(tmp_path, value, expected):
    cfg = _loaded(tmp_path, {"agent": {"workflow_run_timeout_secs": value}})
    assert cfg.agent.workflow_run_timeout_secs == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        "invalid",
        "",
        "120.5",
        120.5,
        float("inf"),
        float("-inf"),
        float("nan"),
        [],
        {},
    ],
)
def test_workflow_run_timeout_secs_invalid_uses_default(tmp_path, value):
    cfg = _loaded(tmp_path, {"agent": {"workflow_run_timeout_secs": value}})
    assert cfg.agent.workflow_run_timeout_secs == 3600

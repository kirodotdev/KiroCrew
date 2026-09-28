"""``agent.interactive_command_policy`` is the source of
``WatchdogSettings.interactive_command_policy``.

The watchdog snapshot would otherwise carry the field with a hard-coded default and a
seam comment; the config key now feeds it through ``_load_watchdog_settings``,
the same path every other watchdog knob takes, so the dashboard's config editor
and a hand-edited ``config.json`` both reach the tool-stall branch.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.acp.session_handle import (
    INTERACTIVE_POLICY_CANCEL,
    INTERACTIVE_POLICY_WAIT,
    WatchdogSettings,
    _load_watchdog_settings,
)
from kiro_crew.config.loader import KiroCrewConfig


def _cfg(policy: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.interactive_command_policy = policy
    return cfg


def test_default_snapshot_is_cancel() -> None:
    assert WatchdogSettings().interactive_command_policy == INTERACTIVE_POLICY_CANCEL
    assert KiroCrewConfig().agent.interactive_command_policy == INTERACTIVE_POLICY_CANCEL


@pytest.mark.parametrize("policy", [INTERACTIVE_POLICY_CANCEL, INTERACTIVE_POLICY_WAIT])
def test_settings_follow_the_config_key(policy: str) -> None:
    with unittest.mock.patch("kiro_crew.config.loader.KiroCrewConfig.load") as load:
        settings = _load_watchdog_settings("", cfg=_cfg(policy))
    load.assert_not_called()
    assert settings.interactive_command_policy == policy


def test_unknown_policy_on_the_dataclass_falls_back_to_cancel() -> None:
    """A value that bypassed the loader (a test, a future writer) never arms an
    undefined branch: the snapshot only ever carries the two known policies."""
    settings = _load_watchdog_settings("", cfg=_cfg("auto-yes"))
    assert settings.interactive_command_policy == INTERACTIVE_POLICY_CANCEL


def test_hand_edited_file_reaches_the_snapshot(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"agent": {"interactive_command_policy": "wait"}}), encoding="utf-8"
    )
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        settings = _load_watchdog_settings("")
    assert settings.interactive_command_policy == INTERACTIVE_POLICY_WAIT

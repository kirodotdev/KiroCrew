"""Tests for the Microsoft Teams channel configuration (``TeamsConfig``).

Covers config round-trip through ``KiroCrewConfig.load()`` / ``to_dict()``,
the soft<=hard threshold clamp, the ``sensitive`` flag on ``app_password``,
and clean defaults when the ``teams`` section is missing.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest.mock
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig, TeamsConfig


def _load_from_dict(data: object) -> KiroCrewConfig:
    """Write *data* to a temp config file and load via KiroCrewConfig.load()."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


class TestTeamsConfig:
    def test_round_trip(self) -> None:
        cfg = _load_from_dict(
            {
                "teams": {
                    "enabled": True,
                    "app_id": "app-123",
                    "app_password": "secret",
                    "tenant_id": "tenant-1",
                    "allowed_emails": ["Alice@example.com", 5, ""],
                    "soft_threshold_pct": 70,
                    "hard_threshold_pct": 90,
                }
            }
        )
        assert cfg.teams.enabled is True
        assert cfg.teams.app_id == "app-123"
        # app_password is env-only: a value in config.json is deliberately NOT
        # loaded, so the agent-readable config never holds the secret.
        assert cfg.teams.app_password == ""
        assert cfg.teams.tenant_id == "tenant-1"
        # non-string / empty entries dropped
        assert cfg.teams.allowed_emails == ["Alice@example.com"]
        assert cfg.teams.soft_threshold_pct == 70
        assert cfg.teams.hard_threshold_pct == 90

        serialized = cfg.to_dict()
        assert serialized["teams"]["enabled"] is True
        assert serialized["teams"]["app_id"] == "app-123"
        assert serialized["teams"]["allowed_emails"] == ["Alice@example.com"]

    def test_threshold_clamp_and_soft_le_hard(self) -> None:
        # soft (90) > hard (50) -> soft clamped down to hard
        cfg = _load_from_dict(
            {"teams": {"soft_threshold_pct": 90, "hard_threshold_pct": 50}}
        )
        assert cfg.teams.hard_threshold_pct == 50
        assert cfg.teams.soft_threshold_pct == 50

        # out-of-range values clamped to [1, 100] (1 floor: a 0% threshold
        # would mean "always over")
        tc = TeamsConfig(soft_threshold_pct=-10, hard_threshold_pct=200)
        assert tc.soft_threshold_pct == 1
        assert tc.hard_threshold_pct == 100

    def test_app_password_marked_sensitive(self) -> None:
        fields = {f.name: f for f in dataclasses.fields(TeamsConfig)}
        assert fields["app_password"].metadata.get("sensitive") is True
        # non-secret fields are not marked sensitive
        assert fields["app_id"].metadata.get("sensitive") in (None, False)
        assert fields["tenant_id"].metadata.get("sensitive") in (None, False)

    def test_missing_section_defaults(self) -> None:
        cfg = _load_from_dict({})
        assert cfg.teams.enabled is False
        assert cfg.teams.app_id == ""
        assert cfg.teams.app_password == ""
        assert cfg.teams.tenant_id == ""
        assert cfg.teams.allowed_emails == []
        assert cfg.teams.soft_threshold_pct == 80
        assert cfg.teams.hard_threshold_pct == 95

    def test_non_dict_section_ignored(self) -> None:
        cfg = _load_from_dict({"teams": "not-a-dict"})
        assert cfg.teams.enabled is False
        assert cfg.teams.allowed_emails == []

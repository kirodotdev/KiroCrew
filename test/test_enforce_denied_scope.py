"""Tests for _enforce_denied_commands scope config."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiro_crew.agent import _denied_cmd_mtimes, _enforce_denied_commands


def _setup(tmp_path: Path, scope: str = "all"):
    """Create bundled defaults and agent configs for testing."""
    bundled_dir = tmp_path / "bundled"
    bundled_dir.mkdir()
    defaults = {
        "toolsSettings": {
            "execute_bash": {"deniedCommands": ["rm -rf /", "git push --force"]},
            "shell": {"deniedCommands": ["rm -rf /", "git push --force"]},
        }
    }
    (bundled_dir / "defaults.json").write_text(json.dumps(defaults))

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(json.dumps({"model": "claude"}))
    (agents_dir / "other-agent.json").write_text(json.dumps({"model": "claude"}))
    (agents_dir / "kirocrew-lite.json").write_text(json.dumps({"model": "lite"}))

    mock_cfg = MagicMock()
    mock_cfg.agent.enforce_denied_commands = scope

    return bundled_dir, agents_dir, mock_cfg


class TestEnforceDeniedScope:
    def setup_method(self):
        _denied_cmd_mtimes.clear()

    def test_scope_all_enforces_on_all_agents(self, tmp_path: Path):
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "all")

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            _enforce_denied_commands()

        for name in ("kirocrew.json", "other-agent.json"):
            data = json.loads((agents_dir / name).read_text())
            assert "rm -rf /" in data["toolsSettings"]["execute_bash"]["deniedCommands"]

    def test_scope_kirocrew_skips_other_agents(self, tmp_path: Path):
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "kirocrew")

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            _enforce_denied_commands()

        mc = json.loads((agents_dir / "kirocrew.json").read_text())
        assert "rm -rf /" in mc["toolsSettings"]["execute_bash"]["deniedCommands"]

        other = json.loads((agents_dir / "other-agent.json").read_text())
        assert "toolsSettings" not in other

    def test_lite_agents_always_skipped(self, tmp_path: Path):
        """Lite agents are skipped regardless of scope."""
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "all")

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.agent._LITE_AGENT_NAMES", frozenset({"kirocrew-lite.json"})),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            _enforce_denied_commands()

        lite = json.loads((agents_dir / "kirocrew-lite.json").read_text())
        assert "toolsSettings" not in lite

    def test_config_load_failure_falls_back_to_all(self, tmp_path: Path):
        bundled_dir, agents_dir, _ = _setup(tmp_path)

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", side_effect=Exception("boom")),
        ):
            _enforce_denied_commands()

        for name in ("kirocrew.json", "other-agent.json"):
            data = json.loads((agents_dir / name).read_text())
            assert "rm -rf /" in data["toolsSettings"]["execute_bash"]["deniedCommands"]
